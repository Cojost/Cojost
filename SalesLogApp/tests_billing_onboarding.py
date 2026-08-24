from datetime import timedelta
import re
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from djstripe.models import Customer, Subscription

from .billing_services import (
    BillingPolicyError,
    generate_founder_grant,
    redeem_founder_code,
    reserve_checkout_attempt,
)
from .billing_webhooks import reconcile_billing_event
from .checks import billing_configuration_check
from .models import (
    BillingAccess,
    BillingCheckoutAttempt,
    PayPlan,
    PayPlanAssignment,
)


BILLING_ONBOARDING_SETTINGS = {
    'BILLING_FEATURE_ENABLED': True,
    'BILLING_ENFORCEMENT_ENABLED': False,
    'BILLING_ONBOARDING_ENABLED': True,
    'STRIPE_LIVE_MODE': False,
    'STRIPE_TEST_PUBLIC_KEY': 'pk_test_onboardingpublic123',
    'STRIPE_TEST_SECRET_KEY': 'sk_test_onboardingsecret123',
    'STRIPE_LIVE_PUBLIC_KEY': 'pk_live_onboardingpublic123',
    'STRIPE_LIVE_SECRET_KEY': 'sk_live_onboardingsecret123',
    'STRIPE_BASIC_MONTHLY_PRICE_ID': 'price_onboardingmonthly123',
    'BILLING_STANDARD_TRIAL_DAYS': 30,
    'BILLING_FOUNDER_TRIAL_DAYS': 90,
    'DJSTRIPE_WEBHOOK_VALIDATION': 'verify_signature',
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
}


