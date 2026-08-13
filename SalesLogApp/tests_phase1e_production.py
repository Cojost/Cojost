import json
import os
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    PayPlanAssistantUsageEvent,
    PayPlanChangeRequest,
    PayPlanConversation,
    PayPlanRule,
    PayPlanVersion,
    UserProfile,
)
from .pay_plan_conversations import PayPlanConversationService
from .pay_plan_intents.interpreter import DeterministicIntentInterpreter
from .pay_plan_intents.openai_provider import (
    OpenAIIntentProvider,
    configured_intent_interpreter,
)
from .pay_plan_intents.providers import (
    ProviderNeutralInterpreter,
    ProviderOutputError,
    ProviderUnavailableError,
)
from .pay_plan_provider_config import load_provider_configuration
from .pay_plan_provider_runtime import stable_rollout_eligible


VALID_PROVIDER_SETTINGS = {
    'PAY_PLAN_ASSISTANT_PROVIDER': 'openai',
    'PAY_PLAN_ASSISTANT_MODEL': 'gpt-5.6-sol',
    'PAY_PLAN_ASSISTANT_TIMEOUT_SECONDS': '10',
    'PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT': '100',
    'PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS': [],
    'PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT': '2',
    'PAY_PLAN_ASSISTANT_MAX_PROVIDER_INPUT_CHARS': '8000',
    'PAY_PLAN_ASSISTANT_MAX_PROVIDER_RESPONSE_BYTES': '65536',
    'PAY_PLAN_ASSISTANT_MAX_OUTPUT_TOKENS': '600',
}


def provider_payload():
    return {
        'action': 'change',
        'target_type': 'front_end_minimum',
        'target_scope': None,
        'amount': '300',
        'percentage': None,
        'unit_threshold': None,
        'current_value': None,
        'new_value': '300',
        'conditions': [],
        'confidence': '0.95',
        'missing_information': [],
        'ambiguities': [],
        'clarification_question': '',
    }


class FakeHTTPClient:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def post_json(self, url, *, headers, payload, timeout):
        self.calls.append({
            'url': url,
            'headers': headers,
            'payload': payload,
            'timeout': timeout,
        })
        if self.error:
            raise self.error
        return {
            'id': 'safe-request-id',
            'status': 'completed',
            'usage': {'input_tokens': 21, 'output_tokens': 13},
            'output': [{
                'type': 'message',
                'content': [{
                    'type': 'output_text',
                    'text': json.dumps(provider_payload()),
                }],
            }],
        }


