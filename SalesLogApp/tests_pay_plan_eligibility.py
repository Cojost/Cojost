from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .commission_service import CommissionEngineService
from .models import (
    PayPlanEligibility,
    PayPlanRule,
    PayPlanRuleCondition,
    Sale,
    UserProfile,
)


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
        response = self.client.post(reverse('pay_plan_eligibility'), {
            'month_start': '2026-07',
            'green_pea': 'true',
            'nps_status': PayPlanEligibility.NPS_EXEMPT,
            'training_requirements_met': 'true',
            'call_requirement_met': 'false',
            'video_requirement_met': '',
            'notes': 'NPS exemption during survey grace period.',
        })
        self.assertEqual(response.status_code, 302)
        eligibility = PayPlanEligibility.objects.get(user=self.user)
        self.assertEqual(eligibility.month_start, date(2026, 7, 1))
        self.assertTrue(eligibility.green_pea)
        self.assertTrue(eligibility.nps_finance_eligible)
        self.assertIsNone(eligibility.video_requirement_met)

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
