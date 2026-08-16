from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .ask_stew_provider import FACT_SENTENCE_BOUNDARY, AskStewProviderResult
from .models import Commission, MonthlyGoal
from .stew_coach_phrasing import (
    COACH_INTENT,
    DUPLICATE_NOTICE,
    RATE_LIMITED_NOTICE,
    UNAVAILABLE_NOTICE,
    StewCoachMessage,
    StewCoachPhrasingError,
    coach_sentences,
    deterministic_coach_message,
    phrase_coach_message,
)
from .stew_coach_presentation import (
    DIAGNOSTIC_MESSAGES,
    present_projection,
    unavailable_projection_context,
)
from .stew_coach_projection import MetricProjection, StewCoachProjectionResult
from .views import _new_coach_phrase_token


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


def _presentation(**overrides):
    metrics = overrides.pop('metrics', (
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
    ))
    return present_projection(_result(metrics=metrics, **overrides))


class CoachSentenceTests(SimpleTestCase):
    def test_in_progress_sentences_cover_period_and_metrics(self):
        sentences = coach_sentences(_presentation())
        joined = ' '.join(sentences)
        self.assertIn(
            'You have completed 12 of 27 selling days in March 2028, with '
            '15 remaining.',
            sentences,
        )
        self.assertIn(
            'Units behind pace: 10.0 recorded so far toward a goal of 20.0, '
            'projected to finish at 18.5.',
            sentences,
        )
        self.assertIn(
            'Averaging 0.7 per remaining selling day reaches the units goal.',
            sentences,
        )
        self.assertIn(
            'Total gross on pace: $25,000.00 recorded so far toward a goal '
            'of $40,000.00, projected to finish at $56,250.00.',
            sentences,
        )
        self.assertIn('No goal is set for commission this month.', sentences)
        self.assertNotIn('2500', joined)

    def test_sentences_survive_provider_fact_splitting(self):
        sentences = coach_sentences(_presentation())
        joined = ' '.join(sentences)
        round_trip = tuple(
            sentence.strip()
            for sentence in FACT_SENTENCE_BOUNDARY.split(joined)
            if sentence.strip()
        )
        self.assertEqual(round_trip, sentences)
        self.assertEqual(deterministic_coach_message(_presentation()), joined)

    def test_goal_reached_wording(self):
        sentences = coach_sentences(_presentation(metrics=(
            _metric(
                'units',
                goal=Decimal('20'),
                actual=Decimal('21'),
                remaining=Decimal('0'),
                progress_percent=Decimal('105'),
                projected_total=Decimal('37.8'),
                status='goal_reached',
            ),
            _metric('total_gross'),
            _metric('commission'),
        )))
        self.assertIn(
            'Units goal reached: 21.0 recorded against a goal of 20.0.',
            sentences,
        )

    def test_complete_month_has_no_pace_sentence(self):
        sentences = coach_sentences(_presentation(
            period_status='complete',
            completed_selling_days=27,
            remaining_selling_days=0,
            future_selling_days=0,
            metrics=(
                _metric(
                    'units',
                    goal=Decimal('20'),
                    actual=Decimal('15'),
                    remaining=Decimal('5'),
                    progress_percent=Decimal('75'),
                    projected_total=Decimal('15'),
                    status='behind',
                ),
                _metric('total_gross'),
                _metric('commission'),
            ),
        ))
        self.assertIn('March 2028 is complete after 27 selling days.', sentences)
        self.assertIn(
            'Units finished at 15.0 against a goal of 20.0.', sentences,
        )
        self.assertFalse(
            any('per remaining selling day' in sentence for sentence in sentences),
        )

    def test_future_month_includes_diagnostic(self):
        sentences = coach_sentences(_presentation(
            period_status='future',
            completed_selling_days=0,
            remaining_selling_days=27,
            future_selling_days=27,
            metrics=(
                _metric('units', status='insufficient_data',
                        diagnostic_code='future_period'),
                _metric('total_gross', status='insufficient_data',
                        diagnostic_code='future_period'),
                _metric('commission', status='insufficient_data',
                        diagnostic_code='future_period'),
            ),
        ))
        self.assertIn('March 2028 has not started yet.', sentences)
        self.assertIn('Units cannot be projected yet.', sentences)
        self.assertEqual(
            sentences.count(DIAGNOSTIC_MESSAGES['future_period']), 1,
        )

    def test_unavailable_presentation_fails_closed(self):
        with self.assertRaises(StewCoachPhrasingError):
            coach_sentences(unavailable_projection_context())
        with self.assertRaises(StewCoachPhrasingError):
            coach_sentences({'available': False})
        with self.assertRaises(StewCoachPhrasingError):
            coach_sentences(None)

    def test_sentence_count_limit_fails_closed(self):
        presentation = dict(_presentation())
        presentation['diagnostics'] = tuple(
            f'Diagnostic sentence number {index} applies.'
            for index in range(40)
        )
        with self.assertRaises(StewCoachPhrasingError):
            coach_sentences(presentation)


