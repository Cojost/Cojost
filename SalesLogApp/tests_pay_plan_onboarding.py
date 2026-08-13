import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from .models import (
    PayPlanDescriptionSubmission,
    PayPlanAssignment,
    PayPlanDocument,
    PayPlanOnboarding,
    PayPlanRule,
    Sale,
    UserProfile,
)
from .pay_plan_imports import parse_description_to_import_draft


TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='stewlog-onboarding-')


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class PayPlanOnboardingTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='onboarding-user',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(self.user)
        self.onboarding = self.user.pay_plan_onboarding
        self.user.sales_profile.commission_system = UserProfile.PAY_PLAN_V2
        self.user.sales_profile.save(update_fields=['commission_system', 'updated_at'])

    def test_incomplete_new_engine_user_is_gated_to_setup(self):
        response = self.client.get(reverse('view_sales'))

        self.assertRedirects(response, reverse('my_pay_plan'))

    def test_legacy_user_cannot_enter_onboarding(self):
        self.user.sales_profile.commission_system = UserProfile.LEGACY
        self.user.sales_profile.save(update_fields=['commission_system', 'updated_at'])

        setup_response = self.client.get(reverse('pay_plan_setup'))
        review_response = self.client.get(reverse('pay_plan_review'))

        self.assertRedirects(setup_response, reverse('view_sales'))
        self.assertRedirects(review_response, reverse('view_sales'))

    def test_description_submission_is_preserved_for_review(self):
        response = self.client.post(reverse('pay_plan_setup'), {
            'setup_method': PayPlanOnboarding.DESCRIBE,
            'description': '25% of front gross with a $250 minimum.',
        })

        self.assertRedirects(response, reverse('pay_plan_review'))
        submission = PayPlanDescriptionSubmission.objects.get(user=self.user)
        self.assertEqual(
            submission.description,
            '25% of front gross with a $250 minimum.',
        )
        self.onboarding.refresh_from_db()
        self.assertEqual(self.onboarding.status, PayPlanOnboarding.SUBMITTED)
        self.assertEqual(
            self.onboarding.questionnaire['description'],
            submission.description,
        )
        self.assertIn('rule_import_draft', self.onboarding.questionnaire)
        self.assertGreaterEqual(
            len(self.onboarding.questionnaire['rule_import_draft'].get('rules', [])),
            1,
        )

    def test_describe_method_requires_description(self):
        response = self.client.post(reverse('pay_plan_setup'), {
            'setup_method': PayPlanOnboarding.DESCRIBE,
            'description': ' ',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Describe your pay plan before continuing.')
        self.assertFalse(
            PayPlanDescriptionSubmission.objects.filter(user=self.user).exists()
        )

    def test_upload_accepts_supported_documents_and_preserves_order(self):
        files = [
            SimpleUploadedFile('page-1.png', b'first-page', 'image/png'),
            SimpleUploadedFile('page-2.pdf', b'%PDF-test', 'application/pdf'),
        ]

        response = self.client.post(reverse('pay_plan_setup'), {
            'setup_method': PayPlanOnboarding.UPLOAD,
            'documents': files,
        })

        self.assertRedirects(response, reverse('pay_plan_review'))
        documents = list(
            PayPlanDocument.objects.filter(user=self.user).order_by('page_order')
        )
        self.assertEqual([document.page_order for document in documents], [1, 2])
        self.assertEqual(
            [document.document_type for document in documents],
            [PayPlanDocument.IMAGE, PayPlanDocument.PDF],
        )
        self.onboarding.refresh_from_db()
        import_draft = self.onboarding.questionnaire.get('rule_import_draft', {})
        self.assertEqual(import_draft.get('source'), 'upload')
        self.assertEqual(import_draft.get('rules'), [])
        self.assertTrue(import_draft.get('warnings'))

    def test_selected_file_overrides_stale_manual_builder_choice(self):
        response = self.client.post(reverse('pay_plan_setup'), {
            'setup_method': PayPlanOnboarding.MANUAL_BUILDER,
            'documents': SimpleUploadedFile(
                'mobile-upload.png', b'image-content', 'image/png',
            ),
        })

        self.assertRedirects(response, reverse('pay_plan_review'))
        self.onboarding.refresh_from_db()
        self.assertEqual(self.onboarding.setup_method, PayPlanOnboarding.UPLOAD)
        self.assertEqual(self.onboarding.status, PayPlanOnboarding.SUBMITTED)
        self.assertTrue(
            PayPlanDocument.objects.filter(
                user=self.user,
                original_filename='mobile-upload.png',
            ).exists()
        )
        self.assertEqual(
            self.onboarding.questionnaire['rule_import_draft']['source'],
            'upload',
        )

    def test_empty_upload_draft_shows_recovery_without_activation_button(self):
        self.client.post(reverse('pay_plan_setup'), {
            'setup_method': PayPlanOnboarding.UPLOAD,
            'documents': SimpleUploadedFile(
                'photo.png', b'image-content', 'image/png',
            ),
        })

        response = self.client.get(reverse('pay_plan_review'))

        self.assertContains(response, 'photo.png')
        self.assertContains(response, 'No usable rules were extracted')
        self.assertContains(response, 'Return to Pay Plan Setup')
        self.assertNotContains(response, 'Activate My Pay Plan')

    def test_view_commission_redirects_incomplete_new_engine_user_to_setup(self):
        response = self.client.get(reverse('view_commission'))

        self.assertRedirects(response, reverse('my_pay_plan'))

    def test_upload_rejects_unsupported_content_type(self):
        response = self.client.post(reverse('pay_plan_setup'), {
            'setup_method': PayPlanOnboarding.UPLOAD,
            'documents': SimpleUploadedFile(
                'pay-plan.txt',
                b'not a supported document',
                'text/plain',
            ),
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'must be a PDF, JPG, PNG, or WEBP file')
        self.assertFalse(PayPlanDocument.objects.filter(user=self.user).exists())

    def test_simplified_subaru_plan_imports_bonus_ladders_and_eligibility(self):
        text = """
        Simplified bonus rules
        Eligibility for Volume, Fast Start, and Spiffs
        Training 100% complete
        Live conversations 40 by mid-month; 80 total
        Unique Co-Videos 50 by mid-month; 100 total
        Green Pea Program
        7-8.5 $500 9-12.5 $1,000 13-16.5 $1,500
        17-20.5 $2,000 21+ $2,500
        All Other Pay Plans
        10-11.5 $500 12-15.5 $750 16-19.5 $2,000
        20-24.5 $2,500 25-29.5 $3,000 30+ $4,000
        Fast Start Bonuses
        Used vehicle qualifier
        """

        draft = parse_description_to_import_draft(text, 'Subaru 2026')

        rules = {rule['name']: rule for rule in draft['rules']}
        self.assertIn('Green Pea Volume Bonus', rules)
        self.assertIn('Standard Volume Bonus', rules)
        self.assertIn('Fast Start - 10 Units by the 10th', rules)
        self.assertIn('Used Vehicle Minimum Deduction', rules)
        self.assertEqual(
            rules['Green Pea Volume Bonus']['configuration']['unit_metric'],
            'fast_start_volume_units',
        )
        green_fields = {
            item['field_name']
            for item in rules['Green Pea Volume Bonus']['conditions']
        }
        self.assertEqual(
            green_fields,
            {
                'green_pea',
                'training_requirements_met',
                'call_requirement_met',
                'video_requirement_met',
            },
        )
        self.assertEqual(draft['confidence'], '0.95')

    def test_automotive_volume_table_is_not_confused_with_minimum_tiers(self):
        text = """
        Minimum Commission (New Only)
        1-4.5 units: $100 minimum commission.
        5+ units: $200 minimum commission.
        Volume Bonus - New Vehicles
        Units Bonus
        5 $500
        7 $750
        8 $1,000
        10 $1,250
        12 $1,500
        14 $1,750
        15+ $2,500
        Qualification: Customer Satisfaction Score must be higher than the
        district to receive the volume bonus.
        Additional Bonuses and Draw
        Salesman of the Month: $250.
        Monthly draw: $2,000 per month, paid semi-monthly.
        """

        draft = parse_description_to_import_draft(text, 'Automotive Plan')
        volume = next(
            rule for rule in draft['rules']
            if rule['rule_type'] == 'volume_bonus'
        )
        draw = next(
            rule for rule in draft['rules']
            if rule['rule_type'] == 'draw'
        )

        self.assertEqual(volume['configuration']['unit_metric'], 'monthly_new_units')
        self.assertEqual(
            volume['configuration']['tiers'],
            [
                {'minimum_units': '5', 'amount': '500.00'},
                {'minimum_units': '7', 'amount': '750.00'},
                {'minimum_units': '8', 'amount': '1000.00'},
                {'minimum_units': '10', 'amount': '1250.00'},
                {'minimum_units': '12', 'amount': '1500.00'},
                {'minimum_units': '14', 'amount': '1750.00'},
                {'minimum_units': '15', 'amount': '2500.00'},
            ],
        )
        self.assertEqual(volume['conditions'], [{
            'field_name': 'nps_bonus_eligible',
            'operator': 'is_true',
            'value': True,
        }])
        self.assertEqual(draw['configuration']['amount'], '2000.00')

    def test_valid_current_version_can_be_activated(self):
        PayPlanAssignment.objects.filter(user=self.user).delete()
        PayPlanRule.objects.create(
            pay_plan_version=self.onboarding.current_version,
            name='Front 5% Rule',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.05', 'gross_field': 'front_end_gross'},
            is_active=True,
            sort_order=1,
        )

        response = self.client.post(reverse('pay_plan_review'), {
            'action': 'activate',
        })

        self.assertRedirects(response, reverse('view_sales'))
        self.onboarding.refresh_from_db()
        self.assertEqual(self.onboarding.status, PayPlanOnboarding.ACTIVE)
        self.assertIsNotNone(self.onboarding.completed_at)
        self.assertTrue(
            PayPlanAssignment.objects.filter(
                user=self.user,
                pay_plan_version=self.onboarding.current_version,
                is_active=True,
            ).exists()
        )

    def test_approve_import_creates_rules_and_marks_ready_to_activate(self):
        response = self.client.post(reverse('pay_plan_setup'), {
            'setup_method': PayPlanOnboarding.DESCRIBE,
            'description': (
                'I earn 25% of front gross with a $250 minimum. '
                'I earn 5% of back-end gross. At 10 units I receive $500.'
            ),
        })
        self.assertRedirects(response, reverse('pay_plan_review'))

        review_response = self.client.post(reverse('pay_plan_review'), {
            'action': 'approve_import',
        })

        self.assertRedirects(review_response, reverse('pay_plan_review'))
        self.onboarding.refresh_from_db()
        self.assertEqual(self.onboarding.status, PayPlanOnboarding.READY_TO_ACTIVATE)
        self.assertTrue(self.onboarding.questionnaire['rule_import_draft']['approved'])
        self.assertGreater(
            PayPlanRule.objects.filter(pay_plan_version=self.onboarding.current_version).count(),
            0,
        )

    def test_active_onboarding_recovers_missing_assignment_for_view_sales(self):
        PayPlanAssignment.objects.filter(user=self.user).delete()
        self.onboarding.status = PayPlanOnboarding.ACTIVE
        self.onboarding.completed_at = timezone.now()
        self.onboarding.save(update_fields=['status', 'completed_at', 'updated_at'])
        Sale.objects.create(
            user=self.user,
            customer='Recovery Buyer',
            dealNumber=5001,
            count='1.0',
            frontEnd='1000.00',
            backend='250.00',
            date=timezone.localdate(),
        )

        response = self.client.get(reverse('view_sales'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            PayPlanAssignment.objects.filter(
                user=self.user,
                pay_plan_version=self.onboarding.current_version,
                is_active=True,
            ).exists()
        )

    def test_active_onboarding_backdates_assignment_to_earliest_sale_for_view_sales(self):
        assignment = PayPlanAssignment.objects.get(user=self.user)
        assignment.effective_start_date = timezone.localdate()
        assignment.save(update_fields=['effective_start_date', 'updated_at'])
        self.onboarding.current_version.effective_start_date = timezone.localdate()
        self.onboarding.current_version.save(update_fields=['effective_start_date', 'updated_at'])
        self.onboarding.status = PayPlanOnboarding.ACTIVE
        self.onboarding.completed_at = timezone.now()
        self.onboarding.save(update_fields=['status', 'completed_at', 'updated_at'])
        Sale.objects.create(
            user=self.user,
            customer='Early Buyer',
            dealNumber=5002,
            count='1.0',
            frontEnd='1500.00',
            backend='300.00',
            date=timezone.localdate().replace(day=1),
        )

        response = self.client.get(reverse('view_sales'))

        self.assertEqual(response.status_code, 200)
        assignment.refresh_from_db()
        self.onboarding.current_version.refresh_from_db()
        self.assertEqual(assignment.effective_start_date, timezone.localdate().replace(day=1))
        self.assertEqual(self.onboarding.current_version.effective_start_date, timezone.localdate().replace(day=1))
