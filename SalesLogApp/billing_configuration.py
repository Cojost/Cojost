import re
from dataclasses import dataclass

from django.conf import settings


_PLACEHOLDER_MARKERS = ('replace', 'example', 'placeholder', 'changeme')
_PRICE_PATTERN = re.compile(r'^price_[A-Za-z0-9]+$')


@dataclass(frozen=True)
class BillingConfiguration:
    mode: str
    feature_enabled: bool
    enforcement_enabled: bool
    onboarding_enabled: bool
    tiered_pricing_enabled: bool
    public_key_configured: bool
    public_key_valid: bool
    secret_key_configured: bool
    secret_key_valid: bool
    price_configured: bool
    price_valid: bool
    basic_yearly_price_configured: bool
    basic_yearly_price_valid: bool
    pro_price_configured: bool
    pro_price_valid: bool
    pro_yearly_price_configured: bool
    pro_yearly_price_valid: bool
    legacy_pro_prices_configured: bool
    tiered_pricing_configuration_ready: bool
    tiered_pricing_errors: tuple[str, ...]
    webhook_validation: str
    ready: bool
    errors: tuple[str, ...]


def _is_placeholder(value):
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _key_valid(value, prefix):
    return bool(value) and value.startswith(prefix) and not _is_placeholder(value)


def _price_valid(value):
    return (
        bool(value)
        and bool(_PRICE_PATTERN.fullmatch(value))
        and not _is_placeholder(value)
    )


def selected_public_key():
    if settings.STRIPE_LIVE_MODE:
        return settings.STRIPE_LIVE_PUBLIC_KEY
    return settings.STRIPE_TEST_PUBLIC_KEY


def selected_secret_key():
    if settings.STRIPE_LIVE_MODE:
        return settings.STRIPE_LIVE_SECRET_KEY
    return settings.STRIPE_TEST_SECRET_KEY


def billing_configuration():
    live_mode = settings.STRIPE_LIVE_MODE
    mode = 'live' if live_mode else 'test'
    public_key = selected_public_key()
    secret_key = selected_secret_key()
    price_id = settings.STRIPE_BASIC_MONTHLY_PRICE_ID
    basic_yearly_price_id = settings.STRIPE_BASIC_YEARLY_PRICE_ID
    pro_price_id = settings.STRIPE_PRO_MONTHLY_PRICE_ID
    pro_yearly_price_id = settings.STRIPE_PRO_YEARLY_PRICE_ID
    legacy_price_ids = tuple(settings.STRIPE_LEGACY_PRO_PRICE_IDS)
    public_prefix = 'pk_live_' if live_mode else 'pk_test_'
    secret_prefix = 'sk_live_' if live_mode else 'sk_test_'

    public_valid = _key_valid(public_key, public_prefix)
    secret_valid = _key_valid(secret_key, secret_prefix)
    price_valid = _price_valid(price_id)
    basic_yearly_price_valid = _price_valid(basic_yearly_price_id)
    pro_price_valid = _price_valid(pro_price_id)
    pro_yearly_price_valid = _price_valid(pro_yearly_price_id)
    legacy_prices_valid = (
        bool(legacy_price_ids)
        and len(legacy_price_ids) == len(set(legacy_price_ids))
        and all(_price_valid(value) for value in legacy_price_ids)
    )
    errors = []
    if not public_valid:
        errors.append(f'{mode} publishable credential is missing or invalid')
    if not secret_valid:
        errors.append(f'{mode} server credential is missing or invalid')
    if not price_valid:
        errors.append('Basic monthly Price configuration is missing or invalid')
    tiered_pricing_errors = []
    if not price_valid:
        tiered_pricing_errors.append(
            'Basic monthly Price configuration is missing or invalid'
        )
    if not basic_yearly_price_valid:
        tiered_pricing_errors.append(
            'Basic yearly Price configuration is missing or invalid'
        )
    if not pro_price_valid:
        tiered_pricing_errors.append(
            'Pro monthly Price configuration is missing or invalid'
        )
    if not pro_yearly_price_valid:
        tiered_pricing_errors.append(
            'Pro yearly Price configuration is missing or invalid'
        )
    current_price_ids = (
        price_id,
        basic_yearly_price_id,
        pro_price_id,
        pro_yearly_price_id,
    )
    configured_current = [value for value in current_price_ids if value]
    if len(configured_current) != len(set(configured_current)):
        tiered_pricing_errors.append('all four current Prices must be different')
    if not legacy_prices_valid:
        tiered_pricing_errors.append(
            'legacy Pro Price allowlist is missing or invalid'
        )
    configured_new = set(configured_current)
    if configured_new.intersection(legacy_price_ids):
        tiered_pricing_errors.append(
            'legacy Pro Prices must differ from current plan Prices'
        )
    if settings.BILLING_TIERED_PRICING_ENABLED:
        errors.extend(
            error for error in tiered_pricing_errors if error not in errors
        )
    if settings.DJSTRIPE_WEBHOOK_VALIDATION != 'verify_signature':
        errors.append('webhook signature verification is not enabled')
    if settings.BILLING_ENFORCEMENT_ENABLED and not settings.BILLING_FEATURE_ENABLED:
        errors.append('billing enforcement requires the billing feature')
    if settings.BILLING_ONBOARDING_ENABLED and not settings.BILLING_FEATURE_ENABLED:
        errors.append('billing onboarding requires the billing feature')
    if (
        settings.BILLING_TIERED_PRICING_ENABLED
        and not settings.BILLING_FEATURE_ENABLED
    ):
        errors.append('tiered pricing requires the billing feature')

    return BillingConfiguration(
        mode=mode,
        feature_enabled=settings.BILLING_FEATURE_ENABLED,
        enforcement_enabled=settings.BILLING_ENFORCEMENT_ENABLED,
        onboarding_enabled=settings.BILLING_ONBOARDING_ENABLED,
        tiered_pricing_enabled=settings.BILLING_TIERED_PRICING_ENABLED,
        public_key_configured=bool(public_key),
        public_key_valid=public_valid,
        secret_key_configured=bool(secret_key),
        secret_key_valid=secret_valid,
        price_configured=bool(price_id),
        price_valid=price_valid,
        basic_yearly_price_configured=bool(basic_yearly_price_id),
        basic_yearly_price_valid=basic_yearly_price_valid,
        pro_price_configured=bool(pro_price_id),
        pro_price_valid=pro_price_valid,
        pro_yearly_price_configured=bool(pro_yearly_price_id),
        pro_yearly_price_valid=pro_yearly_price_valid,
        legacy_pro_prices_configured=legacy_prices_valid,
        tiered_pricing_configuration_ready=not tiered_pricing_errors,
        tiered_pricing_errors=tuple(tiered_pricing_errors),
        webhook_validation=settings.DJSTRIPE_WEBHOOK_VALIDATION,
        ready=not errors,
        errors=tuple(errors),
    )
