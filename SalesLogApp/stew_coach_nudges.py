"""SC-5 proactive in-app nudges over allowlisted Stew Coach facts.

Nudges are deterministic and derived only from the SC-3 presentation context
(rounded copies of frozen SC-2 facts) plus two owner-scoped lookups: whether
any daily activity exists for the month and which nudges the owner already
dismissed. Nudges never compute new numbers, appear only for in-progress
months, and fail closed to no nudges on any error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .models import DailyActivity, StewCoachNudgeDismissal
from .models.nudges import (
    NUDGE_BEHIND_PACE,
    NUDGE_LOG_ACTIVITY,
    NUDGE_MONTH_END_PUSH,
    NUDGE_SET_GOALS,
)

NUDGES_VERSION = 'sc5.v1'
MAX_VISIBLE_NUDGES = 2
MONTH_END_REMAINING_SELLING_DAYS = 5

LEVEL_INFO = 'info'
LEVEL_WARNING = 'warning'


@dataclass(frozen=True)
class StewCoachNudge:
    key: str
    level: str
    message: str


def _labels_text(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f'{labels[0]} and {labels[1].lower()}'
    joined = ', '.join(label.lower() for label in labels[1:-1])
    return f'{labels[0]}, {joined}, and {labels[-1].lower()}'


def _nudgeable(presentation: Any) -> bool:
    return (
        isinstance(presentation, Mapping)
        and bool(presentation.get('available'))
        and presentation.get('period_status') == 'in_progress'
    )


def candidate_nudges(
    presentation: Mapping[str, Any],
    *,
    has_activity: bool,
) -> tuple[StewCoachNudge, ...]:
    """Ordered candidate nudges before dismissal filtering."""

    if not _nudgeable(presentation):
        return ()
    month_label = presentation['month_start'].strftime('%B %Y')
    rows = tuple(presentation['rows'])
    remaining = presentation['remaining_selling_days']
    behind_labels = [row['label'] for row in rows if row['status'] == 'behind']
    nudges: list[StewCoachNudge] = []
    if behind_labels and 0 < remaining <= MONTH_END_REMAINING_SELLING_DAYS:
        day_word = 'day' if remaining == 1 else 'days'
        nudges.append(StewCoachNudge(
            NUDGE_MONTH_END_PUSH,
            LEVEL_WARNING,
            f'Only {remaining} selling {day_word} left in {month_label}. '
            f'{_labels_text(behind_labels)} still behind pace — finish '
            'strong.',
        ))
    elif behind_labels and remaining > MONTH_END_REMAINING_SELLING_DAYS:
        nudges.append(StewCoachNudge(
            NUDGE_BEHIND_PACE,
            LEVEL_WARNING,
            f'{_labels_text(behind_labels)} behind pace for {month_label}. '
            'Open Activity & Goals to see the pace you need.',
        ))
    if rows and all(row['status'] == 'no_goal' for row in rows):
        nudges.append(StewCoachNudge(
            NUDGE_SET_GOALS,
            LEVEL_INFO,
            f'No goals are set for {month_label}. Set unit, total gross, '
            'and commission goals to unlock pace coaching.',
        ))
    if not has_activity:
        nudges.append(StewCoachNudge(
            NUDGE_LOG_ACTIVITY,
            LEVEL_INFO,
            f'No daily activity is logged for {month_label}. Log leads and '
            'calls to keep your work on record.',
        ))
    return tuple(nudges)


def active_nudges(owner, presentation: Any) -> tuple[StewCoachNudge, ...]:
    """Owner-scoped visible nudges for the presented month."""

    if owner is None or not getattr(owner, 'is_authenticated', False):
        return ()
    if not _nudgeable(presentation):
        return ()
    month_start = presentation['month_start']
    month_end = presentation['month_end']
    has_activity = DailyActivity.objects.filter(
        user=owner, date__gte=month_start, date__lte=month_end,
    ).exists()
    nudges = candidate_nudges(presentation, has_activity=has_activity)
    if not nudges:
        return ()
    dismissed = set(
        StewCoachNudgeDismissal.objects.filter(
            user=owner,
            month_start=month_start,
            nudge_key__in=[nudge.key for nudge in nudges],
        ).values_list('nudge_key', flat=True)
    )
    visible = tuple(
        nudge for nudge in nudges if nudge.key not in dismissed
    )
    return visible[:MAX_VISIBLE_NUDGES]
