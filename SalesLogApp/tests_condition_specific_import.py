from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from .commission_engine import calculate_period_commission
from .pay_plan_imports import (
    apply_import_draft_to_version,
    parse_description_to_import_draft,
)
from .models.sales import Sale


PLAN_TEXT = """
Automotive Sales Pay Plan
New Vehicles Commission: 18% of vehicle gross profit (no soft pack and paid on holdback).
5% of vehicle F&I gross after a $150 pack.
Minimum Commission (New Only) 1-4.5 units: $100; 5+ units: $200.
Additional $150 paid for all demos over 4,000 miles.
Volume Bonus - New Vehicles Units Bonus 5 $500 7 $750 8 $1,000.
Pre-Owned Vehicles have a $300 soft pack.
Monthly Pre-Owned Units Front-End Commission Rate
1-4.5 25%
5-9.5 30%
10-14.5 35%
15+ 40%
Important: These percentages are NOT retroactive.
5% F&I after $150 pack.
"""


class ConditionSpecificAutomotiveImportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='michael-generic-test', password='test',
        )
        self.assignment = self.user.pay_plan_assignments.select_related(
            'pay_plan_version'
        ).get()
        self.version = self.assignment.pay_plan_version
        self.version.rules.all().delete()
        self.draft = parse_description_to_import_draft(PLAN_TEXT, 'Uploaded plan')
        result = apply_import_draft_to_version(self.version, self.draft)
        self.assertEqual(result['rejected_rules'], [])
        self.start = self.assignment.effective_start_date

    def sale(self, number, condition, front='2000', back='1000', count='1', days=0):
        return Sale.objects.create(
            user=self.user, customer=f'Customer {number}', dealNumber=number,
            count=Decimal(count), split_with_name='', frontEnd=Decimal(front),
            backend=Decimal(back), date=self.start + timedelta(days=days),
            vehicle_condition=condition,
        )

    def test_import_compiles_executable_condition_specific_rules(self):
        rules = {rule.rule_type: rule for rule in self.version.rules.all()}
        self.assertEqual(rules['front_gross_percentage'].configuration['rate'], '0.18')
        progressive = rules['progressive_unit_position_percentage']
        self.assertEqual(progressive.configuration['pack_amount'], '300.00')
        self.assertTrue(progressive.configuration['non_retroactive'])
        self.assertEqual(len(progressive.configuration['tiers']), 4)
        backend = rules['back_gross_percentage']
        self.assertEqual(backend.configuration['pack_amount'], '150.00')

    def test_new_front_backend_and_low_unit_minimum(self):
        sale = self.sale(81001, 'New', front='2000', back='1000')
        result = calculate_period_commission(
            self.user, [sale], self.start, self.start,
        ).sale_results[0]
        self.assertEqual(result.base_commission, Decimal('402.50'))
        self.assertEqual(result.total, Decimal('402.50'))

    def test_new_minimum_uses_only_monthly_new_units(self):
        new_sales = [
            self.sale(81100 + i, 'new', front='100', back='0', days=i)
            for i in range(5)
        ]
        used = self.sale(81200, 'used', front='300', back='0')
        result = calculate_period_commission(
            self.user, new_sales + [used], self.start, self.start + timedelta(days=5),
        )
        new_results = {item.sale.id: item for item in result.sale_results}
        self.assertEqual(new_results[new_sales[0].id].base_commission, Decimal('200.00'))

    def test_used_tiers_are_non_retroactive_and_pack_applies(self):
        sales = [
            self.sale(81300 + i, 'pre-owned', front='2000', back='0', days=i)
            for i in range(5)
        ]
        result = calculate_period_commission(
            self.user, sales, self.start, self.start + timedelta(days=4),
        )
        amounts = [item.base_commission for item in result.sale_results]
        self.assertEqual(amounts[:4], [Decimal('425.00')] * 4)
        self.assertEqual(amounts[4], Decimal('510.00'))

    def test_missing_condition_does_not_select_new_or_used(self):
        sale = self.sale(81400, '', front='2000', back='0')
        result = calculate_period_commission(
            self.user, [sale], self.start, self.start,
        ).sale_results[0]
        self.assertEqual(result.base_commission, Decimal('0.00'))
        self.assertTrue(any('missing_vehicle_condition' in item for item in result.warnings))

    def test_other_user_sales_do_not_affect_used_position(self):
        other = get_user_model().objects.create_user(username='other-user', password='test')
        Sale.objects.create(
            user=other, customer='Other', dealNumber=81500, count=Decimal('2'),
            split_with_name='', frontEnd=2000, backend=0, date=self.start,
            vehicle_condition='used',
        )
        sale = self.sale(81501, 'used', front='2000', back='0')
        result = calculate_period_commission(
            self.user, [sale], self.start, self.start,
        ).sale_results[0]
        self.assertEqual(result.base_commission, Decimal('425.00'))
