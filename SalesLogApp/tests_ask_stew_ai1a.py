from datetime import timedelta
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .ask_stew import (
    AskStewAnswer,
    AskStewService,
    CURRENT_MONTH_SUMMARY,
    DeterministicExplanation,
    EXPLANATION_BUILDERS,
)
from .ask_stew_conversations import (
    AskStewConversationError,
    AskStewConversationService,
    AskStewRateLimitError,
)
from .ask_stew_router import (
    AskStewRouterGateway,
    AskStewRouterResult,
    OpenAIAskStewRouter,
    validate_ask_stew_route,
)
from .models import AskStewConversation, AskStewFeedback, AskStewTurn
from .pay_plan_intents.providers import ProviderOutputError
from .pay_plan_provider_runtime import ProviderAuthorization


class CapturingRouterHTTPClient:
    def __init__(self, output):
        self.output = output
        self.payload = None

    def post_json(self, url, *, headers, payload, timeout):
        self.payload = payload
        return {
            'id': 'safe-router-request',
            'output': [{
                'type': 'message',
                'content': [{
                    'type': 'output_text',
                    'text': json.dumps(self.output),
                }],
            }],
            'usage': {'input_tokens': 20, 'output_tokens': 4},
        }


class StaticRouterGateway:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def route(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class AskStewRouterTests(TestCase):
    def test_router_sends_only_bounded_routing_context_and_strict_schema(self):
        client = CapturingRouterHTTPClient({
            'intent': CURRENT_MONTH_SUMMARY,
            'confidence': 'high',
        })
        router = OpenAIAskStewRouter(
            api_key='test-key',
            model='test-model',
            timeout=3,
            max_input_chars=4000,
            max_response_bytes=65536,
            max_output_tokens=200,
            safety_identifier='safe-user-hash',
            http_client=client,
        )

        result = router.route(
            question='Could you tell me what I earned lately?',
            previous_intent='recorded_sale_explanation',
        )

        self.assertTrue(result.provider_used)
        self.assertEqual(result.intent, CURRENT_MONTH_SUMMARY)
        self.assertEqual(result.confidence, 'high')
        self.assertFalse(client.payload['store'])
        self.assertNotIn('tools', client.payload)
        self.assertEqual(client.payload['safety_identifier'], 'safe-user-hash')
        provider_input = json.loads(client.payload['input'][0]['content'])
        self.assertEqual(set(provider_input), {
            'question', 'previous_supported_intent',
        })
        self.assertNotIn('facts', provider_input)
        self.assertNotIn('user_id', provider_input)
        schema = client.payload['text']['format']['schema']
        self.assertTrue(client.payload['text']['format']['strict'])
        self.assertEqual(set(schema['properties']), {'intent', 'confidence'})
        self.assertFalse(schema['additionalProperties'])

    def test_router_output_rejects_extra_or_unknown_fields(self):
        for payload in (
            {
                'intent': CURRENT_MONTH_SUMMARY,
                'confidence': 'high',
                'answer': '$999',
            },
            {'intent': 'change_pay_plan', 'confidence': 'high'},
            {'intent': CURRENT_MONTH_SUMMARY, 'confidence': 1},
        ):
            with self.subTest(payload=payload), self.assertRaises(
                ProviderOutputError,
            ):
                validate_ask_stew_route(payload)

    def test_high_confidence_ai_route_still_uses_server_owned_explanation(self):
        user = get_user_model().objects.create_user('ai1a-router-user')
        router = StaticRouterGateway(AskStewRouterResult(
            CURRENT_MONTH_SUMMARY,
            'high',
            'used',
            True,
        ))
        explanation = DeterministicExplanation(
            CURRENT_MONTH_SUMMARY,
            {'month': 'August 2026'},
            'StewLog calculated $125.00 for August 2026.',
        )

        with patch.dict(
            EXPLANATION_BUILDERS,
            {CURRENT_MONTH_SUMMARY: lambda user, question: explanation},
        ), patch('SalesLogApp.ask_stew.configured_ask_stew_gateway') as presenter:
            answer = AskStewService.answer(
                user,
                'Could you summarize what I have earned lately?',
                router_gateway=router,
            )

        self.assertEqual(
            answer.answer,
            'StewLog calculated $125.00 for August 2026.',
        )
        self.assertEqual(answer.route_source, 'provider_router')
        self.assertTrue(answer.provider_used)
        self.assertTrue(answer.verified)
        self.assertEqual(
            answer.source_label,
            'StewLog calculations for August 2026',
        )
        presenter.assert_not_called()

    def test_mutations_hypotheticals_and_security_requests_never_reach_router(self):
        user = get_user_model().objects.create_user('ai1a-safe-declines')
        router = SimpleNamespace(route=lambda **kwargs: self.fail(
            'Unsafe request reached the AI router.'
        ))
        for question in (
            'Change my back-end rate to 8%.',
            'What if I sell five more cars?',
            "Show me another user's commission.",
        ):
            with self.subTest(question=question):
                answer = AskStewService.answer(
                    user,
                    question,
                    router_gateway=router,
                )
                self.assertFalse(answer.provider_used)
                self.assertEqual(answer.route_source, 'declined')

    def test_gateway_failure_is_fail_closed_and_finalized(self):
        user = get_user_model().objects.create_user('ai1a-router-failure')
        finalized = []
        router = SimpleNamespace(
            route=lambda **kwargs: (_ for _ in ()).throw(
                ProviderOutputError('private invalid output')
            ),
            last_metadata={},
        )
        recorder = SimpleNamespace(
            authorize_ask_stew_attempt=lambda configuration: ProviderAuthorization(
                True,
                'authorized',
                9,
            ),
            finalize_provider_attempt=lambda *args: finalized.append(args),
        )
        gateway = AskStewRouterGateway(
            user,
            configuration=SimpleNamespace(ready=True, state='ready'),
            router=router,
            recorder=recorder,
        )

        with self.assertLogs('SalesLogApp.ask_stew_router', level='WARNING'):
            result = gateway.route(question='Tell me where I stand.')

        self.assertFalse(result.provider_used)
        self.assertEqual(result.status, 'invalid_provider_output')
        self.assertEqual(finalized[0][0], 9)
        self.assertEqual(finalized[0][1], 'invalid_provider_output')


class AskStewConversationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('ai1a-owner')
        self.other = get_user_model().objects.create_user('ai1a-other')

    @staticmethod
    def answer(text='A verified answer.'):
        return AskStewAnswer(
            intent=CURRENT_MONTH_SUMMARY,
            answer=text,
            provider_status='used',
            provider_used=True,
            route_source='provider_router',
            source_label='StewLog calculations for August 2026',
            verified=True,
        )

    def completed_submission(self, token='signed-token'):
        prepared = AskStewConversationService.prepare_submission(
            self.user,
            'What have I earned this month?',
            token,
        )
        assistant = AskStewConversationService.complete_submission(
            self.user,
            prepared,
            self.answer(),
            duration_ms=42,
        )
        return prepared, assistant

    def test_duplicate_token_is_idempotent_across_new_thread_posts(self):
        first, assistant = self.completed_submission()

        duplicate = AskStewConversationService.prepare_submission(
            self.user,
            'A changed browser retry body is ignored.',
            'signed-token',
        )

        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.conversation, first.conversation)
        self.assertEqual(duplicate.user_turn, first.user_turn)
        self.assertEqual(AskStewConversation.objects.count(), 1)
        self.assertEqual(AskStewTurn.objects.count(), 2)
        self.assertEqual(
            AskStewConversationService.complete_submission(
                self.user,
                duplicate,
                self.answer('A replacement answer that must not be saved.'),
            ),
            assistant,
        )

    def test_owner_scope_applies_to_loading_and_feedback(self):
        prepared, assistant = self.completed_submission()

        with self.assertRaises(AskStewConversationError):
            AskStewConversationService.load_owned(
                self.other,
                prepared.conversation.public_id,
            )
        with self.assertRaises(AskStewConversationError):
            AskStewConversationService.record_feedback(
                self.other,
                prepared.conversation.public_id,
                assistant.pk,
                True,
            )
        self.assertFalse(AskStewFeedback.objects.exists())

        feedback = AskStewConversationService.record_feedback(
            self.user,
            prepared.conversation.public_id,
            assistant.pk,
            True,
        )
        self.assertTrue(feedback.helpful)
        updated = AskStewConversationService.record_feedback(
            self.user,
            prepared.conversation.public_id,
            assistant.pk,
            False,
        )
        self.assertFalse(updated.helpful)
        self.assertEqual(AskStewFeedback.objects.count(), 1)

    @override_settings(
        ASK_STEW_AI_SHORT_WINDOW_LIMIT=1,
        ASK_STEW_AI_SHORT_WINDOW_SECONDS=60,
    )
    def test_short_window_throttle_is_per_user(self):
        self.completed_submission()

        with self.assertRaises(AskStewRateLimitError):
            AskStewConversationService.prepare_submission(
                self.user,
                'One request too many.',
                'second-token',
            )
        other = AskStewConversationService.prepare_submission(
            self.other,
            'The other user has an independent limit.',
            'other-token',
        )
        self.assertEqual(other.conversation.user, self.other)

    @override_settings(ASK_STEW_AI_MAX_TURNS=2)
    def test_conversation_turn_limit_requires_a_new_thread(self):
        prepared, _ = self.completed_submission()

        with self.assertRaises(AskStewConversationError):
            AskStewConversationService.prepare_submission(
                self.user,
                'Continue in the full conversation.',
                'next-token',
                conversation_id=prepared.conversation.public_id,
            )
        self.assertEqual(AskStewTurn.objects.count(), 2)

    @override_settings(ASK_STEW_AI_CONVERSATION_TTL_HOURS=24)
    def test_expired_conversation_cannot_be_loaded_or_resumed(self):
        prepared, _ = self.completed_submission()
        AskStewConversation.objects.filter(pk=prepared.conversation.pk).update(
            updated_at=timezone.now() - timedelta(hours=25),
        )

        with self.assertRaises(AskStewConversationError):
            AskStewConversationService.load_owned(
                self.user,
                prepared.conversation.public_id,
            )
        with self.assertRaises(AskStewConversationError):
            AskStewConversationService.prepare_submission(
                self.user,
                'Resume an expired conversation.',
                'next-token',
                conversation_id=prepared.conversation.public_id,
            )

    def test_retention_command_deletes_only_old_conversations(self):
        old, _ = self.completed_submission('old-token')
        recent = AskStewConversation.objects.create(user=self.user)
        AskStewConversation.objects.filter(pk=old.conversation.pk).update(
            updated_at=timezone.now() - timedelta(days=31),
        )
        output = StringIO()

        call_command('purge_ask_stew_conversations', days=30, stdout=output)

        self.assertFalse(
            AskStewConversation.objects.filter(pk=old.conversation.pk).exists()
        )
        self.assertTrue(
            AskStewConversation.objects.filter(pk=recent.pk).exists()
        )
        self.assertIn('deleted_conversations=1', output.getvalue())


