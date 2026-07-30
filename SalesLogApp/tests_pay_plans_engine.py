from datetime import date
from datetime import timedelta
from types import SimpleNamespace

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import PayPlan, PayPlanRule, PayPlanRuleCondition, PayPlanVersion
from .commission_engine import calculate_sale_commission, calculate_period_commission, resolve_pay_plan_version
from .commission_engine.engine import build_period_context
from .models.sales import Sale


class PayPlanRuleEngineTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='rule-engine-user',
            password='test-password',
        )
        self.assignment = self.user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan'
        ).get()
        self.version = self.assignment.pay_plan_version
        self.pay_plan = self.version.pay_plan
        self.sale_date = self.assignment.effective_start_date

        self.sale = Sale.objects.create(
            user=self.user,
            customer='Test Customer',
            dealNumber=123,
            count=Decimal('1.0'),
            split_with_name='',
            frontEnd=Decimal('2000.00'),
            backend=Decimal('500.00'),
            date=self.sale_date,
        )

    def test_resolve_pay_plan_version_returns_active_version(self):
        resolved = resolve_pay_plan_version(self.user, self.sale_date)
        self.assertEqual(resolved, self.version)

    def test_calculate_sale_commission_with_front_gross_percentage_rule(self):
        rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front 5% Rule',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.05', 'gross_field': 'front_end_gross'},
            is_active=True,
            sort_order=1,
        )
        PayPlanRuleCondition.objects.create(
            rule=rule,
            field_name='deal_type',
            operator='equals',
            value='automotive',
            sort_order=1,
        )

        result = calculate_sale_commission(self.user, self.sale)
        self.assertEqual(result.base_commission, Decimal('100.00'))
        self.assertEqual(result.total, Decimal('100.00'))
        self.assertEqual(len(result.line_items), 1)
        self.assertTrue(result.line_items[0].applied)

    def test_calculate_period_commission_includes_period_bonus(self):
        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front 5% Rule',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.05', 'gross_field': 'front_end_gross'},
            is_active=True,
            sort_order=1,
        )
        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Volume Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [
                    {'minimum_units': '1', 'amount': '50.00'},
                ],
                'tier_mode': 'highest_only',
            },
            is_active=True,
            sort_order=2,
        )

        period_result = calculate_period_commission(
            self.user,
            [self.sale],
            period_start=self.sale_date,
            period_end=self.sale_date,
        )

        self.assertEqual(period_result.base_commission, Decimal('100.00'))
        self.assertEqual(period_result.bonuses, Decimal('50.00'))
        self.assertEqual(period_result.total, Decimal('150.00'))
        self.assertEqual(len(period_result.sale_results), 1)

    def test_half_deal_halves_deal_commission_but_not_period_bonus(self):
        self.sale.count = Decimal('0.5')
        self.sale.save(update_fields=['count'])
        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front 10% Rule',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={
                'rate': '0.10', 'gross_field': 'front_end_gross',
            },
            is_active=True,
            sort_order=1,
        )
        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Half Unit Period Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [{
                    'minimum_units': '0.5', 'amount': '100.00',
                }],
                'tier_mode': 'highest_only',
            },
            is_active=True,
            sort_order=2,
        )

        result = calculate_period_commission(
            self.user, [self.sale],
            period_start=self.sale_date, period_end=self.sale_date,
        )

        self.assertEqual(result.sale_results[0].total, Decimal('100.00'))
        self.assertEqual(result.bonuses, Decimal('100.00'))
        self.assertEqual(result.total, Decimal('200.00'))

    def test_fast_start_metrics_double_first_seven_non_sunday_days(self):
        sales = [
            SimpleNamespace(
                date=date(2026, 7, 2), unit_credit=Decimal('3'),
                frontEnd=0, backend=0, vehicle_condition='new',
            ),
            SimpleNamespace(
                date=date(2026, 7, 9), unit_credit=Decimal('4'),
                frontEnd=0, backend=0, vehicle_condition='used',
            ),
        ]

        metrics = build_period_context(sales)

        self.assertEqual(metrics['monthly_units'], Decimal('7'))
        self.assertEqual(metrics['fast_start_volume_units'], Decimal('10'))
        self.assertEqual(metrics['units_by_day_10'], Decimal('7'))

    def test_calculate_period_commission_uses_assignment_that_starts_mid_month(self):
        period_start = self.sale_date.replace(day=1)
        if period_start == self.sale_date:
            self.assignment.effective_start_date = self.sale_date + timedelta(days=1)
            self.assignment.save(update_fields=['effective_start_date', 'updated_at'])
            self.version.effective_start_date = self.assignment.effective_start_date
            self.version.save(update_fields=['effective_start_date', 'updated_at'])
            self.sale.date = self.assignment.effective_start_date
            self.sale.save(update_fields=['date'])

        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front 5% Rule',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.05', 'gross_field': 'front_end_gross'},
            is_active=True,
            sort_order=1,
        )

        period_result = calculate_period_commission(
            self.user,
            [self.sale],
            period_start=self.sale.date.replace(day=1),
            period_end=self.sale.date,
        )

        self.assertEqual(period_result.pay_plan_version, self.version)
        self.assertEqual(period_result.total, Decimal('100.00'))
