"""Server-owned BILL-3 plan and Stripe Price policy.

Browser input may select only an allowlisted tier and billing interval. Stripe
Price IDs always come from deployment configuration, and synchronized
subscription items are classified against the same allowlist. The rollout flag
preserves the original one-Price behavior until all current Prices and the
legacy-Pro allowlist are ready.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


BASIC = 'basic'
PRO = 'pro'
MONTH = 'month'
YEAR = 'year'
PLAN_CHOICES = ((BASIC, 'Basic'), (PRO, 'Pro'))
BILLING_INTERVAL_CHOICES = ((MONTH, 'Monthly'), (YEAR, 'Yearly'))


@dataclass(frozen=True)
class PricePolicy:
    setting_name: str
    cents: int
    interval: str


PRICE_POLICY = {
    (BASIC, MONTH): PricePolicy(
        'STRIPE_BASIC_MONTHLY_PRICE_ID', 499, MONTH,
    ),
    (BASIC, YEAR): PricePolicy(
        'STRIPE_BASIC_YEARLY_PRICE_ID', 4900, YEAR,
    ),
    (PRO, MONTH): PricePolicy(
        'STRIPE_PRO_MONTHLY_PRICE_ID', 999, MONTH,
    ),
    (PRO, YEAR): PricePolicy(
        'STRIPE_PRO_YEARLY_PRICE_ID', 9900, YEAR,
    ),
}


class BillingPlanError(ValueError):
    pass


@dataclass(frozen=True)
class SubscriptionPlan:
    eligible: bool
    tier: str = BASIC
    billing_interval: str = ''
    grandfathered: bool = False
    price_id: str = ''


def tiered_pricing_enabled() -> bool:
    return settings.BILLING_TIERED_PRICING_ENABLED


def legacy_pro_price_ids() -> tuple[str, ...]:
    return tuple(dict.fromkeys(settings.STRIPE_LEGACY_PRO_PRICE_IDS))


def checkout_tiers(*, founder=False) -> tuple[str, ...]:
    if not tiered_pricing_enabled() or founder:
        return (PRO,)
    return (BASIC, PRO)


def checkout_intervals() -> tuple[str, ...]:
    if not tiered_pricing_enabled():
        return (MONTH,)
    return (MONTH, YEAR)


def checkout_selections(*, founder=False) -> tuple[tuple[str, str], ...]:
    return tuple(
        (tier, billing_interval)
        for tier in checkout_tiers(founder=founder)
        for billing_interval in checkout_intervals()
    )


def price_policy_for(tier: str, billing_interval: str) -> PricePolicy:
    try:
        return PRICE_POLICY[(tier, billing_interval)]
    except KeyError as exc:
        raise BillingPlanError('The selected plan is unavailable.') from exc


def price_id_for_checkout_selection(tier: str, billing_interval: str) -> str:
    if not tiered_pricing_enabled():
        if tier != PRO or billing_interval != MONTH:
            raise BillingPlanError('The selected plan is unavailable.')
        return settings.STRIPE_BASIC_MONTHLY_PRICE_ID
    policy = price_policy_for(tier, billing_interval)
    return getattr(settings, policy.setting_name)


def current_price_ids() -> dict[tuple[str, str], str]:
    return {
        selection: getattr(settings, policy.setting_name)
        for selection, policy in PRICE_POLICY.items()
    }


def configured_subscription_price_ids() -> frozenset[str]:
    if not tiered_pricing_enabled():
        values = (settings.STRIPE_BASIC_MONTHLY_PRICE_ID,)
    else:
        values = (*current_price_ids().values(), *legacy_pro_price_ids())
    return frozenset(value for value in values if value)


def subscription_price_ids(subscription) -> tuple[str, ...]:
    items = ((subscription.stripe_data or {}).get('items') or {}).get('data') or []
    if not isinstance(items, (list, tuple)):
        return ()
    values = []
    for item in items:
        if not isinstance(item, dict):
            continue
        price = item.get('price') or item.get('plan') or {}
        price_id = price.get('id') if isinstance(price, dict) else price
        if isinstance(price_id, str) and price_id:
            values.append(price_id)
    return tuple(values)


def subscription_uses_price(subscription, price_id: str) -> bool:
    return bool(price_id) and subscription_price_ids(subscription) == (price_id,)


def classify_subscription_plan(subscription) -> SubscriptionPlan:
    items = ((subscription.stripe_data or {}).get('items') or {}).get('data') or []
    price_ids = subscription_price_ids(subscription)
    if (
        not isinstance(items, (list, tuple))
        or len(items) != 1
        or len(price_ids) != 1
    ):
        return SubscriptionPlan(False)
    price_id = price_ids[0]

    if not tiered_pricing_enabled():
        legacy_price = settings.STRIPE_BASIC_MONTHLY_PRICE_ID
        if legacy_price and price_id == legacy_price:
            return SubscriptionPlan(True, PRO, MONTH, True, legacy_price)
        return SubscriptionPlan(False)

    current_matches = [
        (tier, billing_interval)
        for (tier, billing_interval), current_id in current_price_ids().items()
        if current_id and price_id == current_id
    ]
    legacy_match = price_id in set(legacy_pro_price_ids())
    if len(current_matches) + int(legacy_match) != 1:
        return SubscriptionPlan(False)
    if legacy_match:
        return SubscriptionPlan(True, PRO, '', True, price_id)
    tier, billing_interval = current_matches[0]
    return SubscriptionPlan(
        True, tier, billing_interval, False, price_id,
    )
