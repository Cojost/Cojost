"""Server-owned BILL-2 plan and Stripe Price policy.

Browser input may select only a tier name. Stripe Price IDs always come from
deployment configuration, and synchronized subscription items are classified
against the same allowlist. The rollout flag preserves the original one-Price
behavior until both new tiers and the legacy-Pro allowlist are ready.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


BASIC = 'basic'
PRO = 'pro'
PLAN_CHOICES = ((BASIC, 'Basic'), (PRO, 'Pro'))
EXPECTED_MONTHLY_CENTS = {BASIC: 399, PRO: 799}


class BillingPlanError(ValueError):
    pass


@dataclass(frozen=True)
class SubscriptionPlan:
    eligible: bool
    tier: str = BASIC
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


def price_id_for_checkout_tier(tier: str) -> str:
    if not tiered_pricing_enabled():
        if tier != PRO:
            raise BillingPlanError('The selected plan is unavailable.')
        return settings.STRIPE_BASIC_MONTHLY_PRICE_ID
    if tier == BASIC:
        return settings.STRIPE_BASIC_MONTHLY_PRICE_ID
    if tier == PRO:
        return settings.STRIPE_PRO_MONTHLY_PRICE_ID
    raise BillingPlanError('The selected plan is unavailable.')


def configured_subscription_price_ids() -> frozenset[str]:
    if not tiered_pricing_enabled():
        values = (settings.STRIPE_BASIC_MONTHLY_PRICE_ID,)
    else:
        values = (
            settings.STRIPE_BASIC_MONTHLY_PRICE_ID,
            settings.STRIPE_PRO_MONTHLY_PRICE_ID,
            *legacy_pro_price_ids(),
        )
    return frozenset(value for value in values if value)


def subscription_price_ids(subscription) -> tuple[str, ...]:
    items = ((subscription.stripe_data or {}).get('items') or {}).get('data') or []
    values = []
    for item in items:
        price = item.get('price') or item.get('plan') or {}
        price_id = price.get('id') if isinstance(price, dict) else price
        if isinstance(price_id, str) and price_id:
            values.append(price_id)
    return tuple(values)


def subscription_uses_price(subscription, price_id: str) -> bool:
    return bool(price_id) and price_id in subscription_price_ids(subscription)


def classify_subscription_plan(subscription) -> SubscriptionPlan:
    price_ids = set(subscription_price_ids(subscription))
    if not tiered_pricing_enabled():
        legacy_price = settings.STRIPE_BASIC_MONTHLY_PRICE_ID
        if legacy_price and price_ids == {legacy_price}:
            return SubscriptionPlan(True, PRO, True, legacy_price)
        return SubscriptionPlan(False)

    basic_id = settings.STRIPE_BASIC_MONTHLY_PRICE_ID
    pro_id = settings.STRIPE_PRO_MONTHLY_PRICE_ID
    legacy_ids = set(legacy_pro_price_ids())
    allowed_ids = {value for value in (basic_id, pro_id, *legacy_ids) if value}
    if not price_ids or not price_ids.issubset(allowed_ids):
        return SubscriptionPlan(False)
    matched_basic = basic_id if basic_id and basic_id in price_ids else ''
    matched_pro = pro_id if pro_id and pro_id in price_ids else ''
    matched_legacy = sorted(price_ids & legacy_ids)
    matches = bool(matched_basic) + bool(matched_pro) + bool(matched_legacy)
    if matches != 1:
        return SubscriptionPlan(False)
    if matched_basic:
        return SubscriptionPlan(True, BASIC, False, matched_basic)
    if matched_pro:
        return SubscriptionPlan(True, PRO, False, matched_pro)
    return SubscriptionPlan(True, PRO, True, matched_legacy[0])
