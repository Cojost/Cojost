"""SC-6 display pricing sourced from the synchronized dj-stripe Price.

The displayed subscription price must come from the configured Price row in
the local database (dj-stripe owns Stripe objects). No price is ever
hardcoded in templates and no Stripe network call happens on page render.
If the configured Price is missing, inactive, in the wrong mode, or not a
simple monthly recurring price, this fails closed to "unavailable" and
templates fall back to copy without a number — never a wrong number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

from .billing_plans import (
    BASIC,
    PRO,
    EXPECTED_MONTHLY_CENTS,
    checkout_tiers,
    price_id_for_checkout_tier,
)

logger = logging.getLogger(__name__)

PRICING_VERSION = 'bill2.v1'

_CURRENCY_SYMBOLS = {'usd': '$'}
_INTERVAL_LABELS = {
    'day': 'per day',
    'week': 'per week',
    'month': 'per month',
    'year': 'per year',
}


@dataclass(frozen=True)
class DisplayPrice:
    available: bool
    formatted: str = ''
    amount: Decimal | None = None
    currency: str = ''
    interval: str = ''


UNAVAILABLE_PRICE = DisplayPrice(available=False)


def display_price(tier=PRO) -> DisplayPrice:
    """Return one plan's display price from its synchronized Stripe row."""

    try:
        price_id = price_id_for_checkout_tier(tier)
    except ValueError:
        return UNAVAILABLE_PRICE
    if not price_id:
        return UNAVAILABLE_PRICE
    try:
        from djstripe.models import Price

        price = Price.objects.filter(
            id=price_id,
            livemode=settings.STRIPE_LIVE_MODE,
            active=True,
        ).first()
        if price is None:
            return UNAVAILABLE_PRICE
        if price.type != 'recurring':
            return UNAVAILABLE_PRICE
        recurring = price.recurring or {}
        interval = recurring.get('interval')
        interval_count = recurring.get('interval_count') or 1
        if interval not in _INTERVAL_LABELS or interval_count != 1:
            return UNAVAILABLE_PRICE
        unit_amount = price.unit_amount
        if unit_amount is None:
            return UNAVAILABLE_PRICE
        amount = (Decimal(int(unit_amount)) / Decimal('100')).quantize(
            Decimal('0.01'),
        )
        currency = (price.currency or '').lower()
        if not currency:
            return UNAVAILABLE_PRICE
        symbol = _CURRENCY_SYMBOLS.get(currency, '')
        formatted = (
            f'{symbol}{amount} {currency.upper()} '
            f'{_INTERVAL_LABELS[interval]}'
        )
        return DisplayPrice(
            available=True,
            formatted=formatted,
            amount=amount,
            currency=currency.upper(),
            interval=interval,
        )
    except Exception as exc:
        logger.warning(
            'Display price unavailable error_type=%s', type(exc).__name__,
        )
        return UNAVAILABLE_PRICE


def display_plan_prices(*, founder=False) -> dict[str, DisplayPrice]:
    return {
        tier: display_price(tier)
        for tier in checkout_tiers(founder=founder)
    }


def synchronized_plan_price_errors() -> tuple[str, ...]:
    """Validate BILL-2 checkout Prices without making a Stripe network call."""

    if not settings.BILLING_TIERED_PRICING_ENABLED:
        return ()
    errors = []
    for tier in (BASIC, PRO):
        price = display_price(tier)
        if not price.available:
            errors.append(f'the synchronized {tier} monthly Price is unavailable')
            continue
        expected = Decimal(EXPECTED_MONTHLY_CENTS[tier]) / Decimal('100')
        if (
            price.amount != expected
            or price.currency != 'USD'
            or price.interval != 'month'
        ):
            errors.append(
                f'the synchronized {tier} monthly Price does not match policy'
            )
    return tuple(errors)
