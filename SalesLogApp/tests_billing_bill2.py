import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from djstripe.models import Customer, Price, Product, Subscription, WebhookEndpoint

from .billing_configuration import billing_configuration
from .checks import billing_configuration_check
from .billing_entitlements import get_billing_entitlement
from .billing_plans import BASIC, PRO, classify_subscription_plan
from .billing_pricing import synchronized_plan_price_errors
from .billing_services import (
    BillingPolicyError,
    finalize_introductory_benefit,
    generate_founder_grant,
    redeem_founder_code,
    reserve_checkout_attempt,
)
from .access import activity_goals_authorized
from .models import BillingAccess, BillingCheckoutAttempt


TIERED_SETTINGS = {
    'BILLING_FEATURE_ENABLED': True,
    'BILLING_ENFORCEMENT_ENABLED': False,
    'BILLING_ONBOARDING_ENABLED': False,
    'BILLING_TIERED_PRICING_ENABLED': True,
    'STRIPE_LIVE_MODE': False,
    'STRIPE_TEST_PUBLIC_KEY': 'pk_test_bill2public123',
    'STRIPE_TEST_SECRET_KEY': 'sk_test_bill2secret123',
    'STRIPE_BASIC_MONTHLY_PRICE_ID': 'price_basic499',
    'STRIPE_BASIC_YEARLY_PRICE_ID': 'price_basic4900',
    'STRIPE_PRO_MONTHLY_PRICE_ID': 'price_pro999',
    'STRIPE_PRO_YEARLY_PRICE_ID': 'price_pro9900',
    'STRIPE_LEGACY_PRO_PRICE_IDS': ['price_legacyinitial'],
    'BILLING_STANDARD_TRIAL_DAYS': 30,
    'BILLING_FOUNDER_TRIAL_DAYS': 90,
    'DJSTRIPE_WEBHOOK_VALIDATION': 'verify_signature',
}


def create_price(
    price_id, cents, *, product_id, active=True, interval='month',
):
    product = Product.objects.create(
        id=product_id,
        name=product_id,
        active=True,
        livemode=False,
        stripe_data={'id': product_id},
    )
    return Price.objects.create(
        id=price_id,
        product=product,
        active=active,
        livemode=False,
        currency='usd',
        stripe_data={
            'id': price_id,
            'unit_amount': cents,
            'unit_amount_decimal': str(cents),
            'currency': 'usd',
            'type': 'recurring',
            'recurring': {'interval': interval, 'interval_count': 1},
        },
    )


def create_current_price_rows():
    create_price('price_basic499', 499, product_id='prod_bill2_basic_month')
    create_price(
        'price_basic4900', 4900, product_id='prod_bill2_basic_year',
        interval='year',
    )
    create_price('price_pro999', 999, product_id='prod_bill2_pro_month')
    create_price(
        'price_pro9900', 9900, product_id='prod_bill2_pro_year',
        interval='year',
    )


class Bill2ConfigurationTests(SimpleTestCase):
    @override_settings(**TIERED_SETTINGS)
    def test_four_distinct_prices_and_legacy_allowlist_are_required(self):
        configuration = billing_configuration()
        self.assertTrue(configuration.ready)
        self.assertTrue(configuration.tiered_pricing_enabled)

    @override_settings(**{
        **TIERED_SETTINGS,
        'STRIPE_LEGACY_PRO_PRICE_IDS': [],
    })
    def test_missing_grandfather_allowlist_fails_closed(self):
        configuration = billing_configuration()
        self.assertFalse(configuration.ready)
        self.assertIn('legacy Pro Price allowlist', '; '.join(configuration.errors))

    @override_settings(**{
        **TIERED_SETTINGS,
        'STRIPE_PRO_MONTHLY_PRICE_ID': 'price_basic499',
    })
    def test_current_prices_cannot_share_one_price(self):
        configuration = billing_configuration()
        self.assertFalse(configuration.ready)
        self.assertIn('must be different', '; '.join(configuration.errors))


