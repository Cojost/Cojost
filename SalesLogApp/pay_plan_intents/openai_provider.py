from __future__ import annotations

import json
import os
import socket
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

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

    def post_json(self, url, *, headers, payload, timeout):
        request = Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers=dict(headers),
            method='POST',
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read()
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
        http_client: JSONHTTPClient | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.http_client = http_client or StandardLibraryJSONClient()

    def interpret(self, source_text, *, prior_turns=()):
        messages = [
            {'role': 'user', 'content': str(item)}
            for item in prior_turns
            if str(item).strip()
        ]
        messages.append({'role': 'user', 'content': (source_text or '').strip()})
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
            'max_output_tokens': 600,
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': 'pay_plan_intent',
                    'strict': True,
                    'schema': PROVIDER_OUTPUT_SCHEMA,
                },
            },
        }
        response = self.http_client.post_json(
            OPENAI_RESPONSES_URL,
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            },
            payload=payload,
            timeout=self.timeout,
        )
        return self._structured_output(response)

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


def configured_intent_interpreter(*, http_client=None):
    enabled = bool(settings.PAY_PLAN_ASSISTANT_PROVIDER_ENABLED)
    if not enabled:
        return ProviderNeutralInterpreter(enabled=False)
    if settings.PAY_PLAN_ASSISTANT_PROVIDER != 'openai':
        return ProviderNeutralInterpreter(enabled=True)
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not api_key:
        return ProviderNeutralInterpreter(enabled=True)
    provider = OpenAIIntentProvider(
        api_key=api_key,
        model=settings.PAY_PLAN_ASSISTANT_MODEL,
        timeout=settings.PAY_PLAN_ASSISTANT_TIMEOUT_SECONDS,
        http_client=http_client,
    )
    return ProviderNeutralInterpreter(provider, enabled=True)
