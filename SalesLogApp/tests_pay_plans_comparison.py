from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from .commission_engine import compare_sale_commission, compare_period_commission
from .models import Industry, PayPlan, PayPlanAssignment, PayPlanVersion
from .models.sales import Sale


class PayPlanComparisonTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='comparison-user',
            password='test-password',
        )
        self.assignment = self.user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan'
        ).first()
        if self.assignment is None:
            self.sale_date = date.today()
            self.industry, _ = Industry.objects.get_or_create(
                slug='automotive',
                defaults={'name': 'Automotive', 'is_active': True},
            )
            self.pay_plan, _ = PayPlan.objects.get_or_create(
                owner_user=self.user,
                industry=self.industry,
                name='Legacy Automotive Pay Plan',
                defaults={
                    'description': (
                        'Compatibility plan created from the existing automotive '
                        'commission foundation. Rules will be migrated in a later stage.'
                    ),
                    'is_template': False,
                    'is_active': True,
                },
            )
            self.version, _ = PayPlanVersion.objects.get_or_create(
                pay_plan=self.pay_plan,
                version_name='Imported Legacy Settings',
                defaults={
                    'effective_start_date': self.sale_date,
                    'status': PayPlanVersion.ACTIVE,
                },
            )
            self.assignment, _ = PayPlanAssignment.objects.get_or_create(
                user=self.user,
                defaults={
                    'pay_plan_version': self.version,
                    'effective_start_date': self.sale_date,
                    'is_active': True,
                },
            )
        else:
            self.sale_date = self.assignment.effective_start_date
            self.version = self.assignment.pay_plan_version
            self.pay_plan = self.version.pay_plan
            self.industry = self.pay_plan.industry
        self.sale = Sale.objects.create(
            user=self.user,
            customer='Comparison Test',
            dealNumber=1234,
            count=Decimal('1.0'),
            split_with_name='',
            frontEnd=Decimal('2000.00'),
            backend=Decimal('500.00'),
            date=self.sale_date,
        )

    def test_compare_sale_commission_returns_legacy_and_engine_totals(self):
        comparison = compare_sale_commission(self.user, self.sale)
        self.assertEqual(comparison.legacy_totals['front_end'], self.sale.calculate_frontEnd)
        self.assertEqual(comparison.legacy_totals['back_end'], self.sale.calculate_backend)
        self.assertEqual(comparison.engine_result.total, comparison.sale_comparisons[0]['engine_total'])
        self.assertFalse(comparison.sale_comparisons[0]['mismatches']['total'])

    def test_compare_period_commission_returns_period_mismatch_report(self):
        comparison = compare_period_commission(self.user, [self.sale], period_start=self.sale.date, period_end=self.sale.date)
        expected_total = self.sale.calculate_frontEnd + self.sale.calculate_backend
        self.assertEqual(comparison.legacy_totals['total'], expected_total)
        self.assertEqual(comparison.engine_result.base_commission, comparison.engine_result.base_commission)
        self.assertFalse(comparison.mismatches['total'])
        self.assertEqual(len(comparison.sale_comparisons), 1)
        self.assertEqual(comparison.sale_comparisons[0]['sale_id'], self.sale.id)
