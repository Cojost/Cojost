from __future__ import annotations

import inspect
import logging
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .models import (
    ArchivedSale,
    BonusLevel,
    Commission,
    CommissionAdjustment,
    Industry,
    MonthlyGoal,
    PayPlan,
    PayPlanAssignment,
    PayPlanEligibility,
    PayPlanRule,
    PayPlanVersion,
    Sale,
    Team,
    TeamMembership,
    UserProfile,
)
from .selling_calendar import (
    SellingDayCalendarError,
    StaticSellingDayCalendar,
    selling_dates_for_month,
)
from .services import reporting_commission_totals
from .stew_coach_projection import (
    CALCULATION_VERSION,
    METRIC_ORDER,
    PROJECTION_METHOD,
    StewCoachAccessDenied,
    StewCoachInputError,
    StewCoachProjectionService,
)


class SellingDayCalendarTests(SimpleTestCase):
    owner = SimpleNamespace(pk=17)

    def calendar(self, closures=(), version='calendar.test.v1'):
        return StaticSellingDayCalendar.for_owner(
            self.owner,
            closure_dates=closures,
            calendar_version=version,
        )

    def selling_dates(self, month_start, month_end, *, closures=()):
        return selling_dates_for_month(
            self.calendar(closures),
            owner=self.owner,
            month_start=month_start,
            month_end=month_end,
        )[1]

    def test_monday_through_saturday_are_open_and_sunday_is_closed(self):
        dates = self.selling_dates(date(2028, 1, 3), date(2028, 1, 9))
        self.assertEqual(
            dates,
            tuple(date(2028, 1, day) for day in range(3, 9)),
        )

    def test_custom_and_consecutive_weekend_closures_are_excluded(self):
        dates = self.selling_dates(
            date(2028, 1, 3),
            date(2028, 1, 10),
            closures={date(2028, 1, 7), date(2028, 1, 8)},
        )
        self.assertNotIn(date(2028, 1, 7), dates)
        self.assertNotIn(date(2028, 1, 8), dates)
        self.assertNotIn(date(2028, 1, 9), dates)
        self.assertIn(date(2028, 1, 10), dates)

    def test_leap_year_february_and_year_boundaries(self):
        february = self.selling_dates(date(2028, 2, 1), date(2028, 2, 29))
        january = self.selling_dates(date(2028, 1, 1), date(2028, 1, 31))
        december = self.selling_dates(date(2028, 12, 1), date(2028, 12, 31))
        self.assertIn(date(2028, 2, 29), february)
        self.assertEqual(january[0].month, 1)
        self.assertEqual(january[-1].month, 1)
        self.assertEqual(december[0].month, 12)
        self.assertEqual(december[-1].month, 12)

    def test_empty_closures_and_calendar_version_are_preserved(self):
        version, dates = selling_dates_for_month(
            self.calendar(version='dealer-17.v4'),
            owner=self.owner,
            month_start=date(2028, 2, 1),
            month_end=date(2028, 2, 29),
        )
        self.assertEqual(version, 'dealer-17.v4')
        self.assertTrue(dates)

    def test_all_potential_dates_can_be_closed(self):
        start = date(2028, 2, 1)
        end = date(2028, 2, 29)
        closures = {
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() != 6
        }
        self.assertEqual(
            self.selling_dates(start, end, closures=closures),
            (),
        )

    def test_invalid_closure_version_owner_and_provider_output_fail_closed(self):
        with self.assertRaises(SellingDayCalendarError):
            self.calendar(closures={'2028-01-01'})
        with self.assertRaises(SellingDayCalendarError):
            self.calendar(version='spaces are invalid')
        calendar = self.calendar()
        with self.assertRaises(SellingDayCalendarError):
            calendar.closed_dates(
                owner=SimpleNamespace(pk=999),
                month_start=date(2028, 1, 1),
                month_end=date(2028, 1, 31),
            )

        class MutableCalendar:
            calendar_version = 'mutable.v1'

            def closed_dates(self, **_kwargs):
                return set()

        with self.assertRaises(SellingDayCalendarError):
            selling_dates_for_month(
                MutableCalendar(),
                owner=self.owner,
                month_start=date(2028, 1, 1),
                month_end=date(2028, 1, 31),
            )
        with self.assertRaises(SellingDayCalendarError):
            selling_dates_for_month(
                None,
                owner=self.owner,
                month_start=date(2028, 1, 1),
                month_end=date(2028, 1, 31),
            )


