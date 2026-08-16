from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from .models import SellingDayClosure
from .selling_calendar import (
    SellingDayCalendarError,
    StaticSellingDayCalendar,
)

CALENDAR_SOURCE_VERSION = 'owner-closures.v1'


def _is_plain_date(value: Any) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _closure_fingerprint(closures: tuple[date, ...]) -> str:
    payload = ','.join(value.isoformat() for value in closures)
    return hashlib.sha256(payload.encode('ascii')).hexdigest()[:12]


def owner_selling_calendar(
    owner: Any,
    *,
    month_start: date,
    month_end: date,
) -> StaticSellingDayCalendar:
    """Build the approved owner-bound SC-3 calendar for one month.

    Loads the owner's explicit closures with a single owner-scoped query and
    returns an immutable, versioned calendar. The version is deterministic for
    an unchanged closure set and changes whenever the closure set changes.
    Invalid owners or boundaries fail closed with ``SellingDayCalendarError``.
    """

    owner_id = getattr(owner, 'pk', None)
    if (
        owner is None
        or not getattr(owner, 'is_authenticated', False)
        or type(owner_id) is not int
        or owner_id <= 0
    ):
        raise SellingDayCalendarError(
            'A selling-day calendar requires an authenticated owner.'
        )
    if not _is_plain_date(month_start) or not _is_plain_date(month_end):
        raise SellingDayCalendarError('Calendar boundaries must be dates.')
    if month_end < month_start:
        raise SellingDayCalendarError('Calendar boundaries are invalid.')

    closures = tuple(
        SellingDayClosure.objects.filter(
            user_id=owner_id,
            date__gte=month_start,
            date__lte=month_end,
        )
        .order_by('date')
        .values_list('date', flat=True)
    )
    if any(not _is_plain_date(value) for value in closures):
        raise SellingDayCalendarError(
            'The stored selling-day closures are invalid.'
        )

    version = (
        f'{CALENDAR_SOURCE_VERSION}.{owner_id}.{_closure_fingerprint(closures)}'
    )
    return StaticSellingDayCalendar(
        owner_id=owner_id,
        closure_dates=frozenset(closures),
        calendar_version=version,
    )
