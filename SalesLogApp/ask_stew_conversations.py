from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .ask_stew import AskStewAnswer
from .models import AskStewConversation, AskStewFeedback, AskStewTurn


class AskStewConversationError(ValidationError):
    pass


class AskStewRateLimitError(AskStewConversationError):
    pass


@dataclass(frozen=True)
class PreparedAskStewSubmission:
    conversation: AskStewConversation
    user_turn: AskStewTurn
    previous_intent: str = ''
    previous_question: str = ''
    duplicate: bool = False


def _submission_reference(submission_token):
    if not submission_token:
        return ''
    return hashlib.sha256(str(submission_token).encode('utf-8')).hexdigest()


class AskStewConversationService:
    """Transactional, owner-scoped lifecycle for read-only Ask Stew threads."""

    @classmethod
    @transaction.atomic
    def prepare_submission(
        cls,
        user,
        question,
        submission_token,
        *,
        conversation_id=None,
    ):
        # Serialize per-user throttling even when two tabs start new threads.
        get_user_model().objects.select_for_update().get(pk=user.pk)
        conversation = None
        if conversation_id:
            conversation = cls._owned(
                user,
                conversation_id,
                for_update=True,
            )
            cls._assert_open(conversation)

        submission_ref = _submission_reference(submission_token)
        if submission_ref:
            existing = AskStewTurn.objects.select_related('conversation').filter(
                conversation__user=user,
                role=AskStewTurn.USER,
                submission_ref=submission_ref,
            ).first()
            if existing is not None:
                conversation = existing.conversation
                previous = cls._previous_user_turn(
                    conversation,
                    before=existing.sequence,
                )
                return PreparedAskStewSubmission(
                    conversation,
                    existing,
                    conversation.last_intent,
                    previous.content if previous else '',
                    True,
                )

        cls._assert_short_window_capacity(user)
        if conversation is None:
            conversation = AskStewConversation.objects.create(user=user)
        cls._assert_turn_capacity(conversation)

        previous = cls._previous_user_turn(conversation)
        next_sequence = cls._next_sequence(conversation)
        user_turn = AskStewTurn(
            conversation=conversation,
            role=AskStewTurn.USER,
            content=str(question or '').strip(),
            sequence=next_sequence,
            submission_ref=submission_ref,
        )
        user_turn.full_clean()
        user_turn.save()
        return PreparedAskStewSubmission(
            conversation,
            user_turn,
            conversation.last_intent,
            previous.content if previous else '',
        )

    @classmethod
    @transaction.atomic
    def complete_submission(cls, user, prepared, answer, *, duration_ms=0):
        conversation = cls._owned(
            user,
            prepared.conversation.public_id,
            for_update=True,
        )
        user_turn = AskStewTurn.objects.select_for_update().get(
            pk=prepared.user_turn.pk,
            conversation=conversation,
            role=AskStewTurn.USER,
        )
        existing_reply = AskStewTurn.objects.filter(
            conversation=conversation,
            reply_to=user_turn,
            role=AskStewTurn.ASSISTANT,
        ).first()
        if existing_reply is not None:
            return existing_reply
        assistant_turn = AskStewTurn(
            conversation=conversation,
            role=AskStewTurn.ASSISTANT,
            content=answer.answer,
            sequence=cls._next_sequence(conversation),
            reply_to=user_turn,
            intent=answer.intent,
            route_source=answer.route_source,
            provider_status=answer.provider_status,
            provider_used=answer.provider_used,
            verified=answer.verified,
            source_label=answer.source_label,
            notice=answer.notice,
            duration_ms=max(0, int(duration_ms)),
        )
        assistant_turn.full_clean()
        assistant_turn.save()
        if answer.intent and answer.intent not in {
            'clarification',
            'declined_change',
            'declined_hypothetical',
            'declined_security',
            'declined_unsupported',
        }:
            conversation.last_intent = answer.intent
        conversation.save(update_fields=['last_intent', 'updated_at'])
        return assistant_turn

    @classmethod
    @transaction.atomic
    def record_feedback(cls, user, conversation_id, assistant_turn_id, helpful):
        conversation = cls._owned(
            user,
            conversation_id,
            for_update=True,
        )
        turn = AskStewTurn.objects.select_for_update().filter(
            pk=assistant_turn_id,
            conversation=conversation,
            role=AskStewTurn.ASSISTANT,
        ).first()
        if turn is None:
            raise AskStewConversationError(
                'That Ask Stew answer is no longer available.',
            )
        feedback = AskStewFeedback.objects.filter(
            assistant_turn=turn,
        ).first() or AskStewFeedback(assistant_turn=turn)
        feedback.user = user
        feedback.helpful = bool(helpful)
        feedback.full_clean()
        feedback.save()
        return feedback

    @classmethod
    def load_owned(cls, user, conversation_id):
        try:
            conversation = AskStewConversation.objects.filter(
                user=user,
                public_id=conversation_id,
            ).first()
        except (TypeError, ValueError, ValidationError):
            conversation = None
        if conversation is None:
            raise AskStewConversationError(
                'That Ask Stew conversation is no longer available.',
            )
        ttl = timedelta(hours=settings.ASK_STEW_AI_CONVERSATION_TTL_HOURS)
        if conversation.updated_at + ttl <= timezone.now():
            raise AskStewConversationError(
                'That Ask Stew conversation expired. Start a new conversation.',
            )
        return conversation

    @staticmethod
    def turns_for(conversation):
        return conversation.turns.select_related('feedback', 'reply_to').order_by(
            'sequence',
        )

    @classmethod
    def _owned(cls, user, conversation_id, *, for_update=False):
        queryset = AskStewConversation.objects
        if for_update:
            queryset = queryset.select_for_update()
        try:
            conversation = queryset.filter(
                user=user,
                public_id=conversation_id,
            ).first()
        except (TypeError, ValueError, ValidationError):
            conversation = None
        if conversation is None:
            raise AskStewConversationError(
                'That Ask Stew conversation is no longer available.',
            )
        return conversation

    @classmethod
    def _assert_open(cls, conversation):
        ttl = timedelta(hours=settings.ASK_STEW_AI_CONVERSATION_TTL_HOURS)
        if conversation.updated_at + ttl <= timezone.now():
            raise AskStewConversationError(
                'That Ask Stew conversation expired. Start a new conversation.',
            )
        if conversation.status != AskStewConversation.OPEN:
            raise AskStewConversationError(
                'That Ask Stew conversation is closed. Start a new conversation.',
            )

    @staticmethod
    def _assert_short_window_capacity(user):
        seconds = settings.ASK_STEW_AI_SHORT_WINDOW_SECONDS
        cutoff = timezone.now() - timedelta(seconds=seconds)
        recent = AskStewTurn.objects.filter(
            conversation__user=user,
            role=AskStewTurn.USER,
            created_at__gte=cutoff,
        ).count()
        if recent >= settings.ASK_STEW_AI_SHORT_WINDOW_LIMIT:
            raise AskStewRateLimitError(
                'Too many Ask Stew questions were submitted too quickly. '
                'Wait a moment and try again.',
            )

    @staticmethod
    def _assert_turn_capacity(conversation):
        current = conversation.turns.count()
        if current + 2 > settings.ASK_STEW_AI_MAX_TURNS:
            raise AskStewConversationError(
                'This conversation reached its turn limit. Start a new '
                'conversation to continue.',
            )

    @staticmethod
    def _next_sequence(conversation):
        current = conversation.turns.aggregate(value=Max('sequence'))['value']
        return (current or 0) + 1

    @staticmethod
    def _previous_user_turn(conversation, *, before=None):
        queryset = conversation.turns.filter(role=AskStewTurn.USER)
        if before is not None:
            queryset = queryset.filter(sequence__lt=before)
        return queryset.order_by('-sequence').first()


def safe_failure_answer():
    return AskStewAnswer(
        intent='clarification',
        answer=(
            'I could not prepare that explanation safely. No account data was '
            'changed. Try a more specific question or try again.'
        ),
        provider_status='provider_unavailable',
        route_source='safe_fallback',
    )
