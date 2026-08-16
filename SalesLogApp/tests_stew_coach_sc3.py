from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import SellingDayClosureForm
from .models import Commission, MonthlyGoal, SellingDayClosure
from .selling_calendar import SellingDayCalendarError, StaticSellingDayCalendar
from .stew_coach_calendar import CALENDAR_SOURCE_VERSION, owner_selling_calendar
from .stew_coach_presentation import (
    CALENDAR_UNAVAILABLE_MESSAGE,
    DIAGNOSTIC_MESSAGES,
    STATUS_BADGE_CLASSES,
    STATUS_LABELS,
    UNAVAILABLE_DISPLAY,
    present_projection,
    unavailable_projection_context,
)
from .stew_coach_projection import StewCoachProjectionService

VERSION_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,63}')


def _first_open_weekday(month_start: date) -> date:
    candidate = month_start
    while candidate.weekday() == 6:
        candidate += timedelta(days=1)
    return candidate


class SellingDayClosureModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', password='pw')

    def test_duplicate_closure_dates_are_rejected(self):
        closure_date = date(2028, 3, 6)
        SellingDayClosure.objects.create(user=self.user, date=closure_date)
        with self.assertRaises(ValidationError):
            SellingDayClosure.objects.create(user=self.user, date=closure_date)

    def test_same_date_is_allowed_for_different_owners(self):
        other = User.objects.create_user('other', password='pw')
        closure_date = date(2028, 3, 6)
        SellingDayClosure.objects.create(user=self.user, date=closure_date)
        SellingDayClosure.objects.create(user=other, date=closure_date)
        self.assertEqual(SellingDayClosure.objects.count(), 2)

    def test_sunday_closures_are_rejected(self):
        with self.assertRaises(ValidationError):
            SellingDayClosure.objects.create(
                user=self.user, date=date(2028, 3, 5),
            )

    def test_form_rejects_sundays_with_a_clear_message(self):
        form = SellingDayClosureForm(data={'date': date(2028, 3, 5)})
        self.assertFalse(form.is_valid())
        self.assertIn(
            'Sundays are always closed and cannot be added.',
            form.errors['date'],
        )

    def test_closures_are_ordered_by_date(self):
        later = SellingDayClosure.objects.create(
            user=self.user, date=date(2028, 3, 10),
        )
        earlier = SellingDayClosure.objects.create(
            user=self.user, date=date(2028, 3, 6),
        )
        self.assertEqual(
            list(SellingDayClosure.objects.all()), [earlier, later],
        )


class OwnerSellingCalendarTests(TestCase):
    month_start = date(2028, 3, 1)
    month_end = date(2028, 3, 31)

    def setUp(self):
        self.user = User.objects.create_user('owner', password='pw')
        self.other = User.objects.create_user('other', password='pw')

    def build(self):
        return owner_selling_calendar(
            self.user,
            month_start=self.month_start,
            month_end=self.month_end,
        )

    def test_returns_an_owner_bound_static_calendar(self):
        SellingDayClosure.objects.create(user=self.user, date=date(2028, 3, 6))
        calendar = self.build()
        self.assertIsInstance(calendar, StaticSellingDayCalendar)
        self.assertEqual(calendar.owner_id, self.user.pk)
        self.assertEqual(calendar.closure_dates, frozenset({date(2028, 3, 6)}))

    def test_only_in_range_owner_closures_are_loaded(self):
        SellingDayClosure.objects.create(user=self.user, date=date(2028, 3, 6))
        SellingDayClosure.objects.create(user=self.user, date=date(2028, 4, 3))
        SellingDayClosure.objects.create(user=self.other, date=date(2028, 3, 7))
        calendar = self.build()
        self.assertEqual(calendar.closure_dates, frozenset({date(2028, 3, 6)}))

    def test_version_is_valid_deterministic_and_change_sensitive(self):
        first = self.build().calendar_version
        self.assertTrue(VERSION_PATTERN.fullmatch(first))
        self.assertLessEqual(len(first), 64)
        self.assertTrue(first.startswith(CALENDAR_SOURCE_VERSION))
        self.assertEqual(first, self.build().calendar_version)
        SellingDayClosure.objects.create(user=self.user, date=date(2028, 3, 6))
        changed = self.build().calendar_version
        self.assertNotEqual(first, changed)
        self.assertTrue(VERSION_PATTERN.fullmatch(changed))

    def test_versions_differ_between_owners_with_identical_closures(self):
        other_calendar = owner_selling_calendar(
            self.other,
            month_start=self.month_start,
            month_end=self.month_end,
        )
        self.assertNotEqual(
            self.build().calendar_version, other_calendar.calendar_version,
        )

    def test_uses_a_single_owner_scoped_query(self):
        SellingDayClosure.objects.create(user=self.user, date=date(2028, 3, 6))
        with self.assertNumQueries(1):
            self.build()

    def test_invalid_owner_fails_closed(self):
        for owner in (None, AnonymousUser(), SimpleNamespace(pk=3)):
            with self.assertRaises(SellingDayCalendarError):
                owner_selling_calendar(
                    owner,
                    month_start=self.month_start,
                    month_end=self.month_end,
                )

    def test_invalid_boundaries_fail_closed(self):
        with self.assertRaises(SellingDayCalendarError):
            owner_selling_calendar(
                self.user,
                month_start=self.month_end,
                month_end=self.month_start,
            )
        with self.assertRaises(SellingDayCalendarError):
            owner_selling_calendar(
                self.user,
                month_start='2028-03-01',
                month_end=self.month_end,
            )

    def test_feeds_the_projection_engine_selling_day_counts(self):
        with patch(
            'SalesLogApp.billing_entitlements.get_billing_entitlement',
            return_value=SimpleNamespace(has_pro_access=True),
        ):
            before = StewCoachProjectionService.calculate(
                owner=self.user,
                month_start=self.month_start,
                as_of_date=date(2028, 3, 15),
                calendar=self.build(),
            )
            SellingDayClosure.objects.create(
                user=self.user, date=date(2028, 3, 6),
            )
            after = StewCoachProjectionService.calculate(
                owner=self.user,
                month_start=self.month_start,
                as_of_date=date(2028, 3, 15),
                calendar=self.build(),
            )
        self.assertEqual(
            after.total_selling_days, before.total_selling_days - 1,
        )
        self.assertNotEqual(before.calendar_version, after.calendar_version)


class PresentationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', password='pw')
        Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('.10'),
            total_calculated_back_end=Decimal('.10'),
        )
        self.entitlement_patch = patch(
            'SalesLogApp.billing_entitlements.get_billing_entitlement',
            return_value=SimpleNamespace(has_pro_access=True),
        )
        self.entitlement_patch.start()
        self.addCleanup(self.entitlement_patch.stop)

    def result(self, *, month_start, as_of_date):
        calendar = owner_selling_calendar(
            self.user,
            month_start=month_start,
            month_end=(month_start.replace(day=28) + timedelta(days=4)).replace(
                day=1
            ) - timedelta(days=1),
        )
        return StewCoachProjectionService.calculate(
            owner=self.user,
            month_start=month_start,
            as_of_date=as_of_date,
            calendar=calendar,
        )

    def test_rows_follow_metric_order_with_rounded_displays(self):
        MonthlyGoal.objects.create(
            user=self.user,
            month_start=date(2028, 3, 1),
            target_units=Decimal('12.5'),
            target_total_gross=Decimal('45000'),
            target_commission=Decimal('6000'),
        )
        result = self.result(
            month_start=date(2028, 3, 1), as_of_date=date(2028, 3, 15),
        )
        presented = present_projection(result)
        self.assertTrue(presented['available'])
        labels = [row['label'] for row in presented['rows']]
        self.assertEqual(labels, ['Units', 'Total gross', 'Commission'])
        units_row = presented['rows'][0]
        gross_row = presented['rows'][1]
        self.assertEqual(units_row['goal'], '12.5')
        self.assertEqual(units_row['actual'], '0.0')
        self.assertEqual(gross_row['goal'], '$45,000.00')
        self.assertEqual(gross_row['remaining'], '$45,000.00')
        self.assertEqual(gross_row['progress_percent'], '0.0%')

    def test_unavailable_values_render_as_a_placeholder(self):
        result = self.result(
            month_start=date(2028, 3, 1), as_of_date=date(2028, 3, 15),
        )
        presented = present_projection(result)
        for row in presented['rows']:
            self.assertEqual(row['status'], 'no_goal')
            self.assertEqual(row['status_label'], STATUS_LABELS['no_goal'])
            self.assertEqual(
                row['badge_class'], STATUS_BADGE_CLASSES['no_goal'],
            )
            self.assertEqual(row['remaining'], UNAVAILABLE_DISPLAY)
            self.assertEqual(row['required_pace'], UNAVAILABLE_DISPLAY)

    def test_presentation_does_not_change_engine_values(self):
        result = self.result(
            month_start=date(2028, 3, 1), as_of_date=date(2028, 3, 15),
        )
        before = tuple(
            (metric.actual, metric.projected_total, metric.progress_percent)
            for metric in result.metrics
        )
        present_projection(result)
        after = tuple(
            (metric.actual, metric.projected_total, metric.progress_percent)
            for metric in result.metrics
        )
        self.assertEqual(before, after)

    def test_future_month_diagnostic_is_translated(self):
        next_year = timezone.localdate().year + 1
        result = self.result(
            month_start=date(next_year, 3, 1),
            as_of_date=timezone.localdate(),
        )
        presented = present_projection(result)
        self.assertIn(
            DIAGNOSTIC_MESSAGES['future_period'], presented['diagnostics'],
        )
        self.assertEqual(len(presented['diagnostics']), 1)

    def test_unavailable_context_fails_closed(self):
        context = unavailable_projection_context()
        self.assertFalse(context['available'])
        self.assertEqual(context['message'], CALENDAR_UNAVAILABLE_MESSAGE)
        self.assertEqual(context['rows'], ())