@override_settings(**VALID_PROVIDER_SETTINGS)
class Phase1EConfigurationTests(SimpleTestCase):
    @override_settings(PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=False)
    def test_provider_is_disabled_and_overrides_rollout(self):
        configuration = load_provider_configuration(credentials_available=True)
        self.assertEqual(configuration.state, 'disabled')
        user = SimpleNamespace(pk=17, is_authenticated=True)
        self.assertFalse(stable_rollout_eligible(user, configuration))

    @override_settings(PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=True)
    def test_missing_credentials_is_distinct_and_safe(self):
        configuration = load_provider_configuration(credentials_available=False)
        self.assertEqual(configuration.state, 'missing_credentials')
        self.assertNotIn('key', ' '.join(configuration.errors).lower())

    @override_settings(
        PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=True,
        PAY_PLAN_ASSISTANT_PROVIDER='unsupported',
    )
    def test_unsupported_provider_is_distinct(self):
        configuration = load_provider_configuration(credentials_available=True)
        self.assertEqual(configuration.state, 'unsupported_provider')

    @override_settings(
        PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=True,
        PAY_PLAN_ASSISTANT_TIMEOUT_SECONDS='not-a-number',
        PAY_PLAN_ASSISTANT_MODEL='bad model name',
        PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT='0',
    )
    def test_invalid_timeout_model_and_limit_are_rejected(self):
        configuration = load_provider_configuration(credentials_available=True)
        self.assertEqual(configuration.state, 'invalid_configuration')
        self.assertGreaterEqual(len(configuration.errors), 3)

    @override_settings(
        PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=True,
        PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT='37',
    )
    def test_percentage_rollout_is_stable_for_each_user(self):
        configuration = load_provider_configuration(credentials_available=True)
        values = [
            stable_rollout_eligible(
                SimpleNamespace(pk=49, is_authenticated=True),
                configuration,
            )
            for _ in range(20)
        ]
        self.assertEqual(len(set(values)), 1)

    @override_settings(
        PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=True,
        PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT='100',
        PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS=['17'],
    )
    def test_allowlist_takes_precedence_over_percentage(self):
        configuration = load_provider_configuration(credentials_available=True)
        allowed = SimpleNamespace(pk=17, is_authenticated=True)
        excluded = SimpleNamespace(pk=18, is_authenticated=True)
        self.assertTrue(stable_rollout_eligible(allowed, configuration))
        self.assertFalse(stable_rollout_eligible(excluded, configuration))

    @override_settings(
        PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=True,
        PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT='0',
        PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS=['17'],
    )
    def test_zero_percent_is_rollout_kill_switch_for_allowlist_too(self):
        configuration = load_provider_configuration(credentials_available=True)
        allowed = SimpleNamespace(pk=17, is_authenticated=True)
        self.assertFalse(stable_rollout_eligible(allowed, configuration))

    def test_provider_input_is_bounded_to_current_and_five_prior_turns(self):
        client = FakeHTTPClient()
        provider = OpenAIIntentProvider(
            api_key='safe-test-value',
            model='configured-model',
            timeout=10,
            max_input_chars=1000,
            http_client=client,
        )
        provider.interpret(
            'current request',
            prior_turns=tuple(f'prior turn {index}' for index in range(9)),
        )
        messages = client.calls[0]['payload']['input']
        self.assertEqual(len(messages), 6)
        self.assertEqual(messages[-1]['content'], 'current request')
        with self.assertRaises(ProviderOutputError):
            provider.interpret('x' * 1001)
        self.assertEqual(len(client.calls), 1)

    def test_provider_failure_is_attempted_once_without_raw_error(self):
        raw_error = 'RAW-PROVIDER-BODY'
        client = FakeHTTPClient(error=ProviderUnavailableError(raw_error))
        provider = OpenAIIntentProvider(
            api_key='safe-test-value',
            model='configured-model',
            timeout=10,
            http_client=client,
        )
        gateway = ProviderNeutralInterpreter(provider, enabled=True)
        intent = gateway.interpret('unrecognized mystery instruction')
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn(raw_error, intent.clarification_question)


