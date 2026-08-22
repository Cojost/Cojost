import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from djstripe.models import WebhookEndpoint

from .billing_enforcement import cohort_enforcement_state
from .billing_enforcement_context import enforcement_notice
from .billing_entitlements import get_billing_entitlement
from .checks import billing_configuration_check
from .models import BillingAccess


BILL4_READY_SETTINGS = {
    'BILLING_FEATURE_ENABLED': True,
    'BILLING_ENFORCEMENT_ENABLED': True,
    'BILLING_ENFORCEMENT_EMERGENCY_BYPASS': False,
    'BILLING_ONBOARDING_ENABLED': False,
    'BILLING_TIERED_PRICING_ENABLED': False,
    'STRIPE_LIVE_MODE': False,
    'STRIPE_TEST_PUBLIC_KEY': 'pk_test_bill4public123',
    'STRIPE_TEST_SECRET_KEY': 'sk_test_bill4secret123',
    'STRIPE_BASIC_MONTHLY_PRICE_ID': 'price_bill4basicmonth',
    'DJSTRIPE_WEBHOOK_VALIDATION': 'verify_signature',
}


class Bill4SettingsTests(SimpleTestCase):
    def test_emergency_bypass_defaults_off(self):
        self.assertFalse(settings.BILLING_ENFORCEMENT_EMERGENCY_BYPASS)


class Bill4EnforcementStateTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = SimpleNamespace(is_authenticated=True, is_superuser=False)

    def access(self, **changes):
        values = {
            'enforcement_enrolled_at': self.now,
            'enforcement_notice_sent_at': self.now,
            'enforcement_grace_ends_at': self.now + timedelta(days=30),
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def state(self, access, *, subscribed=False):
        return cohort_enforcement_state(
            self.user,
            access,
            subscription_access=subscribed,
            at_time=self.now,
        )

    def test_only_notified_cohort_past_grace_is_blocked(self):
        self.assertEqual(self.state(None).code, 'not_enrolled')
        self.assertFalse(self.state(None).should_block)
        pending = self.access(enforcement_notice_sent_at=None)
        self.assertEqual(self.state(pending).code, 'notice_pending')
        self.assertFalse(self.state(pending).should_block)
        unconfigured = self.access(enforcement_grace_ends_at=None)
        self.assertEqual(self.state(unconfigured).code, 'grace_unconfigured')
        self.assertFalse(self.state(unconfigured).should_block)
        self.assertEqual(self.state(self.access()).code, 'grace_active')
        expired = self.access(
            enforcement_grace_ends_at=self.now - timedelta(seconds=1),
        )
        self.assertEqual(self.state(expired).code, 'enforcement_due')
        self.assertTrue(self.state(expired).should_block)

    def test_subscription_and_superuser_never_block(self):
        expired = self.access(
            enforcement_grace_ends_at=self.now - timedelta(seconds=1),
        )
        self.assertEqual(
            self.state(expired, subscribed=True).code,
            'subscribed',
        )
        self.user.is_superuser = True
        state = self.state(expired)
        self.assertEqual(state.code, 'superuser_exempt')
        self.assertFalse(state.should_block)


@override_settings(**BILL4_READY_SETTINGS)
class Bill4EntitlementAndMiddlewareTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='bill4-user',
            email='bill4-user@example.test',
        )

    def expire_grace(self):
        return BillingAccess.objects.create(
            user=self.user,
            enforcement_enrolled_at=timezone.now() - timedelta(days=31),
            enforcement_notice_sent_at=timezone.now() - timedelta(days=30),
            enforcement_grace_ends_at=timezone.now() - timedelta(seconds=1),
        )

    def test_unenrolled_existing_user_keeps_access(self):
        entitlement = get_billing_entitlement(self.user)
        self.assertTrue(entitlement.has_access)
        self.assertEqual(entitlement.source, 'enforcement_not_enrolled')
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('view_sales')).status_code, 200)

    def test_expired_notified_user_is_redirected_but_billing_is_open(self):
        self.expire_grace()
        entitlement = get_billing_entitlement(self.user)
        self.assertFalse(entitlement.has_access)
        self.assertEqual(entitlement.source, 'enforcement_due')
        self.client.force_login(self.user)
        self.assertRedirects(
            self.client.get(reverse('view_sales')),
            reverse('billing_overview'),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self.client.get(reverse('billing_overview')).status_code,
            200,
        )

    def test_active_grace_shows_notice_without_blocking(self):
        grace_end = timezone.now() + timedelta(days=7)
        BillingAccess.objects.create(
            user=self.user,
            enforcement_enrolled_at=timezone.now(),
            enforcement_notice_sent_at=timezone.now(),
            enforcement_grace_ends_at=grace_end,
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('view_sales'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose a StewLog plan before')
        self.assertContains(response, reverse('billing_overview'))

    @override_settings(BILLING_ENFORCEMENT_EMERGENCY_BYPASS=True)
    def test_emergency_bypass_restores_access(self):
        self.expire_grace()
        entitlement = get_billing_entitlement(self.user)
        self.assertTrue(entitlement.has_access)
        self.assertEqual(entitlement.source, 'enforcement_emergency_bypass')
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('view_sales')).status_code, 200)

    def test_superuser_is_exempt_from_billing_and_onboarding(self):
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        BillingAccess.objects.create(
            user=self.user,
            onboarding_required_at=timezone.now(),
            enforcement_enrolled_at=timezone.now(),
            enforcement_notice_sent_at=timezone.now(),
            enforcement_grace_ends_at=timezone.now() - timedelta(days=1),
        )
        self.client.force_login(self.user)
        with self.settings(BILLING_ONBOARDING_ENABLED=True):
            self.assertEqual(
                self.client.get(reverse('view_sales')).status_code,
                200,
            )
        entitlement = get_billing_entitlement(self.user)
        self.assertTrue(entitlement.has_access)
        self.assertEqual(entitlement.source, 'internal_superuser')

    def test_subscribed_state_hides_notice(self):
        access = self.expire_grace()
        request = RequestFactory().get('/')
        request.user = self.user
        subscribed = SimpleNamespace(subscription_access=True)
        with patch(
            'SalesLogApp.billing_enforcement_context.get_billing_entitlement',
            return_value=subscribed,
        ):
            self.assertIsNone(enforcement_notice(request)['billing_enforcement_notice'])
        access.refresh_from_db()