class StewCoachProjectionTests(TestCase):
    month = date(2028, 2, 1)
    as_of = date(2028, 2, 10)

    def setUp(self):
        self.entitlement = patch(
            'SalesLogApp.stew_coach_projection.activity_goals_authorized',
            return_value=True,
        )
        self.entitlement.start()
        self.addCleanup(self.entitlement.stop)
        self.user = User.objects.create_user('coach-owner', password='pw')
        self.other = User.objects.create_user('coach-other', password='pw')
        self.commission = Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.10'),
            total_calculated_back_end=Decimal('0.10'),
        )
        Commission.objects.create(
            user=self.other,
            total_calculated_front_end=Decimal('0.95'),
            total_calculated_back_end=Decimal('0.95'),
        )
        self.deal_number = 870000

    def calendar(self, *, owner=None, closures=(), version='dealer.v1'):
        return StaticSellingDayCalendar.for_owner(
            owner or self.user,
            closure_dates=closures,
            calendar_version=version,
        )

    def goal(self, *, owner=None, units='10.0', gross='1000.00', commission='100.00'):
        return MonthlyGoal.objects.create(
            user=owner or self.user,
            month_start=self.month,
            target_units=Decimal(units),
            target_total_gross=Decimal(gross),
            target_commission=Decimal(commission),
        )

    def sale(
        self,
        *,
        owner=None,
        sale_date=None,
        count='1.0',
        front='100.00',
        back='0.00',
        customer='Projection customer',
    ):
        self.deal_number += 1
        return Sale.objects.create(
            user=owner or self.user,
            customer=customer,
            dealNumber=self.deal_number,
            count=Decimal(count),
            frontEnd=Decimal(front),
            backend=Decimal(back),
            date=sale_date or self.as_of,
        )

    def calculate(self, *, as_of=None, calendar=None):
        return StewCoachProjectionService.calculate(
            owner=self.user,
            month_start=self.month.replace(day=17),
            as_of_date=as_of or self.as_of,
            calendar=calendar or self.calendar(),
        )

    @staticmethod
    def metric(result, metric_id):
        return next(item for item in result.metrics if item.metric_id == metric_id)

    def test_open_day_projection_includes_today_once_and_uses_only_prior_rate(self):
        self.goal()
        self.sale(sale_date=date(2028, 2, 5))
        before_today = self.calculate()
        self.sale(sale_date=self.as_of)
        result = self.calculate()
        units = self.metric(result, 'units')

        self.assertEqual(result.period_status, 'in_progress')
        self.assertEqual(result.completed_selling_days, 8)
        self.assertEqual(result.remaining_selling_days, 17)
        self.assertEqual(result.future_selling_days, 16)
        self.assertEqual(units.actual_through_prior_day, Decimal('1.0'))
        self.assertEqual(units.actual, Decimal('2.0'))
        self.assertEqual(
            units.projected_total,
            Decimal('2') + Decimal('1') / Decimal('8') * Decimal('16'),
        )
        self.assertEqual(units.remaining, Decimal('8.0'))
        self.assertEqual(
            units.required_pace,
            Decimal('8') / Decimal('17'),
        )
        self.assertEqual(
            self.metric(before_today, 'units').actual,
            Decimal('1.0'),
        )
        self.assertEqual(
            self.metric(before_today, 'units').required_pace,
            Decimal('9') / Decimal('17'),
        )

    def test_today_is_not_completed_and_is_removed_from_remaining_when_closed(self):
        self.goal()
        self.sale(sale_date=date(2028, 2, 5))
        self.sale(sale_date=self.as_of)
        result = self.calculate(
            calendar=self.calendar(closures={self.as_of}),
        )
        units = self.metric(result, 'units')

        self.assertEqual(result.completed_selling_days, 8)
        self.assertEqual(result.remaining_selling_days, 16)
        self.assertEqual(result.future_selling_days, 16)
        self.assertEqual(units.actual, Decimal('2.0'))
        self.assertEqual(units.actual_through_prior_day, Decimal('1.0'))
        self.assertEqual(
            units.projected_total,
            Decimal('2') + Decimal('1') / Decimal('8') * Decimal('16'),
        )

    def test_first_selling_day_has_actual_but_no_projection(self):
        self.goal()
        self.sale(sale_date=self.month)
        result = self.calculate(as_of=self.month)
        units = self.metric(result, 'units')

        self.assertEqual(result.completed_selling_days, 0)
        self.assertEqual(units.actual, Decimal('1.0'))
        self.assertEqual(units.actual_through_prior_day, Decimal('0'))
        self.assertIsNone(units.projected_total)
        self.assertEqual(units.status, 'insufficient_data')
        self.assertEqual(units.diagnostic_code, 'no_completed_selling_days')

    def test_missing_and_zero_goals_use_no_goal_without_hiding_actuals(self):
        self.sale(sale_date=date(2028, 2, 5))
        missing = self.calculate()
        self.assertTrue(all(item.status == 'no_goal' for item in missing.metrics))
        self.assertEqual(self.metric(missing, 'units').actual, Decimal('1.0'))

        self.goal(units='0', gross='0', commission='0')
        zero = self.calculate()
        self.assertTrue(all(item.status == 'no_goal' for item in zero.metrics))
        self.assertTrue(all(item.required_pace is None for item in zero.metrics))

    def test_zero_sales_negative_gross_and_unclamped_progress(self):
        self.goal(units='5', gross='100', commission='100')
        empty = self.calculate()
        self.assertEqual(
            self.metric(empty, 'units').projected_total,
            Decimal('0'),
        )
        self.assertEqual(self.metric(empty, 'units').status, 'behind')

        self.sale(
            sale_date=date(2028, 2, 5),
            front='-150.00',
            back='25.00',
        )
        negative = self.calculate()
        gross = self.metric(negative, 'total_gross')
        self.assertEqual(gross.actual, Decimal('-125.00'))
        self.assertEqual(gross.remaining, Decimal('225.00'))
        self.assertEqual(gross.progress_percent, Decimal('-125'))

    def test_half_normal_and_double_credit_preserve_gross_and_commission_policy(self):
        self.goal(units='3.5', gross='300', commission='25')
        self.sale(sale_date=date(2028, 2, 5), count='0.5')
        self.sale(sale_date=date(2028, 2, 7), count='1.0')
        self.sale(sale_date=self.as_of, count='2.0')
        result = self.calculate()

        self.assertEqual(self.metric(result, 'units').actual, Decimal('3.5'))
        self.assertEqual(
            self.metric(result, 'total_gross').actual,
            Decimal('300.00'),
        )
        self.assertEqual(
            self.metric(result, 'commission').actual,
            Decimal('25.000'),
        )
        self.assertTrue(
            all(item.status == 'goal_reached' for item in result.metrics)
        )

    def test_exact_exceeded_on_pace_and_behind_statuses(self):
        goal = self.goal(units='2', gross='1000', commission='100')
        self.sale(sale_date=date(2028, 2, 5))
        self.sale(sale_date=date(2028, 2, 7))
        exact = self.calculate()
        self.assertEqual(self.metric(exact, 'units').status, 'goal_reached')

        goal.target_units = Decimal('1.5')
        goal.save(update_fields=['target_units', 'updated_at'])
        exceeded = self.calculate()
        self.assertEqual(self.metric(exceeded, 'units').status, 'goal_reached')
        self.assertGreater(
            self.metric(exceeded, 'units').progress_percent,
            Decimal('100'),
        )

        goal.target_units = Decimal('4.0')
        goal.save(update_fields=['target_units', 'updated_at'])
        on_pace = self.calculate()
        self.assertEqual(self.metric(on_pace, 'units').status, 'on_pace')

        goal.target_units = Decimal('100.0')
        goal.save(update_fields=['target_units', 'updated_at'])
        behind = self.calculate()
        self.assertEqual(self.metric(behind, 'units').status, 'behind')

    def test_future_month_ignores_future_records_and_exposes_required_pace(self):
        self.goal(units='22')
        self.sale(sale_date=self.as_of, count='2.0')
        result = self.calculate(as_of=date(2028, 1, 31))
        units = self.metric(result, 'units')

        self.assertEqual(result.period_status, 'future')
        self.assertEqual(result.effective_cutoff_date, date(2028, 1, 31))
        self.assertEqual(result.completed_selling_days, 0)
        self.assertEqual(
            result.remaining_selling_days,
            result.total_selling_days,
        )
        self.assertEqual(units.actual, Decimal('0'))
        self.assertIsNone(units.projected_total)
        self.assertEqual(units.status, 'insufficient_data')
        self.assertEqual(
            units.required_pace,
            Decimal('22') / Decimal(result.total_selling_days),
        )

    def test_completed_month_uses_full_actual_and_has_no_remaining_pace(self):
        self.goal(units='3')
        self.sale(sale_date=date(2028, 2, 29), count='1.0')
        result = self.calculate(as_of=date(2028, 3, 1))
        units = self.metric(result, 'units')

        self.assertEqual(result.period_status, 'complete')
        self.assertEqual(result.effective_cutoff_date, date(2028, 2, 29))
        self.assertEqual(result.completed_selling_days, result.total_selling_days)
        self.assertEqual(result.remaining_selling_days, 0)
        self.assertEqual(units.actual, Decimal('1.0'))
        self.assertEqual(units.actual_through_prior_day, Decimal('1.0'))
        self.assertEqual(units.projected_total, Decimal('1.0'))
        self.assertEqual(units.status, 'behind')
        self.assertIsNone(units.required_pace)

    def test_full_decimal_precision_metric_order_and_versions(self):
        self.goal(units='10')
        self.sale(sale_date=date(2028, 2, 2))
        result = self.calculate(as_of=date(2028, 2, 9))
        units = self.metric(result, 'units')

        self.assertEqual(tuple(item.metric_id for item in result.metrics), METRIC_ORDER)
        self.assertEqual(result.calculation_version, CALCULATION_VERSION)
        self.assertEqual(result.projection_method, PROJECTION_METHOD)
        self.assertEqual(result.calendar_version, 'dealer.v1')
        self.assertEqual(result.month_start, self.month)
        self.assertEqual(
            units.projected_total,
            units.actual
            + units.actual_through_prior_day
            / Decimal(result.completed_selling_days)
            * Decimal(result.future_selling_days),
        )
        self.assertNotEqual(
            units.projected_total,
            units.projected_total.quantize(Decimal('0.01')),
        )

    def test_all_dates_closed_leave_actuals_but_no_pace_or_projection(self):
        self.goal()
        self.sale(sale_date=self.as_of)
        end = date(2028, 2, 29)
        closures = {
            self.month + timedelta(days=offset)
            for offset in range((end - self.month).days + 1)
            if (self.month + timedelta(days=offset)).weekday() != 6
        }
        result = self.calculate(calendar=self.calendar(closures=closures))
        units = self.metric(result, 'units')

        self.assertEqual(result.total_selling_days, 0)
        self.assertEqual(units.actual, Decimal('1.0'))
        self.assertIsNone(units.projected_total)
        self.assertIsNone(units.required_pace)
        self.assertEqual(units.status, 'insufficient_data')
        self.assertEqual(units.diagnostic_code, 'no_selling_days')

    def test_legacy_adjustment_and_period_bonus_are_authoritative_and_not_duplicated(self):
        self.goal(commission='1000')
        BonusLevel.objects.create(
            user=self.user,
            commission=self.commission,
            count_threshold=2,
            amount=Decimal('50.00'),
            active=True,
        )
        CommissionAdjustment.objects.create(
            user=self.user,
            commission=self.commission,
            description='Existing adjustment',
            kind=CommissionAdjustment.BONUS,
            amount=Decimal('7.00'),
            active=True,
        )
        self.sale(sale_date=date(2028, 2, 5))
        self.sale(sale_date=self.as_of)
        commission = self.metric(self.calculate(), 'commission')

        self.assertEqual(commission.actual_through_prior_day, Decimal('17.000'))
        self.assertEqual(commission.actual, Decimal('77.000'))
        self.assertEqual(
            commission.projected_total,
            Decimal('77') + Decimal('17') / Decimal('8') * Decimal('16'),
        )

    def test_v2_authoritative_commission_and_period_bonus_are_counted_once(self):
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system'])
        PayPlanAssignment.objects.filter(user=self.user).delete()
        industry = Industry.objects.create(name='Coach V2', slug='coach-v2')
        plan = PayPlan.objects.create(
            industry=industry,
            owner_user=self.user,
            name='Coach plan',
            is_active=True,
        )
        version = PayPlanVersion.objects.create(
            pay_plan=plan,
            version_name='v1',
            status=PayPlanVersion.ACTIVE,
            effective_start_date=self.month,
        )
        PayPlanAssignment.objects.create(
            user=self.user,
            pay_plan_version=version,
            effective_start_date=self.month,
            is_active=True,
        )
        PayPlanRule.objects.create(
            pay_plan_version=version,
            name='Ten percent front',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.10', 'gross_field': 'front_end_gross'},
            is_active=True,
            sort_order=1,
        )
        PayPlanRule.objects.create(
            pay_plan_version=version,
            name='Two unit bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tier_mode': 'highest_only',
                'tiers': [{
                    'minimum_units': '2',
                    'maximum_units': None,
                    'amount': '100.00',
                }],
            },
            is_active=True,
            sort_order=2,
        )
        self.goal(commission='500')
        self.sale(sale_date=date(2028, 2, 5))
        self.sale(sale_date=self.as_of)

        commission = self.metric(self.calculate(), 'commission')
        self.assertEqual(commission.actual_through_prior_day, Decimal('10.00'))
        self.assertEqual(commission.actual, Decimal('120.00'))

    def test_commission_service_receives_only_real_records_through_each_cutoff(self):
        self.goal()
        self.sale(sale_date=date(2028, 2, 5))
        self.sale(sale_date=self.as_of)
        future = self.sale(sale_date=date(2028, 2, 15))

        with patch(
            'SalesLogApp.stew_coach_projection.reporting_commission_totals',
            wraps=reporting_commission_totals,
        ) as authoritative:
            self.calculate()

        self.assertEqual(authoritative.call_count, 2)
        for call in authoritative.call_args_list:
            records = tuple(call.args[1])
            self.assertTrue(records)
            self.assertTrue(all(record.date <= self.as_of for record in records))
            self.assertNotIn(future, records)

    def test_unavailable_archive_commission_does_not_disable_units_or_gross(self):
        self.goal(units='2', gross='150', commission='25')
        ArchivedSale.objects.create(
            user=self.user,
            customer='Archived projection customer',
            dealNumber=879999,
            count=Decimal('2.0'),
            frontEnd=Decimal('100.00'),
            backend=Decimal('50.00'),
            date=date(2028, 2, 5),
        )
        result = self.calculate(as_of=date(2028, 3, 1))

        self.assertEqual(self.metric(result, 'units').actual, Decimal('2.0'))
        self.assertEqual(
            self.metric(result, 'total_gross').actual,
            Decimal('150.00'),
        )
        commission = self.metric(result, 'commission')
        self.assertIsNone(commission.actual)
        self.assertIsNone(commission.actual_through_prior_day)
        self.assertIsNone(commission.projected_total)
        self.assertEqual(commission.status, 'insufficient_data')
        self.assertEqual(
            commission.diagnostic_code,
            'archive_snapshot_unavailable',
        )


