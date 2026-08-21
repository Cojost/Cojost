from django.conf import settings
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from .billing_entitlements import get_billing_entitlement
from .billing_onboarding import billing_onboarding_redirect_name


EXEMPT_NAMES = {
    'account_login',
    'account_logout',
    'account_signup',
    'account_reset_password',
    'account_reset_password_done',
    'account_reset_password_from_key',
    'account_reset_password_from_key_done',
    'account_email_verification_sent',
    'account_confirm_email',
    'profile_avatar_file',
    'billing_overview',
    'billing_checkout_start',
    'billing_checkout_success',
    'billing_checkout_cancel',
    'billing_founder_redeem',
    'billing_portal',
}
EXEMPT_NAMESPACES = {'admin', 'djstripe'}
EXEMPT_PREFIXES = (
    '/static/', '/media/', '/admin/', '/stripe/', '/accounts/',
    '/health/', '/healthz/', '/legal/', '/privacy/', '/terms/', '/support/',
)


class BillingEnforcementMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not (
            settings.BILLING_ENFORCEMENT_ENABLED
            or settings.BILLING_ONBOARDING_ENABLED
        ):
            return self.get_response(request)
        if request.path.startswith(EXEMPT_PREFIXES):
            return self.get_response(request)
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return self.get_response(request)
        if (
            match.url_name in EXEMPT_NAMES
            or match.namespace in EXEMPT_NAMESPACES
        ):
            return self.get_response(request)
        if not request.user.is_authenticated:
            return self.get_response(request)
        if settings.BILLING_ONBOARDING_ENABLED:
            redirect_name = billing_onboarding_redirect_name(request.user)
            if redirect_name:
                return redirect(redirect_name)
        if not settings.BILLING_ENFORCEMENT_ENABLED:
            return self.get_response(request)
        entitlement = get_billing_entitlement(request.user)
        if entitlement.has_access:
            return self.get_response(request)
        return redirect('billing_overview')
