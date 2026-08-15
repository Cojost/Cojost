from django.conf import settings
from django.db import models


class EmailVerificationDispatch(models.Model):
    PENDING = 'pending'
    SENT = 'sent'
    SKIPPED = 'skipped'
    FAILED = 'failed'
    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (SENT, 'Sent'),
        (SKIPPED, 'Skipped'),
        (FAILED, 'Failed'),
    ]

    BACKFILL = 'backfill'
    TEAM_JOIN = 'team_join'
    SOURCE_CHOICES = [
        (BACKFILL, 'Existing-user verification backfill'),
        (TEAM_JOIN, 'Team invitation verification'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='email_verification_dispatches',
    )
    recipient_digest = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=PENDING,
    )
    attempted_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(
                fields=['user', 'status', 'attempted_at'],
                name='sl_ev_user_status_attempt',
            ),
        ]
