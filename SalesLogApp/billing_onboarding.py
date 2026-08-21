from django.conf import settings
from django.utils import timezone

from .billing_entitlements import get_billing_entitlement
from .email_verification import has_verified_canonical_email
from .models import BillingAccess, PayPlanOnboarding


def mark_signup_for_billing_onboarding(user):
    """Persist the staged onboarding cohort at account-creation time."""
    if not settings.BILLING_ONBOARDING_ENABLED:
        return None
    access, _ = BillingAccess.objects.get_or_create(user=user)
    if access.onboarding_required_at is None:
        access.onboarding_required_at = timezone.now()
        access.save(update_fields=['onboarding_required_at', 'updated_at'])
    return access


def billing_onboarding_marked(user):
    if not settings.BILLING_ONBOARDING_ENABLED or not user.is_authenticated:
        return False
    return BillingAccess.objects.filter(
        user=user,
        onboarding_required_at__isnull=False,
    ).exists()


def billing_onboarding_redirect_name(user):
    """Return the required next stop without mutating account state."""
    if not billing_onboarding_marked(user):
        return None
    if not has_verified_canonical_email(user):
        return 'account_email_verification_sent'
    if not get_billing_entitlement(user).subscription_access:
        return 'billing_overview'
    return None


def billing_onboarding_handoff_name(user):
    """Send a subscribed cohort user to setup once, then the dashboard."""
    if not billing_onboarding_marked(user):
        return None
    onboarding = getattr(user, 'pay_plan_onboarding', None)
    if onboarding and onboarding.status == PayPlanOnboarding.ACTIVE:
        return 'view_sales'
    return 'my_pay_plan'