@override_settings(**TIERED_SETTINGS)
class Bill2SynchronizedPriceTests(TestCase):
    def webhook(self):
        return WebhookEndpoint.objects.create(
            id='we_bill2_ready',
            livemode=False,
            url='https://example.test/stripe/webhook/bill2/',
            enabled_events=['customer.subscription.updated'],
            secret='whsec_bill2_signing_secret',
            status='enabled',
            stripe_data={},
        )

    def test_exact_stripe_synced_prices_pass_policy(self):
        create_current_price_rows()
        self.assertEqual(synchronized_plan_price_errors(), ())

    def test_system_check_accepts_complete_tiered_rollout(self):
        create_current_price_rows()
        self.webhook()
        self.assertEqual(billing_configuration_check(None), [])
        output = StringIO()
        call_command('billing_readiness', '--json', stdout=output)
        rendered = output.getvalue()
        report = json.loads(rendered)
        self.assertTrue(report['tiered_pricing_ready'])
        self.assertNotIn(settings.STRIPE_BASIC_MONTHLY_PRICE_ID, rendered)
        self.assertNotIn(settings.STRIPE_PRO_MONTHLY_PRICE_ID, rendered)
        self.assertNotIn(settings.STRIPE_LEGACY_PRO_PRICE_IDS[0], rendered)

    def test_wrong_pro_amount_fails_without_rendering_wrong_policy(self):
        create_current_price_rows()
        Price.objects.filter(id='price_pro999').update(
            stripe_data={
                'id': 'price_pro999',
                'unit_amount': 899,
                'unit_amount_decimal': '899',
                'currency': 'usd',
                'type': 'recurring',
                'recurring': {'interval': 'month', 'interval_count': 1},
            },
        )
        self.assertIn(
            'the synchronized pro monthly Price has the wrong amount',
            synchronized_plan_price_errors(),
        )

    def test_system_check_rejects_wrong_synchronized_amount(self):
        create_current_price_rows()
        Price.objects.filter(id='price_pro999').update(
            stripe_data={
                'id': 'price_pro999',
                'unit_amount': 899,
                'unit_amount_decimal': '899',
                'currency': 'usd',
                'type': 'recurring',
                'recurring': {'interval': 'month', 'interval_count': 1},
            },
        )
        self.webhook()
        messages = billing_configuration_check(None)
        self.assertEqual([message.id for message in messages], ['SalesLogApp.E003'])
        self.assertIn('has the wrong amount', str(messages[0]))

    def test_public_landing_displays_both_synchronized_plan_prices(self):
        create_current_price_rows()
        response = self.client.get(reverse('landing_page'))
        self.assertContains(response, '$4.99 USD per month')
        self.assertContains(response, '$49.00 USD per year')
        self.assertContains(response, '$9.99 USD per month')
        self.assertContains(response, '$99.00 USD per year')
        self.assertContains(response, 'Simple monthly or yearly plans')


