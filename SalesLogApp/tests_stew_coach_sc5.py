from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Commission, DailyActivity, StewCoachNudgeDismissal
from .models.nudges import (
    NUDGE_BEHIND_PACE,
    NUDGE_LOG_ACTIVITY,
    NUDGE_MONTH_END_PUSH,
    NUDGE_SET_GOALS,
)
from .stew_coach_nudges import (
    MAX_VISIBLE_NUDGES,
    active_nudges,
    candidate_nudges,
)
from .stew_coach_presentation import (
    present_projection,
    unavailable_projection_context,
)
from .stew_coach_projection import MetricProjection, StewCoachProjectionResult


def _metric(metric_id, **overrides):
    fields = {
        'goal': None,
        'actual': None,
        'actual_through_prior_day': None,
        'remaining': None,
        'progress_percent': None,
        'projected_total': None,
        'required_pace': None,
        'status': 'no_goal',
        'diagnostic_code': None,
    }
    fields.update(overrides)
    return MetricProjection(metric_id=metric_id, **fields)


def _result(**overrides):
    fields = {
        'calculation_version': 'sc2.v1',
        'projection_method': 'completed_day_run_rate',
        'calendar_version': 'owner-closures.v1.1.abcdefabcdef',
        'month_start': date(2028, 3, 1),
        'month_end': date(2028, 3, 31),
        'requested_as_of_date': date(2028, 3, 15),
        'effective_cutoff_date': date(2028, 3, 14),
        'period_status': 'in_progress',
        'total_selling_days': 27,
        'completed_selling_days': 12,
        'remaining_selling_days': 15,
        'future_selling_days': 15,
        'metrics': (),
    }
    fields.update(overrides)
    return StewCoachProjectionResult(**fields)


def _behind_metrics():
    return (
        _metric(
            'units',
            goal=Decimal('20'),
            actual=Decimal('10'),
            remaining=Decimal('10'),
            progress_percent=Decimal('50'),
            projected_total=Decimal('18.5'),
            required_pace=Decimal('0.667'),
            status='behind',
        ),
        _metric(
            'total_gross',
            goal=Decimal('40000'),
            actual=Decimal('25000'),
            remaining=Decimal('15000'),
            progress_percent=Decimal('62.5'),
            projected_total=Decimal('56250'),
            required_pace=Decimal('1000'),
            status='on_pace',
        ),
        _metric('commission', actual=Decimal('2500'), status='no_goal'),
    )


def _presentation(**overrides):
    metrics = overrides.pop('metrics', _behind_metrics())
    return present_projection(_result(metrics=metrics, **overrides))


def _no_goal_metrics():
    return (
        _metric('units', actual=Decimal('10'), status='no_goal'),
        _metric('total_gross', actual=Decimal('25000'), status='no_goal'),
        _metric('commission', actual=Decimal('2500'), status='no_goal'),
    )


