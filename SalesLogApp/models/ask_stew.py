from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class AskStewConversation(models.Model):
    """Owner-scoped, short-lived conversation state for the Ask Stew pilot."""

    OPEN = 'open'
    CLOSED = 'closed'
    EXPIRED = 'expired'
    STATUS_CHOICES = [
        (OPEN, 'Open'),
        (CLOSED, 'Closed'),
        (EXPIRED, 'Expired'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ask_stew_conversations',
    )
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=OPEN,
        db_index=True,
    )
    last_intent = models.CharField(max_length=48, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(
                fields=['user', 'status', 'updated_at'],
                name='ask_stew_user_status_idx',
            ),
        ]

    def __str__(self):
        return f'Ask Stew {self.public_id} for {self.user}'


class AskStewTurn(models.Model):
    USER = 'user'
    ASSISTANT = 'assistant'
    ROLE_CHOICES = [
        (USER, 'User'),
        (ASSISTANT, 'Assistant'),
    ]

    ROUTE_DETERMINISTIC = 'deterministic'
    ROUTE_PROVIDER = 'provider_router'
    ROUTE_DECLINED = 'declined'
    ROUTE_FALLBACK = 'safe_fallback'
    ROUTE_CHOICES = [
        (ROUTE_DETERMINISTIC, 'Deterministic'),
        (ROUTE_PROVIDER, 'Provider router'),
        (ROUTE_DECLINED, 'Declined'),
        (ROUTE_FALLBACK, 'Safe fallback'),
    ]

    conversation = models.ForeignKey(
        AskStewConversation,
        on_delete=models.CASCADE,
        related_name='turns',
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.TextField()
    sequence = models.PositiveIntegerField()
    reply_to = models.OneToOneField(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='reply',
    )
    submission_ref = models.CharField(max_length=64, blank=True, db_index=True)
    intent = models.CharField(max_length=48, blank=True)
    route_source = models.CharField(
        max_length=24,
        choices=ROUTE_CHOICES,
        blank=True,
    )
    provider_status = models.CharField(max_length=32, blank=True)
    provider_used = models.BooleanField(default=False)
    verified = models.BooleanField(default=False)
    source_label = models.CharField(max_length=160, blank=True)
    notice = models.CharField(max_length=500, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['conversation', 'sequence']
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'sequence'],
                name='unique_ask_stew_turn_sequence',
            ),
            models.UniqueConstraint(
                fields=['conversation', 'submission_ref'],
                condition=~Q(submission_ref=''),
                name='unique_ask_stew_submission_ref',
            ),
        ]
        indexes = [
            models.Index(
                fields=['conversation', 'created_at'],
                name='ask_stew_conv_time_idx',
            ),
            models.Index(
                fields=['intent', 'route_source', 'created_at'],
                name='ask_stew_route_time_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if self.role == self.USER and self.reply_to_id:
            raise ValidationError('A user turn cannot reply to another turn.')
        if self.role == self.ASSISTANT and not self.reply_to_id:
            raise ValidationError('An assistant turn must reply to a user turn.')
        if self.reply_to_id:
            if self.reply_to.role != self.USER:
                raise ValidationError('An assistant turn must reply to a user turn.')
            if self.reply_to.conversation_id != self.conversation_id:
                raise ValidationError('Reply turns must belong to one conversation.')

    def __str__(self):
        return f'{self.role} turn {self.sequence} in {self.conversation.public_id}'


class AskStewFeedback(models.Model):
    """Explicit owner feedback for one completed Ask Stew answer."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ask_stew_feedback',
    )
    assistant_turn = models.OneToOneField(
        AskStewTurn,
        on_delete=models.CASCADE,
        related_name='feedback',
    )
    helpful = models.BooleanField()
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(
                fields=['helpful', 'updated_at'],
                name='ask_stew_helpful_time_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if self.assistant_turn.role != AskStewTurn.ASSISTANT:
            raise ValidationError('Feedback must reference an assistant turn.')
        if self.assistant_turn.conversation.user_id != self.user_id:
            raise ValidationError('Feedback must belong to the conversation owner.')

    def __str__(self):
        label = 'Helpful' if self.helpful else 'Not helpful'
        return f'{label}: {self.assistant_turn}'
