import json
import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .pay_plan_intents.openai_provider import (
    OpenAIIntentProvider,
    configured_intent_interpreter,
)
from .pay_plan_intents.providers import (
    ProviderNeutralInterpreter,
    ProviderOutputError,
    ProviderRefusalError,
    ProviderUnavailableError,
    safe_provider_interpret,
    validate_provider_output,
)


def provider_payload(**overrides):
    payload = {
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
    payload.update(overrides)
    return payload


def responses_api_result(payload=None):
    return {
        'status': 'completed',
        'output': [{
            'type': 'message',
            'content': [{
                'type': 'output_text',
                'text': json.dumps(payload or provider_payload()),
            }],
        }],
    }


class FakeHTTPClient:
    def __init__(self, response=None, error=None):
        self.response = response or responses_api_result()
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
        return self.response


class Phase1DProviderTests(SimpleTestCase):
    @override_settings(PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=False)
    def test_provider_is_disabled_by_default_and_needs_no_key(self):
        with patch.dict(os.environ, {}, clear=True):
            interpreter = configured_intent_interpreter()
        self.assertFalse(interpreter.enabled)
        self.assertFalse(interpreter.provider_requested)

    def test_deterministic_success_never_calls_provider(self):
        class CountingProvider:
            calls = 0

            def interpret(self, source_text, **kwargs):
                self.calls += 1
                return provider_payload()

        provider = CountingProvider()
        interpreter = ProviderNeutralInterpreter(provider, enabled=True)
        intent = interpreter.interpret('change front minimum to 300')
        self.assertEqual(intent.target_type, 'front_end_minimum')
        self.assertEqual(provider.calls, 0)
        self.assertEqual(interpreter.last_route, 'deterministic')

    def test_provider_is_called_only_when_deterministic_target_is_unknown(self):
        class CountingProvider:
            calls = 0

            def interpret(self, source_text, **kwargs):
                self.calls += 1
                return provider_payload()

        provider = CountingProvider()
        interpreter = ProviderNeutralInterpreter(provider, enabled=True)
        intent = interpreter.interpret('make my mystery floor better')
        self.assertEqual(provider.calls, 1)
        self.assertEqual(intent.target_type, 'front_end_minimum')
        self.assertEqual(interpreter.last_route, 'provider')

    def test_responses_payload_contains_only_bounded_conversation_text(self):
        client = FakeHTTPClient()
        provider = OpenAIIntentProvider(
            api_key='not-a-real-key',
            model='configured-test-model',
            timeout=7,
            http_client=client,
        )
        provider.interpret(
            'make the amount 300',
            prior_turns=('change my front minimum',),
        )
        call = client.calls[0]
        self.assertEqual(call['timeout'], 7)
        self.assertEqual(call['payload']['model'], 'configured-test-model')
        self.assertFalse(call['payload']['store'])
        self.assertEqual(call['payload']['input'], [
            {'role': 'user', 'content': 'change my front minimum'},
            {'role': 'user', 'content': 'make the amount 300'},
        ])
        input_text = json.dumps(call['payload']['input'])
        for forbidden_value in (
            'Customer Secret', 'Deal 88771', 'user@example.com',
            'semantic-key-123', 'database-id-42', 'source-document.pdf',
        ):
            self.assertNotIn(forbidden_value, input_text)
        self.assertNotIn('not-a-real-key', json.dumps(call['payload']))

    def test_valid_structured_output_becomes_validated_intent(self):
        client = FakeHTTPClient()
        provider = OpenAIIntentProvider(
            api_key='fake', model='configured', timeout=10, http_client=client,
        )
        intent = safe_provider_interpret(provider, 'unknown phrasing')
        self.assertEqual(intent.action, 'change')
        self.assertEqual(intent.target_type, 'front_end_minimum')
        self.assertEqual(str(intent.new_value), '300')
        self.assertIsNone(intent.rule_selector)

    def test_unknown_fields_ids_selectors_and_nested_configuration_are_rejected(self):
        forbidden_payloads = (
            provider_payload(rule_id=9),
            provider_payload(rule_selector='trusted-key'),
            provider_payload(database_id=4),
            provider_payload(conditions=[{
                'field_name': 'video_requirement_met',
                'operator': 'is_true',
                'value': True,
                'configuration': {'sql': 'DROP TABLE rules'},
            }]),
        )
        for payload in forbidden_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ProviderOutputError):
                    validate_provider_output('request', payload)

    def test_low_confidence_output_cannot_reach_draftable_state(self):
        intent = validate_provider_output(
            'uncertain request',
            provider_payload(confidence='0.20'),
        )
        self.assertIn('provider_confidence', intent.missing_information)
        self.assertFalse(intent.is_complete)

    def test_timeout_auth_rate_connection_refusal_and_malformed_fail_safely(self):
        cases = (
            (TimeoutError(), 'provider_timeout'),
            (ProviderUnavailableError('authentication'), 'provider_unavailable'),
            (ProviderUnavailableError('rate limit'), 'provider_unavailable'),
            (ConnectionError('connection'), 'provider_unavailable'),
            (ProviderRefusalError('refusal'), 'provider_refusal'),
        )
        for error, marker in cases:
            class FailingProvider:
                def interpret(self, source_text):
                    raise error

            with self.subTest(marker=marker):
                intent = safe_provider_interpret(FailingProvider(), 'request')
                self.assertIn(marker, intent.missing_information)
                if str(error):
                    self.assertNotIn(str(error), intent.clarification_question)

        malformed = FakeHTTPClient(response={
            'status': 'completed',
            'output': [{'type': 'message', 'content': [{
                'type': 'output_text', 'text': '{not-json',
            }]}],
        })
        provider = OpenAIIntentProvider(
            api_key='fake', model='configured', timeout=10,
            http_client=malformed,
        )
        intent = safe_provider_interpret(provider, 'request')
        self.assertIn('invalid_provider_output', intent.missing_information)

    def test_refusal_response_is_a_safe_clarification(self):
        client = FakeHTTPClient(response={
            'status': 'completed',
            'output': [{'type': 'message', 'content': [{
                'type': 'refusal', 'refusal': 'raw provider refusal text',
            }]}],
        })
        provider = OpenAIIntentProvider(
            api_key='fake', model='configured', timeout=10,
            http_client=client,
        )
        intent = safe_provider_interpret(provider, 'request')
        self.assertIn('provider_refusal', intent.missing_information)
        self.assertNotIn('raw provider refusal text', intent.clarification_question)
