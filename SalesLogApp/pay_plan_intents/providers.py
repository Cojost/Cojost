from __future__ import annotations

from dataclasses import fields
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from .contract import ACTIONS, TARGET_TYPES, PayPlanIntent
from .interpreter import DeterministicIntentInterpreter


class IntentProvider(Protocol):
    def interpret(self, source_text: str) -> Mapping[str, Any]:
        """Return structured intent data only. Providers never receive plan data."""


class ProviderOutputError(ValueError):
    pass


class ProviderNeutralInterpreter:
    """Optional provider gateway with deterministic-first safe fallback."""

    def __init__(self, provider=None, *, enabled=False):
        self.provider = provider
        self.enabled = bool(enabled and provider is not None)
        self.deterministic = DeterministicIntentInterpreter()

    def interpret(
        self,
        source_text: str,
        *,
        effective_date: date | None = None,
    ) -> PayPlanIntent:
        deterministic = self.deterministic.interpret(
            source_text,
            effective_date=effective_date,
        )
        if deterministic.target_type or not self.enabled:
            return deterministic
        provider_intent = safe_provider_interpret(
            self.provider,
            source_text,
            effective_date=effective_date,
        )
        if (
            'provider_timeout' in provider_intent.missing_information
            or 'invalid_provider_output' in provider_intent.missing_information
        ):
            return deterministic
        return provider_intent


PROVIDER_FIELDS = frozenset({
    'action', 'target_type', 'target_scope', 'amount', 'percentage',
    'unit_threshold', 'current_value', 'new_value', 'conditions',
    'confidence', 'missing_information', 'ambiguities',
    'clarification_question',
})


def validate_provider_output(
    source_text: str,
    payload: Mapping[str, Any],
    *,
    effective_date: date | None = None,
    minimum_confidence: Decimal = Decimal('0.75'),
) -> PayPlanIntent:
    if not isinstance(payload, Mapping):
        raise ProviderOutputError('Provider output must be a structured object.')
    unknown = set(payload) - PROVIDER_FIELDS
    if unknown:
        raise ProviderOutputError(
            f'Provider output contained unsupported fields: {sorted(unknown)}'
        )
    action = payload.get('action')
    target = payload.get('target_type')
    if action is not None and action not in ACTIONS:
        raise ProviderOutputError('Provider action is not allowed.')
    if target is not None and target not in TARGET_TYPES:
        raise ProviderOutputError('Provider target is not allowed.')
    confidence = _decimal(payload.get('confidence', '0'))
    missing = tuple(str(item) for item in payload.get('missing_information') or ())
    ambiguities = tuple(str(item) for item in payload.get('ambiguities') or ())
    question = str(payload.get('clarification_question') or '')
    if confidence < minimum_confidence:
        missing = tuple(dict.fromkeys((*missing, 'provider_confidence')))
        question = question or (
            'I need a little more detail before I can safely interpret that request.'
        )
    conditions = payload.get('conditions') or ()
    if not isinstance(conditions, (list, tuple)) or not all(
        isinstance(item, Mapping) for item in conditions
    ):
        raise ProviderOutputError('Provider conditions must be structured objects.')
    # Database IDs and selectors are intentionally not accepted from providers.
    accepted = {
        'source_text': source_text,
        'action': action,
        'target_type': target,
        'target_scope': payload.get('target_scope'),
        'amount': _optional_decimal(payload.get('amount')),
        'percentage': _optional_decimal(payload.get('percentage')),
        'unit_threshold': _optional_decimal(payload.get('unit_threshold')),
        'current_value': _optional_decimal(payload.get('current_value')),
        'new_value': _optional_decimal(payload.get('new_value')),
        'conditions': tuple(dict(item) for item in conditions),
        'effective_date': effective_date,
        'confidence': confidence,
        'missing_information': missing,
        'ambiguities': ambiguities,
        'clarification_question': question,
    }
    allowed_names = {item.name for item in fields(PayPlanIntent)}
    return PayPlanIntent(**{
        key: value for key, value in accepted.items() if key in allowed_names
    })


def safe_provider_interpret(
    provider: IntentProvider,
    source_text: str,
    *,
    effective_date: date | None = None,
) -> PayPlanIntent:
    try:
        payload = provider.interpret(source_text)
        return validate_provider_output(
            source_text, payload, effective_date=effective_date,
        )
    except TimeoutError:
        return PayPlanIntent(
            source_text=source_text,
            effective_date=effective_date,
            confidence=Decimal('0'),
            missing_information=('provider_timeout',),
            clarification_question=(
                'The conversational interpreter timed out. Please try a more '
                'specific request or try again.'
            ),
        )
    except (ProviderOutputError, TypeError, ValueError, InvalidOperation):
        return PayPlanIntent(
            source_text=source_text,
            effective_date=effective_date,
            confidence=Decimal('0'),
            missing_information=('invalid_provider_output',),
            clarification_question=(
                'The conversational interpretation could not be validated. '
                'Please rephrase the request.'
            ),
        )


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value) -> Decimal | None:
    return None if value in (None, '') else _decimal(value)
