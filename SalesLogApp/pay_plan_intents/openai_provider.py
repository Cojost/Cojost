from __future__ import annotations

import json
import os
import socket
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

from SalesLogApp.pay_plan_provider_config import load_provider_configuration
from SalesLogApp.pay_plan_provider_runtime import (
    ProviderUsageRecorder,
    privacy_safe_identifier,
    stable_rollout_eligible,
)

from .providers import (
    ProviderNeutralInterpreter,
    ProviderOutputError,
    ProviderRefusalError,
    ProviderUnavailableError,
)


OPENAI_RESPONSES_URL = 'https://api.openai.com/v1/responses'


PROVIDER_OUTPUT_SCHEMA = {
    'type': 'object',
    'properties': {
        'action': {
            'type': ['string', 'null'],
            'enum': [
                'add', 'change', 'remove', 'replace', 'increase', 'decrease',
                'enable', 'disable', 'rename', 'duplicate', None,
            ],
        },
        'target_type': {
            'type': ['string', 'null'],
            'enum': [
                'front_end_minimum', 'front_end_maximum',
                'front_end_percentage', 'back_end_minimum',
                'back_end_maximum', 'back_end_percentage', 'front_end_pack',
                'back_end_pack', 'volume_bonus_tier', 'flat_bonus',
                'model_bonus', 'new_vehicle_bonus', 'used_vehicle_bonus',
                'draw', 'manufacturer_incentive', 'condition_requirement',
                None,
            ],
        },
        'target_scope': {
            'type': ['string', 'null'],
            'enum': ['new', 'used', 'green_pea', 'standard', None],
        },
        'amount': {'type': ['number', 'string', 'null']},
        'percentage': {'type': ['number', 'string', 'null']},
        'unit_threshold': {'type': ['number', 'string', 'null']},
        'current_value': {'type': ['number', 'string', 'null']},
        'new_value': {'type': ['number', 'string', 'null']},
        'conditions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'field_name': {
                        'type': 'string',
                        'enum': [
                            'nps_finance_eligible', 'green_pea',
                            'ar_requirement_met', 'appointment_ratio_met',
                            'training_requirements_met',
                            'call_requirement_met', 'video_requirement_met',
                            'nps_bonus_eligible',
                        ],
                    },
                    'operator': {
                        'type': 'string',
                        'enum': ['is_true', 'equals'],
                    },
                    'value': {
                        'type': ['string', 'number', 'boolean', 'null'],
                    },
                },
                'required': ['field_name', 'operator', 'value'],
                'additionalProperties': False,
            },
        },
        'confidence': {'type': ['number', 'string']},
        'missing_information': {
            'type': 'array',
            'items': {'type': 'string'},
        },
        'ambiguities': {
            'type': 'array',
            'items': {'type': 'string'},
        },
        'clarification_question': {'type': 'string'},
    },
    'required': [
        'action', 'target_type', 'target_scope', 'amount', 'percentage',
        'unit_threshold', 'current_value', 'new_value', 'conditions',
        'confidence', 'missing_information', 'ambiguities',
        'clarification_question',
    ],
    'additionalProperties': False,
}


class JSONHTTPClient(Protocol):
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: int,
    ) -> Mapping[str, Any]:
        ...


class StandardLibraryJSONClient:
    """Small injectable JSON client; it performs one bounded request only."""

    def __init__(self, *, max_response_bytes):
        self.max_response_bytes = max_response_bytes

    def post_json(self, url, *, headers, payload, timeout):
        request = Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers=dict(headers),
            method='POST',
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            raise ProviderUnavailableError('Provider request failed.') from exc
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError('Provider request timed out.') from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TimeoutError('Provider request timed out.') from exc
            raise ProviderUnavailableError('Provider connection failed.') from exc
        except OSError as exc:
            raise ProviderUnavailableError('Provider connection failed.') from exc
        if len(body) > self.max_response_bytes:
            raise ProviderOutputError('Provider response exceeded the size limit.')
        try:
            parsed = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderOutputError('Provider response was not valid JSON.') from exc
        if not isinstance(parsed, Mapping):
            raise ProviderOutputError('Provider response must be an object.')
        return parsed


