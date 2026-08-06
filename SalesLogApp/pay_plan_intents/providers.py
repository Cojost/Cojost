from __future__ import annotations

from dataclasses import fields, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

from .contract import ACTIONS, TARGET_TYPES, PayPlanIntent
from .interpreter import DeterministicIntentInterpreter


class IntentProvider(Protocol):
    def interpret(
        self,
        source_text: str,
        *,
        prior_turns: tuple[str, ...] = (),
    ) -> Mapping[str, Any]:
        """Return structured intent data only. Providers never receive plan data."""


class ProviderOutputError(ValueError):
    pass


class ProviderUnavailableError(RuntimeError):
    pass


class ProviderRefusalError(RuntimeError):
    pass


class ProviderNeutralInterpreter:
    """Optional provider gateway with deterministic-first safe fallback."""

    def __init__(self, provider=None, *, enabled=False):
        self.provider = provider
        self.provider_requested = bool(enabled)
        self.enabled = bool(self.provider_requested and provider is not None)
        self.deterministic = DeterministicIntentInterpreter()
        self.last_route = 'deterministic'
        self.last_provider_status = (
            'disabled' if not self.provider_requested else 'unavailable'
        )

    def interpret(
        self,
        source_text: str,
        *,
        effective_date: date | None = None,
        prior_turns: tuple[str, ...] = (),
    ) -> PayPlanIntent:
        combined_text = '\n'.join((
            *(str(item).strip() for item in prior_turns if str(item).strip()),
            (source_text or '').strip(),
        )).strip()
        deterministic = self.deterministic.interpret(
            combined_text,
            effective_date=effective_date,
        )
        self.last_route = 'deterministic'
        if deterministic.target_type:
            self.last_provider_status = 'not_needed'
            return deterministic
        if not self.enabled:
            self.last_provider_status = (
                'disabled' if not self.provider_requested else 'unavailable'
            )
            return deterministic
        provider_intent = safe_provider_interpret(
            self.provider,
            source_text,
            effective_date=effective_date,
            prior_turns=prior_turns,
        )
        provider_failures = {
            'provider_timeout', 'provider_unavailable', 'provider_refusal',
            'invalid_provider_output',
        }
        if provider_failures.intersection(provider_intent.missing_information):
            self.last_provider_status = next(
                item for item in provider_intent.missing_information
                if item in provider_failures
            )
            return deterministic
        self.last_route = 'provider'
        self.last_provider_status = 'used'
        return replace(provider_intent, source_text=combined_text)


PROVIDER_FIELDS = frozenset({
    'action', 'target_type', 'target_scope', 'amount', 'percentage',
    'unit_threshold', 'current_value', 'new_value', 'conditions',
    'confidence', 'missing_information', 'ambiguities',
    'clarification_question',
})

PROVIDER_TARGET_SCOPES = frozenset({
    'new', 'used', 'green_pea', 'standard',
})

PROVIDER_CONDITION_FIELDS = frozenset({
    'nps_finance_eligible', 'green_pea', 'ar_requirement_met',
    'appointment_ratio_met', 'training_requirements_met',
    'call_requirement_met', 'video_requirement_met', 'nps_bonus_eligible',
})

PROVIDER_CONDITION_KEYS = frozenset({'field_name', 'operator', 'value'})
PROVIDER_CONDITION_OPERATORS = frozenset({'is_true', 'equals'})


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
    target_scope = payload.get('target_scope')
    if action is not None and action not in ACTIONS:
        raise ProviderOutputError('Provider action is not allowed.')
    if target is not None and target not in TARGET_TYPES:
        raise ProviderOutputError('Provider target is not allowed.')
    if target_scope is not None and target_scope not in PROVIDER_TARGET_SCOPES:
        raise ProviderOutputError('Provider target scope is not allowed.')
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
    normalized_conditions = []
    for condition in conditions:
        unknown_condition_keys = set(condition) - PROVIDER_CONDITION_KEYS
        if unknown_condition_keys:
            raise ProviderOutputError(
                'Provider conditions contained unsupported fields.'
            )
        field_name = condition.get('field_name')
        if field_name not in PROVIDER_CONDITION_FIELDS:
            raise ProviderOutputError('Provider condition field is not allowed.')
        operator = condition.get('operator') or 'is_true'
        if operator not in PROVIDER_CONDITION_OPERATORS:
            raise ProviderOutputError('Provider condition operator is not allowed.')
        normalized_conditions.append({
            'field_name': field_name,
            'operator': operator,
            'value': condition.get('value'),
        })
    # Database IDs and selectors are intentionally not accepted from providers.
    accepted = {
        'source_text': source_text,
        'action': action,
        'target_type': target,
        'target_scope': target_scope,
        'amount': _optional_decimal(payload.get('amount')),
        'percentage': _optional_decimal(payload.get('percentage')),
        'unit_threshold': _optional_decimal(payload.get('unit_threshold')),
        'current_value': _optional_decimal(payload.get('current_value')),
        'new_value': _optional_decimal(payload.get('new_value')),
        'conditions': tuple(normalized_conditions),
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
    prior_turns: tuple[str, ...] = (),
) -> PayPlanIntent:
    try:
        if prior_turns:
            payload = provider.interpret(
                source_text,
                prior_turns=prior_turns,
            )
        else:
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
    except ProviderRefusalError:
        return PayPlanIntent(
            source_text=source_text,
            effective_date=effective_date,
            confidence=Decimal('0'),
            missing_information=('provider_refusal',),
            clarification_question=(
                'The optional interpreter could not handle that request. '
                'Please describe the pay-plan change more specifically.'
            ),
        )
    except (ProviderUnavailableError, ConnectionError, OSError):
        return PayPlanIntent(
            source_text=source_text,
            effective_date=effective_date,
            confidence=Decimal('0'),
            missing_information=('provider_unavailable',),
            clarification_question=(
                'The optional interpreter is unavailable. Please describe '
                'the pay-plan change more specifically.'
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
