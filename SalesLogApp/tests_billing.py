import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from djstripe.models import Customer, Subscription, WebhookEndpoint

from SalesLog.settings import env_bounded_int, env_strict_bool

from .billing_configuration import (
    billing_configuration,
    selected_public_key,
    selected_secret_key,
)
from .checks import billing_configuration_check
from .billing_entitlements import get_billing_entitlement
from .billing_gateway import BillingGatewayError, customer_for_user
from .billing_services import (
    BillingPolicyError,
    finalize_introductory_benefit,
    generate_founder_grant,
    redeem_founder_code,
    reserve_checkout_attempt,
)
from .billing_webhooks import reconcile_billing_event
from .models import (
    BillingAccess,
    BillingCheckoutAttempt,
    FounderGrant,
    Team,
)
from .team_entitlements import get_team_entitlement
from .team_services import create_team


BILLING_READY_SETTINGS = {
    'BILLING_FEATURE_ENABLED': True,
    'BILLING_ENFORCEMENT_ENABLED': False,
    'STRIPE_LIVE_MODE': False,
    'STRIPE_TEST_PUBLIC_KEY': 'pk_test_unitpublic123',
    'STRIPE_TEST_SECRET_KEY': 'sk_test_unitsecret123',
    'STRIPE_LIVE_PUBLIC_KEY': 'pk_live_unitpublic123',
    'STRIPE_LIVE_SECRET_KEY': 'sk_live_unitsecret123',
    'STRIPE_BASIC_MONTHLY_PRICE_ID': 'price_unitmonthly123',
    'BILLING_STANDARD_TRIAL_DAYS': 30,
    'BILLING_FOUNDER_TRIAL_DAYS': 90,
    'DJSTRIPE_WEBHOOK_VALIDATION': 'verify_signature',
}


class BillingSettingsTests(SimpleTestCase):
    def test_billing_defaults_are_disabled(self):
        self.assertFalse(settings.BILLING_FEATURE_ENABLED)
        self.assertFalse(settings.BILLING_ENFORCEMENT_ENABLED)
        self.assertFalse(settings.STRIPE_LIVE_MODE)

    def test_strict_flags_and_bounded_trial_parser(self):
        with patch.dict('os.environ', {'TEST_BILLING_FLAG': 'false'}):
            self.assertFalse(env_strict_bool('TEST_BILLING_FLAG'))
        with patch.dict('os.environ', {'TEST_BILLING_FLAG': 'enabled'}):
            with self.assertRaises(ImproperlyConfigured):
                env_strict_bool('TEST_BILLING_FLAG')
        with patch.dict('os.environ', {'TEST_TRIAL_DAYS': '90'}):
            self.assertEqual(env_bounded_int(
                'TEST_TRIAL_DAYS', 30, minimum=1, maximum=365
            ), 90)
        with patch.dict('os.environ', {'TEST_TRIAL_DAYS': '366'}):
            with self.assertRaises(ImproperlyConfigured):
                env_bounded_int(
                    'TEST_TRIAL_DAYS', 30, minimum=1, maximum=365
                )

    @override_settings(**BILLING_READY_SETTINGS)
    def test_test_mode_selects_only_test_credentials(self):
        self.assertEqual(selected_public_key(), 'pk_test_unitpublic123')
        self.assertEqual(selected_secret_key(), 'sk_test_unitsecret123')
        self.assertTrue(billing_configuration().ready)

    @override_settings(**{**BILLING_READY_SETTINGS, 'STRIPE_LIVE_MODE': True})
    def test_live_mode_selects_only_live_credentials(self):
        self.assertEqual(selected_public_key(), 'pk_live_unitpublic123')
        self.assertEqual(selected_secret_key(), 'sk_live_unitsecret123')
        self.assertTrue(billing_configuration().ready)

    @override_settings(
        BILLING_FEATURE_ENABLED=True,
        BILLING_ENFORCEMENT_ENABLED=False,
        STRIPE_LIVE_MODE=False,
        STRIPE_TEST_PUBLIC_KEY='',
        STRIPE_TEST_SECRET_KEY='',
        STRIPE_BASIC_MONTHLY_PRICE_ID='',
    )
    def test_feature_enabled_missing_configuration_warns_safely(self):
        messages = billing_configuration_check(None)
        self.assertEqual(len(messages), 1)
        self.assertNotIn('sk_', str(messages[0]))

    @override_settings(
        BILLING_FEATURE_ENABLED=True,
        BILLING_ENFORCEMENT_ENABLED=True,
        STRIPE_LIVE_MODE=False,
        STRIPE_TEST_PUBLIC_KEY='',
        STRIPE_TEST_SECRET_KEY='',
        STRIPE_BASIC_MONTHLY_PRICE_ID='',
    )
    def test_enforcement_enabled_missing_configuration_is_system_error(self):
        messages = billing_configuration_check(None)
        self.assertEqual(len(messages), 1)
        self.assertNotIn('sk_', str(messages[0]))


