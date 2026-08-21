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
    public_key_configured: bool
    public_key_valid: bool
    secret_key_configured: bool
    secret_key_valid: bool
    price_configured: bool
    price_valid: bool
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
    public_prefix = 'pk_live_' if live_mode else 'pk_test_'
    secret_prefix = 'sk_live_' if live_mode else 'sk_test_'

    public_valid = _key_valid(public_key, public_prefix)
    secret_valid = _key_valid(secret_key, secret_prefix)
    price_valid = _price_valid(price_id)
    errors = []
    if not public_valid:
        errors.append(f'{mode} publishable credential is missing or invalid')
    if not secret_valid:
        errors.append(f'{mode} server credential is missing or invalid')
    if not price_valid:
        errors.append('monthly Price configuration is missing or invalid')
    if settings.DJSTRIPE_WEBHOOK_VALIDATION != 'verify_signature':
        errors.append('webhook signature verification is not enabled')
    if settings.BILLING_ENFORCEMENT_ENABLED and not settings.BILLING_FEATURE_ENABLED:
        errors.append('billing enforcement requires the billing feature')
    if settings.BILLING_ONBOARDING_ENABLED and not settings.BILLING_FEATURE_ENABLED:
        errors.append('billing onboarding requires the billing feature')

    return BillingConfiguration(
        mode=mode,
        feature_enabled=settings.BILLING_FEATURE_ENABLED,
        enforcement_enabled=settings.BILLING_ENFORCEMENT_ENABLED,
        onboarding_enabled=settings.BILLING_ONBOARDING_ENABLED,
        public_key_configured=bool(public_key),
        public_key_valid=public_valid,
        secret_key_configured=bool(secret_key),
        secret_key_valid=secret_valid,
        price_configured=bool(price_id),
        price_valid=price_valid,
        webhook_validation=settings.DJSTRIPE_WEBHOOK_VALIDATION,
        ready=not errors,
        errors=tuple(errors),
    )