class ActivityGoalsPageTests(TestCase):
    def setUp(self):
        self.entitlement_patch = patch(
            'SalesLogApp.billing_entitlements.get_billing_entitlement',
            return_value=SimpleNamespace(has_pro_access=True),
        )
        self.entitlement_patch.start()
        self.addCleanup(self.entitlement_patch.stop)
        self.user = User.objects.create_user('owner', password='pw')
        self.other = User.objects.create_user('other', password='pw')
        Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('.10'),
            total_calculated_back_end=Decimal('.10'),
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse('activity_goals')
        self.today = timezone.localdate()
        self.month = self.today.replace(day=1)
        self.open_day = _first_open_weekday(self.month)

    def test_page_renders_projection_and_calendar_sections(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Stew Coach month projection')
        self.assertContains(response, 'Selling calendar')
        stew_coach = response.context['stew_coach']
        self.assertTrue(stew_coach['available'])
        self.assertEqual(len(stew_coach['rows']), 3)

    def test_closure_add_is_owner_bound_and_redirects(self):
        response = self.client.post(self.url, {
            'form_type': 'closure',
            'month': self.month.strftime('%Y-%m'),
            'date': self.open_day.isoformat(),
            'label': 'Inventory day',
            'user': self.other.pk,
        })
        self.assertEqual(response.status_code, 302)
        closure = SellingDayClosure.objects.get()
        self.assertEqual(closure.user, self.user)
        self.assertEqual(closure.date, self.open_day)
        follow = self.client.get(self.url)
        self.assertContains(follow, 'Inventory day')

    def test_sunday_closure_is_rejected(self):
        sunday = self.month
        while sunday.weekday() != 6:
            sunday += timedelta(days=1)
        response = self.client.post(self.url, {
            'form_type': 'closure',
            'month': self.month.strftime('%Y-%m'),
            'date': sunday.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, 'Sundays are always closed and cannot be added.',
        )
        self.assertFalse(SellingDayClosure.objects.exists())

    def test_duplicate_closure_shows_an_error_without_a_second_row(self):
        SellingDayClosure.objects.create(user=self.user, date=self.open_day)
        response = self.client.post(self.url, {
            'form_type': 'closure',
            'month': self.month.strftime('%Y-%m'),
            'date': self.open_day.isoformat(),
        }, follow=True)
        self.assertContains(
            response, 'already on your selling calendar',
        )
        self.assertEqual(SellingDayClosure.objects.count(), 1)

    def test_closures_reduce_projection_selling_days(self):
        baseline = self.client.get(self.url).context['stew_coach']
        SellingDayClosure.objects.create(user=self.user, date=self.open_day)
        updated = self.client.get(self.url).context['stew_coach']
        self.assertEqual(
            updated['total_selling_days'],
            baseline['total_selling_days'] - 1,
        )

    def test_closure_delete_only_removes_owned_rows(self):
        own = SellingDayClosure.objects.create(
            user=self.user, date=self.open_day,
        )
        foreign = SellingDayClosure.objects.create(
            user=self.other, date=self.open_day,
        )
        response = self.client.post(self.url, {
            'form_type': 'closure_delete',
            'month': self.month.strftime('%Y-%m'),
            'closure_id': foreign.pk,
        }, follow=True)
        self.assertContains(response, 'could not be removed')
        self.assertTrue(
            SellingDayClosure.objects.filter(pk=foreign.pk).exists(),
        )
        response = self.client.post(self.url, {
            'form_type': 'closure_delete',
            'month': self.month.strftime('%Y-%m'),
            'closure_id': own.pk,
        }, follow=True)
        self.assertContains(response, 'Selling-day closure removed.')
        self.assertFalse(SellingDayClosure.objects.filter(pk=own.pk).exists())

    def test_malformed_closure_delete_ids_are_handled(self):
        response = self.client.post(self.url, {
            'form_type': 'closure_delete',
            'month': self.month.strftime('%Y-%m'),
            'closure_id': 'not-a-number',
        }, follow=True)
        self.assertContains(response, 'could not be removed')

    def test_calendar_failure_fails_closed_without_breaking_the_page(self):
        with patch(
            'SalesLogApp.views.owner_selling_calendar',
            side_effect=SellingDayCalendarError('bad calendar'),
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, CALENDAR_UNAVAILABLE_MESSAGE)
        self.assertFalse(response.context['stew_coach']['available'])

    def test_basic_users_cannot_manage_closures(self):
        self.entitlement_patch.stop()
        denied_patch = patch(
            'SalesLogApp.billing_entitlements.get_billing_entitlement',
            return_value=SimpleNamespace(has_pro_access=False),
        )
        denied_patch.start()
        response = self.client.post(self.url, {
            'form_type': 'closure',
            'month': self.month.strftime('%Y-%m'),
            'date': self.open_day.isoformat(),
        })
        denied_patch.stop()
        self.entitlement_patch.start()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(SellingDayClosure.objects.exists())