@override_settings(**BILLING_READY_SETTINGS)
class FounderGrantTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='founder-user', email='founder@example.test'
        )
        self.other = get_user_model().objects.create_user(
            username='founder-other', email='other@example.test'
        )

    def test_code_is_hashed_single_use_and_auditable(self):
        grant, raw_code = generate_founder_grant()
        _, second_code = generate_founder_grant()
        self.assertNotEqual(grant.code_digest, raw_code)
        self.assertNotIn(raw_code, grant.code_digest)
        self.assertNotEqual(raw_code, second_code)
        self.assertEqual(grant.code_prefix, raw_code[:12])
        redeemed = redeem_founder_code(self.user, raw_code)
        redeemed.refresh_from_db()
        self.assertEqual(redeemed.redeemed_user, self.user)
        self.assertEqual(redeemed.redemption_count, 1)
        self.assertIsNotNone(redeemed.redeemed_at)
        with self.assertRaises(BillingPolicyError):
            redeem_founder_code(self.other, raw_code)

    def test_codes_expire_and_can_be_revoked_before_use(self):
        expired, expired_code = generate_founder_grant(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaises(BillingPolicyError):
            redeem_founder_code(self.user, expired_code)
        grant, raw_code = generate_founder_grant()
        grant.revoked_at = timezone.now()
        grant.save(update_fields=['revoked_at'])
        with self.assertRaises(BillingPolicyError):
            redeem_founder_code(self.user, raw_code)

    def test_one_user_cannot_stack_founder_grants_or_used_intro(self):
        first, first_code = generate_founder_grant()
        redeem_founder_code(self.user, first_code)
        second, second_code = generate_founder_grant()
        with self.assertRaises(BillingPolicyError):
            redeem_founder_code(self.user, second_code)
        access = BillingAccess.objects.get(user=self.user)
        access.introductory_benefit_consumed_at = timezone.now()
        access.introductory_benefit_kind = BillingAccess.STANDARD
        access.save()
        third_user = get_user_model().objects.create_user(username='already-used')
        BillingAccess.objects.create(
            user=third_user,
            introductory_benefit_consumed_at=timezone.now(),
            introductory_benefit_kind=BillingAccess.STANDARD,
        )
        third, third_code = generate_founder_grant()
        with self.assertRaises(BillingPolicyError):
            redeem_founder_code(third_user, third_code)

    def test_database_enforces_one_grant_per_redeemed_user(self):
        first, _ = generate_founder_grant()
        second, _ = generate_founder_grant()
        first.redeemed_user = self.user
        first.redemption_count = 1
        first.save()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                second.redeemed_user = self.user
                second.redemption_count = 1
                second.save()

    def test_generation_and_revocation_commands_do_not_store_plaintext(self):
        output = StringIO()
        call_command('generate_founder_code', stdout=output)
        lines = output.getvalue().strip().splitlines()
        raw_code = lines[-1]
        grant = FounderGrant.objects.get()
        self.assertTrue(raw_code.startswith('stewf_'))
        self.assertNotEqual(grant.code_digest, raw_code)
        revoke_output = StringIO()
        call_command(
            'revoke_founder_code', str(grant.public_id), stdout=revoke_output
        )
        grant.refresh_from_db()
        self.assertIsNotNone(grant.revoked_at)
        self.assertNotIn(raw_code, revoke_output.getvalue())


@override_settings(**BILLING_READY_SETTINGS)
class BillingCheckoutTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='checkout-user', email='checkout@example.test'
        )
        self.other = get_user_model().objects.create_user(
            username='checkout-other', email='other@example.test'
        )
        self.client.force_login(self.user)

    def customer(self, user=None, suffix='owned'):
        user = user or self.user
        return Customer.objects.create(
            id=f'cus_mock_{suffix}',
            subscriber=user,
            livemode=False,
            email=user.email,
            stripe_data={
                'id': f'cus_mock_{suffix}',
                'object': 'customer',
                'livemode': False,
            },
        )

    def subscription(
        self, customer, *, status='active', suffix='active', trial_end=None,
        current_period_end=None, metadata=None, pause_collection=None,
    ):
        now = timezone.now()
        data = {
            'id': f'sub_mock_{suffix}',
            'object': 'subscription',
            'customer': customer.id,
            'status': status,
            'livemode': False,
            'metadata': metadata or {},
            'trial_end': int(trial_end.timestamp()) if trial_end else None,
            'current_period_end': (
                int(current_period_end.timestamp())
                if current_period_end else int((now + timedelta(days=30)).timestamp())
            ),
            'pause_collection': pause_collection,
            'items': {'data': [{
                'price': {'id': settings.STRIPE_BASIC_MONTHLY_PRICE_ID},
            }]},
        }
        return Subscription.objects.create(
            id=data['id'],
            customer=customer,
            livemode=False,
            stripe_data=data,
        )

    @patch('SalesLogApp.billing_views.customer_for_user')
    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_standard_checkout_uses_server_owned_policy(self, create_session, customer):
        customer.return_value = SimpleNamespace(id='cus_mock_authenticated')
        create_session.return_value = SimpleNamespace(
            url='https://checkout.stripe.com/c/pay/mock-session'
        )
        response = self.client.post(reverse('billing_checkout_start'), {
            'trial_days': '999',
            'price': 'price_attacker',
            'customer': 'cus_other',
            'user_id': self.other.pk,
            'tier': 'founder_pro',
            'success_url': 'https://evil.example/',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('https://checkout.stripe.com/'))
        kwargs = create_session.call_args.kwargs
        self.assertEqual(kwargs['mode'], 'subscription')
        self.assertEqual(kwargs['line_items'], [{
            'price': settings.STRIPE_BASIC_MONTHLY_PRICE_ID,
            'quantity': 1,
        }])
        self.assertEqual(kwargs['payment_method_collection'], 'always')
        self.assertEqual(
            kwargs['expires_at'],
            int(BillingCheckoutAttempt.objects.get(
                user=self.user
            ).reservation_expires_at.timestamp()),
        )
        self.assertEqual(kwargs['subscription_data']['trial_period_days'], 30)
        self.assertEqual(kwargs['customer'], 'cus_mock_authenticated')
        self.assertEqual(
            kwargs['metadata']['djstripe_subscriber'], str(self.user.pk)
        )
        self.assertEqual(kwargs['client_reference_id'], str(self.user.pk))
        self.assertEqual(
            kwargs['idempotency_key'],
            f'saleslog-checkout-{BillingCheckoutAttempt.objects.get(user=self.user).public_id}',
        )
        self.assertNotIn('evil.example', kwargs['success_url'])

    @patch('SalesLogApp.billing_views.customer_for_user')
    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_founder_checkout_receives_90_days_without_stacking(
        self, create_session, customer
    ):
        grant, raw_code = generate_founder_grant()
        redeem_founder_code(self.user, raw_code)
        customer.return_value = SimpleNamespace(id='cus_mock_founder')
        create_session.return_value = SimpleNamespace(
            url='https://checkout.stripe.com/c/pay/mock-founder'
        )
        self.client.post(reverse('billing_checkout_start'))
        kwargs = create_session.call_args.kwargs
        self.assertEqual(kwargs['subscription_data']['trial_period_days'], 90)
        self.assertEqual(kwargs['metadata']['intro_trial_kind'], 'founder')
        attempt = BillingCheckoutAttempt.objects.get(user=self.user)
        self.assertEqual(attempt.founder_grant, grant)
        self.assertIsNone(
            BillingAccess.objects.get(user=self.user).introductory_benefit_consumed_at
        )

    @patch('SalesLogApp.billing_views.customer_for_user')
    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_used_intro_checkout_has_no_second_trial(self, create_session, customer):
        BillingAccess.objects.create(
            user=self.user,
            introductory_benefit_consumed_at=timezone.now(),
            introductory_benefit_kind=BillingAccess.STANDARD,
        )
        customer.return_value = SimpleNamespace(id='cus_mock_returning')
        create_session.return_value = SimpleNamespace(
            url='https://checkout.stripe.com/c/pay/mock-returning'
        )
        self.client.post(reverse('billing_checkout_start'))
        subscription_data = create_session.call_args.kwargs['subscription_data']
        self.assertNotIn('trial_period_days', subscription_data)

    def test_abandoned_checkout_expires_without_consuming_trial(self):
        attempt, created = reserve_checkout_attempt(self.user)
        self.assertTrue(created)
        self.assertEqual(attempt.trial_days, 30)
        attempt.reservation_expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=['reservation_expires_at'])
        replacement, created = reserve_checkout_attempt(self.user)
        attempt.refresh_from_db()
        self.assertTrue(created)
        self.assertNotEqual(replacement.pk, attempt.pk)
        self.assertEqual(attempt.status, BillingCheckoutAttempt.EXPIRED)
        self.assertFalse(BillingAccess.objects.filter(
            user=self.user,
            introductory_benefit_consumed_at__isnull=False,
        ).exists())

    def test_concurrent_style_reservations_reuse_one_policy_record(self):
        first, first_created = reserve_checkout_attempt(self.user)
        second, second_created = reserve_checkout_attempt(self.user)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(BillingCheckoutAttempt.objects.filter(
            user=self.user,
            status__in=BillingCheckoutAttempt.ACTIVE_STATUSES,
        ).count(), 1)

    def test_success_page_does_not_consume_or_grant_trial(self):
        response = self.client.get(reverse('billing_checkout_success'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Stripe synchronization is authoritative')
        self.assertFalse(BillingAccess.objects.filter(
            user=self.user,
            introductory_benefit_consumed_at__isnull=False,
        ).exists())
        self.assertFalse(get_billing_entitlement(self.user).subscription_access)

    def test_existing_valid_subscription_prevents_duplicate_checkout(self):
        customer = self.customer()
        subscription = self.subscription(customer, status='active')
        BillingAccess.objects.create(
            user=self.user,
            authoritative_subscription=subscription,
        )
        with self.assertRaises(BillingPolicyError):
            reserve_checkout_attempt(self.user)

    def test_customer_ownership_uses_authenticated_user_only(self):
        own = self.customer(self.user, 'own')
        other = self.customer(self.other, 'other')
        with patch('SalesLogApp.billing_gateway.Customer.get_or_create') as create:
            resolved = customer_for_user(self.user)
        self.assertEqual(resolved, own)
        self.assertNotEqual(resolved, other)
        create.assert_not_called()

    def test_duplicate_customer_mapping_fails_closed(self):
        self.customer(self.user, 'duplicate-one')
        self.customer(self.user, 'duplicate-two')
        with self.assertRaises(BillingGatewayError):
            customer_for_user(self.user)

    def test_missing_email_fails_before_any_stripe_call(self):
        self.user.email = ''
        self.user.save(update_fields=['email'])
        with patch(
            'SalesLogApp.billing_gateway.stripe.checkout.Session.create'
        ) as create_session:
            response = self.client.post(reverse('billing_checkout_start'))
        self.assertEqual(response.status_code, 302)
        create_session.assert_not_called()

    @patch('SalesLogApp.billing_views.customer_for_user')
    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_stripe_errors_are_generic_and_secret_free(self, create_session, customer):
        customer.return_value = SimpleNamespace(id='cus_mock_error')
        create_session.side_effect = stripe.APIConnectionError('private-card-data')
        response = self.client.post(
            reverse('billing_checkout_start'), follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Stripe Checkout is temporarily unavailable')
        self.assertNotContains(response, 'private-card-data')
        self.assertNotContains(response, settings.STRIPE_TEST_SECRET_KEY)

    @patch('SalesLogApp.billing_gateway.stripe.billing_portal.Session.create')
    def test_portal_uses_only_owned_customer_and_named_return(self, create_portal):
        own = self.customer(self.user, 'portal-own')
        self.customer(self.other, 'portal-other')
        create_portal.return_value = SimpleNamespace(
            url='https://billing.stripe.com/p/session/mock-portal'
        )
        response = self.client.post(reverse('billing_portal'), {
            'customer': 'cus_mock_portal-other',
            'return_url': 'https://evil.example/',
        })
        self.assertEqual(response.status_code, 302)
        kwargs = create_portal.call_args.kwargs
        self.assertEqual(kwargs['customer'], own.id)
        self.assertEqual(
            kwargs['return_url'],
            f'http://testserver{reverse("billing_overview")}',
        )
        self.assertNotIn('evil.example', kwargs['return_url'])

    def test_portal_missing_customer_is_safe_and_makes_no_network_call(self):
        with patch(
            'SalesLogApp.billing_gateway.stripe.billing_portal.Session.create'
        ) as create_portal:
            response = self.client.post(reverse('billing_portal'), follow=True)
        self.assertContains(response, 'billing portal is temporarily unavailable')
        create_portal.assert_not_called()

    def test_billing_mutations_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        for route_name in (
            'billing_checkout_start',
            'billing_founder_redeem',
            'billing_portal',
        ):
            self.assertEqual(csrf_client.post(reverse(route_name)).status_code, 403)

    @override_settings(
        BILLING_FEATURE_ENABLED=False,
        BILLING_ENFORCEMENT_ENABLED=False,
    )
    def test_disabled_billing_ui_is_not_exposed(self):
        self.assertEqual(self.client.get(reverse('billing_overview')).status_code, 404)


@override_settings(**BILLING_READY_SETTINGS)
class BillingEntitlementAndWebhookTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='entitled-user', email='entitled@example.test'
        )
        self.customer = Customer.objects.create(
            id='cus_mock_entitled',
            subscriber=self.user,
            livemode=False,
            email=self.user.email,
            stripe_data={
                'id': 'cus_mock_entitled',
                'object': 'customer',
                'livemode': False,
            },
        )

    def subscription(
        self, status, *, suffix=None, trial_end=None,
        current_period_end=None, pause_collection=None, metadata=None,
    ):
        suffix = suffix or status
        data = {
            'id': f'sub_mock_{suffix}',
            'object': 'subscription',
            'customer': self.customer.id,
            'status': status,
            'livemode': False,
            'metadata': metadata or {},
            'trial_end': int(trial_end.timestamp()) if trial_end else None,
            'current_period_end': (
                int(current_period_end.timestamp())
                if current_period_end else None
            ),
            'pause_collection': pause_collection,
            'items': {'data': [{
                'price': {'id': settings.STRIPE_BASIC_MONTHLY_PRICE_ID},
            }]},
        }
        return Subscription.objects.create(
            id=data['id'],
            customer=self.customer,
            livemode=False,
            stripe_data=data,
        )

    def use_subscription(self, subscription, **access_defaults):
        access, _ = BillingAccess.objects.update_or_create(
            user=self.user,
            defaults={
                'authoritative_subscription': subscription,
                **access_defaults,
            },
        )
        return access

    def test_all_subscription_states_are_explicit(self):
        now = timezone.now()
        cases = [
            ('trialing', now + timedelta(days=10), now + timedelta(days=10), True),
            ('active', None, now + timedelta(days=20), True),
            ('incomplete', None, None, False),
            ('incomplete_expired', None, None, False),
            ('unpaid', None, now + timedelta(days=20), False),
            ('paused', None, now + timedelta(days=20), False),
        ]
        for index, (status, trial_end, period_end, expected) in enumerate(cases):
            subscription = self.subscription(
                status,
                suffix=f'{status}_{index}',
                trial_end=trial_end,
                current_period_end=period_end,
            )
            self.use_subscription(subscription)
            result = get_billing_entitlement(self.user)
            self.assertEqual(result.subscription_access, expected, status)

        paused_active = self.subscription(
            'active',
            suffix='active_paused_collection',
            current_period_end=now + timedelta(days=20),
            pause_collection={'behavior': 'void'},
        )
        self.use_subscription(paused_active)
        result = get_billing_entitlement(self.user)
        self.assertEqual(result.subscription_status, 'paused')
        self.assertFalse(result.subscription_access)

    def test_unrelated_subscription_price_does_not_grant_access(self):
        subscription = self.subscription(
            'active',
            suffix='wrong_price',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        subscription.stripe_data['items']['data'][0]['price']['id'] = (
            'price_unrelated'
        )
        subscription.save(update_fields=['stripe_data'])
        self.use_subscription(subscription)
        entitlement = get_billing_entitlement(self.user)
        self.assertFalse(entitlement.subscription_access)
        self.assertEqual(entitlement.tier, 'basic')

    def test_past_due_has_exact_seven_day_grace(self):
        now = timezone.now()
        in_grace = self.subscription(
            'past_due',
            suffix='past_due_grace',
            current_period_end=now - timedelta(days=2),
        )
        self.use_subscription(in_grace)
        result = get_billing_entitlement(self.user, at_time=now)
        self.assertTrue(result.subscription_access)
        self.assertEqual(result.source, 'past_due_grace')
        self.assertEqual(result.grace_ends_at, in_grace.current_period_end + timedelta(days=7))

        expired = self.subscription(
            'past_due',
            suffix='past_due_expired',
            current_period_end=now - timedelta(days=8),
        )
        self.use_subscription(expired)
        self.assertFalse(
            get_billing_entitlement(self.user, at_time=now).subscription_access
        )

    def test_canceled_access_ends_at_authorized_period(self):
        now = timezone.now()
        current = self.subscription(
            'canceled',
            suffix='canceled_current',
            current_period_end=now + timedelta(days=2),
        )
        self.use_subscription(current)
        self.assertTrue(get_billing_entitlement(self.user).subscription_access)
        expired = self.subscription(
            'canceled',
            suffix='canceled_expired',
            current_period_end=now - timedelta(seconds=1),
        )
        self.use_subscription(expired)
        self.assertFalse(get_billing_entitlement(self.user).subscription_access)

    def test_enforcement_disabled_preserves_existing_application_access(self):
        entitlement = get_billing_entitlement(self.user)
        self.assertTrue(entitlement.has_access)
        self.assertFalse(entitlement.subscription_access)
        self.client.force_login(self.user)
        self.assertNotEqual(self.client.get(reverse('profile')).status_code, 302)

    @override_settings(**{**BILLING_READY_SETTINGS, 'BILLING_ENFORCEMENT_ENABLED': True})
    def test_enforcement_boundary_redirects_unentitled_but_exempts_billing(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('view_sales'))
        self.assertRedirects(
            response,
            reverse('billing_overview'),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(reverse('billing_overview')).status_code, 200)

    def test_webhook_finalizes_trial_once_and_duplicate_is_idempotent(self):
        attempt, _ = reserve_checkout_attempt(self.user)
        trial_end = timezone.now() + timedelta(days=30)
        subscription = self.subscription(
            'trialing',
            suffix='webhook_trial',
            trial_end=trial_end,
            current_period_end=trial_end,
            metadata={'billing_attempt': str(attempt.public_id)},
        )
        event = SimpleNamespace(
            type='customer.subscription.created',
            created=timezone.now(),
            data={'object': subscription.stripe_data},
        )
        self.assertTrue(reconcile_billing_event(event))
        consumed_at = BillingAccess.objects.get(
            user=self.user
        ).introductory_benefit_consumed_at
        self.assertTrue(reconcile_billing_event(event))
        access = BillingAccess.objects.get(user=self.user)
        attempt.refresh_from_db()
        self.assertEqual(access.introductory_benefit_consumed_at, consumed_at)
        self.assertEqual(access.introductory_benefit_kind, BillingAccess.STANDARD)
        self.assertEqual(attempt.status, BillingCheckoutAttempt.CONFIRMED)

    def test_out_of_order_event_does_not_regress_latest_audit(self):
        subscription = self.subscription(
            'active',
            suffix='out_of_order',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        newer = SimpleNamespace(
            type='customer.subscription.updated',
            created=timezone.now(),
            data={'object': subscription.stripe_data},
        )
        older = SimpleNamespace(
            type='customer.subscription.created',
            created=newer.created - timedelta(days=1),
            data={'object': subscription.stripe_data},
        )
        reconcile_billing_event(newer)
        reconcile_billing_event(older)
        access = BillingAccess.objects.get(user=self.user)
        self.assertEqual(access.last_event_type, newer.type)
        self.assertEqual(access.last_event_created_at, newer.created)

    def test_late_trial_confirmation_does_not_replace_newer_subscription(self):
        attempt, _ = reserve_checkout_attempt(self.user)
        old_subscription = self.subscription(
            'trialing',
            suffix='late_old',
            trial_end=timezone.now() + timedelta(days=30),
            current_period_end=timezone.now() + timedelta(days=30),
            metadata={'billing_attempt': str(attempt.public_id)},
        )
        newer_subscription = self.subscription(
            'active',
            suffix='late_newer',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        newer_time = timezone.now()
        access = self.use_subscription(newer_subscription)
        access.last_event_type = 'customer.subscription.updated'
        access.last_event_created_at = newer_time
        access.save(update_fields=['last_event_type', 'last_event_created_at'])
        late_event = SimpleNamespace(
            type='customer.subscription.created',
            created=newer_time - timedelta(seconds=1),
            data={'object': old_subscription.stripe_data},
        )
        self.assertTrue(reconcile_billing_event(late_event))
        access.refresh_from_db()
        self.assertEqual(access.authoritative_subscription, newer_subscription)
        self.assertIsNotNone(access.introductory_benefit_consumed_at)

    def test_checkout_and_invoice_events_are_supported_without_granting_access(self):
        attempt, _ = reserve_checkout_attempt(self.user)
        completed = SimpleNamespace(
            type='checkout.session.completed',
            created=timezone.now(),
            data={'object': {
                'customer': self.customer.id,
                'metadata': {'billing_attempt': str(attempt.public_id)},
            }},
        )
        reconcile_billing_event(completed)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, BillingCheckoutAttempt.CHECKOUT_COMPLETED)
        self.assertIsNone(
            BillingAccess.objects.get(user=self.user).introductory_benefit_consumed_at
        )
        for event_type in ('invoice.paid', 'invoice.payment_failed'):
            invoice_event = SimpleNamespace(
                type=event_type,
                created=timezone.now() + timedelta(seconds=1),
                data={'object': {'customer': self.customer.id}},
            )
            self.assertTrue(reconcile_billing_event(invoice_event))

    def test_remaining_subscription_lifecycle_events_are_supported(self):
        event_types = (
            'customer.subscription.updated',
            'customer.subscription.deleted',
            'customer.subscription.trial_will_end',
        )
        for index, event_type in enumerate(event_types):
            subscription = self.subscription(
                'canceled' if event_type.endswith('deleted') else 'trialing',
                suffix=f'lifecycle_{index}',
                trial_end=timezone.now() + timedelta(days=5),
                current_period_end=timezone.now() + timedelta(days=5),
            )
            event = SimpleNamespace(
                type=event_type,
                created=timezone.now() + timedelta(seconds=index),
                data={'object': subscription.stripe_data},
            )
            self.assertTrue(reconcile_billing_event(event), event_type)

    def test_founder_entitlement_maps_to_teams_then_owner_loss_is_read_only(self):
        grant, raw_code = generate_founder_grant()
        redeem_founder_code(self.user, raw_code)
        attempt, _ = reserve_checkout_attempt(self.user)
        trial_end = timezone.now() + timedelta(days=90)
        subscription = self.subscription(
            'trialing',
            suffix='founder_teams',
            trial_end=trial_end,
            current_period_end=trial_end,
            metadata={'billing_attempt': str(attempt.public_id)},
        )
        finalize_introductory_benefit(attempt.public_id, subscription)
        with override_settings(TEAMS_FEATURE_ENABLED=True):
            team_entitlement = get_team_entitlement(self.user)
            self.assertEqual(team_entitlement.tier, 'founder_pro')
            team = create_team(
                self.user,
                name='Billing Team',
                timezone_name='UTC',
                monthly_unit_goal=None,
                display_mode=Team.RANKED,
            )
            subscription.stripe_data['status'] = 'unpaid'
            subscription.save(update_fields=['stripe_data'])
            self.client.force_login(self.user)
            response = self.client.get(reverse('team_detail', args=[team.public_id]))
            self.assertContains(response, 'Team management is read-only')
            self.assertNotContains(response, self.customer.id)
            self.assertNotContains(response, subscription.id)
            self.assertTrue(Team.objects.filter(pk=team.pk, is_active=True).exists())

    def test_active_subscription_maps_to_teams_pro_entitlement(self):
        subscription = self.subscription(
            'active',
            suffix='teams_pro',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        self.use_subscription(subscription)
        team_entitlement = get_team_entitlement(self.user)
        self.assertEqual(team_entitlement.tier, 'pro')
        self.assertTrue(team_entitlement.has_pro_access)
        with override_settings(TEAMS_FEATURE_ENABLED=True):
            team = create_team(
                self.user,
                name='Paid Pro Team',
                timezone_name='UTC',
                monthly_unit_goal=None,
                display_mode=Team.RANKED,
            )
        self.assertEqual(team.owner, self.user)

    @override_settings(**{
        **BILLING_READY_SETTINGS,
        'BILLING_ENFORCEMENT_ENABLED': True,
    })
    def test_enforcement_requires_a_signed_current_mode_webhook(self):
        messages = billing_configuration_check(None)
        self.assertEqual([message.id for message in messages], ['SalesLogApp.E003'])
        WebhookEndpoint.objects.create(
            id='we_mock_enforcement_ready',
            livemode=False,
            url='https://example.test/stripe/webhook/ready/',
            enabled_events=['customer.subscription.updated'],
            secret='whsec_mock_ready',
            status='enabled',
            stripe_data={},
        )
        self.assertEqual(billing_configuration_check(None), [])

    def test_invalid_webhook_signature_is_rejected(self):
        endpoint = WebhookEndpoint.objects.create(
            id='we_mock_invalid_signature',
            livemode=False,
            url='https://example.test/stripe/webhook/mock/',
            enabled_events=['customer.subscription.updated'],
            secret='whsec_mock_signing_secret',
            status='enabled',
            stripe_data={},
        )
        url = reverse(
            'djstripe:djstripe_webhook_by_uuid',
            kwargs={'uuid': endpoint.djstripe_uuid},
        )
        response = self.client.post(
            url,
            data=json.dumps({
                'id': 'evt_mock_invalid',
                'livemode': False,
                'api_version': '2026-05-27.dahlia',
                'data': {'object': {'object': 'customer'}},
            }),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='t=1,v1=invalid',
        )
        self.assertEqual(response.status_code, 400)

    def test_readiness_diagnostics_never_print_secret_values(self):
        output = StringIO()
        call_command('billing_readiness', '--json', stdout=output)
        rendered = output.getvalue()
        self.assertNotIn(settings.STRIPE_TEST_SECRET_KEY, rendered)
        self.assertNotIn(settings.STRIPE_TEST_PUBLIC_KEY, rendered)
        self.assertNotIn(settings.STRIPE_BASIC_MONTHLY_PRICE_ID, rendered)
        report = json.loads(rendered)
        self.assertTrue(report['webhook_route_present'])
        self.assertTrue(report['signature_verification'])
        self.assertIn('enforcement_ready', report)
