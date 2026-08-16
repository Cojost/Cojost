from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

SUNDAY_WEEKDAY = 6


class SellingDayClosure(models.Model):
    """An explicit owner-configured dealership closure date.

    SC-3 calendar source for the SC-2 projection engine. Sundays are always
    closed by contract and cannot be stored as explicit closures.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='selling_day_closures',
    )
    date = models.DateField()
    label = models.CharField(max_length=80, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'date'],
                name='unique_selling_day_closure_user_date',
            ),
            models.CheckConstraint(
                condition=~models.Q(date__week_day=1),
                name='selling_day_closure_not_sunday',
            ),
        ]
        indexes = [
            models.Index(
                fields=['user', 'date'],
                name='closure_user_date_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if self.date and self.date.weekday() == SUNDAY_WEEKDAY:
            raise ValidationError({
                'date': 'Sundays are always closed and cannot be added.',
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return f'{self.date.isoformat()} closure'
