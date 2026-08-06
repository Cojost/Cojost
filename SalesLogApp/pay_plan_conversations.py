from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import PayPlanConversation, PayPlanConversationTurn
from .pay_plan_intents.contract import (
    ACTIONS,
    TARGET_TYPES,
    IntentResolution,
    PayPlanIntent,
)
from .pay_plan_intents.handlers import active_version_for_user
from .pay_plan_intents.openai_provider import configured_intent_interpreter
from .pay_plan_intents.providers import (
    PROVIDER_CONDITION_FIELDS,
    PROVIDER_CONDITION_KEYS,
    PROVIDER_CONDITION_OPERATORS,
    PROVIDER_TARGET_SCOPES,
)
from .pay_plan_intents.service import create_draft_from_intent, resolve_intent


PENDING_INTENT_FIELDS = frozenset({
    'action', 'target_type', 'target_scope', 'amount', 'percentage',
    'unit_threshold', 'current_value', 'new_value', 'conditions',
    'effective_date', 'confidence', 'missing_information', 'ambiguities',
    'clarification_question',
})


@dataclass(frozen=True)
class ConversationOutcome:
    conversation: PayPlanConversation
    resolution: IntentResolution | None = None


class ConversationStateError(ValidationError):
    pass


class PayPlanConversationService:
    """Transactional, authenticated lifecycle for assistant conversations."""

    @classmethod
    @transaction.atomic
    def start(cls, user, request_text, effective_date, *, interpreter=None):
        version = active_version_for_user(user)
        conversation = PayPlanConversation(
            user=user,
            plan_version=version,
            conversation_key=uuid4().hex,
            context={'effective_date': effective_date.isoformat()},
        )
        conversation.full_clean()
        conversation.save()
        cls._assert_turn_capacity(conversation, additional=2)
        cls._append_turn(
            conversation,
            PayPlanConversationTurn.USER,
            request_text,
        )
        return cls._interpret_and_store(
            conversation,
            user,
            request_text,
            effective_date,
            prior_turns=(),
            interpreter=interpreter,
        )

    @classmethod
    @transaction.atomic
    def begin_existing(
        cls,
        user,
        conversation_key,
        request_text,
        effective_date,
        *,
        interpreter=None,
    ):
        conversation = cls._owned(conversation_key, user, for_update=True)
        cls._assert_open_and_current(conversation, user)
        if conversation.turns.exists() or conversation.pending_intent:
            raise ConversationStateError(
                'This conversation has already started. Resume it instead.'
            )
        cls._assert_turn_capacity(conversation, additional=2)
        conversation.context = {'effective_date': effective_date.isoformat()}
        conversation.save(update_fields=['context', 'updated_at'])
        cls._append_turn(
            conversation,
            PayPlanConversationTurn.USER,
            request_text,
        )
        return cls._interpret_and_store(
            conversation,
            user,
            request_text,
            effective_date,
            prior_turns=(),
            interpreter=interpreter,
        )

    @classmethod
    @transaction.atomic
    def start_over(cls, user, conversation_key):
        previous = cls._owned(conversation_key, user, for_update=True)
        if previous.status == PayPlanConversation.OPEN:
            previous.status = PayPlanConversation.CANCELLED
            previous.pending_intent = {}
            previous.selected_rule_key = ''
            previous.save(update_fields=[
                'status', 'pending_intent', 'selected_rule_key', 'updated_at',
            ])
        version = active_version_for_user(user)
        effective_date = (
            previous.context.get('effective_date')
            or (timezone.localdate() + timedelta(days=1)).isoformat()
        )
        conversation = PayPlanConversation(
            user=user,
            plan_version=version,
            conversation_key=uuid4().hex,
            context={'effective_date': effective_date},
        )
        conversation.full_clean()
        conversation.save()
        return ConversationOutcome(conversation)

    @classmethod
    @transaction.atomic
    def resume(cls, user, conversation_key):
        conversation = cls._owned(conversation_key, user, for_update=True)
        cls._refresh_lifecycle(conversation, user)
        resolution = None
        if conversation.status == PayPlanConversation.OPEN and conversation.pending_intent:
            intent = pending_payload_to_intent(
                conversation.pending_intent,
                source_text=cls._combined_user_text(conversation),
            )
            resolution = resolve_intent(
                user,
                intent,
                selected_target=conversation.selected_rule_key or None,
            )
        return ConversationOutcome(conversation, resolution)

    @classmethod
    @transaction.atomic
    def follow_up(
        cls,
        user,
        conversation_key,
        *,
        response_text='',
        candidate_index=None,
        interpreter=None,
    ):
        conversation = cls._owned(conversation_key, user, for_update=True)
        cls._assert_open_and_current(conversation, user)
        cls._assert_turn_capacity(conversation, additional=2)
        effective_date = date.fromisoformat(conversation.context['effective_date'])
        current_intent = pending_payload_to_intent(
            conversation.pending_intent,
            source_text=cls._combined_user_text(conversation),
        )

        if candidate_index not in (None, ''):
            current_resolution = resolve_intent(user, current_intent)
            candidates = current_resolution.intent.candidate_targets
            try:
                candidate = candidates[int(candidate_index)]
            except (IndexError, TypeError, ValueError) as exc:
                raise ConversationStateError(
                    'Select one of the currently available rules.'
                ) from exc
            user_text = f'Selected: {candidate.label} — {candidate.rule_name}'
            cls._append_turn(
                conversation,
                PayPlanConversationTurn.USER,
                user_text,
            )
            resolution = resolve_intent(
                user,
                current_intent,
                selected_target=candidate.selector,
            )
            conversation.selected_rule_key = candidate.selector
            route = conversation.context.get('interpretation_source', 'deterministic')
            provider_status = conversation.context.get('provider_status', 'disabled')
            interpreted = current_intent
        else:
            text = (response_text or '').strip()
            if not text:
                raise ConversationStateError('Enter a follow-up answer.')
            prior_turns = tuple(
                conversation.turns.filter(
                    role=PayPlanConversationTurn.USER,
                ).order_by('sequence').values_list('content', flat=True)
            )
            gateway = interpreter or configured_intent_interpreter()
            interpreted = gateway.interpret(
                text,
                effective_date=effective_date,
                prior_turns=prior_turns,
            )
            interpreted = merge_intents(current_intent, interpreted)
            cls._append_turn(
                conversation,
                PayPlanConversationTurn.USER,
                text,
            )
            conversation.selected_rule_key = ''
            resolution = resolve_intent(user, interpreted)
            route = gateway.last_route
            provider_status = gateway.last_provider_status

        cls._store_interpretation(
            conversation,
            interpreted,
            resolution,
            route=route,
            provider_status=provider_status,
        )
        return ConversationOutcome(conversation, resolution)

    @classmethod
    @transaction.atomic
    def cancel(cls, user, conversation_key):
        conversation = cls._owned(conversation_key, user, for_update=True)
        if conversation.status != PayPlanConversation.OPEN:
            raise ConversationStateError(
                'Only an open conversation can be cancelled.'
            )
        conversation.status = PayPlanConversation.CANCELLED
        conversation.pending_intent = {}
        conversation.selected_rule_key = ''
        conversation.save(update_fields=[
            'status', 'pending_intent', 'selected_rule_key', 'updated_at',
        ])
        return ConversationOutcome(conversation)

    @classmethod
    @transaction.atomic
    def create_draft(cls, user, conversation_key):
        conversation = cls._owned(conversation_key, user, for_update=True)
        cls._assert_open_and_current(conversation, user)
        if not conversation.pending_intent:
            raise ConversationStateError(
                'The conversation has no reviewed interpretation.'
            )
        intent = pending_payload_to_intent(
            conversation.pending_intent,
            source_text=cls._combined_user_text(conversation),
        )
        resolution = resolve_intent(
            user,
            intent,
            selected_target=conversation.selected_rule_key or None,
        )
        if not resolution.may_create_draft:
            raise ConversationStateError(
                resolution.message
                or 'The request needs clarification before a draft can be created.'
            )
        effective_date = date.fromisoformat(conversation.context['effective_date'])
        change_request = create_draft_from_intent(
            user,
            intent,
            effective_date,
            selected_target=conversation.selected_rule_key or None,
            expected_source_version_id=conversation.plan_version_id,
            expected_current_value=conversation.context.get(
                'expected_current_value',
            ),
        )
        conversation.status = PayPlanConversation.RESOLVED
        conversation.pending_intent = {}
        conversation.selected_rule_key = ''
        conversation.context = {
            **conversation.context,
            'draft_created': True,
        }
        conversation.save(update_fields=[
            'status', 'pending_intent', 'selected_rule_key', 'context',
            'updated_at',
        ])
        return change_request

    @classmethod
    def open_for_user(cls, user):
        conversations = list(
            PayPlanConversation.objects.filter(
                user=user,
                status=PayPlanConversation.OPEN,
            ).order_by('-updated_at')[:10]
        )
        for conversation in conversations:
            cls.refresh(conversation, user)
        return [
            item for item in conversations
            if item.status == PayPlanConversation.OPEN
        ]

    @classmethod
    @transaction.atomic
    def refresh(cls, conversation, user):
        locked = cls._owned(conversation.conversation_key, user, for_update=True)
        cls._refresh_lifecycle(locked, user)
        conversation.status = locked.status
        conversation.pending_intent = locked.pending_intent
        return conversation

    @classmethod
    def _interpret_and_store(
        cls,
        conversation,
        user,
        request_text,
        effective_date,
        *,
        prior_turns,
        interpreter=None,
    ):
        gateway = interpreter or configured_intent_interpreter()
        intent = gateway.interpret(
            request_text,
            effective_date=effective_date,
            prior_turns=prior_turns,
        )
        resolution = resolve_intent(user, intent)
        cls._store_interpretation(
            conversation,
            intent,
            resolution,
            route=gateway.last_route,
            provider_status=gateway.last_provider_status,
        )
        return ConversationOutcome(conversation, resolution)

    @classmethod
    def _store_interpretation(
        cls,
        conversation,
        intent,
        resolution,
        *,
        route,
        provider_status,
    ):
        conversation.pending_intent = intent_to_pending_payload(intent)
        context = {
            **conversation.context,
            'interpretation_source': route,
            'provider_status': provider_status,
        }
        if resolution.may_create_draft:
            context['expected_current_value'] = resolution.proposal.current_display
        else:
            context.pop('expected_current_value', None)
        conversation.context = context
        conversation.save(update_fields=[
            'pending_intent', 'selected_rule_key', 'context', 'updated_at',
        ])
        cls._append_turn(
            conversation,
            PayPlanConversationTurn.ASSISTANT,
            cls._assistant_message(resolution),
            structured_intent=intent_to_pending_payload(intent),
        )

    @staticmethod
    def _assistant_message(resolution):
        if resolution.status == 'clarification':
            return (
                resolution.message
                or resolution.intent.clarification_question
                or 'Please provide one more detail.'
            )
        if resolution.status == 'unsupported':
            return resolution.message
        return (
            'I have enough information to show an interpretation review. '
            'No draft has been created, and your active plan is unchanged.'
        )

    @classmethod
    def _append_turn(cls, conversation, role, content, *, structured_intent=None):
        # Callers hold a row lock (or own a not-yet-shared new conversation).
        last_sequence = conversation.turns.aggregate(
            maximum=Max('sequence'),
        )['maximum'] or 0
        return PayPlanConversationTurn.objects.create(
            conversation=conversation,
            role=role,
            content=(content or '').strip(),
            structured_intent=structured_intent or {},
            sequence=last_sequence + 1,
        )

    @staticmethod
    def _owned(conversation_key, user, *, for_update=False):
        queryset = PayPlanConversation.objects.select_related(
            'plan_version__pay_plan',
        ).filter(user=user, conversation_key=conversation_key)
        if for_update:
            queryset = queryset.select_for_update()
        try:
            return queryset.get()
        except PayPlanConversation.DoesNotExist as exc:
            raise ObjectDoesNotExist(
                'The conversation was not found.'
            ) from exc

    @classmethod
    def _assert_open_and_current(cls, conversation, user):
        cls._refresh_lifecycle(conversation, user)
        messages = {
            PayPlanConversation.CANCELLED: 'This conversation was cancelled.',
            PayPlanConversation.RESOLVED: 'This conversation is already resolved.',
            PayPlanConversation.EXPIRED: (
                'This conversation expired. Start over to make a new request.'
            ),
            PayPlanConversation.STALE: (
                'Your active pay plan changed. Start over to review the current plan.'
            ),
        }
        if conversation.status != PayPlanConversation.OPEN:
            raise ConversationStateError(messages.get(
                conversation.status,
                'This conversation is no longer open.',
            ))

    @classmethod
    def _refresh_lifecycle(cls, conversation, user):
        if conversation.status != PayPlanConversation.OPEN:
            return
        ttl = timedelta(
            hours=settings.PAY_PLAN_ASSISTANT_CONVERSATION_TTL_HOURS,
        )
        if conversation.updated_at + ttl <= timezone.now():
            cls._close_as(conversation, PayPlanConversation.EXPIRED)
            return
        try:
            active_version = active_version_for_user(user)
        except ValidationError:
            cls._close_as(conversation, PayPlanConversation.STALE)
            return
        if active_version.id != conversation.plan_version_id:
            cls._close_as(conversation, PayPlanConversation.STALE)

    @staticmethod
    def _close_as(conversation, status):
        conversation.status = status
        conversation.pending_intent = {}
        conversation.selected_rule_key = ''
        conversation.save(update_fields=[
            'status', 'pending_intent', 'selected_rule_key', 'updated_at',
        ])

    @staticmethod
    def _assert_turn_capacity(conversation, *, additional):
        count = conversation.turns.count()
        if count + additional > settings.PAY_PLAN_ASSISTANT_MAX_TURNS:
            raise ConversationStateError(
                'This conversation reached its turn limit. Start over to continue.'
            )

    @staticmethod
    def _combined_user_text(conversation):
        return '\n'.join(
            conversation.turns.filter(
                role=PayPlanConversationTurn.USER,
            ).order_by('sequence').values_list('content', flat=True)
        )


