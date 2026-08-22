from dataclasses import dataclass

from django.utils import timezone


@dataclass(frozen=True)
class BillingEnforcementState:
    code: str
    subject_to_enforcement: bool
    should_block: bool
    grace_ends_at: object = None


def cohort_enforcement_state(
    user,
    access,
    *,
    subscription_access,
    at_time=None,
):
    """Return a deterministic cohort state without performing database writes."""

    now = at_time or timezone.now()
    if not getattr(user, 'is_authenticated', False):
        return BillingEnforcementState('anonymous', False, False)
    if getattr(user, 'is_superuser', False):
        return BillingEnforcementState('superuser_exempt', False, False)
    if access is None or access.enforcement_enrolled_at is None:
        return BillingEnforcementState('not_enrolled', False, False)
    if subscription_access:
        return BillingEnforcementState('subscribed', True, False)
    if access.enforcement_notice_sent_at is None:
        return BillingEnforcementState('notice_pending', True, False)
    if access.enforcement_grace_ends_at is None:
        return BillingEnforcementState('grace_unconfigured', True, False)
    if access.enforcement_grace_ends_at > now:
        return BillingEnforcementState(
            'grace_active',
            True,
            False,
            access.enforcement_grace_ends_at,
        )
    return BillingEnforcementState(
        'enforcement_due',
        True,
        True,
        access.enforcement_grace_ends_at,
    )