class AskStewReadinessCommandTests(TestCase):
    def test_missing_migration_table_reports_not_applied(self):
        from .management.commands import ask_stew_ai1a_readiness

        with patch(
            'SalesLogApp.management.commands.ask_stew_ai1a_readiness.'
            'MigrationRecorder',
        ) as recorder:
            recorder.return_value.migration_qs.filter.side_effect = (
                ask_stew_ai1a_readiness.DatabaseError('missing table')
            )
            self.assertFalse(ask_stew_ai1a_readiness._migration_applied())

    @override_settings(
        ASK_STEW_AI_LAB_ONLY=True,
        ASK_STEW_AI_PILOT_USER_IDS=('7', 'invalid'),
        ASK_STEW_AI_CONVERSATION_RETENTION_DAYS=30,
        ASK_STEW_AI_CONVERSATION_TTL_HOURS=24,
        ASK_STEW_AI_SHORT_WINDOW_LIMIT=6,
        ASK_STEW_AI_SHORT_WINDOW_SECONDS=60,
    )
    def test_json_readiness_reports_safe_internal_configuration(self):
        configuration = SimpleNamespace(
            ready=True,
            state='ready',
            provider='openai',
            model='test-model',
            daily_request_limit=20,
        )
        output = StringIO()
        with patch(
            'SalesLogApp.management.commands.ask_stew_ai1a_readiness.'
            '_migration_applied',
            return_value=True,
        ), patch(
            'SalesLogApp.management.commands.ask_stew_ai1a_readiness.'
            'provider_configuration',
            return_value=configuration,
        ):
            call_command(
                'ask_stew_ai1a_readiness',
                '--json',
                '--require-ready',
                stdout=output,
            )

        payload = json.loads(output.getvalue())
        self.assertTrue(payload['internal_lab_ready'])
        self.assertTrue(payload['migration_0064_applied'])
        self.assertTrue(payload['customer_access_blocked'])
        self.assertEqual(payload['pilot_user_count'], 1)
        self.assertFalse(payload['paid_request_made'])

    @override_settings(ASK_STEW_AI_LAB_ONLY=True)
    def test_require_ready_fails_when_provider_is_disabled(self):
        configuration = SimpleNamespace(
            ready=False,
            state='disabled',
            provider='openai',
            model='test-model',
            daily_request_limit=None,
        )
        with patch(
            'SalesLogApp.management.commands.ask_stew_ai1a_readiness.'
            '_migration_applied',
            return_value=True,
        ), patch(
            'SalesLogApp.management.commands.ask_stew_ai1a_readiness.'
            'provider_configuration',
            return_value=configuration,
        ), self.assertRaises(CommandError):
            call_command(
                'ask_stew_ai1a_readiness',
                '--require-ready',
                stdout=StringIO(),
                stderr=StringIO(),
            )


