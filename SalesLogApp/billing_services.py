import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import BillingAccess, BillingCheckoutAttempt, FounderGrant


class BillingPolicyError(Exception):
    pass


def subscription_uses_configured_price(subscription):
    """Accept only the one mode-selected Price controlled by this application."""
    items = ((subscription.stripe_data or {}).get('items') or {}).get('data') or []
    for item in items:
        price = item.get('price') or item.get('plan') or {}
        price_id = price.get('id') if isinstance(price, dict) else price
        if price_id == settings.STRIPE_BASIC_MONTHLY_PRICE_ID:
            return True
    return False


def _founder_code_digest(raw_code):
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        f'founder-grant:{raw_code}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def generate_founder_grant(
    *, created_by=None, expires_at=None, trial_days=None, administrative_note=''
):
    trial_days = trial_days or settings.BILLING_FOUNDER_TRIAL_DAYS
    if not 1 <= trial_days <= 365:
        raise ValidationError('Founder trial days must be between 1 and 365.')
    raw_code = f'stewf_{secrets.token_urlsafe(24)}'
    grant = FounderGrant(
        code_digest=_founder_code_digest(raw_code),
        code_prefix=raw_code[:12],
        created_by=created_by,
        expires_at=expires_at,
        trial_days=trial_days,
        administrative_note=administrative_note.strip(),
    )
    grant.full_clean()
    grant.save()
    return grant, raw_code


@transaction.atomic
def redeem_founder_code(user, raw_code):
    get_user_model().objects.select_for_update().get(pk=user.pk)
    access, _ = BillingAccess.objects.select_for_update().get_or_create(user=user)
    if access.introductory_benefit_consumed_at:
        raise BillingPolicyError('An introductory benefit has already been used.')
    if FounderGrant.objects.filter(redeemed_user=user).exists():
        raise BillingPolicyError('A founder grant has already been redeemed.')
    try:
        grant = FounderGrant.objects.select_for_update().get(
            code_digest=_founder_code_digest(raw_code.strip())
        )
    except FounderGrant.DoesNotExist as exc:
        raise BillingPolicyError('The founder code is invalid or unavailable.') from exc
    now = timezone.now()
    if (
        grant.revoked_at is not None
        or (grant.expires_at is not None and grant.expires_at <= now)
        or grant.redemption_count >= grant.max_redemptions
        or grant.redeemed_user_id is not None
    ):
        raise BillingPolicyError('The founder code is invalid or unavailable.')
    grant.redemption_count += 1
    grant.redeemed_user = user
    grant.redeemed_at = now
    grant.save(update_fields=[
        'redemption_count', 'redeemed_user', 'redeemed_at',
    ])
    access.founder_grant = grant
    access.save(update_fields=['founder_grant', 'updated_at'])
    return grant


def _expire_stale_attempts(user, now):
    BillingCheckoutAttempt.objects.filter(
        user=user,
        status__in=BillingCheckoutAttempt.ACTIVE_STATUSES,
        reservation_expires_at__lte=now,
    ).update(status=BillingCheckoutAttempt.EXPIRED, updated_at=now)


@transaction.atomic
def reserve_checkout_attempt(user):
    get_user_model().objects.select_for_update().get(pk=user.pk)
    now = timezone.now()
    _expire_stale_attempts(user, now)
    existing = BillingCheckoutAttempt.objects.select_for_update().filter(
        user=user,
        status__in=BillingCheckoutAttempt.ACTIVE_STATUSES,
        reservation_expires_at__gt=now,
    ).first()
    if existing:
        return existing, False

    from .billing_entitlements import get_billing_entitlement

    entitlement = get_billing_entitlement(user)
    if entitlement.subscription_access:
        raise BillingPolicyError('An eligible subscription already exists.')

    access, _ = BillingAccess.objects.select_for_update().get_or_create(user=user)
    trial_kind = BillingCheckoutAttempt.NONE
    trial_days = 0
    founder_grant = None
    if access.introductory_benefit_consumed_at is None:
        founder_grant = access.founder_grant
        if (
            founder_grant is not None
            and founder_grant.redeemed_user_id == user.pk
            and founder_grant.revoked_at is None
        ):
            trial_kind = BillingCheckoutAttempt.FOUNDER
            trial_days = founder_grant.trial_days
        else:
            founder_grant = None
            trial_kind = BillingCheckoutAttempt.STANDARD
            trial_days = settings.BILLING_STANDARD_TRIAL_DAYS

    attempt = BillingCheckoutAttempt.objects.create(
        user=user,
        founder_grant=founder_grant,
        trial_kind=trial_kind,
        trial_days=trial_days,
        reservation_expires_at=(
            now + timedelta(minutes=settings.BILLING_CHECKOUT_RESERVATION_MINUTES)
        ),
    )
    return attempt, True