@override_settings(**TIERED_SETTINGS)
class Bill2EntitlementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='bill2-entitled', email='bill2-entitled@example.test'
        )
        self.customer = Customer.objects.create(
            id='cus_bill2_entitled',
            subscriber=self.user,
            livemode=False,
            email=self.user.email,
            stripe_data={'id': 'cus_bill2_entitled', 'livemode': False},
        )

    def subscription(
        self, price_id, *, suffix, status='active', current_period_end=None,
        trial_end=None,
    ):
        data = {
            'id': f'sub_bill2_{suffix}',
            'object': 'subscription',
            'customer': self.customer.id,
            'status': status,
            'livemode': False,
            'metadata': {},
            'trial_end': int(trial_end.timestamp()) if trial_end else None,
            'current_period_end': (
                int(current_period_end.timestamp())
                if current_period_end else None
            ),
            'items': {'data': [{'price': {'id': price_id}}]},
        }
        return Subscription.objects.create(
            id=data['id'],
            customer=self.customer,
            livemode=False,
            stripe_data=data,
        )

    def entitlement_for(self, subscription, *, at_time=None):
        BillingAccess.objects.update_or_create(
            user=self.user,
            defaults={'authoritative_subscription': subscription},
        )
        return get_billing_entitlement(self.user, at_time=at_time)

    def test_basic_subscription_has_access_without_pro_features(self):
        subscription = self.subscription(
            'price_basic499', suffix='basic',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        entitlement = self.entitlement_for(subscription)
        self.assertTrue(entitlement.subscription_access)
        self.assertEqual(entitlement.tier, BASIC)
        self.assertFalse(entitlement.has_pro_access)
        self.assertFalse(entitlement.grandfathered)
        self.assertFalse(activity_goals_authorized(self.user))

    def test_current_pro_subscription_has_pro_access(self):
        subscription = self.subscription(
            'price_pro999', suffix='pro',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        entitlement = self.entitlement_for(subscription)
        self.assertTrue(entitlement.has_pro_access)
        self.assertEqual(entitlement.tier, PRO)
        self.assertFalse(entitlement.grandfathered)
        self.assertTrue(activity_goals_authorized(self.user))

    def test_legacy_price_is_grandfathered_pro_while_uninterrupted(self):
        now = timezone.now()
        subscription = self.subscription(
            'price_legacyinitial',
            suffix='legacy_current',
            status='canceled',
            current_period_end=now + timedelta(days=2),
        )
        entitlement = self.entitlement_for(subscription, at_time=now)
        self.assertTrue(entitlement.subscription_access)
        self.assertTrue(entitlement.has_pro_access)
        self.assertTrue(entitlement.grandfathered)

        subscription.stripe_data['current_period_end'] = int(
            (now - timedelta(seconds=1)).timestamp()
        )
        subscription.save(update_fields=['stripe_data'])
        expired = get_billing_entitlement(self.user, at_time=now)
        self.assertFalse(expired.subscription_access)
        self.assertFalse(expired.grandfathered)

    def test_mixed_basic_and_pro_subscription_fails_closed(self):
        subscription = self.subscription(
            'price_basic499', suffix='mixed',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        subscription.stripe_data['items']['data'].append({
            'price': {'id': 'price_pro999'},
        })
        subscription.save(update_fields=['stripe_data'])
        self.assertFalse(classify_subscription_plan(subscription).eligible)
        self.assertFalse(self.entitlement_for(subscription).subscription_access)

    def test_valid_and_unknown_price_subscription_fails_closed(self):
        subscription = self.subscription(
            'price_pro999', suffix='unknown_addon',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        subscription.stripe_data['items']['data'].append({
            'price': {'id': 'price_unknown_addon'},
        })
        subscription.save(update_fields=['stripe_data'])
        self.assertFalse(classify_subscription_plan(subscription).eligible)
        self.assertFalse(self.entitlement_for(subscription).subscription_access)

    @override_settings(
        BILLING_TIERED_PRICING_ENABLED=False,
        STRIPE_BASIC_MONTHLY_PRICE_ID='price_legacyinitial',
    )
    def test_rollout_off_preserves_original_single_price_as_pro(self):
        subscription = self.subscription(
            'price_legacyinitial', suffix='rollout_off',
            current_period_end=timezone.now() + timedelta(days=30),
        )
        entitlement = self.entitlement_for(subscription)
        self.assertTrue(entitlement.has_pro_access)
        self.assertTrue(entitlement.grandfathered)


@override_settings(**TIERED_SETTINGS)
class Bill2CheckoutTests(TestCase):
    def setUp(self):
        create_current_price_rows()
        self.user = get_user_model().objects.create_user(
            username='bill2-checkout',
            email='bill2-checkout@example.test',
            password='safe-test-password',
        )
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )
        self.client.force_login(self.user)

    @patch('SalesLogApp.billing_views.customer_for_user')
    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_basic_checkout_uses_only_server_configured_basic_price(
        self, create_session, customer_for_user,
    ):
        customer_for_user.return_value = SimpleNamespace(id='cus_bill2_basic')
        create_session.return_value = SimpleNamespace(
            url='https://checkout.stripe.com/c/pay/bill2-basic'
        )
        response = self.client.post(reverse('billing_checkout_start'), {
            'tier': BASIC,
            'billing_interval': 'month',
        })
        self.assertTrue(response.url.startswith('https://checkout.stripe.com/'))
        attempt = BillingCheckoutAttempt.objects.get(user=self.user)
        self.assertEqual(attempt.selected_tier, BASIC)
        self.assertEqual(attempt.selected_price_id, 'price_basic499')
        kwargs = create_session.call_args.kwargs
        self.assertEqual(kwargs['line_items'][0]['price'], 'price_basic499')
        self.assertEqual(kwargs['metadata']['selected_tier'], BASIC)

    @patch('SalesLogApp.billing_views.customer_for_user')
    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_pro_checkout_binds_pro_tier_and_price(
        self, create_session, customer_for_user,
    ):
        customer_for_user.return_value = SimpleNamespace(id='cus_bill2_pro')
        create_session.return_value = SimpleNamespace(
            url='https://checkout.stripe.com/c/pay/bill2-pro'
        )
        self.client.post(reverse('billing_checkout_start'), {
            'tier': PRO,
            'billing_interval': 'month',
        })
        attempt = BillingCheckoutAttempt.objects.get(user=self.user)
        self.assertEqual(attempt.selected_tier, PRO)
        self.assertEqual(attempt.selected_price_id, 'price_pro999')

    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_missing_or_unknown_plan_fails_before_stripe(self, create_session):
        for payload in (
            {},
            {'tier': 'founder_pro', 'billing_interval': 'month'},
            {'tier': 'price_pro999', 'billing_interval': 'month'},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    reverse('billing_checkout_start'), payload, follow=True,
                )
                self.assertContains(response, 'Choose an available StewLog plan.')
        create_session.assert_not_called()
        self.assertFalse(BillingCheckoutAttempt.objects.filter(user=self.user).exists())

    def test_switching_plan_expires_prior_attempt_instead_of_reusing_policy(self):
        first, _ = reserve_checkout_attempt(self.user, tier=BASIC)
        second, created = reserve_checkout_attempt(self.user, tier=PRO)
        first.refresh_from_db()
        self.assertTrue(created)
        self.assertEqual(first.status, BillingCheckoutAttempt.EXPIRED)
        self.assertEqual(second.selected_price_id, 'price_pro999')

    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_founder_cannot_select_basic_price(self, create_session):
        grant, raw_code = generate_founder_grant()
        redeem_founder_code(self.user, raw_code)
        response = self.client.post(
            reverse('billing_checkout_start'), {
                'tier': BASIC,
                'billing_interval': 'month',
            }, follow=True,
        )
        self.assertContains(response, 'Choose an available StewLog plan.')
        self.assertFalse(BillingCheckoutAttempt.objects.filter(user=self.user).exists())
        create_session.assert_not_called()
        grant.refresh_from_db()
        self.assertEqual(grant.redemption_count, 1)

    def test_webhook_finalization_rejects_tier_or_price_mismatch(self):
        attempt, _ = reserve_checkout_attempt(self.user, tier=PRO)
        customer = Customer.objects.create(
            id='cus_bill2_mismatch',
            subscriber=self.user,
            livemode=False,
            email=self.user.email,
            stripe_data={'id': 'cus_bill2_mismatch', 'livemode': False},
        )
        data = {
            'id': 'sub_bill2_mismatch',
            'object': 'subscription',
            'customer': customer.id,
            'status': 'trialing',
            'livemode': False,
            'trial_end': int((timezone.now() + timedelta(days=30)).timestamp()),
            'current_period_end': int(
                (timezone.now() + timedelta(days=30)).timestamp()
            ),
            'items': {'data': [{'price': {'id': 'price_basic499'}}]},
        }
        subscription = Subscription.objects.create(
            id=data['id'], customer=customer, livemode=False, stripe_data=data,
        )
        with self.assertRaises(BillingPolicyError):
            finalize_introductory_benefit(attempt.public_id, subscription)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, BillingCheckoutAttempt.RESERVED)
