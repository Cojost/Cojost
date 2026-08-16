from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from .access import activity_goals_authorized
from .archive_aggregation import (
    ARCHIVE_SNAPSHOT_UNAVAILABLE,
    HISTORICAL_PLAN_INCOMPLETE,
    load_owned_sale_records,
)
from .models import MonthlyGoal
from .selling_calendar import SellingDayCalendar, selling_dates_for_month
from .services import month_bounds, reporting_commission_totals


CALCULATION_VERSION = 'sc2.v1'
PROJECTION_METHOD = 'linear_completed_day_rate_open_day'
ZERO = Decimal('0')

MetricId = Literal['units', 'total_gross', 'commission']
MetricStatus = Literal[
    'no_goal',
    'insufficient_data',
    'goal_reached',
    'on_pace',
    'behind',
]
PeriodStatus = Literal['future', 'in_progress', 'complete']
DiagnosticCode = Literal[
    'archive_snapshot_unavailable',
    'historical_pay_plan_incomplete',
    'commission_unavailable',
    'future_period',
    'no_completed_selling_days',
    'no_selling_days',
]

METRIC_ORDER: tuple[MetricId, ...] = (
    'units',
    'total_gross',
    'commission',
)
_COMMISSION_DIAGNOSTICS: frozenset[str] = frozenset({
    ARCHIVE_SNAPSHOT_UNAVAILABLE,
    HISTORICAL_PLAN_INCOMPLETE,
})


class StewCoachProjectionError(Exception):
    """Base error for the deterministic SC-2 projection boundary."""


class StewCoachAccessDenied(StewCoachProjectionError):
    """Raised when an owner does not have SC-1 Pro authorization."""


class StewCoachInputError(StewCoachProjectionError, ValueError):
    """Raised when projection dates or owner input cannot be verified."""


@dataclass(frozen=True, slots=True)
class MetricProjection:
    metric_id: MetricId
    goal: Decimal | None
    actual: Decimal | None
    actual_through_prior_day: Decimal | None
    remaining: Decimal | None
    progress_percent: Decimal | None
    projected_total: Decimal | None
    required_pace: Decimal | None
    status: MetricStatus
    diagnostic_code: DiagnosticCode | None = None


@dataclass(frozen=True, slots=True)
class StewCoachProjectionResult:
    calculation_version: str
    projection_method: str
    calendar_version: str
    month_start: date
    month_end: date
    requested_as_of_date: date
    effective_cutoff_date: date
    period_status: PeriodStatus
    total_selling_days: int
    completed_selling_days: int
    remaining_selling_days: int
    future_selling_days: int
    metrics: tuple[MetricProjection, ...]


@dataclass(frozen=True, slots=True)
class _ActualValues:
    units: Decimal
    total_gross: Decimal
    commission: Decimal | None
    commission_diagnostic: DiagnosticCode | None


def _is_plain_date(value: Any) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _period_status(
    as_of_date: date,
    month_start: date,
    month_end: date,
) -> PeriodStatus:
    if as_of_date < month_start:
        return 'future'
    if as_of_date > month_end:
        return 'complete'
    return 'in_progress'


def _commission_value(owner: Any, records: tuple[Any, ...]) -> tuple[
    Decimal | None,
    DiagnosticCode | None,
]:
    totals = reporting_commission_totals(owner, records)
    if totals['commission_complete'] and totals['total'] is not None:
        return Decimal(str(totals['total'])), None
    source = totals.get('commission_source')
    diagnostic: DiagnosticCode = (
        source if source in _COMMISSION_DIAGNOSTICS else 'commission_unavailable'
    )
    return None, diagnostic


def _actual_values(
    owner: Any,
    records: tuple[Any, ...],
    *,
    units: Decimal,
    total_gross: Decimal,
) -> _ActualValues:
    commission, diagnostic = _commission_value(owner, records)
    return _ActualValues(
        units=units,
        total_gross=total_gross,
        commission=commission,
        commission_diagnostic=diagnostic,
    )


def _zero_actuals() -> _ActualValues:
    return _ActualValues(
        units=ZERO,
        total_gross=ZERO,
        commission=ZERO,
        commission_diagnostic=None,
    )


def _metric_projection(
    *,
    metric_id: MetricId,
    goal: Decimal | None,
    actual: Decimal | None,
    prior_actual: Decimal | None,
    period_status: PeriodStatus,
    total_days: int,
    completed_days: int,
    remaining_days: int,
    future_days: int,
    unavailable_diagnostic: DiagnosticCode | None = None,
) -> MetricProjection:
    projection_diagnostic = unavailable_diagnostic
    if actual is None:
        projected = None
    elif total_days == 0:
        projected = None
        projection_diagnostic = projection_diagnostic or 'no_selling_days'
    elif period_status == 'future':
        projected = None
        projection_diagnostic = projection_diagnostic or 'future_period'
    elif period_status == 'complete':
        projected = actual
    elif completed_days == 0 or prior_actual is None:
        projected = None
        projection_diagnostic = (
            projection_diagnostic or 'no_completed_selling_days'
        )
    else:
        projected = actual + (
            prior_actual / Decimal(completed_days) * Decimal(future_days)
        )

    positive_goal = goal is not None and goal > ZERO
    if positive_goal and actual is not None:
        remaining = max(goal - actual, ZERO)
        progress_percent = actual / goal * Decimal('100')
    else:
        remaining = None
        progress_percent = None

    if not positive_goal or actual is None or total_days == 0:
        required_pace = None
    elif remaining == ZERO:
        required_pace = ZERO
    elif remaining_days == 0:
        required_pace = None
    else:
        required_pace = remaining / Decimal(remaining_days)

    if not positive_goal:
        status: MetricStatus = 'no_goal'
    elif actual is None or projected is None:
        status = 'insufficient_data'
    elif actual >= goal:
        status = 'goal_reached'
    elif projected >= goal:
        status = 'on_pace'
    else:
        status = 'behind'

    return MetricProjection(
        metric_id=metric_id,
        goal=goal,
        actual=actual,
        actual_through_prior_day=prior_actual,
        remaining=remaining,
        progress_percent=progress_percent,
        projected_total=projected,
        required_pace=required_pace,
        status=status,
        diagnostic_code=projection_diagnostic,
    )


