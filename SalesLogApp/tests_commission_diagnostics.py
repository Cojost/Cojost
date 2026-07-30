from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .commission_service import (
    CommissionEngineService,
    STATUS_CALCULATED,
    STATUS_CONFIGURATION_ERROR,
    STATUS_MISSING_PLAN,
    STATUS_NO_MATCHING_RULE,
)
from .models import Commission, PayPlanOnboarding, PayPlanRule, PayPlanRuleCondition, UserProfile
from .models.sales import Sale


class CommissionDiagnosticsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='diag-user',
            password='test-password',
        )
        self.user.sales_profile.commission_system = UserProfile.PAY_PLAN_V2
        self.user.sales_profile.save(update_fields=['commission_system', 'updated_at'])
        self.onboarding = self.user.pay_plan_onboarding
        self.onboarding.status = PayPlanOnboarding.ACTIVE
        self.onboarding.save(update_fields=['status', 'updated_at'])
        self.assignment = self.user.pay_plan_assignments.select_related('pay_plan_version').get()
        self.version = self.assignment.pay_plan_version
        self.sale = Sale.objects.create(
            user=self.user,
            customer='Diagnostic Buyer',
            dealNumber=91001,
            count=Decimal('1.0'),
            frontEnd=Decimal('2500.00'),
            backend=Decimal('1000.00'),
            date=self.assignment.effective_start_date,
        )

    def test_pay_plan_user_with_legacy_record_uses_only_pay_plan_engine(self):
        Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.50'),
            total_calculated_back_end=Decimal('0.50'),
        )

        result = CommissionEngineService.calculate_sale(self.user, self.sale)

        self.assertEqual(result.engine, UserProfile.PAY_PLAN_V2)
        self.assertEqual(result.status, STATUS_CONFIGURATION_ERROR)
        self.assertIn(
            'no default backend calculation is configured',
            ' '.join(result.errors),
        )

    def test_missing_plan_returns_missing_plan_status(self):
        self.assignment.effective_start_date = self.sale.date + timedelta(days=10)
        self.assignment.save(update_fields=['effective_start_date', 'updated_at'])

        result = CommissionEngineService.calculate_sale(self.user, self.sale)

        self.assertEqual(result.status, STATUS_MISSING_PLAN)

    def test_no_matching_rule_returns_explicit_status(self):
        rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front 5% Rule Used Only',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.05', 'gross_field': 'front_end_gross'},
            is_active=True,
            sort_order=1,
        )
        PayPlanRuleCondition.objects.create(
            rule=rule,
            field_name='vehicle_condition',
            operator='equals',
            value='used',
            sort_order=1,
        )

        result = CommissionEngineService.calculate_sale(self.user, self.sale)

        self.assertEqual(result.status, STATUS_CONFIGURATION_ERROR)
        self.assertEqual(result.total_commission, Decimal('0.00'))

    def test_zero_dollar_valid_rule_returns_calculated(self):
        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front 5% Rule',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.05', 'gross_field': 'front_end_gross'},
            is_active=True,
            sort_order=1,
        )
        zero_sale = Sale.objects.create(
            user=self.user,
            customer='Zero Buyer',
            dealNumber=91002,
            count=Decimal('1.0'),
            frontEnd=Decimal('0.00'),
            backend=Decimal('0.00'),
            date=self.assignment.effective_start_date,
        )

        result = CommissionEngineService.calculate_sale(self.user, zero_sale)

        self.assertEqual(result.status, STATUS_CALCULATED)
        self.assertEqual(result.total_commission, Decimal('0.00'))


class InspectCommissionUserCommandTests(TestCase):
    def test_command_reports_case_insensitive_username(self):
        user = get_user_model().objects.create_user(
            username='ItsumiTest',
            email='itsumi-test@example.com',
            password='pw',
        )
        user.sales_profile.commission_system = UserProfile.PAY_PLAN_V2
        user.sales_profile.save(update_fields=['commission_system', 'updated_at'])
        onboarding = user.pay_plan_onboarding
        onboarding.status = PayPlanOnboarding.ACTIVE
        onboarding.save(update_fields=['status', 'updated_at'])

        output = StringIO()
        call_command('inspect_commission_user', 'itsumitest', stdout=output)

        text = output.getvalue()
        self.assertIn('Username: ItsumiTest', text)
        self.assertIn('Commission engine: pay_plan_v2', text)