class StewCoachProjectionSecurityAndPerformanceTests(TestCase):
    month = date(2028, 2, 1)
    as_of = date(2028, 2, 10)

    def setUp(self):
        self.user = User.objects.create_user('secure-coach', password='pw')
        self.other = User.objects.create_user('secure-other', password='pw')
        Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.10'),
            total_calculated_back_end=Decimal('0.10'),
        )
        Commission.objects.create(
            user=self.other,
            total_calculated_front_end=Decimal('0.99'),
            total_calculated_back_end=Decimal('0.99'),
        )
        self.calendar = StaticSellingDayCalendar.for_owner(
            self.user,
            closure_dates=(),
            calendar_version='secure.v1',
        )

    def calculate(self, owner=None, calendar=None):
        return StewCoachProjectionService.calculate(
            owner=owner or self.user,
            month_start=self.month,
            as_of_date=self.as_of,
            calendar=calendar or self.calendar,
        )

    def test_basic_is_denied_and_pro_staff_and_superuser_are_allowed(self):
        with patch(
            'SalesLogApp.stew_coach_projection.activity_goals_authorized',
            return_value=False,
        ):
            with self.assertRaises(StewCoachAccessDenied):
                self.calculate()
        with patch(
            'SalesLogApp.stew_coach_projection.activity_goals_authorized',
            return_value=True,
        ):
            self.assertEqual(self.calculate().calendar_version, 'secure.v1')

        for username, attributes in (
            ('coach-staff', {'is_staff': True}),
            ('coach-super', {'is_superuser': True}),
        ):
            internal = User.objects.create_user(username, password='pw', **attributes)
            internal_calendar = StaticSellingDayCalendar.for_owner(
                internal,
                closure_dates=(),
                calendar_version='internal.v1',
            )
            self.assertEqual(
                self.calculate(owner=internal, calendar=internal_calendar).calendar_version,
                'internal.v1',
            )

    def test_anonymous_invalid_dates_and_foreign_calendar_are_rejected(self):
        with self.assertRaises(StewCoachAccessDenied):
            StewCoachProjectionService.calculate(
                owner=AnonymousUser(),
                month_start=self.month,
                as_of_date=self.as_of,
                calendar=self.calendar,
            )
        with patch(
            'SalesLogApp.stew_coach_projection.activity_goals_authorized',
            return_value=True,
        ):
            with self.assertRaises(StewCoachInputError):
                StewCoachProjectionService.calculate(
                    owner=self.user,
                    month_start='2028-02',
                    as_of_date=self.as_of,
                    calendar=self.calendar,
                )
            with self.assertRaises(SellingDayCalendarError):
                self.calculate(
                    calendar=StaticSellingDayCalendar.for_owner(
                        self.other,
                        closure_dates=(),
                        calendar_version='foreign.v1',
                    )
                )

    @patch(
        'SalesLogApp.stew_coach_projection.activity_goals_authorized',
        return_value=True,
    )
    def test_team_membership_does_not_cross_owner_data(self, _authorized):
        MonthlyGoal.objects.create(
            user=self.user,
            month_start=self.month,
            target_units=Decimal('2.0'),
            target_total_gross=Decimal('200.00'),
            target_commission=Decimal('20.00'),
        )
        MonthlyGoal.objects.create(
            user=self.other,
            month_start=self.month,
            target_units=Decimal('99.0'),
            target_total_gross=Decimal('99999.00'),
            target_commission=Decimal('9999.00'),
        )
        Sale.objects.create(
            user=self.user,
            customer='Owned fact',
            dealNumber=880001,
            count=Decimal('1.0'),
            frontEnd=Decimal('100.00'),
            backend=Decimal('0.00'),
            date=date(2028, 2, 5),
        )
        Sale.objects.create(
            user=self.other,
            customer='Private foreign fact',
            dealNumber=880002,
            count=Decimal('2.0'),
            frontEnd=Decimal('9999.00'),
            backend=Decimal('9999.00'),
            date=date(2028, 2, 5),
        )
        ArchivedSale.objects.create(
            user=self.other,
            customer='Private archive',
            dealNumber=880003,
            count=Decimal('2.0'),
            frontEnd=Decimal('9999.00'),
            backend=Decimal('9999.00'),
            date=date(2028, 2, 5),
        )
        PayPlanEligibility.objects.create(
            user=self.other,
            month_start=self.month,
        )
        team = Team.objects.create(name='No projection sharing', owner=self.other)
        TeamMembership.objects.create(
            team=team,
            user=self.user,
            role=TeamMembership.MEMBER,
            status=TeamMembership.ACTIVE,
        )

        result = self.calculate()
        self.assertEqual(result.metrics[0].goal, Decimal('2.0'))
        self.assertEqual(result.metrics[0].actual, Decimal('1.0'))
        self.assertEqual(result.metrics[1].actual, Decimal('100.00'))
        rendered = repr(result)
        self.assertNotIn('Private foreign fact', rendered)
        self.assertNotIn('880002', rendered)
        self.assertNotIn('9999', rendered)

    @patch(
        'SalesLogApp.stew_coach_projection.activity_goals_authorized',
        return_value=True,
    )
    def test_public_api_accepts_no_foreign_object_identifiers(self, _authorized):
        parameters = inspect.signature(
            StewCoachProjectionService.calculate
        ).parameters
        self.assertEqual(
            tuple(parameters),
            ('owner', 'month_start', 'as_of_date', 'calendar'),
        )
        with self.assertRaises(TypeError):
            StewCoachProjectionService.calculate(
                owner=self.user,
                month_start=self.month,
                as_of_date=self.as_of,
                calendar=self.calendar,
                goal_id=123,
            )

    def test_missing_profile_is_not_created_for_read_only_staff_calculation(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        UserProfile.objects.filter(user=self.user).delete()
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())

        self.calculate()

        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())

    @patch(
        'SalesLogApp.stew_coach_projection.activity_goals_authorized',
        return_value=True,
    )
    def test_calculation_performs_no_writes_and_emits_no_sensitive_logs(
        self, _authorized,
    ):
        MonthlyGoal.objects.create(
            user=self.user,
            month_start=self.month,
            target_units=Decimal('2.0'),
            target_total_gross=Decimal('200.00'),
            target_commission=Decimal('20.00'),
        )
        Sale.objects.create(
            user=self.user,
            customer='Never log this customer',
            dealNumber=880004,
            count=Decimal('1.0'),
            frontEnd=Decimal('100.00'),
            backend=Decimal('0.00'),
            date=date(2028, 2, 5),
        )
        before = {
            'goals': MonthlyGoal.objects.count(),
            'sales': Sale.objects.count(),
            'archives': ArchivedSale.objects.count(),
            'profiles': UserProfile.objects.count(),
            'assignments': PayPlanAssignment.objects.count(),
            'eligibility': PayPlanEligibility.objects.count(),
        }
        with patch.object(logging.Logger, '_log') as log_call:
            with CaptureQueriesContext(connection) as queries:
                result = self.calculate()
        writes = [
            query['sql'] for query in queries
            if query['sql'].lstrip().upper().startswith(
                ('INSERT', 'UPDATE', 'DELETE', 'REPLACE')
            )
        ]
        self.assertEqual(writes, [])
        self.assertEqual(before, {
            'goals': MonthlyGoal.objects.count(),
            'sales': Sale.objects.count(),
            'archives': ArchivedSale.objects.count(),
            'profiles': UserProfile.objects.count(),
            'assignments': PayPlanAssignment.objects.count(),
            'eligibility': PayPlanEligibility.objects.count(),
        })
        log_call.assert_not_called()
        self.assertNotIn('Never log this customer', repr(result))
        self.assertNotIn('880004', repr(result))

    @patch(
        'SalesLogApp.stew_coach_projection.reporting_commission_totals',
        return_value={
            'commission_complete': True,
            'commission_source': 'live_sales',
            'commission_diagnostic': '',
            'total': Decimal('0'),
        },
    )
    @patch(
        'SalesLogApp.stew_coach_projection.activity_goals_authorized',
        return_value=True,
    )
    def test_owner_data_query_count_is_constant_for_one_and_many_sales(
        self, _authorized, _commission,
    ):
        MonthlyGoal.objects.create(
            user=self.user,
            month_start=self.month,
            target_units=Decimal('100.0'),
            target_total_gross=Decimal('10000.00'),
            target_commission=Decimal('1000.00'),
        )

        def add_sale(deal):
            Sale.objects.create(
                user=self.user,
                customer=f'Query {deal}',
                dealNumber=deal,
                count=Decimal('1.0'),
                frontEnd=Decimal('100.00'),
                backend=Decimal('0.00'),
                date=date(2028, 2, 5),
            )

        add_sale(881000)
        with CaptureQueriesContext(connection) as one_queries:
            self.calculate()
        for offset in range(1, 20):
            add_sale(881000 + offset)
        with CaptureQueriesContext(connection) as many_queries:
            self.calculate()

        self.assertEqual(len(one_queries), len(many_queries))
        self.assertEqual(len(one_queries), 3)
