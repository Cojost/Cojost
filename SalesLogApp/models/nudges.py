"""SC-5 Stew Coach nudge dismissals.

Dismissals are owner-scoped and month-scoped. They only hide a nudge for the
owner who dismissed it; they never change calculation, projection, calendar,
or team behavior.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

NUDGE_SET_GOALS = 'set_goals'
NUDGE_LOG_ACTIVITY = 'log_activity'
NUDGE_BEHIND_PACE = 'behind_pace'
NUDGE_MONTH_END_PUSH = 'month_end_push'

NUDGE_KEY_CHOICES = (
    (NUDGE_SET_GOALS, 'Set monthly goals'),
    (NUDGE_LOG_ACTIVITY, 'Log daily activity'),
    (NUDGE_BEHIND_PACE, 'Behind pace'),
    (NUDGE_MONTH_END_PUSH, 'Month-end push'),
)
NUDGE_KEYS = frozenset(key for key, _label in NUDGE_KEY_CHOICES)


class StewCoachNudgeDismissal(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='stew_coach_nudge_dismissals',
    )
    nudge_key = models.CharField(max_length=32, choices=NUDGE_KEY_CHOICES)
    month_start = models.DateField()
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-month_start', 'nudge_key']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'nudge_key', 'month_start'],
                name='unique_stew_nudge_dismissal_user_key_month',
            ),
        ]

    def clean(self):
        super().clean()
        if self.month_start and self.month_start.day != 1:
            raise ValidationError(
                {'month_start': 'Nudge dismissals are month-scoped and must '
                                'use the first day of the month.'},
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.nudge_key} dismissed for {self.month_start:%Y-%m}'
