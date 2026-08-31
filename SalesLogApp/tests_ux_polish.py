from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import SaleForm
from .models import (
    Commission,
    PayPlanEligibility,
    PayPlanAssignment,
    PayPlanRule,
    PayPlanRuleCondition,
    PayPlanVersion,
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

    def test_dashboard_subtitle_uses_uncorrupted_apostrophe(self):
        response = self.client.get(reverse('view_sales'))

        self.assertContains(
            response,
            'Manage deals and review each deal&rsquo;s estimated commission.',
        )
        self.assertNotContains(response, 'dealâ€™s')

    def test_header_uses_mark_only_logo_link_to_dashboard(self):
        response = self.client.get(reverse('view_sales'))

        self.assertContains(
            response,
            f'href="{reverse("view_sales")}"',
        )
        self.assertContains(response, 'class="site-logo-link"')
        self.assertContains(response, 'aria-label="StewLog Dashboard"')
        self.assertContains(
            response,
            "SalesLogApp/images/stewlog-mark.png",
        )
        self.assertNotContains(response, '<h1>Sales Log</h1>', html=True)
        self.assertNotContains(
            response,
            "SalesLogApp/images/stewlog-logo.png",
        )

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

    def add_nps_survey_rule(
        self, version, require_passing=True, rate=Decimal('250.00'),
    ):
        rate = Decimal(str(rate))
        rule = PayPlanRule.objects.create(
            pay_plan_version=version,
            name='NPS Survey Bonus',
            rule_type='survey_count_bonus',
            calculation_scope='period',
            configuration={
                'qualifying_count_field': 'nps_qualifying_surveys',
                'low_score_count_field': 'nps_low_score_surveys',
                'grid': [
                    {
                        'count': count,
                        'rate_per_survey': str(rate),
                        'total': str(Decimal(count) * rate),
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

    def test_dashboard_nps_bonus_updates_authoritative_user_inputs(self):
        version = self.enable_new_engine()
        self.add_nps_survey_rule(version)
        self.make_sale()
        other_record = PayPlanEligibility.objects.create(
            user=self.other,
            month_start=self.month,
            nps_status=PayPlanEligibility.NPS_INELIGIBLE,
            nps_qualifying_surveys=3,
            nps_low_score_surveys=1,
        )
        before = self.client.get(reverse('view_sales'))
        saved = self.client.post(reverse('view_sales'), {
            'form_type': 'nps_bonus',
            'month': self.month.strftime('%Y-%m'),
            'nps_status': PayPlanEligibility.NPS_ELIGIBLE,
            'nps_qualifying_surveys': '8',
            'nps_low_score_surveys': '2',
        })
        self.assertRedirects(saved, f"{reverse('view_sales')}?month={self.month:%Y-%m}")
        record = PayPlanEligibility.objects.get(user=self.user, month_start=self.month)
        self.assertEqual(record.nps_status, PayPlanEligibility.NPS_ELIGIBLE)
        self.assertEqual(record.nps_qualifying_surveys, 8)
        self.assertEqual(record.nps_low_score_surveys, 2)
        other_record.refresh_from_db()
        self.assertEqual(
            other_record.nps_status, PayPlanEligibility.NPS_INELIGIBLE,
        )
        self.assertEqual(other_record.nps_qualifying_surveys, 3)
        self.assertEqual(other_record.nps_low_score_surveys, 1)

        updated = self.client.get(reverse('view_sales'))
        self.assertEqual(
            updated.context['total_bonus'] - before.context['total_bonus'],
            Decimal('1500.00'),
        )
        self.assertEqual(
            updated.context['total_commission'] - before.context['total_commission'],
            Decimal('1500.00'),
        )

        invalid = self.client.post(reverse('view_sales'), {
            'form_type': 'nps_bonus',
            'month': self.month.strftime('%Y-%m'),
            'nps_status': PayPlanEligibility.NPS_ELIGIBLE,
            'nps_qualifying_surveys': '8',
            'nps_low_score_surveys': '-1',
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, 'Ensure this value is greater than or equal to 0')
        self.assertContains(invalid, 'value="-1"')
        record.refresh_from_db()
        self.assertEqual(record.nps_low_score_surveys, 2)

    def test_dashboard_hides_nps_bonus_without_survey_based_pay(self):
        no_survey_pay = self.client.get(reverse('view_sales'))
        self.assertNotContains(no_survey_pay, 'id="nps-bonus-heading"')
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
            'id="nps-bonus-heading"',
        )

    def test_dashboard_hides_non_period_survey_rule_from_nps_editor(self):
        version = self.enable_new_engine()
        version.rules.all().delete()
        PayPlanRule.objects.create(
            pay_plan_version=version,
            name='Per-deal survey rule',
            rule_type='survey_count_bonus',
            calculation_scope='per_sale',
            configuration={
                'qualifying_count_field': 'nps_qualifying_surveys',
                'low_score_count_field': 'nps_low_score_surveys',
                'grid': [{
                    'count': 1,
                    'rate_per_survey': '125.00',
                    'total': '125.00',
                }],
            },
        )

        response = self.client.get(reverse('view_sales'))

        self.assertNotContains(response, 'id="nps-bonus-heading"')

    def test_mid_month_plan_change_uses_same_nps_rule_as_period_total(self):
        old_version = self.enable_new_engine()
        old_version.rules.all().delete()
        old_rule = self.add_nps_survey_rule(
            old_version, rate=Decimal('125.00'),
        )
        change_date = self.month + timedelta(days=14)
        prior_day = change_date - timedelta(days=1)
        old_assignment = self.user.pay_plan_assignments.get()
        old_assignment.effective_end_date = prior_day
        old_assignment.save(update_fields=[
            'effective_end_date', 'updated_at',
        ])
        old_version.effective_end_date = prior_day
        old_version.status = PayPlanVersion.INACTIVE
        old_version.save(update_fields=[
            'effective_end_date', 'status', 'updated_at',
        ])
        new_version = PayPlanVersion.objects.create(
            pay_plan=old_version.pay_plan,
            version_name='Mid-month replacement',
            effective_start_date=change_date,
            status=PayPlanVersion.ACTIVE,
            activated_at=timezone.now(),
        )
        new_rule = self.add_nps_survey_rule(
            new_version, rate=Decimal('300.00'),
        )
        PayPlanAssignment.objects.create(
            user=self.user,
            pay_plan_version=new_version,
            effective_start_date=change_date,
            is_active=True,
        )
        onboarding = self.user.pay_plan_onboarding
        onboarding.current_version = new_version
        onboarding.save(update_fields=['current_version', 'updated_at'])
        PayPlanEligibility.objects.create(
            user=self.user,
            month_start=self.month,
            nps_status=PayPlanEligibility.NPS_ELIGIBLE,
            nps_qualifying_surveys=1,
            nps_low_score_surveys=0,
        )

        response = self.client.get(reverse('view_sales'))

        self.assertContains(response, 'id="nps-bonus-heading"')
        self.assertEqual(response.context['total_bonus'], Decimal('125.00'))
        self.assertEqual(response.context['nps_bonus']['payout'], Decimal('125.00'))
        self.assertEqual(
            response.context['commission_diagnostics']['unit_bonus']['line_items'][0]['rule_id'],
            old_rule.id,
        )
        self.assertNotEqual(old_rule.id, new_rule.id)

    def test_dashboard_nps_bonus_is_authoritative_and_itemized(self):
        version = self.enable_new_engine()
        version.rules.all().delete()
        nps_rule = self.add_nps_survey_rule(
            version, rate=Decimal('125.00'),
        )
        PayPlanRule.objects.create(
            pay_plan_version=version,
            name='Acquisition Bonus',
            rule_type='acquisition_bonus',
            calculation_scope='per_sale',
            configuration={'amount': '50.00'},
        )
        PayPlanRule.objects.create(
            pay_plan_version=version,
            name='Vehicle Bonus',
            rule_type='vehicle_spiff',
            calculation_scope='per_sale',
            configuration={'amount': '25.00'},
        )
        PayPlanRule.objects.create(
            pay_plan_version=version,
            name='CSI Survey Bonus',
            rule_type='survey_count_bonus',
            calculation_scope='period',
            configuration={
                'qualifying_count_field': 'monthly_units',
                'low_score_count_field': 'monthly_new_units',
                'grid': [{
                    'count': 1,
                    'rate_per_survey': '300.00',
                    'total': '300.00',
                }],
            },
        )
        eligibility = PayPlanEligibility.objects.create(
            user=self.user,
            month_start=self.month,
            nps_status=PayPlanEligibility.NPS_ELIGIBLE,
            nps_qualifying_surveys=1,
            nps_low_score_surveys=0,
        )
        self.make_sale()
        response = self.client.get(reverse('view_sales'))
        self.assertContains(response, 'NPS Survey Bonus')
        self.assertContains(response, 'This changes your commission total.')
        self.assertContains(response, 'Net survey count')
        self.assertContains(response, '>+1<', html=False)
        self.assertContains(response, '$125.00 per good survey')
        self.assertContains(response, '>Current payout<', html=False)
        self.assertNotContains(response, 'Projection')
        self.assertNotContains(response, 'does not affect payroll')
        self.assertEqual(response.context['total_bonus'], Decimal('500.00'))
        self.assertEqual(response.context['nps_bonus']['payout'], Decimal('125.00'))
        items = response.context['bonus_breakdown']['items']
        self.assertEqual(sum((item['amount'] for item in items), Decimal('0')), Decimal('500.00'))
        self.assertEqual(
            {item['label']: item['amount'] for item in items},
            {
                'NPS Survey Bonus': Decimal('125.00'),
                'CSI Survey Bonus': Decimal('300.00'),
                'Acquisition Bonus': Decimal('50.00'),
                'Vehicle Bonus': Decimal('25.00'),
            },
        )
        self.assertContains(response, 'NPS Survey Bonus')
        self.assertContains(response, '$125.00')

        eligibility.nps_status = PayPlanEligibility.NPS_INELIGIBLE
        eligibility.save(update_fields=['nps_status'])
        failing = self.client.get(reverse('view_sales'))
        self.assertContains(failing, 'requires eligible NPS status')
        self.assertEqual(failing.context['nps_bonus']['payout'], Decimal('0.00'))
        self.assertEqual(failing.context['total_bonus'], Decimal('375.00'))

        for status in (
            PayPlanEligibility.NPS_EXEMPT,
            PayPlanEligibility.NPS_PENDING,
        ):
            eligibility.nps_status = status
            eligibility.save(update_fields=['nps_status'])
            blocked = self.client.get(reverse('view_sales'))
            self.assertContains(blocked, 'requires eligible NPS status')
            self.assertEqual(blocked.context['nps_bonus']['payout'], Decimal('0.00'))

        nps_rule.conditions.all().delete()
        passing_not_required = self.client.get(reverse('view_sales'))
        self.assertNotContains(passing_not_required, 'requires eligible NPS status')
        self.assertEqual(
            passing_not_required.context['nps_bonus']['payout'],
            Decimal('125.00'),
        )
        self.assertEqual(
            passing_not_required.context['total_bonus'], Decimal('500.00'),
        )

    def test_dashboard_nps_bonus_uses_compact_dialog_controls(self):
        version = self.enable_new_engine()
        self.add_nps_survey_rule(version)
        PayPlanEligibility.objects.create(
            user=self.user,
            month_start=self.month,
            nps_status=PayPlanEligibility.NPS_ELIGIBLE,
            nps_qualifying_surveys=8,
            nps_low_score_surveys=2,
        )

        response = self.client.get(reverse('view_sales'))
        content = response.content.decode()

        self.assertContains(response, 'class="card nps-bonus-card"')
        self.assertContains(
            response,
            'aria-label="NPS survey bonus summary"',
        )
        self.assertContains(response, '>Eligible<', html=False)
        self.assertContains(response, '>Qualifying surveys<', html=False)
        self.assertContains(response, '>Low-score surveys<', html=False)
        self.assertContains(response, '>Current payout<', html=False)
        self.assertContains(response, 'data-dialog="nps-bonus-dialog"')
        self.assertContains(response, 'id="nps-bonus-dialog"')
        self.assertContains(response, 'npsBonusDialog.showModal()')
        self.assertLess(
            content.index('id="nps-bonus-dialog"'),
            content.index('<form method="post" novalidate>'),
        )

    def test_nps_bonus_pays_without_recorded_sales(self):
        version = self.enable_new_engine()
        version.rules.all().delete()
        self.add_nps_survey_rule(version, rate=Decimal('125.00'))
        PayPlanEligibility.objects.create(
            user=self.user,
            month_start=self.month,
            nps_status=PayPlanEligibility.NPS_ELIGIBLE,
            nps_qualifying_surveys=1,
            nps_low_score_surveys=0,
        )

        response = self.client.get(reverse('view_sales'))

        self.assertFalse(response.context['sales'].exists())
        self.assertEqual(response.context['total_bonus'], Decimal('125.00'))
        self.assertEqual(response.context['total_commission'], Decimal('125.00'))
        self.assertEqual(response.context['nps_bonus']['payout'], Decimal('125.00'))
        self.assertContains(response, 'id="commission-totals-heading"')
        self.assertContains(response, 'NPS Survey Bonus')
        self.assertContains(response, '$125.00')

    def test_negative_nps_bonus_uses_standard_currency_order(self):
        version = self.enable_new_engine()
        version.rules.all().delete()
        self.add_nps_survey_rule(version, rate=Decimal('125.00'))
        PayPlanEligibility.objects.create(
            user=self.user,
            month_start=self.month,
            nps_status=PayPlanEligibility.NPS_ELIGIBLE,
            nps_qualifying_surveys=1,
            nps_low_score_surveys=2,
        )

        dashboard = self.client.get(reverse('view_sales'))
        commission = self.client.get(reverse('view_commission'))

        self.assertEqual(dashboard.context['total_bonus'], Decimal('-125.00'))
        self.assertContains(
            dashboard,
            '<span>Current bonus</span><strong>-$125.00</strong>',
            html=True,
        )
        self.assertContains(
            dashboard,
            '<span>Current payout</span><strong>-$125.00</strong>',
            html=True,
        )
        self.assertContains(
            dashboard,
            '<span>Estimated total commission</span><strong>-$125.00</strong>',
            html=True,
        )
        self.assertContains(
            commission,
            '<dt>Total commission</dt><dd>-$125.00</dd>',
            html=True,
        )
        self.assertContains(commission, '<dd>-$125.00</dd>', html=True)

    def test_legacy_user_cannot_activate_nps_bonus_from_dashboard(self):
        version = self.user.pay_plan_assignments.get().pay_plan_version
        self.add_nps_survey_rule(version)
        eligibility = PayPlanEligibility.objects.create(
            user=self.user,
            month_start=self.month,
            nps_status=PayPlanEligibility.NPS_PENDING,
        )

        dashboard = self.client.get(reverse('view_sales'))
        self.assertNotContains(dashboard, 'id="nps-bonus-heading"')

        saved = self.client.post(reverse('view_sales'), {
            'form_type': 'nps_bonus',
            'month': self.month.strftime('%Y-%m'),
            'nps_status': PayPlanEligibility.NPS_ELIGIBLE,
            'nps_qualifying_surveys': '4',
            'nps_low_score_surveys': '0',
        })
        self.assertRedirects(
            saved, f"{reverse('view_sales')}?month={self.month:%Y-%m}",
        )
        eligibility.refresh_from_db()
        self.assertEqual(eligibility.nps_status, PayPlanEligibility.NPS_PENDING)
        self.assertEqual(eligibility.nps_qualifying_surveys, 0)

    def test_bonus_breakdown_keeps_distinct_same_named_rules(self):
        version = self.enable_new_engine()
        version.rules.all().delete()
        for amount in ('100.00', '200.00'):
            PayPlanRule.objects.create(
                pay_plan_version=version,
                name='Monthly Bonus',
                rule_type='volume_bonus',
                calculation_scope='period',
                configuration={
                    'tiers': [{
                        'minimum_units': '1',
                        'maximum_units': None,
                        'amount': amount,
                    }],
                    'tier_mode': 'highest_only',
                },
            )
        self.make_sale()

        response = self.client.get(reverse('view_sales'))

        items = response.context['bonus_breakdown']['items']
        self.assertEqual(len(items), 2)
        self.assertEqual(
            [item['amount'] for item in items],
            [Decimal('100.00'), Decimal('200.00')],
        )
        self.assertEqual(len({item['rule_id'] for item in items}), 2)

    def test_invalid_nps_bonus_reopens_dialog_with_errors(self):
        version = self.enable_new_engine()
        self.add_nps_survey_rule(version)

        response = self.client.post(reverse('view_sales'), {
            'form_type': 'nps_bonus',
            'month': self.month.strftime('%Y-%m'),
            'nps_status': PayPlanEligibility.NPS_ELIGIBLE,
            'nps_qualifying_surveys': '2',
            'nps_low_score_surveys': '-1',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-open-on-load="true"')
        self.assertContains(
            response,
            'Ensure this value is greater than or equal to 0',
        )

    def test_historical_nps_bonus_is_read_only(self):
        previous_month = (self.month - timedelta(days=1)).replace(day=1)
        version = self.enable_new_engine()
        assignment = self.user.pay_plan_assignments.get()
        assignment.effective_start_date = previous_month
        assignment.save(update_fields=['effective_start_date', 'updated_at'])
        version.effective_start_date = previous_month
        version.save(update_fields=['effective_start_date', 'updated_at'])
        self.add_nps_survey_rule(version)
        eligibility = PayPlanEligibility.objects.create(
            user=self.user,
            month_start=previous_month,
            nps_status=PayPlanEligibility.NPS_ELIGIBLE,
            nps_qualifying_surveys=3,
            nps_low_score_surveys=1,
        )

        historical = self.client.get(
            reverse('view_sales'), {'month': previous_month.strftime('%Y-%m')},
        )
        self.assertContains(historical, 'id="nps-bonus-heading"')
        self.assertNotContains(historical, 'data-dialog="nps-bonus-dialog"')
        self.assertNotContains(historical, 'Save and recalculate bonus')

        saved = self.client.post(reverse('view_sales'), {
            'form_type': 'nps_bonus',
            'month': previous_month.strftime('%Y-%m'),
            'nps_status': PayPlanEligibility.NPS_INELIGIBLE,
            'nps_qualifying_surveys': '9',
            'nps_low_score_surveys': '4',
        })
        self.assertRedirects(
            saved,
            f"{reverse('view_sales')}?month={previous_month:%Y-%m}",
        )
        eligibility.refresh_from_db()
        self.assertEqual(eligibility.nps_status, PayPlanEligibility.NPS_ELIGIBLE)
        self.assertEqual(eligibility.nps_qualifying_surveys, 3)
        self.assertEqual(eligibility.nps_low_score_surveys, 1)

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