@override_settings(**BILL4_READY_SETTINGS)
class Bill4CohortCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='cohort-user')
        self.other = get_user_model().objects.create_user(username='other-user')
        self.superuser = get_user_model().objects.create_superuser(
            username='internal-admin',
            email='admin@example.test',
            password='unused-test-password',
        )

    def run_json(self, *args):
        output = StringIO()
        call_command('billing_enforcement_cohort', *args, '--json', stdout=output)
        return json.loads(output.getvalue())

    def test_audit_is_read_only_and_details_are_opt_in(self):
        with patch('stripe.checkout.Session.create') as stripe_create:
            report = self.run_json('--action', 'audit', '--all-existing')
        stripe_create.assert_not_called()
        self.assertFalse(report['applied'])
        self.assertFalse(report['network_calls'])
        self.assertNotIn('details', report)
        self.assertEqual(BillingAccess.objects.count(), 0)

        detailed = self.run_json(
            '--action', 'audit', '--user-id', str(self.user.pk), '--details',
        )
        self.assertEqual(detailed['details'][0]['username'], self.user.username)

    def test_enroll_requires_apply_and_exact_confirmation(self):
        dry_run = self.run_json(
            '--action', 'enroll', '--user-id', str(self.user.pk),
        )
        self.assertFalse(dry_run['applied'])
        self.assertFalse(BillingAccess.objects.filter(user=self.user).exists())
        with self.assertRaises(CommandError):
            call_command(
                'billing_enforcement_cohort',
                '--action', 'enroll',
                '--user-id', str(self.user.pk),
                '--apply',
                '--confirm', 'yes',
            )

        report = self.run_json(
            '--action', 'enroll',
            '--user-id', str(self.user.pk),
            '--apply',
            '--confirm', 'APPLY_BILLING_ENFORCEMENT_COHORT',
        )
        self.assertTrue(report['applied'])
        self.assertEqual(report['emails_sent'], 0)
        access = BillingAccess.objects.get(user=self.user)
        self.assertIsNotNone(access.enforcement_enrolled_at)
        self.assertIsNone(access.enforcement_notice_sent_at)

    def test_superusers_cannot_be_mutation_targets(self):
        with self.assertRaises(CommandError):
            call_command(
                'billing_enforcement_cohort',
                '--action', 'enroll',
                '--user-id', str(self.superuser.pk),
            )

    def test_all_existing_excludes_onboarding_cohort(self):
        BillingAccess.objects.create(
            user=self.other,
            onboarding_required_at=timezone.now(),
        )
        self.run_json(
            '--action', 'enroll',
            '--all-existing',
            '--apply',
            '--confirm', 'APPLY_BILLING_ENFORCEMENT_COHORT',
        )
        self.assertIsNotNone(
            BillingAccess.objects.get(user=self.user).enforcement_enrolled_at,
        )
        self.assertIsNone(
            BillingAccess.objects.get(user=self.other).enforcement_enrolled_at,
        )

    def test_notice_requires_enrollment_and_starts_grace_once(self):
        with self.assertRaises(CommandError):
            call_command(
                'billing_enforcement_cohort',
                '--action', 'mark-notice',
                '--user-id', str(self.user.pk),
                '--apply',
                '--confirm', 'APPLY_BILLING_ENFORCEMENT_COHORT',
            )
        BillingAccess.objects.create(
            user=self.user,
            enforcement_enrolled_at=timezone.now(),
        )
        before = timezone.now() + timedelta(days=29)
        report = self.run_json(
            '--action', 'mark-notice',
            '--user-id', str(self.user.pk),
            '--grace-days', '30',
            '--apply',
            '--confirm', 'APPLY_BILLING_ENFORCEMENT_COHORT',
        )
        self.assertEqual(report['notice_recorded'], 1)
        self.assertEqual(report['emails_sent'], 0)
        access = BillingAccess.objects.get(user=self.user)
        original_grace_end = access.enforcement_grace_ends_at
        self.assertGreater(original_grace_end, before)
        repeat = self.run_json(
            '--action', 'mark-notice',
            '--user-id', str(self.user.pk),
            '--grace-days', '60',
            '--apply',
            '--confirm', 'APPLY_BILLING_ENFORCEMENT_COHORT',
        )
        self.assertEqual(repeat['unchanged'], 1)
        access.refresh_from_db()
        self.assertEqual(access.enforcement_grace_ends_at, original_grace_end)


