from django.db import DatabaseError

from .billing_enforcement import cohort_enforcement_state
from .billing_entitlements import get_billing_entitlement
from .models import BillingAccess


def enforcement_notice(request):
    user = getattr(request, 'user', None)
    if (
        user is None
        or not getattr(user, 'is_authenticated', False)
        or getattr(user, 'is_superuser', False)
    ):
        return {'billing_enforcement_notice': None}
    try:
        access = BillingAccess.objects.filter(user=user).first()
    except DatabaseError:
        return {'billing_enforcement_notice': None}
    if (
        access is None
        or access.enforcement_enrolled_at is None
        or access.enforcement_notice_sent_at is None
    ):
        return {'billing_enforcement_notice': None}
    entitlement = get_billing_entitlement(user)
    state = cohort_enforcement_state(
        user,
        access,
        subscription_access=entitlement.subscription_access,
    )
    if state.code == 'subscribed':
        return {'billing_enforcement_notice': None}
    return {
        'billing_enforcement_notice': {
            'state': state.code,
            'grace_ends_at': state.grace_ends_at,
        },
    }