class CandidateNudgeTests(SimpleTestCase):
    def test_behind_metric_produces_behind_pace_warning(self):
        nudges = candidate_nudges(_presentation(), has_activity=True)
        self.assertEqual([nudge.key for nudge in nudges], [NUDGE_BEHIND_PACE])
        nudge = nudges[0]
        self.assertEqual(nudge.level, 'warning')
        self.assertEqual(
            nudge.message,
            'Units behind pace for March 2028. Open Activity & Goals to '
            'see the pace you need.',
        )

    def test_month_end_push_replaces_behind_pace_when_few_days_remain(self):
        nudges = candidate_nudges(
            _presentation(remaining_selling_days=4, future_selling_days=4,
                          completed_selling_days=23),
            has_activity=True,
        )
        self.assertEqual(
            [nudge.key for nudge in nudges], [NUDGE_MONTH_END_PUSH],
        )
        self.assertEqual(
            nudges[0].message,
            'Only 4 selling days left in March 2028. Units still behind '
            'pace — finish strong.',
        )

    def test_month_end_push_uses_singular_day(self):
        nudges = candidate_nudges(
            _presentation(remaining_selling_days=1, future_selling_days=1,
                          completed_selling_days=26),
            has_activity=True,
        )
        self.assertIn('Only 1 selling day left', nudges[0].message)

    def test_no_pace_nudge_when_no_selling_days_remain(self):
        nudges = candidate_nudges(
            _presentation(remaining_selling_days=0, future_selling_days=0,
                          completed_selling_days=27),
            has_activity=True,
        )
        self.assertEqual(nudges, ())

    def test_two_behind_metrics_joined_naturally(self):
        metrics = (
            _metric('units', goal=Decimal('20'), actual=Decimal('5'),
                    remaining=Decimal('15'), progress_percent=Decimal('25'),
                    projected_total=Decimal('11'),
                    required_pace=Decimal('1'), status='behind'),
            _metric('total_gross', goal=Decimal('40000'),
                    actual=Decimal('10000'), remaining=Decimal('30000'),
                    progress_percent=Decimal('25'),
                    projected_total=Decimal('22500'),
                    required_pace=Decimal('2000'), status='behind'),
            _metric('commission', actual=Decimal('2500'), status='no_goal'),
        )
        nudges = candidate_nudges(
            _presentation(metrics=metrics), has_activity=True,
        )
        self.assertIn('Units and total gross behind pace', nudges[0].message)

    def test_all_no_goal_produces_set_goals_nudge(self):
        nudges = candidate_nudges(
            _presentation(metrics=_no_goal_metrics()), has_activity=True,
        )
        self.assertEqual([nudge.key for nudge in nudges], [NUDGE_SET_GOALS])
        self.assertEqual(nudges[0].level, 'info')
        self.assertIn('No goals are set for March 2028.', nudges[0].message)

    def test_missing_activity_produces_log_activity_nudge(self):
        nudges = candidate_nudges(
            _presentation(metrics=_no_goal_metrics()), has_activity=False,
        )
        self.assertEqual(
            [nudge.key for nudge in nudges],
            [NUDGE_SET_GOALS, NUDGE_LOG_ACTIVITY],
        )
        self.assertIn(
            'No daily activity is logged for March 2028.',
            nudges[1].message,
        )

    def test_mixed_statuses_do_not_produce_set_goals(self):
        nudges = candidate_nudges(_presentation(), has_activity=True)
        self.assertNotIn(
            NUDGE_SET_GOALS, [nudge.key for nudge in nudges],
        )

    def test_completed_period_produces_no_nudges(self):
        presentation = _presentation(
            period_status='complete',
            requested_as_of_date=date(2028, 4, 10),
            effective_cutoff_date=date(2028, 3, 31),
            remaining_selling_days=0,
            future_selling_days=0,
            completed_selling_days=27,
        )
        self.assertEqual(
            candidate_nudges(presentation, has_activity=False), (),
        )

    def test_unavailable_presentation_produces_no_nudges(self):
        presentation = unavailable_projection_context()
        self.assertEqual(
            candidate_nudges(presentation, has_activity=False), (),
        )

    def test_non_mapping_presentation_produces_no_nudges(self):
        self.assertEqual(candidate_nudges(None, has_activity=False), ())


def _past_presentation(**overrides):
    """March 2024 presentation so owner rows can use non-future dates."""

    fields = {
        'month_start': date(2024, 3, 1),
        'month_end': date(2024, 3, 31),
        'requested_as_of_date': date(2024, 3, 15),
        'effective_cutoff_date': date(2024, 3, 14),
    }
    fields.update(overrides)
    return _presentation(**fields)


class ActiveNudgeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', password='pw')
        self.other = User.objects.create_user('other', password='pw')
        self.month = date(2024, 3, 1)

    def test_missing_activity_detected_from_owner_rows(self):
        nudges = active_nudges(self.user, _past_presentation())
        self.assertIn(
            NUDGE_LOG_ACTIVITY, [nudge.key for nudge in nudges],
        )

    def test_activity_in_month_hides_log_activity_nudge(self):
        DailyActivity.objects.create(
            user=self.user, date=date(2024, 3, 10),
            leads_taken=3, phone_calls_made=5,
        )
        nudges = active_nudges(self.user, _past_presentation())
        self.assertNotIn(
            NUDGE_LOG_ACTIVITY, [nudge.key for nudge in nudges],
        )

    def test_other_user_activity_does_not_count(self):
        DailyActivity.objects.create(
            user=self.other, date=date(2024, 3, 10),
            leads_taken=3, phone_calls_made=5,
        )
        nudges = active_nudges(self.user, _past_presentation())
        self.assertIn(
            NUDGE_LOG_ACTIVITY, [nudge.key for nudge in nudges],
        )

    def test_dismissal_hides_nudge_for_owner_and_month(self):
        StewCoachNudgeDismissal.objects.create(
            user=self.user, nudge_key=NUDGE_BEHIND_PACE,
            month_start=self.month,
        )
        nudges = active_nudges(self.user, _past_presentation())
        self.assertNotIn(
            NUDGE_BEHIND_PACE, [nudge.key for nudge in nudges],
        )

    def test_other_user_dismissal_does_not_hide_nudge(self):
        StewCoachNudgeDismissal.objects.create(
            user=self.other, nudge_key=NUDGE_BEHIND_PACE,
            month_start=self.month,
        )
        nudges = active_nudges(self.user, _past_presentation())
        self.assertIn(
            NUDGE_BEHIND_PACE, [nudge.key for nudge in nudges],
        )

    def test_dismissal_for_other_month_does_not_hide_nudge(self):
        StewCoachNudgeDismissal.objects.create(
            user=self.user, nudge_key=NUDGE_BEHIND_PACE,
            month_start=date(2024, 2, 1),
        )
        nudges = active_nudges(self.user, _past_presentation())
        self.assertIn(
            NUDGE_BEHIND_PACE, [nudge.key for nudge in nudges],
        )

    def test_visible_nudges_capped(self):
        nudges = active_nudges(self.user, _past_presentation())
        self.assertLessEqual(len(nudges), MAX_VISIBLE_NUDGES)

    def test_anonymous_owner_gets_no_nudges(self):
        self.assertEqual(active_nudges(None, _past_presentation()), ())


class NudgeDismissalModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', password='pw')

    def test_mid_month_date_rejected(self):
        with self.assertRaises(ValidationError):
            StewCoachNudgeDismissal.objects.create(
                user=self.user, nudge_key=NUDGE_SET_GOALS,
                month_start=date(2028, 3, 15),
            )

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValidationError):
            StewCoachNudgeDismissal.objects.create(
                user=self.user, nudge_key='surprise_key',
                month_start=date(2028, 3, 1),
            )

    def test_duplicate_dismissal_rejected(self):
        StewCoachNudgeDismissal.objects.create(
            user=self.user, nudge_key=NUDGE_SET_GOALS,
            month_start=date(2028, 3, 1),
        )
        with self.assertRaises(ValidationError):
            StewCoachNudgeDismissal.objects.create(
                user=self.user, nudge_key=NUDGE_SET_GOALS,
                month_start=date(2028, 3, 1),
            )


class NudgeViewTests(TestCase):
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
        self.dismiss_url = reverse('dismiss_stew_nudge')
        self.month = timezone.localdate().replace(day=1)

    def test_activity_goals_shows_log_activity_nudge(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No daily activity is logged for')
        self.assertContains(response, self.dismiss_url)

    def test_dismiss_hides_nudge_and_redirects_back(self):
        response = self.client.post(self.dismiss_url, {
            'nudge_key': NUDGE_LOG_ACTIVITY,
            'month': self.month.strftime('%Y-%m'),
        })
        self.assertRedirects(
            response,
            f'{self.url}?month={self.month:%Y-%m}',
            fetch_redirect_response=False,
        )
        dismissal = StewCoachNudgeDismissal.objects.get(user=self.user)
        self.assertEqual(dismissal.nudge_key, NUDGE_LOG_ACTIVITY)
        self.assertEqual(dismissal.month_start, self.month)
        follow_up = self.client.get(self.url)
        self.assertNotContains(
            follow_up, 'No daily activity is logged for',
        )

    def test_repeat_dismissal_is_idempotent(self):
        payload = {
            'nudge_key': NUDGE_LOG_ACTIVITY,
            'month': self.month.strftime('%Y-%m'),
        }
        self.client.post(self.dismiss_url, payload)
        response = self.client.post(self.dismiss_url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            StewCoachNudgeDismissal.objects.filter(user=self.user).count(),
            1,
        )

    def test_invalid_key_creates_nothing(self):
        response = self.client.post(self.dismiss_url, {
            'nudge_key': 'surprise_key',
            'month': self.month.strftime('%Y-%m'),
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(StewCoachNudgeDismissal.objects.exists())

    def test_dismiss_requires_post(self):
        response = self.client.get(self.dismiss_url)
        self.assertEqual(response.status_code, 405)

    def test_next_view_sales_redirects_to_dashboard(self):
        response = self.client.post(self.dismiss_url, {
            'nudge_key': NUDGE_LOG_ACTIVITY,
            'month': self.month.strftime('%Y-%m'),
            'next': 'view_sales',
        })
        self.assertRedirects(
            response,
            f"{reverse('view_sales')}?month={self.month:%Y-%m}",
            fetch_redirect_response=False,
        )

    def test_unexpected_next_value_falls_back_to_activity_goals(self):
        response = self.client.post(self.dismiss_url, {
            'nudge_key': NUDGE_LOG_ACTIVITY,
            'month': self.month.strftime('%Y-%m'),
            'next': 'https://evil.example.com/',
        })
        self.assertRedirects(
            response,
            f'{self.url}?month={self.month:%Y-%m}',
            fetch_redirect_response=False,
        )

    def test_dashboard_shows_nudges_for_pro_user(self):
        response = self.client.get(reverse('view_sales'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No daily activity is logged for')

    def test_dashboard_hides_nudges_without_pro_access(self):
        no_pro = patch(
            'SalesLogApp.billing_entitlements.get_billing_entitlement',
            return_value=SimpleNamespace(has_pro_access=False),
        )
        no_pro.start()
        self.addCleanup(no_pro.stop)
        response = self.client.get(reverse('view_sales'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, 'No daily activity is logged for',
        )

    def test_dismissals_are_owner_scoped_in_views(self):
        StewCoachNudgeDismissal.objects.create(
            user=self.other, nudge_key=NUDGE_LOG_ACTIVITY,
            month_start=self.month,
        )
        response = self.client.get(self.url)
        self.assertContains(response, 'No daily activity is logged for')
