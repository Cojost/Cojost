from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable


_CALENDAR_VERSION_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,63}')


class SellingDayCalendarError(ValueError):
    """Raised when a selling-day calendar cannot be verified safely."""


def _validated_version(value: Any) -> str:
    if not isinstance(value, str) or not _CALENDAR_VERSION_PATTERN.fullmatch(value):
        raise SellingDayCalendarError('A valid calendar version is required.')
    return value


def _is_plain_date(value: Any) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


@runtime_checkable
class SellingDayCalendar(Protocol):
    """Owner-aware source of explicitly configured dealership closures."""

    @property
    def calendar_version(self) -> str: ...

    def closed_dates(
        self,
        *,
        owner: Any,
        month_start: date,
        month_end: date,
    ) -> frozenset[date]: ...


@dataclass(frozen=True, slots=True)
class StaticSellingDayCalendar:
    """Immutable calendar for tests and trusted internal callers."""

    owner_id: int
    closure_dates: frozenset[date]
    calendar_version: str

    def __post_init__(self) -> None:
        if type(self.owner_id) is not int or self.owner_id <= 0:
            raise SellingDayCalendarError('The calendar must be bound to an owner.')
        _validated_version(self.calendar_version)
        try:
            closures = frozenset(self.closure_dates)
        except TypeError as exc:
            raise SellingDayCalendarError(
                'Closure dates must be an immutable collection of dates.'
            ) from exc
        if any(not _is_plain_date(value) for value in closures):
            raise SellingDayCalendarError(
                'Every dealership closure must be a date.'
            )
        object.__setattr__(self, 'closure_dates', closures)

    @classmethod
    def for_owner(
        cls,
        owner: Any,
        *,
        closure_dates: Any,
        calendar_version: str,
    ) -> StaticSellingDayCalendar:
        return cls(
            owner_id=getattr(owner, 'pk', None),
            closure_dates=closure_dates,
            calendar_version=calendar_version,
        )

    def closed_dates(
        self,
        *,
        owner: Any,
        month_start: date,
        month_end: date,
    ) -> frozenset[date]:
        if getattr(owner, 'pk', None) != self.owner_id:
            raise SellingDayCalendarError(
                'The selling-day calendar does not belong to this owner.'
            )
        return frozenset(
            value
            for value in self.closure_dates
            if month_start <= value <= month_end
        )


def selling_dates_for_month(
    calendar: SellingDayCalendar,
    *,
    owner: Any,
    month_start: date,
    month_end: date,
) -> tuple[str, tuple[date, ...]]:
    """Return verified Monday-Saturday selling dates for one month."""

    if not _is_plain_date(month_start) or not _is_plain_date(month_end):
        raise SellingDayCalendarError('Calendar boundaries must be dates.')
    if month_end < month_start:
        raise SellingDayCalendarError('Calendar boundaries are invalid.')
    if calendar is None or not isinstance(calendar, SellingDayCalendar):
        raise SellingDayCalendarError('A selling-day calendar is required.')

    version = _validated_version(calendar.calendar_version)
    try:
        closures = calendar.closed_dates(
            owner=owner,
            month_start=month_start,
            month_end=month_end,
        )
    except SellingDayCalendarError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise SellingDayCalendarError(
            'The selling-day calendar returned invalid data.'
        ) from exc
    if not isinstance(closures, frozenset):
        raise SellingDayCalendarError(
            'The selling-day calendar must return immutable closure dates.'
        )
    if any(
        not _is_plain_date(value) or value < month_start or value > month_end
        for value in closures
    ):
        raise SellingDayCalendarError(
            'The selling-day calendar returned an invalid closure date.'
        )

    dates = []
    candidate = month_start
    while candidate <= month_end:
        if candidate.weekday() != 6 and candidate not in closures:
            dates.append(candidate)
        candidate += timedelta(days=1)
    return version, tuple(dates)
