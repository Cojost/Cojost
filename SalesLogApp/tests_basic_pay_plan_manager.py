from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .commission_service import CommissionEngineService
from .models import (
    PayPlanActivationEvent,
    PayPlanAssignment,
    PayPlanDocument,
    PayPlanRule,
    PayPlanVersion,
    Sale,
    UserProfile,
)
from .pay_plan_management import create_replacement_draft


class BasicPayPlanManagerTests(TestCase):
    password = 'basic-plan-password'

    def setUp(self):
        self.media = TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media.cleanup)
        self.user = self._create_v2_user('basic-plan-owner')
        self.other = self._create_v2_user('basic-plan-other')
        self.active = self.user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan',
        ).get().pay_plan_version
        self.active.rules.all().delete()
        self.active.default_backend_percentage = None
        self.active.default_backend_minimum = None
        self.active.default_backend_maximum = None
        self.active.status = PayPlanVersion.ACTIVE
        self.active.save(update_fields=[
            'default_backend_percentage', 'default_backend_minimum',
            'default_backend_maximum', 'status', 'updated_at',
        ])
        self.active_rule = PayPlanRule.objects.create(
            pay_plan_version=self.active,
            name='Ten percent front commission',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.10', 'gross_field': 'front_end_gross'},
            sort_order=1,
        )
        self.client.force_login(self.user)

    def _create_v2_user(self, username, *, staff=False):
        user = get_user_model().objects.create_user(
            username=username,
            password=self.password,
            is_staff=staff,
        )
        profile = user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        assignment = user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan',
        ).get()
        assignment.pay_plan_version.status = PayPlanVersion.ACTIVE
        assignment.pay_plan_version.save(update_fields=['status', 'updated_at'])
        onboarding = user.pay_plan_onboarding
        onboarding.current_pay_plan = assignment.pay_plan_version.pay_plan
        onboarding.current_version = assignment.pay_plan_version
        onboarding.status = onboarding.ACTIVE
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        return user

    @staticmethod
    def pdf(name='pay-plan.pdf'):
        return SimpleUploadedFile(
            name,
            b'%PDF-1.4\nBasic manager regression document\n%%EOF',
            content_type='application/pdf',
        )

    def _upload_draft(self):
        response = self.client.post(reverse('replace_pay_plan'), {
            'plan_name': self.active.pay_plan.name,
            'apply_from': 'current_month',
            'confirm_retroactive': 'on',
            'documents': self.pdf(),
        })
        self.assertEqual(response.status_code, 302)
        return PayPlanVersion.objects.filter(
            pay_plan__owner_user=self.user,
            status=PayPlanVersion.REVIEW_REQUIRED,
        ).exclude(pk=self.active.pk).latest('id')

    def _add_guided_rule(self, version, *, percentage='20'):
        return self.client.post(
            reverse('replacement_pay_plan_review', args=[version.pk]),
            {
                'action': 'add_rule',
                'name': 'Reviewed front commission',
                'rule_kind': 'front_percentage',
                'percentage': percentage,
                'amount': '',
                'minimum_units': '',
                'vehicle_condition': '',
            },
        )

    def test_regular_user_has_single_plain_language_pay_plan_entry_point(self):
        response = self.client.get(reverse('my_pay_plan'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'My Pay Plan')
        self.assertContains(response, 'How your commission is calculated')
        self.assertContains(response, 'Edit this plan')
        self.assertNotContains(response, 'Commission Sandbox')
        self.assertNotContains(response, 'Ask to change my plan')
        self.assertNotContains(response, 'front_gross_percentage')
        self.assertNotContains(response, 'configuration')
        self.assertNotContains(response, 'JSON')

        upload = self.client.get(reverse('replace_pay_plan'))
        self.assertNotContains(upload, 'pasted_text')
        self.assertNotContains(upload, 'Paste Text')

    def test_user_without_active_assignment_sees_upload_setup_state(self):
        PayPlanAssignment.objects.filter(user=self.user).update(is_active=False)
        self.active.status = PayPlanVersion.INACTIVE
        self.active.save(update_fields=['status', 'updated_at'])

        response = self.client.get(reverse('my_pay_plan'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Set up your pay plan')
        self.assertContains(response, 'Upload my pay plan')
        self.assertNotContains(response, 'Commission Sandbox')

    def test_upload_creates_private_inactive_draft_and_preserves_active_plan(self):
        draft = self._upload_draft()

        self.active.refresh_from_db()
        assignment = PayPlanAssignment.objects.get(
            user=self.user, is_active=True, effective_end_date__isnull=True,
        )
        self.assertEqual(self.active.status, PayPlanVersion.ACTIVE)
        self.assertEqual(assignment.pay_plan_version, self.active)
        self.assertEqual(draft.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertEqual(draft.previous_version, self.active)
        self.assertEqual(draft.documents.get().user, self.user)
        self.assertEqual(Sale.objects.filter(user=self.user).count(), 0)

    def test_upload_processing_failure_rolls_back_and_returns_normal_form(self):
        before_versions = PayPlanVersion.objects.filter(
            pay_plan__owner_user=self.user,
        ).count()
        before_documents = PayPlanDocument.objects.filter(user=self.user).count()

        with patch(
            'SalesLogApp.pay_plan_management.build_upload_import_draft',
            side_effect=RuntimeError('sensitive provider detail'),
        ):
            with self.assertLogs('SalesLogApp.views', level='ERROR') as logs:
                response = self.client.post(reverse('replace_pay_plan'), {
                    'plan_name': self.active.pay_plan.name,
                    'apply_from': 'current_month',
                    'confirm_retroactive': 'on',
                    'documents': self.pdf('private-name.pdf'),
                })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Your active pay plan was not changed')
        self.assertEqual(
            PayPlanVersion.objects.filter(pay_plan__owner_user=self.user).count(),
            before_versions,
        )
        self.assertEqual(PayPlanDocument.objects.filter(user=self.user).count(), before_documents)
        self.active.refresh_from_db()
        self.assertEqual(self.active.status, PayPlanVersion.ACTIVE)
        self.assertNotIn('private-name.pdf', ' '.join(logs.output))

    def test_guided_review_adds_rule_without_projection_or_technical_output(self):
        draft = self._upload_draft()
        with patch(
            'SalesLogApp.views.preview_version',
            side_effect=AssertionError('Basic review must not project sales'),
        ):
            response = self._add_guided_rule(draft)
        self.assertEqual(response.status_code, 302)
        rule = draft.rules.get(name='Reviewed front commission')
        self.assertEqual(rule.configuration['rate'], '0.2')
        draft.refresh_from_db()
        self.assertFalse(draft.processing_errors)

        response = self.client.get(
            reverse('replacement_pay_plan_review', args=[draft.pk]),
        )
        self.assertContains(response, '20% of front-end gross')
        self.assertNotContains(response, 'Sales preview')
        self.assertNotContains(response, 'front_gross_percentage')
        self.assertNotContains(response, 'Parser version')
        self.assertNotContains(response, '<code>')

    def test_active_edit_creates_deep_draft_and_only_draft_rule_changes(self):
        response = self.client.post(reverse('edit_pay_plan_manually'))
        self.assertEqual(response.status_code, 302)
        draft = PayPlanVersion.objects.get(
            pay_plan=self.active.pay_plan,
            source_type=PayPlanVersion.SOURCE_MANUAL,
            status=PayPlanVersion.REVIEW_REQUIRED,
        )
        draft_rule = draft.rules.get(semantic_key=self.active_rule.semantic_key)
        response = self.client.post(
            reverse('edit_pay_plan_rule', args=[draft.pk, draft_rule.pk]),
            {
                'name': 'Updated front commission',
                'rule_kind': 'front_percentage',
                'percentage': '25',
                'amount': '',
                'minimum_units': '',
                'vehicle_condition': 'new',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.active_rule.refresh_from_db()
        draft_rule.refresh_from_db()
        self.active.refresh_from_db()
        self.assertEqual(self.active_rule.configuration['rate'], '0.10')
        self.assertEqual(draft_rule.configuration['rate'], '0.25')
        self.assertEqual(draft_rule.conditions.get().value, 'new')
        self.assertEqual(self.active.status, PayPlanVersion.ACTIVE)

    def test_regular_user_cannot_edit_an_active_rule_in_place(self):
        response = self.client.get(reverse(
            'edit_pay_plan_rule', args=[self.active.pk, self.active_rule.pk],
        ))
        self.assertRedirects(response, reverse('my_pay_plan'))
        self.active_rule.refresh_from_db()
        self.assertEqual(self.active_rule.configuration['rate'], '0.10')

    def test_final_confirmation_get_is_read_only_and_post_activates_with_history(self):
        draft = self._upload_draft()
        self._add_guided_rule(draft)
        PayPlanRule.objects.create(
            pay_plan_version=draft,
            name='Disabled draft rule',
            rule_type='flat_per_deal',
            calculation_scope='per_sale',
            configuration={'amount': '25.00'},
            is_active=False,
        )
        url = reverse('confirm_pay_plan_activation', args=[draft.pk])

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'This pay plan is a <strong>Draft</strong> and has not been activated.',
        )
        self.assertContains(
            response,
            '<span class="status-badge status-draft">Draft</span>',
            html=True,
        )
        self.assertContains(
            response,
            '<span class="status-badge status-active">Enabled</span>',
            html=True,
        )
        self.assertContains(
            response,
            '<span class="status-badge status-inactive">Disabled</span>',
            html=True,
        )
        self.assertNotContains(
            response,
            '<span class="status-badge status-active">Active</span>',
            html=True,
        )
        draft.refresh_from_db()
        self.active.refresh_from_db()
        self.assertEqual(draft.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertEqual(self.active.status, PayPlanVersion.ACTIVE)

        response = self.client.post(url, {
            'confirm': 'on',
            'approve_warnings': 'on',
        })
        self.assertRedirects(response, reverse('my_pay_plan'))
        draft.refresh_from_db()
        self.active.refresh_from_db()
        self.assertEqual(draft.status, PayPlanVersion.ACTIVE)
        self.assertEqual(self.active.status, PayPlanVersion.INACTIVE)
        self.assertTrue(PayPlanActivationEvent.objects.filter(
            user=self.user,
            version=draft,
        ).exists())
        self.assertEqual(Sale.objects.filter(user=self.user).count(), 0)

    def test_active_plan_and_unfinished_draft_statuses_remain_distinct(self):
        draft = self._upload_draft()
        response = self.client.get(reverse('my_pay_plan'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Active pay plan')
        self.assertContains(
            response,
            '<span class="status-badge status-active">Active</span>',
            html=True,
        )
        self.assertContains(response, 'Draft waiting for review')
        self.assertContains(
            response,
            '<span class="status-badge status-draft">Draft</span>',
            html=True,
        )
        self.assertContains(response, 'It is not being used for commission calculations.')
        self.assertEqual(draft.status, PayPlanVersion.REVIEW_REQUIRED)

    def test_invalid_draft_cannot_activate_or_disturb_active_plan(self):
        draft = self._upload_draft()
        response = self.client.post(
            reverse('confirm_pay_plan_activation', args=[draft.pk]),
            {'confirm': 'on', 'approve_warnings': 'on'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Activation blocked')
        draft.refresh_from_db()
        self.active.refresh_from_db()
        self.assertEqual(draft.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertEqual(self.active.status, PayPlanVersion.ACTIVE)
        self.assertFalse(PayPlanActivationEvent.objects.filter(version=draft).exists())

    def test_cross_user_plan_draft_rule_and_upload_details_are_not_available(self):
        other_active = self.other.pay_plan_assignments.get().pay_plan_version
        other_draft = create_replacement_draft(
            self.other,
            [self.pdf('other-private.pdf')],
            other_active.pay_plan.name,
            timezone.localdate(),
        )
        other_rule = PayPlanRule.objects.create(
            pay_plan_version=other_draft,
            name='Other private rule',
            rule_type='flat_per_deal',
            calculation_scope='per_sale',
            configuration={'amount': '500.00'},
        )
        urls = (
            reverse('replacement_pay_plan_review', args=[other_draft.pk]),
            reverse('confirm_pay_plan_activation', args=[other_draft.pk]),
            reverse('pay_plan_rules', args=[other_draft.pk]),
            reverse('edit_pay_plan_rule', args=[other_draft.pk, other_rule.pk]),
        )
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertNotContains(response, 'other-private.pdf', status_code=404)

    def test_basic_direct_assistant_and_sandbox_urls_are_blocked_without_provider_call(self):
        with patch('SalesLogApp.views.provider_availability_for_user') as provider:
            assistant = self.client.post(reverse('pay_plan_assistant'), {
                'request_text': 'change my plan',
            })
        sandbox = self.client.get(reverse('commission_sandbox_index'))

        self.assertRedirects(assistant, reverse('my_pay_plan'))
        self.assertRedirects(sandbox, reverse('my_pay_plan'))
        provider.assert_not_called()

    def test_staff_retains_internal_assistant_and_sandbox_access(self):
        staff = self._create_v2_user('pay-plan-staff', staff=True)
        superuser = self._create_v2_user('pay-plan-superuser')
        superuser.is_superuser = True
        superuser.save(update_fields=['is_superuser'])

        for user in (staff, superuser):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(
                    self.client.get(reverse('pay_plan_assistant')).status_code,
                    200,
                )
                self.assertEqual(
                    self.client.get(reverse('commission_sandbox_index')).status_code,
                    200,
                )

    def test_state_changes_require_post_and_csrf(self):
        self.assertEqual(self.client.get(reverse('edit_pay_plan_manually')).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        response = csrf_client.post(reverse('edit_pay_plan_manually'))
        self.assertEqual(response.status_code, 403)

    def test_commission_and_half_double_credit_behavior_are_unchanged(self):
        sales = [
            Sale.objects.create(
                user=self.user,
                customer='Half deal',
                dealNumber=81001,
                count=Decimal('0.5'),
                frontEnd=Decimal('1000.00'),
                backend=Decimal('0.00'),
                date=timezone.localdate(),
            ),
            Sale.objects.create(
                user=self.user,
                customer='Double unit deal',
                dealNumber=81002,
                count=Decimal('2.0'),
                frontEnd=Decimal('1000.00'),
                backend=Decimal('0.00'),
                date=timezone.localdate(),
            ),
        ]
        half = CommissionEngineService.calculate_sale(self.user, sales[0])
        double = CommissionEngineService.calculate_sale(self.user, sales[1])

        self.assertEqual(sales[0].unit_credit, Decimal('0.5'))
        self.assertEqual(half.total_commission, Decimal('50.00'))
        self.assertEqual(sales[1].unit_credit, Decimal('2'))
        self.assertEqual(double.total_commission, Decimal('100.00'))
