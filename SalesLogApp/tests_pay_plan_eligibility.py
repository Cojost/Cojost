from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .commission_service import CommissionEngineService
from .eligibility_forms import PayPlanEligibilityForm
from .monthly_eligibility import ensure_current_month_eligibility
from .models import (
    PayPlanEligibility,
    PayPlanRule,
    PayPlanRuleCondition,
    Sale,
    UserProfile,
)
from .plan_requirements import PlanRequirementService


class MonthlyPayPlanEligibilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='eligibility-owner', password='test-password',
        )
        self.other = get_user_model().objects.create_user(
            username='eligibility-other', password='test-password',
        )
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        assignment = self.user.pay_plan_assignments.get()
        self.version = assignment.pay_plan_version
        self.version.effective_start_date = date(2026, 7, 1)
        self.version.save(update_fields=['effective_start_date', 'updated_at'])
        assignment.effective_start_date = date(2026, 7, 1)
        assignment.save(update_fields=['effective_start_date', 'updated_at'])
        onboarding = self.user.pay_plan_onboarding
        onboarding.current_pay_plan = self.version.pay_plan
        onboarding.current_version = self.version
        onboarding.status = onboarding.ACTIVE
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='25% Front',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.25', 'gross_field': 'front_end_gross'},
            sort_order=1,
        )
        back_rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='3% Finance if NPS Eligible',
            rule_type='back_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.03', 'gross_field': 'back_end_gross'},
            sort_order=2,
        )
        PayPlanRuleCondition.objects.create(
            rule=back_rule,
            field_name='nps_finance_eligible',
            operator='is_true',
            value=True,
        )
        green_rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Green Pea Flat',
            rule_type='flat_per_deal',
            calculation_scope='per_sale',
            configuration={'amount': '100.00'},
            sort_order=3,
        )
        PayPlanRuleCondition.objects.create(
            rule=green_rule,
            field_name='green_pea',
            operator='is_true',
            value=True,
        )
        for order, (name, field_name) in enumerate((
            ('Training Requirement', 'training_requirements_met'),
            ('Call Requirement', 'call_requirement_met'),
            ('Video Requirement', 'video_requirement_met'),
        ), start=4):
            requirement_rule = PayPlanRule.objects.create(
                pay_plan_version=self.version,
                name=name,
                rule_type='flat_per_deal',
                calculation_scope='per_sale',
                configuration={'amount': '1.00'},
                sort_order=order,
            )
            PayPlanRuleCondition.objects.create(
                rule=requirement_rule,
                field_name=field_name,
                operator='is_true',
                value=True,
            )
        self.sale = Sale.objects.create(
            user=self.user, customer='Buyer', dealNumber=990001,
            count=Decimal('1.0'), frontEnd=Decimal('1200.00'),
            backend=Decimal('2500.00'), date=date(2026, 7, 10),
        )
        self.client.login(username=self.user.username, password='test-password')

    def save_eligibility(self, **overrides):
        values = {
            'user': self.user,
            'month_start': date(2026, 7, 1),
            'green_pea': False,
            'nps_status': PayPlanEligibility.NPS_PENDING,
            'training_requirements_met': None,
            'call_requirement_met': None,
            'video_requirement_met': None,
        }
        values.update(overrides)
        return PayPlanEligibility.objects.create(**values)

    def test_pending_nps_does_not_assume_finance_eligibility(self):
        self.save_eligibility()
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.total_commission, Decimal('300.00'))
        self.assertNotIn('3% Finance if NPS Eligible', result.matched_rules)

    def test_eligible_and_exempt_nps_enable_finance_rule(self):
        eligibility = self.save_eligibility(
            nps_status=PayPlanEligibility.NPS_ELIGIBLE,
        )
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.total_commission, Decimal('375.00'))
        eligibility.nps_status = PayPlanEligibility.NPS_EXEMPT
        eligibility.save()
        exempt_result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(exempt_result.total_commission, Decimal('375.00'))

    def test_green_pea_switch_enables_only_conditioned_rule(self):
        eligibility = self.save_eligibility(green_pea=False)
        regular = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(regular.total_commission, Decimal('300.00'))
        eligibility.green_pea = True
        eligibility.save()
        green = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(green.total_commission, Decimal('400.00'))
        self.assertIn('Green Pea Flat', green.matched_rules)

    def test_eligibility_is_effective_dated_by_month(self):
        self.save_eligibility(nps_status=PayPlanEligibility.NPS_ELIGIBLE)
        PayPlanEligibility.objects.create(
            user=self.user, month_start=date(2026, 8, 1),
            nps_status=PayPlanEligibility.NPS_INELIGIBLE,
        )
        august_sale = Sale.objects.create(
            user=self.user, customer='August Buyer', dealNumber=990002,
            count=Decimal('1.0'), frontEnd=Decimal('1200.00'),
            backend=Decimal('2500.00'), date=date(2026, 8, 10),
        )
        july = CommissionEngineService.calculate_sale(self.user, self.sale)
        august = CommissionEngineService.calculate_sale(self.user, august_sale)
        self.assertEqual(july.total_commission, Decimal('375.00'))
        self.assertEqual(august.total_commission, Decimal('300.00'))

    def test_user_can_save_monthly_eligibility(self):
        current_month = timezone.localdate().replace(day=1)
        response = self.client.post(reverse('pay_plan_eligibility'), {
            'month_start': current_month.strftime('%Y-%m'),
            'green_pea': 'true',
            'nps_status': PayPlanEligibility.NPS_EXEMPT,
            'training_requirements_met': 'true',
            'call_requirement_met': 'false',
            'video_requirement_met': '',
            'notes': 'NPS exemption during survey grace period.',
        })
        self.assertEqual(response.status_code, 302)
        eligibility = PayPlanEligibility.objects.get(
            user=self.user,
            month_start=current_month,
        )
        self.assertEqual(eligibility.month_start, current_month)
        self.assertTrue(eligibility.green_pea)
        self.assertTrue(eligibility.nps_finance_eligible)
        self.assertIsNone(eligibility.video_requirement_met)

    def test_current_month_defaults_only_active_requirements_to_eligible(self):
        current_month = timezone.localdate().replace(day=1)

        response = self.client.get(reverse('pay_plan_eligibility'))

        self.assertEqual(response.status_code, 200)
        eligibility = PayPlanEligibility.objects.get(
            user=self.user,
            month_start=current_month,
        )
        self.assertEqual(
            eligibility.nps_status,
            PayPlanEligibility.NPS_ELIGIBLE,
        )
        self.assertTrue(eligibility.green_pea)
        self.assertTrue(eligibility.training_requirements_met)
        self.assertTrue(eligibility.call_requirement_met)
        self.assertTrue(eligibility.video_requirement_met)
        self.assertIsNone(eligibility.ar_requirement_met)
        self.assertIsNone(eligibility.holiday_bonus_eligible)
        self.assertEqual(eligibility.updated_by, self.user)
        self.assertContains(response, 'starts Eligible')

    def test_current_month_default_never_overwrites_saved_choices(self):
        current_month = timezone.localdate().replace(day=1)
        eligibility = PayPlanEligibility.objects.create(
            user=self.user,
            month_start=current_month,
            nps_status=PayPlanEligibility.NPS_INELIGIBLE,
            green_pea=False,
            training_requirements_met=False,
        )

        resolved, created = ensure_current_month_eligibility(
            self.user,
            current_month,
        )

        eligibility.refresh_from_db()
        self.assertFalse(created)
        self.assertEqual(resolved, eligibility)
        self.assertEqual(
            eligibility.nps_status,
            PayPlanEligibility.NPS_INELIGIBLE,
        )
        self.assertFalse(eligibility.green_pea)
        self.assertFalse(eligibility.training_requirements_met)

    def test_historical_month_is_read_only_and_never_auto_created(self):
        current_month = timezone.localdate().replace(day=1)
        historical_month = (current_month - timedelta(days=1)).replace(day=1)
        historical = PayPlanEligibility.objects.create(
            user=self.user,
            month_start=historical_month,
            nps_status=PayPlanEligibility.NPS_INELIGIBLE,
            green_pea=False,
        )

        history_page = self.client.get(
            f"{reverse('pay_plan_eligibility')}?month={historical_month:%Y-%m}"
        )
        self.assertContains(history_page, 'Historical record')
        self.assertNotContains(history_page, 'Save Monthly Eligibility')
        self.assertContains(history_page, 'disabled', count=7)

        response = self.client.post(reverse('pay_plan_eligibility'), {
            'month_start': historical_month.strftime('%Y-%m'),
            'green_pea': 'true',
            'nps_status': PayPlanEligibility.NPS_ELIGIBLE,
            'training_requirements_met': 'true',
            'call_requirement_met': 'true',
            'video_requirement_met': 'true',
            'notes': 'Attempted history rewrite.',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Only the current month can be updated')
        historical.refresh_from_db()
        self.assertEqual(
            historical.nps_status,
            PayPlanEligibility.NPS_INELIGIBLE,
        )
        self.assertFalse(historical.green_pea)
        missing_month = (historical_month - timedelta(days=1)).replace(day=1)
        resolved, created = ensure_current_month_eligibility(
            self.user,
            missing_month,
            today=current_month,
        )
        self.assertIsNone(resolved)
        self.assertFalse(created)
        self.assertFalse(PayPlanEligibility.objects.filter(
            user=self.user,
            month_start=missing_month,
        ).exists())

    def test_month_rollover_creates_a_new_record_without_changing_history(self):
        current_month = timezone.localdate().replace(day=1)
        next_month = (current_month + timedelta(days=32)).replace(day=1)
        current, current_created = ensure_current_month_eligibility(
            self.user,
            current_month,
            today=current_month,
        )
        current.nps_status = PayPlanEligibility.NPS_INELIGIBLE
        current.save(update_fields=['nps_status', 'updated_at'])

        following, following_created = ensure_current_month_eligibility(
            self.user,
            next_month,
            today=next_month,
        )

        current.refresh_from_db()
        self.assertTrue(current_created)
        self.assertTrue(following_created)
        self.assertEqual(
            current.nps_status,
            PayPlanEligibility.NPS_INELIGIBLE,
        )
        self.assertEqual(
            following.nps_status,
            PayPlanEligibility.NPS_ELIGIBLE,
        )
        self.assertEqual(
            PayPlanEligibility.objects.filter(user=self.user).count(),
            2,
        )

    def test_default_creation_is_strictly_owner_scoped(self):
        current_month = timezone.localdate().replace(day=1)

        ensure_current_month_eligibility(self.user, current_month)

        self.assertTrue(PayPlanEligibility.objects.filter(
            user=self.user,
            month_start=current_month,
        ).exists())
        self.assertFalse(PayPlanEligibility.objects.filter(
            user=self.other,
            month_start=current_month,
        ).exists())

    def test_nps_bonus_only_plan_still_exposes_adjustable_eligibility(self):
        self.version.rules.all().delete()
        rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Survey count bonus',
            rule_type='survey_count_bonus',
            calculation_scope='period',
            configuration={'grid': []},
        )
        PayPlanRuleCondition.objects.create(
            rule=rule,
            field_name='nps_bonus_eligible',
            operator='is_true',
            value=True,
        )

        requirements = PlanRequirementService.get_for_user(self.user)
        form = PayPlanEligibilityForm(enabled_requirements=['nps_bonus'])
        response = self.client.get(reverse('pay_plan_eligibility'))

        self.assertTrue(requirements['has_monthly_requirements'])
        self.assertIn('nps_status', form.fields)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'NPS survey bonus')

    def test_page_does_not_show_another_users_history(self):
        PayPlanEligibility.objects.create(
            user=self.other, month_start=date(2026, 7, 1),
            notes='PRIVATE OTHER USER NOTE',
        )
        response = self.client.get(reverse('pay_plan_eligibility'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'PRIVATE OTHER USER NOTE')

    def test_eligibility_page_requires_authentication(self):
        self.client.logout()
        response = self.client.get(reverse('pay_plan_eligibility'))
        self.assertEqual(response.status_code, 302)
