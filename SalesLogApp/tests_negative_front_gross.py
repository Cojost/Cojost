from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .commission_engine import calculate_sale_commission
from .commission_service import CommissionEngineService
from .forms import SaleForm
from .models import (
    Commission,
    PayPlanRule,
    Sale,
    VehicleMake,
    VehicleModel,
)


class NegativeFrontGrossTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            'negative-gross-owner', password='password',
        )
        Commission.objects.create(user=self.user)
        profile = self.user.sales_profile
        profile.commission_system = profile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        self.assignment = self.user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan',
        ).get()
        self.version = self.assignment.pay_plan_version
        onboarding = self.user.pay_plan_onboarding
        onboarding.current_pay_plan = self.version.pay_plan
        onboarding.current_version = self.version
        onboarding.status = onboarding.ACTIVE
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        self.version.rules.all().delete()
        self.sale_date = self.assignment.effective_start_date
        self.make = VehicleMake.objects.create(name='Subaru', verified=True)
        self.model = VehicleModel.objects.create(
            make=self.make, name='Outback', verified=True,
        )
        self.client.force_login(self.user)

    def sale_data(self, *, deal_number, front_end):
        return {
            'customer': 'Negative Gross Customer',
            'date': self.sale_date.isoformat(),
            'frontEnd': front_end,
            'backend': '0.00',
            'dealNumber': str(deal_number),
            'count': '1',
            'split_with_name': '',
            'year': str(timezone.localdate().year),
            'make': self.make.name,
            'make_id': str(self.make.pk),
            'model': self.model.name,
            'model_id': str(self.model.pk),
            'mileage': '12000',
            'stock_number': 'NEG-1',
            'vin': '',
        }

    def make_sale(self, *, deal_number=91002, front_end='1000.00'):
        return Sale.objects.create(
            user=self.user,
            customer='Existing Customer',
            dealNumber=deal_number,
            count=Decimal('1.0'),
            frontEnd=Decimal(front_end),
            backend=Decimal('0.00'),
            date=self.sale_date,
        )

    def add_percentage_rule(self):
        return PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front 10%',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={
                'rate': '0.10',
                'gross_field': 'front_end_gross',
            },
            sort_order=1,
        )

    def add_minimum_rule(self, amount='200.00'):
        return PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front Minimum',
            rule_type='minimum_commission',
            calculation_scope='per_sale',
            configuration={
                'minimum_amount': amount,
                'applies_to_categories': ['front_end'],
            },
            sort_order=2,
        )

    def test_form_accepts_signed_front_gross_and_rejects_malformed_value(self):
        for value in ('-1', '-250', '-250.50', '0', '250.50'):
            with self.subTest(value=value):
                form = SaleForm({
                    'customer': 'Customer',
                    'date': self.sale_date,
                    'frontEnd': value,
                    'backend': '0',
                    'dealNumber': '91000',
                    'count': '1',
                    'split_with_name': '',
                })
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data['frontEnd'], Decimal(value))

        malformed = SaleForm({
            'customer': 'Customer',
            'date': self.sale_date,
            'frontEnd': '--250',
            'backend': '0',
            'dealNumber': '91000',
            'count': '1',
            'split_with_name': '',
        })
        self.assertFalse(malformed.is_valid())
        self.assertEqual(
            malformed.errors['frontEnd'], ['Enter a number.'],
        )

    def test_add_sale_saves_negative_front_gross_without_unsigned_input_limit(self):
        page = self.client.get(reverse('add_sale'))
        front_widget = page.context['form']['frontEnd']
        self.assertNotIn('min', front_widget.field.widget.attrs)

        response = self.client.post(
            reverse('add_sale'),
            self.sale_data(deal_number=91001, front_end='-1250.00'),
        )
        self.assertRedirects(response, reverse('view_sales'))
        sale = Sale.objects.get(dealNumber=91001)
        self.assertEqual(sale.frontEnd, Decimal('-1250.00'))

    def test_edit_sale_changes_positive_front_gross_to_negative(self):
        sale = self.make_sale()
        response = self.client.post(
            reverse('edit_sale', args=[sale.pk]),
            {
                **self.sale_data(
                    deal_number=sale.dealNumber,
                    front_end='-250.50',
                ),
                'year': '',
                'make': '',
                'make_id': '',
                'model': '',
                'model_id': '',
                'mileage': '',
                'stock_number': '',
            },
        )
        self.assertRedirects(response, reverse('view_sales'))
        sale.refresh_from_db()
        self.assertEqual(sale.frontEnd, Decimal('-250.50'))

    def test_negative_gross_reaches_percentage_rule_without_invented_minimum(self):
        self.add_percentage_rule()
        sale = self.make_sale(front_end='-1250.00')

        result = calculate_sale_commission(self.user, sale)
        breakdown = CommissionEngineService.calculate_sale(self.user, sale)

        self.assertEqual(breakdown.frontend_gross, Decimal('-1250.00'))
        self.assertEqual(breakdown.frontend_commission, Decimal('-125.00'))
        self.assertEqual(result.base_commission, Decimal('-125.00'))
        self.assertEqual(result.total, Decimal('-125.00'))
        percentage = result.line_items[0]
        self.assertEqual(percentage.metadata['raw_gross'], '-1250.00')
        self.assertEqual(percentage.metadata['commissionable_gross'], '-1250.00')

    def test_plan_defined_minimum_applies_to_negative_percentage_commission(self):
        self.add_percentage_rule()
        self.add_minimum_rule('200.00')
        sale = self.make_sale(front_end='-1250.00')

        result = calculate_sale_commission(self.user, sale)
        breakdown = CommissionEngineService.calculate_sale(self.user, sale)

        self.assertEqual(breakdown.frontend_gross, Decimal('-1250.00'))
        self.assertEqual(breakdown.frontend_commission, Decimal('200.00'))
        self.assertEqual(result.base_commission, Decimal('200.00'))
        self.assertEqual(result.total, Decimal('200.00'))

    def test_existing_positive_and_zero_percentage_results_are_unchanged(self):
        self.add_percentage_rule()
        positive = self.make_sale(deal_number=91003, front_end='1000.00')
        zero = self.make_sale(deal_number=91004, front_end='0.00')

        positive_result = calculate_sale_commission(self.user, positive)
        zero_result = calculate_sale_commission(self.user, zero)

        self.assertEqual(positive_result.base_commission, Decimal('100.00'))
        self.assertEqual(positive_result.total, Decimal('100.00'))
        self.assertEqual(zero_result.base_commission, Decimal('0.00'))
        self.assertEqual(zero_result.total, Decimal('0.00'))