@override_settings(**BILLING_ONBOARDING_SETTINGS)
class BillingSignupOnboardingTests(TestCase):
    def create_user(self, username='new-owner', *, marked=True, verified=False):
        email = f'{username}@example.test'
        user = get_user_model().objects.create_user(
            username=username,
            email=email,
            password='safe-test-password-482!',
        )
        EmailAddress.objects.create(
            user=user,
            email=email,
            primary=True,
            verified=verified,
        )
        if marked:
            BillingAccess.objects.create(
                user=user,
                onboarding_required_at=timezone.now(),
            )
        return user

    def subscription(self, user, *, suffix='trial', status='trialing'):
        customer = Customer.objects.create(
            id=f'cus_onboarding_{suffix}',
            subscriber=user,
            livemode=False,
            email=user.email,
            stripe_data={
                'id': f'cus_onboarding_{suffix}',
                'object': 'customer',
                'livemode': False,
            },
        )
        trial_end = timezone.now() + timedelta(days=30)
        data = {
            'id': f'sub_onboarding_{suffix}',
            'object': 'subscription',
            'customer': customer.id,
            'status': status,
            'livemode': False,
            'metadata': {},
            'trial_end': int(trial_end.timestamp()),
            'current_period_end': int(trial_end.timestamp()),
            'items': {'data': [{
                'price': {'id': settings.STRIPE_BASIC_MONTHLY_PRICE_ID},
            }]},
        }
        subscription = Subscription.objects.create(
            id=data['id'],
            customer=customer,
            livemode=False,
            stripe_data=data,
        )
        BillingAccess.objects.filter(user=user).update(
            authoritative_subscription=subscription,
        )
        return subscription

    def test_signup_marks_only_enabled_cohort_and_starts_at_verification(self):
        response = self.client.post(reverse('account_signup'), {
            'username': 'signup-owner',
            'email': 'signup-owner@example.test',
            'password1': 'A-strong-local-test-password-482!',
            'password2': 'A-strong-local-test-password-482!',
        })

        user = get_user_model().objects.get(username='signup-owner')
        self.assertRedirects(
            response,
            reverse('account_email_verification_sent'),
        )
        self.assertIsNotNone(user.billing_access.onboarding_required_at)
        self.assertFalse(
            EmailAddress.objects.get(user=user, primary=True).verified
        )
        self.assertFalse(
            PayPlanAssignment.objects.filter(user=user, is_active=True).exists()
        )
        self.assertFalse(
            PayPlan.objects.filter(
                owner_user=user,
                name='Legacy Automotive Pay Plan',
            ).exists()
        )
        onboarding = user.pay_plan_onboarding
        self.assertEqual(onboarding.status, onboarding.NOT_STARTED)
        self.assertIsNone(onboarding.current_pay_plan_id)
        self.assertIsNone(onboarding.current_version_id)

    @override_settings(BILLING_ENFORCEMENT_ENABLED=True)
    def test_new_signup_billing_page_uses_customer_onboarding_wording(self):
        user = self.create_user('wording-owner', verified=True)
        self.client.force_login(user)

        response = self.client.get(reverse('billing_overview'))

        self.assertContains(
            response,
            'Choose a StewLog plan to continue setting up your account.',
        )
        self.assertNotContains(
            response,
            'This account is not enrolled for billing enforcement.',
        )

    def test_confirmed_new_signup_sees_empty_normalized_pay_plan_setup(self):
        response = self.client.post(reverse('account_signup'), {
            'username': 'normalized-owner',
            'email': 'normalized-owner@example.test',
            'password1': 'A-strong-local-test-password-482!',
            'password2': 'A-strong-local-test-password-482!',
        })
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(username='normalized-owner')
        EmailAddress.objects.filter(user=user).update(verified=True)
        self.subscription(user, suffix='normalized-owner')
        self.client.force_login(user)

        response = self.client.get(reverse('my_pay_plan'))

        self.assertContains(response, 'Set up your pay plan')
        self.assertContains(response, 'Upload my pay plan')
        self.assertNotContains(response, 'Active pay plan')
        self.assertNotContains(response, 'Legacy Automotive Pay Plan')

    def test_email_confirmation_continues_to_billing(self):
        self.client.post(reverse('account_signup'), {
            'username': 'verify-owner',
            'email': 'verify-owner@example.test',
            'password1': 'A-strong-local-test-password-482!',
            'password2': 'A-strong-local-test-password-482!',
        })
        confirmation_url = re.search(
            r'https?://[^\s]+/accounts/confirm-email/[^\s]+/',
            mail.outbox[0].body,
        ).group(0)

        response = self.client.post(urlsplit(confirmation_url).path)

        self.assertRedirects(response, reverse('billing_overview'))
        self.assertTrue(
            EmailAddress.objects.get(
                user__username='verify-owner',
                primary=True,
            ).verified
        )

    def test_legacy_registration_route_cannot_bypass_allauth_signup(self):
        response = self.client.post(reverse('register'), {
            'username': 'legacy-bypass',
            'email': 'legacy-bypass@example.test',
            'password1': 'A-strong-local-test-password-482!',
            'password2': 'A-strong-local-test-password-482!',
        })

        self.assertRedirects(response, reverse('account_signup'))
        self.assertFalse(
            get_user_model().objects.filter(username='legacy-bypass').exists()
        )

    @override_settings(BILLING_ONBOARDING_ENABLED=False)
    def test_disabled_flag_does_not_mark_new_signup(self):
        response = self.client.post(reverse('account_signup'), {
            'username': 'flag-off-owner',
            'email': 'flag-off-owner@example.test',
            'password1': 'A-strong-local-test-password-482!',
            'password2': 'A-strong-local-test-password-482!',
        })

        user = get_user_model().objects.get(username='flag-off-owner')
        self.assertRedirects(
            response,
            reverse('pay_plan_setup'),
            fetch_redirect_response=False,
        )
        self.assertFalse(BillingAccess.objects.filter(user=user).exists())

    def test_existing_user_without_marker_keeps_existing_login_flow(self):
        user = self.create_user('existing-owner', marked=False, verified=False)

        response = self.client.post(reverse('account_login'), {
            'login': user.username,
            'password': 'safe-test-password-482!',
        })

        self.assertRedirects(
            response,
            reverse('pay_plan_setup'),
            fetch_redirect_response=False,
        )

    def test_marked_user_login_requires_verification_then_billing(self):
        user = self.create_user('gated-owner')

        response = self.client.post(reverse('account_login'), {
            'login': user.username,
            'password': 'safe-test-password-482!',
        })
        self.assertRedirects(
            response,
            reverse('account_email_verification_sent'),
        )

        EmailAddress.objects.filter(user=user).update(verified=True)
        self.client.logout()
        response = self.client.post(reverse('account_login'), {
            'login': user.username,
            'password': 'safe-test-password-482!',
        })
        self.assertRedirects(response, reverse('billing_overview'))

    def test_direct_application_routes_cannot_bypass_onboarding(self):
        user = self.create_user('direct-route-owner')
        self.client.force_login(user)

        for route_name in ('view_sales', 'pay_plan_setup'):
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                self.assertRedirects(
                    response,
                    reverse('account_email_verification_sent'),
                    fetch_redirect_response=False,
                )

        settings_response = self.client.get(
            reverse('profile'), {'section': 'billing'},
        )
        self.assertEqual(settings_response.status_code, 200)
        self.assertContains(settings_response, 'id="billing-settings"')
        self.assertContains(settings_response, 'Verify your email before Checkout.')

        EmailAddress.objects.filter(user=user).update(verified=True)
        response = self.client.get(reverse('pay_plan_setup'))
        self.assertRedirects(
            response,
            reverse('billing_overview'),
            fetch_redirect_response=False,
        )

    @override_settings(BILLING_ONBOARDING_ENABLED=False)
    def test_disabling_rollout_releases_marked_user_without_data_changes(self):
        user = self.create_user('rollout-off-owner')
        self.client.force_login(user)

        response = self.client.get(reverse('profile'))

        self.assertEqual(response.status_code, 200)
        user.billing_access.refresh_from_db()
        self.assertIsNotNone(user.billing_access.onboarding_required_at)

    def test_verified_alternate_email_does_not_unlock_canonical_identity(self):
        user = self.create_user('alternate-email-owner')
        EmailAddress.objects.create(
            user=user,
            email='alternate@example.test',
            verified=True,
            primary=False,
        )
        self.client.force_login(user)

        response = self.client.post(reverse('billing_checkout_start'))

        self.assertRedirects(
            response,
            reverse('account_email_verification_sent'),
        )
        self.assertFalse(BillingCheckoutAttempt.objects.filter(user=user).exists())

    @patch('SalesLogApp.billing_views.customer_for_user')
    @patch('SalesLogApp.billing_gateway.stripe.checkout.Session.create')
    def test_verified_checkout_can_retry_without_duplicate_attempt(
        self,
        create_session,
        customer_for_user,
    ):
        user = self.create_user('retry-owner', verified=True)
        self.client.force_login(user)
        customer_for_user.return_value = SimpleNamespace(id='cus_retry_owner')
        create_session.return_value = SimpleNamespace(
            url='https://checkout.stripe.com/c/pay/retry-owner'
        )

        first = self.client.post(reverse('billing_checkout_start'))
        attempt = BillingCheckoutAttempt.objects.get(user=user)
        canceled = self.client.get(reverse('billing_checkout_cancel'))
        second = self.client.post(reverse('billing_checkout_start'))

        self.assertTrue(first.url.startswith('https://checkout.stripe.com/'))
        self.assertEqual(canceled.status_code, 200)
        self.assertTrue(second.url.startswith('https://checkout.stripe.com/'))
        self.assertEqual(BillingCheckoutAttempt.objects.filter(user=user).count(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.trial_days, 0)
        self.assertIsNone(
            user.billing_access.introductory_benefit_consumed_at
        )

    def test_service_rejects_unverified_checkout_before_reservation(self):
        user = self.create_user('service-owner')

        with self.assertRaisesMessage(
            BillingPolicyError,
            'Verify your account email before starting billing.',
        ):
            reserve_checkout_attempt(user)

        self.assertFalse(BillingCheckoutAttempt.objects.filter(user=user).exists())

    def test_unverified_account_cannot_consume_founder_code(self):
        user = self.create_user('unverified-founder-owner')
        grant, raw_code = generate_founder_grant()

        with self.assertRaisesMessage(
            BillingPolicyError,
            'Verify your account email before redeeming a founder code.',
        ):
            redeem_founder_code(user, raw_code)

        grant.refresh_from_db()
        self.assertEqual(grant.redemption_count, 0)
        self.assertIsNone(grant.redeemed_user_id)

    def test_confirmed_subscription_hands_user_to_pay_plan(self):
        user = self.create_user('subscribed-owner', verified=True)
        self.subscription(user)
        self.client.force_login(user)

        success = self.client.get(reverse('billing_checkout_success'))
        application = self.client.get(reverse('profile'))

        self.assertRedirects(
            success,
            reverse('my_pay_plan'),
            fetch_redirect_response=False,
        )
        self.assertEqual(application.status_code, 200)

    def test_completed_pay_plan_returns_subscribed_user_to_dashboard(self):
        user = self.create_user('active-plan-owner', verified=True)
        self.subscription(user, suffix='active-plan')
        user.pay_plan_onboarding.status = user.pay_plan_onboarding.ACTIVE
        user.pay_plan_onboarding.save(update_fields=['status', 'updated_at'])

        response = self.client.post(reverse('account_login'), {
            'login': user.username,
            'password': 'safe-test-password-482!',
        })

        self.assertRedirects(
            response,
            reverse('view_sales'),
            fetch_redirect_response=False,
        )

    def test_duplicate_webhook_keeps_one_owner_scoped_onboarding_record(self):
        user = self.create_user('webhook-owner', verified=True)
        attempt, _ = reserve_checkout_attempt(user)
        subscription = self.subscription(user, suffix='webhook')
        subscription.stripe_data['metadata'] = {
            'billing_attempt': str(attempt.public_id),
        }
        subscription.save(update_fields=['stripe_data'])
        event = SimpleNamespace(
            type='customer.subscription.created',
            created=timezone.now(),
            data={'object': subscription.stripe_data},
        )

        self.assertTrue(reconcile_billing_event(event))
        consumed_at = BillingAccess.objects.get(
            user=user
        ).introductory_benefit_consumed_at
        self.assertTrue(reconcile_billing_event(event))

        access = BillingAccess.objects.get(user=user)
        self.assertEqual(access.introductory_benefit_consumed_at, consumed_at)
        self.assertIsNotNone(access.onboarding_required_at)
        self.assertEqual(BillingAccess.objects.filter(user=user).count(), 1)


class BillingOnboardingConfigurationTests(TestCase):
    @override_settings(
        BILLING_FEATURE_ENABLED=False,
        BILLING_ENFORCEMENT_ENABLED=False,
        BILLING_ONBOARDING_ENABLED=True,
        STRIPE_TEST_PUBLIC_KEY='pk_test_onboardingpublic123',
        STRIPE_TEST_SECRET_KEY='sk_test_onboardingsecret123',
        STRIPE_BASIC_MONTHLY_PRICE_ID='price_onboardingmonthly123',
        DJSTRIPE_WEBHOOK_VALIDATION='verify_signature',
    )
    def test_onboarding_cannot_be_enabled_without_billing_feature(self):
        messages = billing_configuration_check(None)

        self.assertEqual([message.id for message in messages], ['SalesLogApp.E002'])
        self.assertIn('billing onboarding requires the billing feature', str(messages[0]))