class _FakeGateway:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def explain(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class PhrasingServiceTests(SimpleTestCase):
    def _phrase(self, provider_result):
        gateway = _FakeGateway(provider_result)
        with patch(
            'SalesLogApp.stew_coach_phrasing.configured_ask_stew_gateway',
            return_value=gateway,
        ):
            message = phrase_coach_message(
                SimpleNamespace(pk=1),
                _presentation(),
                submission_token='token-1',
            )
        return message, gateway

    def test_provider_used_returns_gateway_answer_without_notice(self):
        deterministic = deterministic_coach_message(_presentation())
        message, gateway = self._phrase(
            AskStewProviderResult(deterministic, 'used', provider_used=True),
        )
        self.assertEqual(message.message, deterministic)
        self.assertTrue(message.provider_used)
        self.assertEqual(message.notice, '')
        self.assertEqual(len(gateway.calls), 1)
        call = gateway.calls[0]
        self.assertEqual(call['intent'], COACH_INTENT)
        self.assertEqual(call['question'], '')
        self.assertEqual(call['deterministic_explanation'], deterministic)

    def test_rate_limited_keeps_deterministic_text_with_notice(self):
        deterministic = deterministic_coach_message(_presentation())
        message, _gateway = self._phrase(
            AskStewProviderResult(deterministic, 'rate_limited'),
        )
        self.assertEqual(message.message, deterministic)
        self.assertFalse(message.provider_used)
        self.assertEqual(message.notice, RATE_LIMITED_NOTICE)

    def test_duplicate_submission_notice(self):
        deterministic = deterministic_coach_message(_presentation())
        message, _gateway = self._phrase(
            AskStewProviderResult(deterministic, 'duplicate_submission'),
        )
        self.assertEqual(message.notice, DUPLICATE_NOTICE)

    def test_disabled_provider_state_maps_to_unavailable_notice(self):
        deterministic = deterministic_coach_message(_presentation())
        message, _gateway = self._phrase(
            AskStewProviderResult(deterministic, 'disabled'),
        )
        self.assertEqual(message.message, deterministic)
        self.assertEqual(message.notice, UNAVAILABLE_NOTICE)


class ActivityGoalsCoachTests(TestCase):
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
        self.month = timezone.localdate().replace(day=1)
        MonthlyGoal.objects.create(
            user=self.user,
            month_start=self.month,
            target_units=Decimal('20'),
            target_total_gross=Decimal('40000'),
            target_commission=Decimal('5000'),
        )

    def _post_payload(self, token):
        return {
            'form_type': 'coach_phrase',
            'month': self.month.strftime('%Y-%m'),
            'submission_token': token,
        }

    def test_get_shows_deterministic_note_without_ai_form(self):
        with patch('SalesLogApp.views.phrase_coach_message') as phrase:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Stew Coach says')
        coach = response.context['stew_coach_message']
        self.assertTrue(coach['text'])
        self.assertFalse(coach['ai_available'])
        self.assertNotContains(response, 'value="coach_phrase"')
        phrase.assert_not_called()

    def test_ai_form_rendered_when_provider_available(self):
        with patch(
            'SalesLogApp.views.ask_stew_provider_availability',
            return_value={'available': True, 'status': 'ready'},
        ):
            response = self.client.get(self.url)
        self.assertContains(response, 'value="coach_phrase"')
        coach = response.context['stew_coach_message']
        self.assertTrue(coach['ai_available'])
        self.assertTrue(coach['token'])

    def test_coach_phrase_post_renders_provider_wording(self):
        token = _new_coach_phrase_token(self.user)
        with patch(
            'SalesLogApp.views.phrase_coach_message',
            return_value=StewCoachMessage(
                'Verified AI-selected coaching note.', 'used', True, '',
            ),
        ) as phrase:
            response = self.client.post(self.url, self._post_payload(token))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Verified AI-selected coaching note.')
        phrase.assert_called_once()
        self.assertEqual(
            phrase.call_args.kwargs['submission_token'], token,
        )

    def test_duplicate_token_is_not_resubmitted(self):
        token = _new_coach_phrase_token(self.user)
        deterministic_response = None
        with patch(
            'SalesLogApp.views.phrase_coach_message',
            return_value=StewCoachMessage('Phrased once.', 'used', True, ''),
        ) as phrase:
            self.client.post(self.url, self._post_payload(token))
            deterministic_response = self.client.post(
                self.url, self._post_payload(token),
            )
        self.assertEqual(phrase.call_count, 1)
        self.assertContains(
            deterministic_response,
            'That coaching request was already processed.',
        )

    def test_invalid_token_keeps_verified_text(self):
        with patch('SalesLogApp.views.phrase_coach_message') as phrase:
            response = self.client.post(
                self.url, self._post_payload('not-a-valid-token'),
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'This coaching request expired. Refresh the page and try again.',
        )
        self.assertTrue(response.context['stew_coach_message']['text'])
        phrase.assert_not_called()

    def test_other_users_token_is_rejected(self):
        token = _new_coach_phrase_token(self.other)
        with patch('SalesLogApp.views.phrase_coach_message') as phrase:
            response = self.client.post(self.url, self._post_payload(token))
        self.assertContains(
            response,
            'This coaching request expired. Refresh the page and try again.',
        )
        phrase.assert_not_called()