def intent_to_pending_payload(intent):
    payload = {
        'action': intent.action,
        'target_type': intent.target_type,
        'target_scope': intent.target_scope,
        'amount': _serialize_decimal(intent.amount),
        'percentage': _serialize_decimal(intent.percentage),
        'unit_threshold': _serialize_decimal(intent.unit_threshold),
        'current_value': _serialize_decimal(intent.current_value),
        'new_value': _serialize_decimal(intent.new_value),
        'conditions': [dict(item) for item in intent.conditions],
        'effective_date': (
            intent.effective_date.isoformat() if intent.effective_date else None
        ),
        'confidence': str(intent.confidence),
        'missing_information': list(intent.missing_information),
        'ambiguities': list(intent.ambiguities),
        'clarification_question': intent.clarification_question,
    }
    # This assertion prevents future PayPlanIntent fields from silently crossing
    # the pending-intent persistence boundary.
    if set(payload) != PENDING_INTENT_FIELDS:
        raise ValidationError('Pending intent schema is not allowlisted.')
    return payload


def pending_payload_to_intent(payload, *, source_text):
    if not isinstance(payload, dict) or set(payload) - PENDING_INTENT_FIELDS:
        raise ValidationError('Stored pending intent is invalid.')
    action = payload.get('action')
    target_type = payload.get('target_type')
    target_scope = payload.get('target_scope')
    if action is not None and action not in ACTIONS:
        raise ValidationError('Stored pending action is invalid.')
    if target_type is not None and target_type not in TARGET_TYPES:
        raise ValidationError('Stored pending target is invalid.')
    if target_scope is not None and target_scope not in PROVIDER_TARGET_SCOPES:
        raise ValidationError('Stored pending scope is invalid.')
    conditions = []
    for item in payload.get('conditions') or ():
        if not isinstance(item, dict) or set(item) - PROVIDER_CONDITION_KEYS:
            raise ValidationError('Stored pending condition is invalid.')
        field_name = item.get('field_name')
        operator = item.get('operator') or 'is_true'
        if field_name not in PROVIDER_CONDITION_FIELDS:
            raise ValidationError('Stored pending condition field is invalid.')
        if operator not in PROVIDER_CONDITION_OPERATORS:
            raise ValidationError('Stored pending condition operator is invalid.')
        conditions.append({
            'field_name': field_name,
            'operator': operator,
            'value': item.get('value'),
        })
    try:
        effective_date = (
            date.fromisoformat(payload['effective_date'])
            if payload.get('effective_date') else None
        )
        confidence = Decimal(str(payload.get('confidence', '0')))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError('Stored pending intent values are invalid.') from exc
    return PayPlanIntent(
        source_text=source_text,
        action=action,
        target_type=target_type,
        target_scope=target_scope,
        amount=_optional_decimal(payload.get('amount')),
        percentage=_optional_decimal(payload.get('percentage')),
        unit_threshold=_optional_decimal(payload.get('unit_threshold')),
        current_value=_optional_decimal(payload.get('current_value')),
        new_value=_optional_decimal(payload.get('new_value')),
        conditions=tuple(conditions),
        effective_date=effective_date,
        confidence=confidence,
        missing_information=tuple(payload.get('missing_information') or ()),
        ambiguities=tuple(payload.get('ambiguities') or ()),
        clarification_question=str(payload.get('clarification_question') or ''),
    )