@override_settings(
    PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=True,
    PAY_PLAN_ASSISTANT_MAX_TURNS=12,
    PAY_PLAN_ASSISTANT_CONVERSATION_TTL_HOURS=24,
    **VALID_PROVIDER_SETTINGS,
)
class Phase1EProductionTests(TestCase):
    password = 'phase-1e-password'

    def setUp(self):
        self.user = self._user('phase-1e-owner')
        self.other = self._user('phase-1e-other')
        self.version = self.user.pay_plan_assignments.get().pay_plan_version
        self.version.rules.all().delete()
        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front Minimum',
            rule_type='minimum_commission',
            calculation_scope='per_sale',
            configuration={
                'minimum_amount': '250.00',
                'applies_to_categories': ['front_end'],
            },
        )
        self.effective_date = timezone.localdate() + timedelta(days=1)

    def _user(self, username):
        user = get_user_model().objects.create_user(
            username=username,
            password=self.password,
            is_staff=True,
        )
        profile = user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        assignment = user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan',
        ).get()
        version = assignment.pay_plan_version
        version.status = PayPlanVersion.ACTIVE
        version.save(update_fields=['status', 'updated_at'])
        onboarding = user.pay_plan_onboarding
        onboarding.current_pay_plan = version.pay_plan
        onboarding.current_version = version
        onboarding.status = onboarding.ACTIVE
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        return user

    def _provider_gateway(self, user, client):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'phase-1e-secret'}):
            return configured_intent_interpreter(
                user=user,
                http_client=client,
            )

    @override_settings(PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT='0')
    def test_zero_percent_rollout_makes_no_provider_call(self):
        client = FakeHTTPClient()
        gateway = self._provider_gateway(self.user, client)
        intent = gateway.interpret('make the mystery floor better')
        self.assertEqual(client.calls, [])
        self.assertIsNone(intent.target_type)
        self.assertEqual(gateway.last_provider_status, 'rollout_excluded')

    def test_full_rollout_allows_one_bounded_provider_call(self):
        client = FakeHTTPClient()
        gateway = self._provider_gateway(self.user, client)
        intent = gateway.interpret('make the mystery floor better')
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(intent.target_type, 'front_end_minimum')
        call = client.calls[0]
        self.assertFalse(call['payload']['store'])
        self.assertLessEqual(
            len(json.dumps(call['payload']['input'])),
            8200,
        )
        self.assertNotIn('phase-1e-secret', json.dumps(call['payload']))

    @override_settings(PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT='1')
    def test_daily_quota_counts_provider_attempts_only_and_is_user_scoped(self):
        deterministic_client = FakeHTTPClient()
        deterministic = self._provider_gateway(self.user, deterministic_client)
        deterministic.interpret('change front minimum to 300')
        self.assertEqual(deterministic_client.calls, [])
        self.assertFalse(PayPlanAssistantUsageEvent.objects.filter(
            user=self.user,
            route=PayPlanAssistantUsageEvent.PROVIDER,
        ).exists())

        first_client = FakeHTTPClient()
        self._provider_gateway(self.user, first_client).interpret(
            'make the mystery floor better',
        )
        limited_client = FakeHTTPClient()
        limited = self._provider_gateway(self.user, limited_client)
        limited.interpret('another mystery instruction')
        self.assertEqual(len(first_client.calls), 1)
        self.assertEqual(limited_client.calls, [])
        self.assertEqual(limited.last_provider_status, 'rate_limited')

        other_client = FakeHTTPClient()
        self._provider_gateway(self.other, other_client).interpret(
            'make the mystery floor better',
        )
        self.assertEqual(len(other_client.calls), 1)

    @override_settings(PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT='1')
    def test_daily_quota_resets_at_local_midnight_boundary(self):
        first = FakeHTTPClient()
        self._provider_gateway(self.user, first).interpret('mystery instruction')
        PayPlanAssistantUsageEvent.objects.filter(
            user=self.user,
            route=PayPlanAssistantUsageEvent.PROVIDER,
        ).update(created_at=timezone.now() - timedelta(days=1))
        second = FakeHTTPClient()
        self._provider_gateway(self.user, second).interpret('another mystery')
        self.assertEqual(len(second.calls), 1)

    def test_operational_events_store_only_allowlisted_metadata(self):
        raw_prompt = 'RAW-PROMPT-customer-88771'
        client = FakeHTTPClient()
        self._provider_gateway(self.user, client).interpret(raw_prompt)
        event = PayPlanAssistantUsageEvent.objects.get(
            user=self.user,
            route=PayPlanAssistantUsageEvent.PROVIDER,
        )
        stored = {
            field.name: getattr(event, field.name)
            for field in event._meta.concrete_fields
        }
        self.assertNotIn(raw_prompt, str(stored))
        self.assertNotIn('phase-1e-secret', str(stored))
        self.assertEqual(event.input_tokens, 21)
        self.assertEqual(event.output_tokens, 13)
        self.assertEqual(event.provider_request_id, 'safe-request-id')
        forbidden_fields = {
            'prompt', 'response', 'authorization', 'rules', 'configuration',
        }
        self.assertTrue(forbidden_fields.isdisjoint(stored))

    def test_health_check_never_prints_or_uses_secret(self):
        output = StringIO()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'never-print-this'}):
            call_command('assistant_provider_health', stdout=output)
        rendered = output.getvalue()
        self.assertIn('paid_request_made=false', rendered)
        self.assertNotIn('never-print-this', rendered)
        self.assertNotIn('Authorization', rendered)

    def test_duplicate_start_and_follow_up_create_one_logical_turn_each(self):
        started = PayPlanConversationService.start(
            self.user,
            'change front minimum',
            self.effective_date,
            submission_token='same-start',
        )
        repeated = PayPlanConversationService.start(
            self.user,
            'different browser replay text',
            self.effective_date,
            submission_token='same-start',
        )
        self.assertEqual(started.conversation.pk, repeated.conversation.pk)
        self.assertEqual(started.conversation.turns.count(), 2)

        first = PayPlanConversationService.follow_up(
            self.user,
            started.conversation.conversation_key,
            response_text='$300',
            submission_token='same-follow-up',
        )
        repeated = PayPlanConversationService.follow_up(
            self.user,
            started.conversation.conversation_key,
            response_text='$900',
            submission_token='same-follow-up',
        )
        self.assertEqual(first.conversation.pk, repeated.conversation.pk)
        self.assertEqual(started.conversation.turns.count(), 4)
        self.assertEqual(repeated.resolution.intent.new_value, first.resolution.intent.new_value)

    def test_duplicate_create_draft_returns_one_inactive_draft(self):
        conversation = PayPlanConversationService.start(
            self.user,
            'change front minimum to 300',
            self.effective_date,
            submission_token='draftable-start',
        ).conversation
        active_assignment_id = self.user.pay_plan_assignments.get().pay_plan_version_id
        first = PayPlanConversationService.create_draft(
            self.user,
            conversation.conversation_key,
            submission_token='same-draft',
        )
        repeated = PayPlanConversationService.create_draft(
            self.user,
            conversation.conversation_key,
            submission_token='same-draft',
        )
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(PayPlanChangeRequest.objects.filter(user=self.user).count(), 1)
        self.assertEqual(first.draft_version.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertEqual(
            self.user.pay_plan_assignments.get().pay_plan_version_id,
            active_assignment_id,
        )

    def test_plan_change_during_interpretation_discards_provider_result(self):
        conversation = PayPlanConversationService.start(
            self.user,
            'change front minimum',
            self.effective_date,
        ).conversation
        service = self

        class PlanChangingInterpreter:
            last_route = 'provider'
            last_provider_status = 'used'

            def interpret(self, source_text, **kwargs):
                service.version.status = PayPlanVersion.INACTIVE
                service.version.save(update_fields=['status', 'updated_at'])
                replacement = PayPlanVersion.objects.create(
                    pay_plan=service.version.pay_plan,
                    version_name='Changed during interpretation',
                    effective_start_date=timezone.localdate(),
                    status=PayPlanVersion.ACTIVE,
                )
                assignment = service.user.pay_plan_assignments.get()
                assignment.pay_plan_version = replacement
                assignment.save(update_fields=['pay_plan_version', 'updated_at'])
                return DeterministicIntentInterpreter().interpret('$300')

        with self.assertRaisesMessage(ValidationError, 'active pay plan changed'):
            PayPlanConversationService.follow_up(
                self.user,
                conversation.conversation_key,
                response_text='$300',
                submission_token='stale-follow-up',
                interpreter=PlanChangingInterpreter(),
            )
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, PayPlanConversation.STALE)
        self.assertEqual(conversation.turns.count(), 3)
        self.assertFalse(PayPlanChangeRequest.objects.exists())

    def test_expiration_during_interpretation_discards_provider_result(self):
        conversation = PayPlanConversationService.start(
            self.user,
            'change front minimum',
            self.effective_date,
        ).conversation

        class ExpiringInterpreter:
            last_route = 'provider'
            last_provider_status = 'used'

            def interpret(inner_self, source_text, **kwargs):
                PayPlanConversation.objects.filter(pk=conversation.pk).update(
                    updated_at=timezone.now() - timedelta(days=2),
                )
                return DeterministicIntentInterpreter().interpret('$300')

        with self.assertRaisesMessage(ValidationError, 'expired'):
            PayPlanConversationService.follow_up(
                self.user,
                conversation.conversation_key,
                response_text='$300',
                submission_token='expired-follow-up',
                interpreter=ExpiringInterpreter(),
            )
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, PayPlanConversation.EXPIRED)
        self.assertEqual(conversation.turns.count(), 3)

    def test_cross_user_request_is_not_found_and_consumes_no_quota(self):
        conversation = PayPlanConversationService.start(
            self.user,
            'change front minimum',
            self.effective_date,
        ).conversation
        with self.assertRaises(ObjectDoesNotExist):
            PayPlanConversationService.follow_up(
                self.other,
                conversation.conversation_key,
                response_text='mystery request',
            )
        self.assertFalse(PayPlanAssistantUsageEvent.objects.filter(
            user=self.other,
            route=PayPlanAssistantUsageEvent.PROVIDER,
        ).exists())

    def test_page_has_accessible_processing_and_safe_fallback_states(self):
        self.client.force_login(self.user)
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post(reverse('pay_plan_assistant'), {
                'assistant_action': 'start',
                'request_text': 'make the mystery amount better',
                'effective_date': self.effective_date.isoformat(),
                'submission_token': 'page-start-token',
            })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Built-in interpretation available')
        self.assertContains(response, 'Using deterministic clarification')
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, 'assistant-submission-form')
        self.assertContains(response, 'Rephrase and try again')
        self.assertNotContains(response, 'OPENAI_API_KEY')
        self.assertNotContains(response, 'authentication')
        self.assertFalse(PayPlanChangeRequest.objects.exists())
