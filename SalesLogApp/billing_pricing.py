"""Synchronized display pricing and BILL-3 Price validation.

All monetary display values come from local dj-stripe Price rows. Page renders,
readiness checks, and automated tests never need a Stripe network call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings

from .billing_plans import (
    MONTH,
    PRICE_POLICY,
    PRO,
    YEAR,
    checkout_selections,
    price_id_for_checkout_selection,
    price_policy_for,
)

logger = logging.getLogger(__name__)

PRICING_VERSION = 'bill3.v1'

_CURRENCY_SYMBOLS = {'usd': '$'}
_INTERVAL_LABELS = {
    'day': 'per day',
    'week': 'per week',
    MONTH: 'per month',
    YEAR: 'per year',
}


@dataclass(frozen=True)
class DisplayPrice:
    available: bool
    formatted: str = ''
    amount: Decimal | None = None
    currency: str = ''
    interval: str = ''
    equivalent_monthly_formatted: str = ''


UNAVAILABLE_PRICE = DisplayPrice(available=False)


def _configured_price_id(tier, billing_interval, *, validate_candidate=False):
    try:
        if validate_candidate:
            policy = price_policy_for(tier, billing_interval)
            return getattr(settings, policy.setting_name)
        return price_id_for_checkout_selection(tier, billing_interval)
    except ValueError:
        return ''


def _synchronized_price(price_id):
    if not price_id:
        return None
    from djstripe.models import Price

    return Price.objects.filter(id=price_id).first()


def _price_policy_error(tier, billing_interval, *, validate_candidate=False):
    label = f'{tier} {billing_interval}ly'
    price_id = _configured_price_id(
        tier, billing_interval, validate_candidate=validate_candidate,
    )
    if not price_id:
        return f'the synchronized {label} Price is unavailable'
    price = _synchronized_price(price_id)
    if price is None:
        return f'the synchronized {label} Price is unavailable'
    if price.livemode != settings.STRIPE_LIVE_MODE:
        return f'the synchronized {label} Price is in the wrong Stripe mode'
    if not price.active:
        return f'the synchronized {label} Price is inactive'
    if price.type != 'recurring':
        return f'the synchronized {label} Price is not recurring'
    currency = (price.currency or '').lower()
    if currency != 'usd':
        return f'the synchronized {label} Price is not USD'
    policy = price_policy_for(tier, billing_interval)
    if price.unit_amount != policy.cents:
        return f'the synchronized {label} Price has the wrong amount'
    recurring = price.recurring or {}
    if (
        recurring.get('interval') != policy.interval
        or recurring.get('interval_count') != 1
    ):
        return f'the synchronized {label} Price has the wrong interval'
    return ''


def _format_price(price):
    recurring = price.recurring or {}
    interval = recurring.get('interval')
    interval_count = recurring.get('interval_count') or 1
    if price.type != 'recurring':
        return UNAVAILABLE_PRICE
    if interval not in _INTERVAL_LABELS or interval_count != 1:
        return UNAVAILABLE_PRICE
    if price.unit_amount is None:
        return UNAVAILABLE_PRICE
    amount = (Decimal(int(price.unit_amount)) / Decimal('100')).quantize(
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
    equivalent_monthly_formatted = ''
    if interval == YEAR:
        equivalent = (amount / Decimal('12')).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )
        equivalent_monthly_formatted = (
            f'{symbol}{equivalent} {currency.upper()} per month equivalent'
        )
    return DisplayPrice(
        available=True,
        formatted=formatted,
        amount=amount,
        currency=currency.upper(),
        interval=interval,
        equivalent_monthly_formatted=equivalent_monthly_formatted,
    )


def display_price(tier=PRO, billing_interval=MONTH) -> DisplayPrice:
    """Return one selection's display price from its synchronized Stripe row."""

    try:
        price_id = _configured_price_id(tier, billing_interval)
        price = _synchronized_price(price_id)
        if price is None:
            return UNAVAILABLE_PRICE
        if price.livemode != settings.STRIPE_LIVE_MODE or not price.active:
            return UNAVAILABLE_PRICE
        if (
            settings.BILLING_TIERED_PRICING_ENABLED
            and _price_policy_error(tier, billing_interval)
        ):
            return UNAVAILABLE_PRICE
        return _format_price(price)
    except Exception as exc:
        logger.warning(
            'Display price unavailable error_type=%s', type(exc).__name__,
        )
        return UNAVAILABLE_PRICE


def display_plan_prices(*, founder=False) -> dict[tuple[str, str], DisplayPrice]:
    return {
        selection: display_price(*selection)
        for selection in checkout_selections(founder=founder)
    }


def synchronized_plan_price_errors(
    *, validate_candidate=False,
) -> tuple[str, ...]:
    """Validate all BILL-3 checkout Prices without a Stripe network call."""

    if not settings.BILLING_TIERED_PRICING_ENABLED and not validate_candidate:
        return ()
    errors = []
    try:
        selections = (
            tuple(PRICE_POLICY)
            if validate_candidate
            else checkout_selections()
        )
        for tier, billing_interval in selections:
            error = _price_policy_error(
                tier,
                billing_interval,
                validate_candidate=validate_candidate,
            )
            if error:
                errors.append(error)
    except Exception as exc:
        logger.warning(
            'Synchronized price validation unavailable error_type=%s',
            type(exc).__name__,
        )
        return ('synchronized plan Prices are unavailable',)
    return tuple(errors)
