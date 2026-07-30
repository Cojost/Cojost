from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .commission_service import CommissionEngineService
from .models import (
    PayPlanActivationEvent,
    PayPlanEligibility,
    PayPlanRule,
    PayPlanVersion,
    Sale,
    UserProfile,
)
from .pay_plan_management import (
    PayPlanActivationService,
    create_replacement_draft,
    recalculate_commissions,
)
from .pay_plan_imports import parse_description_to_import_draft


class PayPlanReplacementWorkflowTests(TestCase):
    def setUp(self):
        self.media = TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media.cleanup)
        self.user = get_user_model().objects.create_user(
            username='replacement-owner', password='test-password',
        )
        self.other = get_user_model().objects.create_user(
            username='replacement-other', password='test-password',
        )
        self.profile = self.user.sales_profile
        self.profile.commission_system = UserProfile.PAY_PLAN_V2
        self.profile.save(update_fields=['commission_system', 'updated_at'])
        self.active_assignment = self.user.pay_plan_assignments.get()
        self.active_version = self.active_assignment.pay_plan_version
        self.active_version.effective_start_date = timezone.localdate().replace(day=1)
        self.active_version.save(update_fields=['effective_start_date', 'updated_at'])
        self.active_assignment.effective_start_date = self.active_version.effective_start_date
        self.active_assignment.save(update_fields=['effective_start_date', 'updated_at'])
        self.user.pay_plan_onboarding.current_pay_plan = self.active_version.pay_plan
        self.user.pay_plan_onboarding.current_version = self.active_version
        self.user.pay_plan_onboarding.status = self.user.pay_plan_onboarding.ACTIVE
        self.user.pay_plan_onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        PayPlanRule.objects.create(
            pay_plan_version=self.active_version,
            name='Existing 10% Front',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.10', 'gross_field': 'front_end_gross'},
        )
        self.sale = Sale.objects.create(
            user=self.user,
            customer='Buyer',
            dealNumber=980001,
            count=Decimal('1.0'),
            frontEnd=Decimal('1000.00'),
            backend=Decimal('0.00'),
            date=timezone.localdate(),
        )
        self.client.login(username=self.user.username, password='test-password')

    @staticmethod
    def pdf(name='replacement.pdf'):
        return SimpleUploadedFile(
            name, b'%PDF-1.4\nsafe test document\n%%EOF',
            content_type='application/pdf',
        )

    def create_valid_draft(self):
        version = PayPlanVersion.objects.create(
            pay_plan=self.active_version.pay_plan,
            version_name='Version 2',
            version_number=2,
            effective_start_date=timezone.localdate().replace(day=1),
            status=PayPlanVersion.REVIEW_REQUIRED,
            source_type=PayPlanVersion.SOURCE_MANUAL,
            previous_version=self.active_version,
            created_by=self.user,
            processing_status='needs_review',
        )
        PayPlanRule.objects.create(
            pay_plan_version=version,
            name='Replacement 20% Front',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.20', 'gross_field': 'front_end_gross'},
        )
        return version

    def test_replacement_upload_creates_review_draft_without_replacing_active(self):
        response = self.client.post(reverse('replace_pay_plan'), {
            'plan_name': self.active_version.pay_plan.name,
            'apply_from': 'current_month',
            'confirm_retroactive': 'on',
            'documents': self.pdf(),
        })
        self.assertEqual(response.status_code, 302)
        draft = PayPlanVersion.objects.filter(
            pay_plan=self.active_version.pay_plan,
        ).exclude(pk=self.active_version.pk).get()
        self.assertEqual(draft.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertEqual(draft.previous_version, self.active_version)
        self.active_version.refresh_from_db()
        self.assertEqual(self.active_version.status, PayPlanVersion.ACTIVE)
        self.assertEqual(draft.rules.count(), 0)
        self.assertTrue(draft.processing_errors)

    def test_retroactive_replacement_requires_explicit_confirmation(self):
        response = self.client.post(reverse('replace_pay_plan'), {
            'plan_name': self.active_version.pay_plan.name,
            'apply_from': 'current_month',
            'documents': self.pdf(),
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Confirm retroactive recalculation or choose future sales only.',
        )
        self.assertEqual(
            PayPlanVersion.objects.filter(
                pay_plan=self.active_version.pay_plan,
            ).count(),
            1,
        )

    def test_pasted_text_creates_review_draft_and_extracted_rules(self):
        response = self.client.post(reverse('replace_pay_plan'), {
            'plan_name': self.active_version.pay_plan.name,
            'apply_from': 'current_month',
            'confirm_retroactive': 'on',
            'pasted_text': (
                'Salesperson receives 25% of front-end gross and '
                '5% of F&I gross.'
            ),
        })
        self.assertEqual(response.status_code, 302)
        draft = PayPlanVersion.objects.exclude(
            pk=self.active_version.pk,
        ).latest('id')
        self.assertEqual(draft.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertEqual(draft.source_type, PayPlanVersion.SOURCE_PASTE)
        self.assertEqual(draft.previous_version, self.active_version)
        self.assertEqual(draft.rules.filter(is_active=True).count(), 2)
        self.active_version.refresh_from_db()
        self.assertEqual(self.active_version.status, PayPlanVersion.ACTIVE)

    def test_replacement_requires_file_or_pasted_text(self):
        response = self.client.post(reverse('replace_pay_plan'), {
            'plan_name': self.active_version.pay_plan.name,
            'apply_from': 'current_month',
            'confirm_retroactive': 'on',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'Upload a pay-plan document or paste the pay-plan text.',
        )

    def test_pasted_text_with_no_rules_stays_reviewable_and_cannot_replace_active(self):
        response = self.client.post(reverse('replace_pay_plan'), {
            'plan_name': self.active_version.pay_plan.name,
            'apply_from': 'current_month',
            'confirm_retroactive': 'on',
            'pasted_text': 'This text contains no commission values.',
        })
        self.assertEqual(response.status_code, 302)
        draft = PayPlanVersion.objects.exclude(
            pk=self.active_version.pk,
        ).latest('id')
        self.assertEqual(draft.rules.count(), 0)
        self.assertTrue(draft.processing_errors)
        self.active_version.refresh_from_db()
        self.assertEqual(self.active_version.status, PayPlanVersion.ACTIVE)

    def test_reload_creates_new_version_and_preserves_source(self):
        first = create_replacement_draft(
            self.user, [self.pdf()], self.active_version.pay_plan.name,
            timezone.localdate().replace(day=1),
        )
        response = self.client.post(reverse('reload_pay_plan'))
        self.assertEqual(response.status_code, 302)
        reloaded = PayPlanVersion.objects.order_by('-id').first()
        self.assertNotEqual(reloaded, first)
        self.assertEqual(reloaded.source_type, PayPlanVersion.SOURCE_RELOAD)
        self.assertEqual(reloaded.source_filename, 'replacement.pdf')

    def test_user_cannot_review_another_users_draft(self):
        other_version = self.other.pay_plan_assignments.get().pay_plan_version
        response = self.client.get(reverse(
            'replacement_pay_plan_review', args=[other_version.id],
        ))
        self.assertEqual(response.status_code, 404)

    def test_empty_draft_cannot_deactivate_current_plan(self):
        draft = PayPlanVersion.objects.create(
            pay_plan=self.active_version.pay_plan,
            version_name='Empty draft',
            effective_start_date=timezone.localdate(),
            status=PayPlanVersion.REVIEW_REQUIRED,
            previous_version=self.active_version,
        )
        with self.assertRaises(ValidationError):
            PayPlanActivationService.activate(self.user, draft)
        self.active_version.refresh_from_db()
        self.assertEqual(self.active_version.status, PayPlanVersion.ACTIVE)

    def test_activation_replaces_plan_and_recalculates_existing_sale(self):
        draft = self.create_valid_draft()
        report = PayPlanActivationService.activate(self.user, draft)
        self.active_version.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(self.active_version.status, PayPlanVersion.INACTIVE)
        self.assertEqual(draft.status, PayPlanVersion.ACTIVE)
        self.assertEqual(report['calculated_count'], 1)
        self.assertEqual(Decimal(report['new_total']), Decimal('200.00'))
        self.assertEqual(Sale.objects.filter(user=self.user).count(), 1)
        self.assertTrue(PayPlanActivationEvent.objects.filter(
            user=self.user, version=draft,
            action=PayPlanActivationEvent.ACTIVATED,
        ).exists())

    def test_preview_and_live_use_same_engine_result(self):
        draft = self.create_valid_draft()
        preview = CommissionEngineService.preview_sales(
            self.user, [self.sale], draft,
        )
        PayPlanActivationService.activate(self.user, draft)
        live = CommissionEngineService.calculate_sales(self.user, [self.sale])
        self.assertEqual(preview['estimated_total'], live['total_commission'])

    def test_manual_recalculation_does_not_create_sales(self):
        before = Sale.objects.filter(user=self.user).count()
        report = recalculate_commissions(self.user)
        self.assertEqual(report['calculated_count'], 1)
        self.assertEqual(Sale.objects.filter(user=self.user).count(), before)

    def test_recalculate_endpoint_requires_post(self):
        response = self.client.get(reverse('recalculate_pay_plan_commissions'))
        self.assertEqual(response.status_code, 405)

    def test_manual_edit_action_clones_rules_without_changing_active_plan(self):
        response = self.client.post(reverse('edit_pay_plan_manually'))
        self.assertEqual(response.status_code, 302)
        draft = PayPlanVersion.objects.filter(
            pay_plan=self.active_version.pay_plan,
            source_type=PayPlanVersion.SOURCE_MANUAL,
        ).exclude(pk=self.active_version.pk).get()
        self.assertEqual(draft.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertEqual(draft.rules.count(), self.active_version.rules.count())
        self.active_version.refresh_from_db()
        self.assertEqual(self.active_version.status, PayPlanVersion.ACTIVE)

    def test_recalculate_endpoint_requires_authentication(self):
        self.client.logout()
        response = self.client.post(reverse('recalculate_pay_plan_commissions'))
        self.assertEqual(response.status_code, 302)

    def test_commission_page_shows_user_friendly_plan_summary(self):
        response = self.client.get(reverse('view_commission'))
        self.assertContains(response, 'Active plan')
        self.assertContains(response, 'Upload replacement plan')
        self.assertContains(response, 'Sales included')
        self.assertNotContains(response, 'Pay Plan Management')
        self.assertNotContains(response, 'Parser Warnings')
        self.assertNotContains(response, 'Matched rules')
        self.assertNotContains(response, self.active_version.version_name)

    def test_legacy_settings_do_not_override_new_engine(self):
        diagnostic = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(diagnostic.engine, UserProfile.PAY_PLAN_V2)
        self.assertEqual(diagnostic.total_commission, Decimal('100.00'))

    def test_parser_recognizes_itsumi_base_terms_without_guessing_qualifiers(self):
        draft = parse_description_to_import_draft(
            'Minimum Commission per full deal shall be $250.00. '
            '25% FRONT END GROSS and 3.0% of Finance Gross. '
            'To qualify for the Finance Gross portion, NPS score must be eligible.',
            'Itsumi Pay Plan',
        )
        configurations = {
            rule['rule_type']: rule['configuration'] for rule in draft['rules']
        }
        self.assertEqual(
            configurations['front_gross_percentage']['rate'], '0.2500',
        )
        self.assertEqual(
            configurations['back_gross_percentage']['rate'], '0.0300',
        )
        back_rule = next(
            rule for rule in draft['rules']
            if rule['rule_type'] == 'back_gross_percentage'
        )
        self.assertEqual(
            back_rule['conditions'],
            [{
                'field_name': 'nps_finance_eligible',
                'operator': 'is_true',
                'value': True,
            }],
        )
        self.assertEqual(
            configurations['minimum_commission']['minimum_amount'], '250.00',
        )

    def test_activation_accepts_unary_boolean_conditions_saved_with_value(self):
        draft = PayPlanVersion.objects.create(
            pay_plan=self.active_version.pay_plan,
            version_name='Version unary bool',
            version_number=3,
            effective_start_date=timezone.localdate().replace(day=1),
            status=PayPlanVersion.REVIEW_REQUIRED,
            source_type=PayPlanVersion.SOURCE_MANUAL,
            previous_version=self.active_version,
            created_by=self.user,
            processing_status='needs_review',
        )
        back_rule = PayPlanRule.objects.create(
            pay_plan_version=draft,
            name='3% Finance if NPS Eligible',
            rule_type='back_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.03', 'gross_field': 'back_end_gross'},
            sort_order=1,
        )
        back_rule.conditions.create(
            field_name='nps_finance_eligible',
            operator='is_true',
            value=True,
            sort_order=1,
        )
        PayPlanEligibility.objects.create(
            user=self.user,
            month_start=timezone.localdate().replace(day=1),
            nps_status=PayPlanEligibility.NPS_ELIGIBLE,
        )

        report = PayPlanActivationService.activate(
            self.user, draft, warnings_approved=True,
        )

        draft.refresh_from_db()
        self.assertEqual(draft.status, PayPlanVersion.ACTIVE)
        self.assertEqual(report['sales_tested'], 1)