def merge_intents(previous, interpreted):
    values = {
        name: (
            getattr(interpreted, name)
            if getattr(interpreted, name) is not None
            else getattr(previous, name)
        )
        for name in (
            'action', 'target_type', 'target_scope', 'amount', 'percentage',
            'unit_threshold', 'current_value', 'new_value',
        )
    }
    conditions = interpreted.conditions or previous.conditions
    missing = list(interpreted.missing_information)
    resolved_fields = {
        'action': values['action'],
        'target_type': values['target_type'],
        'target_scope': values['target_scope'],
        'amount': values['amount'],
        'percentage': values['percentage'],
        'unit_threshold': values['unit_threshold'],
        'current_value': values['current_value'],
        'new_value': values['new_value'],
        'conditions': conditions,
    }
    missing = [item for item in missing if not resolved_fields.get(item)]
    for item in previous.missing_information:
        if item not in missing and not resolved_fields.get(item):
            missing.append(item)
    question = interpreted.clarification_question
    if missing and not question:
        question = previous.clarification_question
    if not missing and not interpreted.ambiguities:
        question = ''
    return PayPlanIntent(
        source_text=interpreted.source_text,
        **values,
        conditions=conditions,
        effective_date=interpreted.effective_date or previous.effective_date,
        confidence=max(previous.confidence, interpreted.confidence),
        missing_information=tuple(dict.fromkeys(missing)),
        ambiguities=interpreted.ambiguities,
        clarification_question=question,
        normalized_text=interpreted.normalized_text,
    )


def _serialize_decimal(value):
    return None if value is None else str(value)


def _optional_decimal(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError('Stored pending numeric value is invalid.') from exc