class StewCoachProjectionService:
    """Calculate owner-scoped SC-2 facts without persisting any result."""

    @classmethod
    def calculate(
        cls,
        *,
        owner: Any,
        month_start: date,
        as_of_date: date,
        calendar: SellingDayCalendar,
    ) -> StewCoachProjectionResult:
        if (
            owner is None
            or not getattr(owner, 'is_authenticated', False)
            or getattr(owner, 'pk', None) is None
        ):
            raise StewCoachAccessDenied('SC-2 requires an authenticated owner.')
        if not activity_goals_authorized(owner):
            raise StewCoachAccessDenied('SC-2 requires Pro access.')
        if not _is_plain_date(month_start) or not _is_plain_date(as_of_date):
            raise StewCoachInputError(
                'Month and as-of values must be explicit dates.'
            )

        normalized_month, next_month = month_bounds(month_start)
        month_end = next_month - timedelta(days=1)
        calendar_version, selling_dates = selling_dates_for_month(
            calendar,
            owner=owner,
            month_start=normalized_month,
            month_end=month_end,
        )
        period_status = _period_status(
            as_of_date,
            normalized_month,
            month_end,
        )
        if period_status == 'future':
            effective_cutoff = normalized_month - timedelta(days=1)
            completed_days = 0
            future_days = len(selling_dates)
            remaining_days = len(selling_dates)
        elif period_status == 'complete':
            effective_cutoff = month_end
            completed_days = len(selling_dates)
            future_days = 0
            remaining_days = 0
        else:
            effective_cutoff = as_of_date
            completed_days = sum(value < as_of_date for value in selling_dates)
            future_days = sum(value > as_of_date for value in selling_dates)
            remaining_days = sum(value >= as_of_date for value in selling_dates)

        goal = MonthlyGoal.objects.filter(
            user=owner,
            month_start=normalized_month,
        ).first()

        if period_status == 'future':
            prior_values = actual_values = _zero_actuals()
        else:
            record_set = load_owned_sale_records(
                owner,
                start_date=normalized_month,
                end_date=next_month,
            )
            actual_records = []
            prior_records = []
            actual_units = ZERO
            prior_units = ZERO
            actual_gross = ZERO
            prior_gross = ZERO
            prior_cutoff = (
                month_end
                if period_status == 'complete'
                else as_of_date - timedelta(days=1)
            )
            for record in record_set.records:
                if record.date <= effective_cutoff:
                    unit_credit = Decimal(str(record.unit_credit or 0))
                    gross = (
                        Decimal(str(record.frontEnd or 0))
                        + Decimal(str(record.backend or 0))
                    )
                    actual_records.append(record)
                    actual_units += unit_credit
                    actual_gross += gross
                    if record.date <= prior_cutoff:
                        prior_records.append(record)
                        prior_units += unit_credit
                        prior_gross += gross

            actual_record_tuple = tuple(actual_records)
            prior_record_tuple = tuple(prior_records)
            actual_values = _actual_values(
                owner,
                actual_record_tuple,
                units=actual_units,
                total_gross=actual_gross,
            )
            if prior_record_tuple == actual_record_tuple:
                prior_values = actual_values
            else:
                prior_values = _actual_values(
                    owner,
                    prior_record_tuple,
                    units=prior_units,
                    total_gross=prior_gross,
                )

        goals: dict[MetricId, Decimal | None] = {
            'units': goal.target_units if goal else None,
            'total_gross': goal.target_total_gross if goal else None,
            'commission': goal.target_commission if goal else None,
        }
        actuals: dict[MetricId, Decimal | None] = {
            'units': actual_values.units,
            'total_gross': actual_values.total_gross,
            'commission': actual_values.commission,
        }
        prior_actuals: dict[MetricId, Decimal | None] = {
            'units': prior_values.units,
            'total_gross': prior_values.total_gross,
            'commission': (
                prior_values.commission
                if actual_values.commission is not None else None
            ),
        }
        metrics = tuple(
            _metric_projection(
                metric_id=metric_id,
                goal=goals[metric_id],
                actual=actuals[metric_id],
                prior_actual=prior_actuals[metric_id],
                period_status=period_status,
                total_days=len(selling_dates),
                completed_days=completed_days,
                remaining_days=remaining_days,
                future_days=future_days,
                unavailable_diagnostic=(
                    actual_values.commission_diagnostic
                    if metric_id == 'commission' else None
                ),
            )
            for metric_id in METRIC_ORDER
        )
        return StewCoachProjectionResult(
            calculation_version=CALCULATION_VERSION,
            projection_method=PROJECTION_METHOD,
            calendar_version=calendar_version,
            month_start=normalized_month,
            month_end=month_end,
            requested_as_of_date=as_of_date,
            effective_cutoff_date=effective_cutoff,
            period_status=period_status,
            total_selling_days=len(selling_dates),
            completed_selling_days=completed_days,
            remaining_selling_days=remaining_days,
            future_selling_days=future_days,
            metrics=metrics,
        )