@override_settings(**BILL4_READY_SETTINGS)
class Bill4ReadinessTests(TestCase):
    def setUp(self):
        WebhookEndpoint.objects.create(
            id='we_bill4_ready',
            livemode=False,
            url='https://example.test/stripe/webhook/bill4/',
            enabled_events=['customer.subscription.updated'],
            secret='whsec_bill4_test_value',
            status='enabled',
            stripe_data={},
        )

    def readiness(self):
        output = StringIO()
        call_command('billing_readiness', '--json', stdout=output)
        return json.loads(output.getvalue())

    def test_readiness_reports_migration_and_effective_enforcement(self):
        report = self.readiness()
        self.assertTrue(report['migrations']['staged_enforcement'])
        self.assertTrue(report['enforcement_ready'])
        self.assertFalse(report['enforcement_emergency_bypass_enabled'])
        self.assertTrue(report['enforcement_effective'])

    @override_settings(BILLING_ENFORCEMENT_EMERGENCY_BYPASS=True)
    def test_bypass_is_visible_and_warns_without_failing_readiness(self):
        report = self.readiness()
        self.assertTrue(report['enforcement_ready'])
        self.assertTrue(report['enforcement_emergency_bypass_enabled'])
        self.assertFalse(report['enforcement_effective'])
        messages = billing_configuration_check(None)
        self.assertEqual([message.id for message in messages], ['SalesLogApp.W003'])