def mark_checkout_session_created(attempt):
    now = timezone.now()
    BillingCheckoutAttempt.objects.filter(
        pk=attempt.pk,
        status__in=[
            BillingCheckoutAttempt.RESERVED,
            BillingCheckoutAttempt.SESSION_CREATED,
        ],
    ).update(
        status=BillingCheckoutAttempt.SESSION_CREATED,
        session_created_at=now,
        failure_code='',
        updated_at=now,
    )


def mark_checkout_gateway_error(attempt):
    BillingCheckoutAttempt.objects.filter(pk=attempt.pk).update(
        failure_code='stripe_unavailable',
        updated_at=timezone.now(),
    )


@transaction.atomic
def mark_checkout_completed(attempt_public_id):
    try:
        attempt = BillingCheckoutAttempt.objects.select_for_update().get(
            public_id=attempt_public_id
        )
    except (BillingCheckoutAttempt.DoesNotExist, ValueError, TypeError):
        return False
    if attempt.status in {
        BillingCheckoutAttempt.RESERVED,
        BillingCheckoutAttempt.SESSION_CREATED,
    }:
        attempt.status = BillingCheckoutAttempt.CHECKOUT_COMPLETED
        attempt.save(update_fields=['status', 'updated_at'])
    return True


@transaction.atomic
def finalize_introductory_benefit(
    attempt_public_id, subscription, *, event_created_at=None
):
    try:
        attempt = BillingCheckoutAttempt.objects.select_for_update().get(
            public_id=attempt_public_id
        )
    except (BillingCheckoutAttempt.DoesNotExist, ValueError, TypeError):
        return False
    subscriber_id = getattr(subscription.customer, 'subscriber_id', None)
    if subscriber_id != attempt.user_id:
        raise BillingPolicyError('Subscription ownership could not be verified.')
    if not subscription_uses_configured_price(subscription):
        raise BillingPolicyError('Subscription Price could not be verified.')
    access, _ = BillingAccess.objects.select_for_update().get_or_create(
        user_id=attempt.user_id
    )
    if attempt.status == BillingCheckoutAttempt.CONFIRMED:
        return True
    now = timezone.now()
    if (
        access.introductory_benefit_consumed_at is None
        and attempt.trial_kind in {
            BillingCheckoutAttempt.STANDARD,
            BillingCheckoutAttempt.FOUNDER,
        }
    ):
        access.introductory_benefit_consumed_at = now
        access.introductory_benefit_kind = attempt.trial_kind
        if attempt.trial_kind == BillingCheckoutAttempt.FOUNDER:
            access.founder_grant = attempt.founder_grant
            access.founder_entitlement_expires_at = subscription.trial_end
    update_authoritative_subscription = (
        access.authoritative_subscription_id is None
        or event_created_at is None
        or access.last_event_created_at is None
        or event_created_at >= access.last_event_created_at
    )
    if update_authoritative_subscription:
        access.authoritative_subscription = subscription
    access.last_synchronized_at = now
    update_fields = [
        'introductory_benefit_consumed_at',
        'introductory_benefit_kind',
        'founder_grant',
        'founder_entitlement_expires_at',
        'last_synchronized_at',
        'updated_at',
    ]
    if update_authoritative_subscription:
        update_fields.append('authoritative_subscription')
    access.save(update_fields=update_fields)
    attempt.status = BillingCheckoutAttempt.CONFIRMED
    attempt.confirmed_at = now
    attempt.failure_code = ''
    attempt.save(update_fields=[
        'status', 'confirmed_at', 'failure_code', 'updated_at',
    ])
    return True
