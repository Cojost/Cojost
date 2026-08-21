from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone

from djstripe.models import Subscription

from .billing_configuration import billing_configuration
from .billing_plans import classify_subscription_plan
from .billing_services import subscription_uses_configured_price
from .models import BillingAccess, BillingCheckoutAttempt


@dataclass(frozen=True)
class BillingEntitlement:
    has_access: bool
    subscription_access: bool
    tier: str
    source: str
    subscription_status: str
    trial_end: object
    current_period_end: object
    founder: bool
    grandfathered: bool
    configuration_ready: bool
    billing_feature_enabled: bool
    billing_enforcement_enabled: bool
    grace_ends_at: object
    reason: str

    @property
    def has_pro_access(self):
        return self.subscription_access and self.tier in {'pro', 'founder_pro'}


def _authorized_end(subscription):
    candidates = [
        value for value in (
            subscription.trial_end,
            subscription.current_period_end,
        ) if value is not None
    ]
    return max(candidates) if candidates else None


def _subscription_for_user(user, access):
    selected_livemode = settings.STRIPE_LIVE_MODE
    subscription = access.authoritative_subscription if access else None
    if (
        subscription is not None
        and subscription.customer.subscriber_id == user.pk
        and subscription.customer.livemode == selected_livemode
    ):
        return subscription
    subscriptions = list(
        Subscription.objects.select_related('customer')
        .filter(
            customer__subscriber=user,
            customer__livemode=selected_livemode,
        )
        .order_by('-created', '-djstripe_id')
    )
    if not subscriptions:
        return None
    eligible_subscriptions = [
        subscription for subscription in subscriptions
        if subscription_uses_configured_price(subscription)
    ]
    if eligible_subscriptions:
        subscriptions = eligible_subscriptions
    priority = {
        'trialing': 0,
        'active': 1,
        'past_due': 2,
        'canceled': 3,
        'incomplete': 4,
        'paused': 5,
        'unpaid': 6,
        'incomplete_expired': 7,
    }
    subscriptions.sort(key=lambda item: priority.get(item.status, 99))
    return subscriptions[0]


def get_billing_entitlement(user, *, at_time=None):
    configuration = billing_configuration()
    now = at_time or timezone.now()
    enforcement = settings.BILLING_ENFORCEMENT_ENABLED
    if not user.is_authenticated:
        return BillingEntitlement(
            has_access=False,
            subscription_access=False,
            tier='basic',
            source='anonymous',
            subscription_status='none',
            trial_end=None,
            current_period_end=None,
            founder=False,
            grandfathered=False,
            configuration_ready=configuration.ready,
            billing_feature_enabled=settings.BILLING_FEATURE_ENABLED,
            billing_enforcement_enabled=enforcement,
            grace_ends_at=None,
            reason='Sign in to view billing status.',
        )
    try:
        access = BillingAccess.objects.select_related(
            'founder_grant',
            'authoritative_subscription__customer',
        ).filter(user=user).first()
        subscription = _subscription_for_user(user, access)
    except DatabaseError:
        access = None
        subscription = None

    founder = bool(access and access.founder_grant_id)
    subscription_plan = classify_subscription_plan(subscription) if subscription else None
    status = subscription.status if subscription else 'none'
    trial_end = subscription.trial_end if subscription else None
    current_period_end = subscription.current_period_end if subscription else None
    grace_end = None
    subscription_access = False
    tier = 'basic'
    source = 'no_subscription'
    reason = 'No synchronized eligible subscription was found.'

    if subscription is not None and not subscription_plan.eligible:
        reason = 'The synchronized subscription does not use the configured Price.'
    elif subscription is not None:
        plan_tier = subscription_plan.tier
        paused = status == 'paused' or bool(subscription.pause_collection)
        if paused:
            status = 'paused'
            reason = 'The subscription is paused and does not currently grant access.'
        elif status == 'trialing':
            if trial_end is not None and trial_end > now:
                subscription_access = True
                tier = 'founder_pro' if founder else plan_tier
                source = 'subscription_trial'
                reason = (
                    'Grandfathered Pro subscription trial is active.'
                    if subscription_plan.grandfathered
                    else 'Subscription trial is active.'
                )
            else:
                reason = 'The synchronized trial period has ended.'
        elif status == 'active':
            subscription_access = True
            tier = plan_tier
            source = 'subscription_active'
            reason = (
                'Grandfathered Pro subscription is active.'
                if subscription_plan.grandfathered
                else 'Subscription is active.'
            )
        elif status == 'past_due':
            if current_period_end is not None:
                grace_end = current_period_end + timedelta(
                    days=settings.BILLING_PAST_DUE_GRACE_DAYS
                )
            if grace_end is not None and grace_end > now:
                subscription_access = True
                tier = plan_tier
                source = 'past_due_grace'
                reason = 'Payment is past due within the seven-day grace period.'
            else:
                reason = 'Payment is past due and the grace period has ended.'
        elif status == 'canceled':
            authorized_end = _authorized_end(subscription)
            if authorized_end is not None and authorized_end > now:
                subscription_access = True
                tier = (
                    'founder_pro'
                    if founder and trial_end is not None and trial_end > now
                    else plan_tier
                )
                source = 'canceled_current_period'
                reason = 'Cancellation is scheduled after the authorized period.'
            else:
                reason = 'The canceled subscription period has ended.'
        elif status == 'incomplete':
            reason = 'Subscription setup is incomplete.'
        elif status == 'incomplete_expired':
            reason = 'Incomplete subscription setup expired.'
        elif status == 'unpaid':
            reason = 'The subscription is unpaid.'
        else:
            reason = 'The synchronized subscription does not grant access.'

    if subscription is None:
        try:
            pending = BillingCheckoutAttempt.objects.filter(
                user=user,
                status__in=BillingCheckoutAttempt.ACTIVE_STATUSES,
                reservation_expires_at__gt=now,
            ).exists()
        except DatabaseError:
            pending = False
        if pending:
            source = 'synchronization_pending'
            reason = 'Checkout is pending authoritative Stripe synchronization.'
    effective_access = subscription_access
    if not enforcement:
        effective_access = True
        if not subscription_access:
            source = 'enforcement_disabled'
            reason = 'Billing enforcement is disabled; application access is unchanged.'
    elif not configuration.ready:
        effective_access = False
        source = 'configuration_unavailable'
        reason = 'Billing configuration is unavailable.'

    return BillingEntitlement(
        has_access=effective_access,
        subscription_access=subscription_access,
        tier=tier,
        source=source,
        subscription_status=status,
        trial_end=trial_end,
        current_period_end=current_period_end,
        founder=founder,
        grandfathered=bool(
            subscription_plan
            and subscription_plan.grandfathered
            and subscription_access
        ),
        configuration_ready=configuration.ready,
        billing_feature_enabled=settings.BILLING_FEATURE_ENABLED,
        billing_enforcement_enabled=enforcement,
        grace_ends_at=grace_end,
        reason=reason,
    )
