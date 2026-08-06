from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import SaleForm
from .models import (
    Commission,
    PayPlanEligibility,
    PayPlanRule,
    PayPlanRuleCondition,
    Sale,
    UserProfile,
    Vehicle,
    VehicleMake,
    VehicleModel,
)


class DashboardUxPolishTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user('ux-owner', password='password')
        self.other = User.objects.create_user('ux-other', password='password')
        Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.10'),
            total_calculated_back_end=Decimal('0.10'),
            frontend_minimum=Decimal('100.00'),
        )
        Commission.objects.create(user=self.other)
        self.month = timezone.localdate().replace(day=1)
        self.client.force_login(self.user)

    def make_sale(self, **overrides):
        values = {
            'user': self.user,
            'customer': 'Saved Customer',
            'dealNumber': 771001,
            'count': Decimal('1.0'),
            'frontEnd': Decimal('900.00'),
            'backend': Decimal('300.00'),
            'date': self.month,
            'vehicle_condition': 'used',
            'acquisition_source': 'trade_in',
            'split_with_name': '',
        }
        values.update(overrides)
        return Sale.objects.create(**values)

    def enable_new_engine(self):
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        assignment = self.user.pay_plan_assignments.get()
        assignment.effective_start_date = self.month
        assignment.save(update_fields=['effective_start_date', 'updated_at'])
        version = assignment.pay_plan_version
        version.effective_start_date = self.month
        version.save(update_fields=['effective_start_date', 'updated_at'])
        onboarding = self.user.pay_plan_onboarding
        onboarding.current_pay_plan = version.pay_plan
        onboarding.current_version = version
        onboarding.status = onboarding.ACTIVE
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        return version

    def add_nps_survey_rule(self, version, require_passing=True):
        rule = PayPlanRule.objects.create(
            pay_plan_version=version,
            name='NPS Survey Projection Rule',
            rule_type='survey_count_bonus',
            calculation_scope='period',
            configuration={
                'qualifying_count_field': 'nps_qualifying_surveys',
                'low_score_count_field': 'nps_low_score_surveys',
                'grid': [
                    {
                        'count': count,
                        'rate_per_survey': '250.00',
                        'total': str(Decimal(count) * Decimal('250.00')),
                    }
                    for count in range(1, 11)
                ],
            },
        )
        if require_passing:
            PayPlanRuleCondition.objects.create(
                rule=rule,
                field_name='nps_bonus_eligible',
                operator='is_true',
                value=True,
            )
        return rule

    def test_dashboard_order_has_no_floating_summary_and_totals_are_authoritative(self):
        self.make_sale()
        response = self.client.get(reverse('view_sales'))
        content = response.content.decode()
        self.assertEqual(content.count('id="sales-log-heading"'), 1)
        self.assertEqual(content.count('id="commission-totals-heading"'), 1)
        self.assertNotIn('id="bonus-heading"', content)
        self.assertLess(
            content.index('id="commission-totals-heading"'),
            content.index('id="sales-log-heading"'),
        )
        self.assertNotContains(response, 'dashboard-summary')
        self.assertNotContains(response, 'aria-label="Estimated commission totals"')
        self.assertEqual(response.context['total_commission'], Decimal('130.000000'))
        self.assertContains(response, 'Current bonus')
        self.assertContains(response, 'Estimated total commission')
        self.assertContains(response, '$130.00')

    def test_dashboard_nps_projection_is_private_by_user_and_month(self):
        version = self.enable_new_engine()
        self.add_nps_survey_rule(version)
        other_record = PayPlanEligibility.objects.create(
            user=self.other,
            month_start=self.month,
            nps_projection_passing=False,
            nps_projected_good_surveys=3,
            nps_projected_bad_surveys=1,
        )
        saved = self.client.post(reverse('view_sales'), {
            'form_type': 'nps_projection',
            'month': self.month.strftime('%Y-%m'),
            'nps_projection_passing': 'True',
            'nps_projected_good_surveys': '8',
            'nps_projected_bad_surveys': '2',
        })
        self.assertRedirects(saved, f"{reverse('view_sales')}?month={self.month:%Y-%m}")
        record = PayPlanEligibility.objects.get(user=self.user, month_start=self.month)
        self.assertIs(record.nps_projection_passing, True)
        self.assertEqual(record.nps_projected_good_surveys, 8)
        self.assertEqual(record.nps_projected_bad_surveys, 2)
        self.assertEqual(record.nps_status, PayPlanEligibility.NPS_PENDING)
        self.assertEqual(record.nps_qualifying_surveys, 0)
        self.assertEqual(record.nps_low_score_surveys, 0)
        other_record.refresh_from_db()
        self.assertIs(other_record.nps_projection_passing, False)
        self.assertEqual(other_record.nps_projected_good_surveys, 3)
        self.assertEqual(other_record.nps_projected_bad_surveys, 1)

        invalid = self.client.post(reverse('view_sales'), {
            'form_type': 'nps_projection',
            'month': self.month.strftime('%Y-%m'),
            'nps_projection_passing': 'True',
            'nps_projected_good_surveys': '8',
            'nps_projected_bad_surveys': '-1',
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, 'Ensure this value is greater than or equal to 0')
        self.assertContains(invalid, 'value="-1"')
        record.refresh_from_db()
        self.assertEqual(record.nps_projected_bad_surveys, 2)

    def test_dashboard_hides_projection_without_survey_based_pay(self):
        no_survey_pay = self.client.get(reverse('view_sales'))
        self.assertNotContains(no_survey_pay, 'NPS Survey Projection')
        version = self.enable_new_engine()
        nps_only_rule = PayPlanRule.objects.create(
            pay_plan_version=version,
            name='NPS requirement',
            rule_type='flat_per_deal',
            calculation_scope='per_sale',
            configuration={'amount': '1.00'},
        )
        PayPlanRuleCondition.objects.create(
            rule=nps_only_rule,
            field_name='nps_finance_eligible',
            operator='is_true',
            value=True,
        )
        self.assertNotContains(
            self.client.get(reverse('view_sales')),
            'NPS Survey Projection',
        )

    def test_dashboard_projects_payout_from_assigned_plan_without_changing_payroll(self):
        version = self.enable_new_engine()
        self.add_nps_survey_rule(version)
        eligibility = PayPlanEligibility.objects.create(
            user=self.user,
            month_start=self.month,
            nps_projection_passing=True,
            nps_projected_good_surveys=8,
            nps_projected_bad_surveys=2,
        )
        response = self.client.get(reverse('view_sales'))
        self.assertContains(response, 'NPS Survey Projection')
        self.assertContains(response, 'Projection only—does not affect payroll.')
        self.assertContains(response, 'They do not recalculate whether your NPS is passing.')
        self.assertContains(response, 'Net projected survey impact')
        self.assertContains(response, '>+6<', html=False)
        self.assertContains(response, '$250.00 per good survey')
        self.assertContains(response, '$1500.00')
        self.assertEqual(response.context['total_bonus'], Decimal('0'))

        eligibility.nps_projection_passing = False
        eligibility.save(update_fields=['nps_projection_passing'])
        failing = self.client.get(reverse('view_sales'))
        self.assertContains(failing, 'requires passing NPS')
        self.assertContains(failing, '$0.00')

        version.rules.get(rule_type='survey_count_bonus').conditions.all().delete()
        passing_not_required = self.client.get(reverse('view_sales'))
        self.assertNotContains(passing_not_required, 'requires passing NPS')
        self.assertContains(passing_not_required, '$1500.00')

    def test_print_uses_authoritative_total_and_draw_without_clamping(self):
        version = self.enable_new_engine()
        PayPlanRule.objects.create(
            pay_plan_version=version, name='Flat commission',
            rule_type='flat_per_deal', calculation_scope='per_sale',
            configuration={'amount': '100.00'}, sort_order=1,
        )
        PayPlanRule.objects.create(
            pay_plan_version=version, name='Monthly draw',
            rule_type='draw', calculation_scope='period',
            configuration={
                'amount': '150.00', 'eligible_categories': ['front_end'],
                'recoverable': False,
            }, sort_order=2,
        )
        self.make_sale(frontEnd=Decimal('0.00'), backend=Decimal('0.00'))
        response = self.client.get(reverse('print_sales'))
        self.assertEqual(response.context['total_commission'], Decimal('100.00'))
        self.assertEqual(response.context['draw_amount'], Decimal('150.00'))
        self.assertEqual(response.context['total_commission_after_draw'], Decimal('-50.00'))
        self.assertContains(response, 'Total Commission')
        self.assertContains(response, '>Draw<', html=False)
        self.assertContains(response, 'Total Commission After Draw')
        self.assertContains(response, '$-50.00')


class SaleFormUxPolishTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user('sale-owner', password='password')
        self.other = User.objects.create_user('sale-other', password='password')
        Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.10'),
            frontend_minimum=Decimal('100.00'),
        )
        Commission.objects.create(user=self.other)
        self.make = VehicleMake.objects.create(name='Subaru')
        self.model = VehicleModel.objects.create(make=self.make, name='Outback')
        self.month = timezone.localdate().replace(day=1)
        self.client.force_login(self.user)

    def sale(self, count=Decimal('1.0'), owner=None, deal=772001):
        sale = Sale.objects.create(
            user=owner or self.user, customer='Original Customer', dealNumber=deal,
            count=count, split_with_name='Partner' if count == Decimal('0.5') else '',
            frontEnd=Decimal('700.00'), backend=Decimal('250.00'), date=self.month,
            vehicle_condition='used', acquisition_source='trade_in',
        )
        Vehicle.objects.create(
            sale=sale, year=2025, make=self.make, model=self.model,
            mileage=12345, stock_number='STK-1', vin='1HGCM82633A004352',
        )
        return sale

    def test_add_defaults_empty_normalization_malformed_validation_and_minimum(self):
        page = self.client.get(reverse('add_sale'))
        self.assertContains(page, 'name="frontEnd" value="0.00"', html=False)
        self.assertContains(page, 'name="backend" value="0.00"', html=False)
        form = SaleForm({
            'customer': 'Zero Gross', 'date': self.month, 'dealNumber': 772010,
            'count': '1', 'frontEnd': '', 'backend': '',
            'vehicle_condition': 'used', 'acquisition_source': '',
        })
        self.assertTrue(form.is_valid(), form.errors)
        sale = form.save(commit=False)
        sale.user = self.user
        sale.save()
        self.assertEqual(sale.frontEnd, Decimal('0.00'))
        self.assertEqual(sale.backend, Decimal('0.00'))
        self.assertEqual(sale.calculate_frontEnd, Decimal('100.00'))
        malformed = SaleForm({
            'customer': 'Bad Gross', 'date': self.month, 'dealNumber': 772011,
            'count': '1', 'frontEnd': 'not-a-number', 'backend': '0',
        })
        self.assertFalse(malformed.is_valid())
        self.assertIn('frontEnd', malformed.errors)

    def test_make_model_have_one_visible_primary_control(self):
        response = self.client.get(reverse('add_sale'))
        content = response.content.decode()
        self.assertEqual(content.count('name="make"'), 1)
        self.assertEqual(content.count('name="model"'), 1)
        self.assertEqual(
            content.count('class="add-catalog-value" hidden'),
            2,
        )
        self.assertNotIn('add-catalog-value button', content)
        css = open('SalesLogApp/static/SalesLogApp/css/styles.css', encoding='utf-8').read()
        self.assertIn('.add-catalog-value[hidden]', css)
        self.assertIn('display: none !important;', css)
        self.assertIn('.add-catalog-value:not([hidden])', css)

        sale = self.sale(deal=772019)
        edit = self.client.get(reverse('edit_sale', args=[sale.pk]))
        edit_content = edit.content.decode()
        self.assertEqual(edit_content.count('name="make"'), 1)
        self.assertEqual(edit_content.count('name="model"'), 1)
        self.assertEqual(
            edit_content.count('class="add-catalog-value" hidden'),
            2,
        )
        self.assertContains(edit, 'name="make" value="Subaru"', html=False)
        self.assertContains(edit, 'name="model" value="Outback"', html=False)

    def test_edit_preselects_all_counts_and_preserves_saved_and_submitted_values(self):
        for index, count in enumerate((Decimal('0.5'), Decimal('1.0'), Decimal('2.0'))):
            sale = self.sale(count=count, deal=772020 + index)
            response = self.client.get(reverse('edit_sale', args=[sale.pk]))
            expected = format(count.normalize(), 'f')
            self.assertEqual(response.context['form']['count'].value(), count)
            self.assertContains(
                response,
                f'value="{expected}" required id="id_count_{index}" checked',
                html=False,
            )

        sale = self.sale(deal=772030)
        response = self.client.post(reverse('edit_sale', args=[sale.pk]), {
            'customer': 'Changed Only',
        })
        self.assertRedirects(response, reverse('view_sales'))
        sale.refresh_from_db()
        self.assertEqual(sale.customer, 'Changed Only')
        self.assertEqual(sale.frontEnd, Decimal('700.00'))
        self.assertEqual(sale.vehicle.stock_number, 'STK-1')

        invalid = self.client.post(reverse('edit_sale', args=[sale.pk]), {
            'customer': 'Submitted Customer', 'dealNumber': 'bad',
            'count': '2', 'frontEnd': '321.45', 'backend': '88.00',
            'date': '2026-08-03', 'vehicle_condition': 'new',
            'acquisition_source': 'auction', 'make': 'Subaru',
            'make_id': self.make.pk, 'model': 'Outback',
            'model_id': self.model.pk, 'year': '2025', 'mileage': '9876',
            'stock_number': 'NEW-STOCK', 'vin': '1HGCM82633A004352',
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.context['form']['customer'].value(), 'Submitted Customer')
        self.assertEqual(invalid.context['form']['count'].value(), '2')
        self.assertEqual(invalid.context['vehicle_form']['stock_number'].value(), 'NEW-STOCK')

    def test_edit_is_owner_isolated(self):
        private = self.sale(owner=self.other, deal=772040)
        self.assertEqual(
            self.client.get(reverse('edit_sale', args=[private.pk])).status_code,
            404,
        )