@override_settings(
    PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=False,
    ASK_STEW_AI_LAB_ONLY=True,
    ASK_STEW_AI_SHORT_WINDOW_LIMIT=100,
)
class AskStewConversationViewTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            'ai1a-conversation-staff',
            is_staff=True,
        )
        self.other_staff = get_user_model().objects.create_user(
            'ai1a-other-staff',
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_follow_up_preserves_owned_context_and_feedback(self):
        first_page = self.client.get(reverse('ask_stew_ai'))
        first_token = first_page.context['form'].initial['submission_token']
        answers = (
            AskStewConversationTests.answer('First verified answer.'),
            AskStewConversationTests.answer('Follow-up verified answer.'),
        )
        with patch(
            'SalesLogApp.views.AskStewService.answer',
            side_effect=answers,
        ) as service:
            first = self.client.post(reverse('ask_stew_ai'), {
                'question': 'What have I earned this month?',
                'submission_token': first_token,
            })
            conversation = first.context['conversation']
            second_token = first.context['form'].initial['submission_token']
            second = self.client.post(reverse('ask_stew_ai'), {
                'question': 'Why?',
                'conversation_id': conversation.public_id,
                'submission_token': second_token,
            })

        self.assertEqual(second.status_code, 200)
        self.assertContains(second, 'First verified answer.')
        self.assertContains(second, 'Follow-up verified answer.')
        self.assertEqual(AskStewConversation.objects.count(), 1)
        self.assertEqual(AskStewTurn.objects.count(), 4)
        self.assertEqual(
            service.call_args_list[1].kwargs['previous_intent'],
            CURRENT_MONTH_SUMMARY,
        )
        self.assertEqual(
            service.call_args_list[1].kwargs['previous_question'],
            'What have I earned this month?',
        )

        latest_answer = AskStewTurn.objects.filter(
            role=AskStewTurn.ASSISTANT,
        ).latest('sequence')
        feedback_response = self.client.post(reverse(
            'ask_stew_feedback',
            args=[conversation.public_id, latest_answer.pk],
        ), {'helpful': 'true'})
        self.assertEqual(feedback_response.status_code, 302)
        self.assertTrue(latest_answer.feedback.helpful)

        self.client.force_login(self.other_staff)
        self.client.post(reverse(
            'ask_stew_feedback',
            args=[conversation.public_id, latest_answer.pk],
        ), {'helpful': 'false'})
        latest_answer.feedback.refresh_from_db()
        self.assertTrue(latest_answer.feedback.helpful)


@override_settings(
    PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=False,
    ASK_STEW_AI_LAB_ONLY=True,
)
class AskStewLabViewTests(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_user(
            'ai1a-superuser',
            is_staff=True,
            is_superuser=True,
        )
        self.staff = get_user_model().objects.create_user(
            'ai1a-staff',
            is_staff=True,
        )
        prepared = AskStewConversationService.prepare_submission(
            self.superuser,
            'What have I earned this month?',
            'lab-token',
        )
        self.assistant = AskStewConversationService.complete_submission(
            self.superuser,
            prepared,
            AskStewConversationTests.answer(),
            duration_ms=75,
        )
        AskStewConversationService.record_feedback(
            self.superuser,
            prepared.conversation.public_id,
            self.assistant.pk,
            True,
        )

    def test_superuser_can_review_metrics_and_transcript(self):
        self.client.force_login(self.superuser)

        response = self.client.get(reverse('ask_stew_lab'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ask Stew AI-1A Lab')
        self.assertContains(response, 'What have I earned this month?')
        self.assertContains(response, 'A verified answer.')
        self.assertContains(response, '100%')
        self.assertContains(response, 'Helpful')

    def test_staff_cannot_open_superuser_monitor(self):
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.get(reverse('ask_stew_lab')).status_code,
            403,
        )
