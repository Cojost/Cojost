from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .stew_coach_projection import (
    MetricProjection,
    StewCoachProjectionResult,
)

UNIT_QUANTUM = Decimal('0.1')
MONEY_QUANTUM = Decimal('0.01')
PERCENT_QUANTUM = Decimal('0.1')
UNAVAILABLE_DISPLAY = '—'

METRIC_LABELS: dict[str, str] = {
    'units': 'Units',
    'total_gross': 'Total gross',
    'commission': 'Commission',
}
_MONEY_METRICS = frozenset({'total_gross', 'commission'})

STATUS_LABELS: dict[str, str] = {
    'no_goal': 'No goal set',
    'insufficient_data': 'Not enough data',
    'goal_reached': 'Goal reached',
    'on_pace': 'On pace',
    'behind': 'Behind pace',
}
STATUS_BADGE_CLASSES: dict[str, str] = {
    'no_goal': 'status-inactive',
    'insufficient_data': 'status-inactive',
    'goal_reached': 'status-active',
    'on_pace': 'status-active',
    'behind': 'status-behind',
}
DIAGNOSTIC_MESSAGES: dict[str, str] = {
    'archive_snapshot_unavailable': (
        'Commission cannot be verified for part of this month, so the '
        'commission projection is unavailable.'
    ),
    'historical_pay_plan_incomplete': (
        'Commission cannot be verified for part of this month, so the '
        'commission projection is unavailable.'
    ),
    'commission_unavailable': (
        'Commission is unavailable for this month, so the commission '
        'projection is unavailable.'
    ),
    'future_period': 'This month has not started, so there is no projection yet.',
    'no_completed_selling_days': (
        'Projections appear after the first completed selling day.'
    ),
    'no_selling_days': 'This month has no selling days on the calendar.',
}
CALENDAR_UNAVAILABLE_MESSAGE = (
    'The selling-day calendar could not be verified, so pace and projection '
    'are unavailable for this month.'
)


def _rounded(value: Decimal | None, quantum: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _display(value: Decimal | None, *, metric_id: str) -> str:
    if value is None:
        return UNAVAILABLE_DISPLAY
    if metric_id in _MONEY_METRICS:
        return f'${_rounded(value, MONEY_QUANTUM):,f}'
    return f'{_rounded(value, UNIT_QUANTUM):,f}'


def _percent_display(value: Decimal | None) -> str:
    if value is None:
        return UNAVAILABLE_DISPLAY
    return f'{_rounded(value, PERCENT_QUANTUM):,f}%'


def _metric_row(metric: MetricProjection) -> dict[str, Any]:
    diagnostic_message = (
        DIAGNOSTIC_MESSAGES.get(metric.diagnostic_code)
        if metric.diagnostic_code else None
    )
    return {
        'metric_id': metric.metric_id,
        'label': METRIC_LABELS[metric.metric_id],
        'goal': _display(metric.goal, metric_id=metric.metric_id),
        'actual': _display(metric.actual, metric_id=metric.metric_id),
        'projected': _display(
            metric.projected_total, metric_id=metric.metric_id
        ),
        'remaining': _display(metric.remaining, metric_id=metric.metric_id),
        'required_pace': _display(
            metric.required_pace, metric_id=metric.metric_id
        ),
        'progress_percent': _percent_display(metric.progress_percent),
        'status': metric.status,
        'status_label': STATUS_LABELS[metric.status],
        'badge_class': STATUS_BADGE_CLASSES[metric.status],
        'diagnostic_message': diagnostic_message,
    }


def unavailable_projection_context() -> dict[str, Any]:
    """Fail-closed presentation when no verified projection exists."""

    return {
        'available': False,
        'message': CALENDAR_UNAVAILABLE_MESSAGE,
        'rows': (),
        'diagnostics': (),
    }


def present_projection(
    result: StewCoachProjectionResult,
) -> dict[str, Any]:
    """Template-ready SC-3 view of immutable SC-2 facts.

    Rounding is applied to rendered copies only; the projection result is
    frozen and never modified.
    """

    rows = tuple(_metric_row(metric) for metric in result.metrics)
    diagnostics = tuple(
        dict.fromkeys(
            row['diagnostic_message']
            for row in rows
            if row['diagnostic_message']
        )
    )
    return {
        'available': True,
        'message': None,
        'period_status': result.period_status,
        'month_start': result.month_start,
        'month_end': result.month_end,
        'as_of_date': result.requested_as_of_date,
        'total_selling_days': result.total_selling_days,
        'completed_selling_days': result.completed_selling_days,
        'remaining_selling_days': result.remaining_selling_days,
        'calculation_version': result.calculation_version,
        'calendar_version': result.calendar_version,
        'rows': rows,
        'diagnostics': diagnostics,
    }