class OpenAIIntentProvider:
    """Responses API adapter that receives conversational text and nothing else."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: int,
        max_input_chars: int = 8000,
        max_response_bytes: int = 65536,
        max_output_tokens: int = 600,
        safety_identifier: str = '',
        http_client: JSONHTTPClient | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_input_chars = max_input_chars
        self.max_response_bytes = max_response_bytes
        self.max_output_tokens = max_output_tokens
        self.safety_identifier = safety_identifier
        self.last_metadata = {}
        self.http_client = http_client or StandardLibraryJSONClient(
            max_response_bytes=max_response_bytes,
        )

    def interpret(self, source_text, *, prior_turns=()):
        messages = self._bounded_messages(source_text, prior_turns)
        payload = {
            'model': self.model,
            'store': False,
            'instructions': (
                'Interpret only the requested pay-plan change. Return the '
                'allowlisted semantic fields in the supplied schema. Never '
                'invent identifiers, selectors, rules, activation steps, code, '
                'SQL, or configuration. Ask one concise question when required.'
            ),
            'input': messages,
            'reasoning': {'effort': 'none'},
            'max_output_tokens': self.max_output_tokens,
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': 'pay_plan_intent',
                    'strict': True,
                    'schema': PROVIDER_OUTPUT_SCHEMA,
                },
            },
        }
        if self.safety_identifier:
            payload['safety_identifier'] = self.safety_identifier
        response = self.http_client.post_json(
            OPENAI_RESPONSES_URL,
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            },
            payload=payload,
            timeout=self.timeout,
        )
        response_size = len(json.dumps(response, separators=(',', ':')).encode('utf-8'))
        if response_size > self.max_response_bytes:
            raise ProviderOutputError('Provider response exceeded the size limit.')
        usage = response.get('usage') or {}
        self.last_metadata = {
            'request_id': str(response.get('id') or '')[:100],
            'input_tokens': usage.get('input_tokens'),
            'output_tokens': usage.get('output_tokens'),
        }
        return self._structured_output(response)

    def _bounded_messages(self, source_text, prior_turns):
        current = (source_text or '').strip()
        if len(current) > self.max_input_chars:
            raise ProviderOutputError('Provider input exceeded the size limit.')
        remaining = self.max_input_chars - len(current)
        selected_prior = []
        for item in reversed(tuple(prior_turns)[-5:]):
            text = str(item).strip()
            if not text:
                continue
            if len(text) > remaining:
                text = text[-remaining:] if remaining else ''
            if not text:
                break
            selected_prior.append(text)
            remaining -= len(text)
            if remaining <= 0:
                break
        messages = [
            {'role': 'user', 'content': text}
            for text in reversed(selected_prior)
        ]
        messages.append({'role': 'user', 'content': current})
        return messages

    @staticmethod
    def _structured_output(response):
        if response.get('status') == 'incomplete':
            raise ProviderOutputError('Provider response was incomplete.')
        for output in response.get('output') or ():
            if not isinstance(output, Mapping) or output.get('type') != 'message':
                continue
            for item in output.get('content') or ():
                if not isinstance(item, Mapping):
                    continue
                if item.get('type') == 'refusal':
                    raise ProviderRefusalError('Provider refused the request.')
                if item.get('type') != 'output_text':
                    continue
                text = item.get('text')
                try:
                    payload = json.loads(text)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ProviderOutputError(
                        'Provider structured output was malformed.'
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise ProviderOutputError(
                        'Provider structured output must be an object.'
                    )
                return payload
        raise ProviderOutputError('Provider returned no structured output.')


def provider_configuration():
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    return load_provider_configuration(credentials_available=bool(api_key))


def configured_intent_interpreter(
    *, user=None, conversation=None, http_client=None,
):
    configuration = provider_configuration()
    conversation_key = getattr(conversation, 'conversation_key', '')
    recorder = (
        ProviderUsageRecorder(
            user,
            conversation_key=conversation_key,
            model_name=configuration.model,
        )
        if user is not None and user.is_authenticated
        else None
    )
    if not configuration.ready:
        return ProviderNeutralInterpreter(
            enabled=bool(settings.PAY_PLAN_ASSISTANT_PROVIDER_ENABLED),
            disabled_status=configuration.state,
            usage_recorder=recorder,
        )
    if user is None or not user.is_authenticated:
        return ProviderNeutralInterpreter(
            enabled=True,
            disabled_status='rollout_excluded',
            usage_recorder=recorder,
        )
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    provider = OpenAIIntentProvider(
        api_key=api_key,
        model=configuration.model,
        timeout=configuration.timeout_seconds,
        max_input_chars=configuration.max_input_chars,
        max_response_bytes=configuration.max_response_bytes,
        max_output_tokens=configuration.max_output_tokens,
        safety_identifier=privacy_safe_identifier(user),
        http_client=http_client,
    )
    return ProviderNeutralInterpreter(
        provider,
        enabled=True,
        disabled_status=(
            'ready'
            if stable_rollout_eligible(user, configuration)
            else 'rollout_excluded'
        ),
        provider_authorizer=(
            lambda: recorder.authorize_provider_attempt(configuration)
        ),
        usage_recorder=recorder,
    )


def provider_availability_for_user(user):
    configuration = provider_configuration()
    eligible = stable_rollout_eligible(user, configuration)
    return {
        'configuration_state': configuration.state,
        'external_available': bool(configuration.ready and eligible),
        'mode': 'provider' if configuration.ready and eligible else 'built_in',
    }
