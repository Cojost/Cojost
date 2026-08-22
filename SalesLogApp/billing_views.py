from dataclasses import dataclass
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from .billing_configuration import billing_configuration
from .billing_entitlements import get_billing_entitlement
from .billing_onboarding import (
    billing_onboarding_handoff_name,
    billing_onboarding_marked,
)
from .billing_plans import (
    BASIC,
    MONTH,
    PRO,
    YEAR,
    checkout_intervals,
    checkout_tiers,
)
from .billing_pricing import (
    display_plan_prices,
    synchronized_plan_price_errors,
)
from .email_verification import has_verified_canonical_email
from .billing_forms import BillingPlanSelectionForm, FounderCodeRedemptionForm
from .billing_gateway import (
    BillingGatewayError,
    create_checkout_session,
    create_portal_session,
    customer_for_user,
    existing_customer_for_user,
)
from .billing_services import (
    BillingPolicyError,
    mark_checkout_gateway_error,
    mark_checkout_session_created,
    redeem_founder_code,
    reserve_checkout_attempt,
)
from .models import BillingAccess, FounderGrant


@dataclass(frozen=True)
class BillingOverviewView:
    status: str
    reason: str
    trial_end: object
    upcoming_billing_date: object
    founder_redeemed: bool
    introductory_benefit_consumed: bool
    offered_trial_days: int
    can_start_checkout: bool
    can_manage_billing: bool
    email_verified: bool
    founder_checkout_eligible: bool


@dataclass(frozen=True)
class BillingPlanOption:
    tier: str
    name: str
    description: str
    prices: tuple


@dataclass(frozen=True)
class BillingIntervalOption:
    billing_interval: str
    name: str
    price: object
    trial_days: int


def billing_feature_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not (
            settings.BILLING_FEATURE_ENABLED
            or settings.BILLING_ENFORCEMENT_ENABLED
        ):
            raise Http404('Page not found.')
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse('account_login'))
        return view_func(request, *args, **kwargs)

    return wrapped


def _overview_projection(user):
    entitlement = get_billing_entitlement(user)
    access = BillingAccess.objects.select_related('founder_grant').filter(
        user=user
    ).first()
    founder_grant = (
        access.founder_grant
        if access and access.founder_grant_id
        else FounderGrant.objects.filter(redeemed_user=user).first()
    )
    founder_redeemed = bool(founder_grant)
    consumed = bool(access and access.introductory_benefit_consumed_at)
    founder_checkout_eligible = bool(
        founder_grant
        and founder_grant.revoked_at is None
        and not consumed
    )
    email_verified = has_verified_canonical_email(user)
    plan_prices_ready = not synchronized_plan_price_errors()
    if consumed:
        offered_trial_days = 0
    elif founder_grant and founder_grant.revoked_at is None:
        offered_trial_days = founder_grant.trial_days
    else:
        offered_trial_days = settings.BILLING_STANDARD_TRIAL_DAYS
    try:
        can_manage = existing_customer_for_user(user) is not None
    except BillingGatewayError:
        can_manage = False
    upcoming = (
        entitlement.trial_end
        if entitlement.subscription_status == 'trialing'
        else entitlement.current_period_end
    )
    return entitlement, BillingOverviewView(
        status=entitlement.subscription_status,
        reason=entitlement.reason,
        trial_end=entitlement.trial_end,
        upcoming_billing_date=upcoming,
        founder_redeemed=founder_redeemed,
        introductory_benefit_consumed=consumed,
        offered_trial_days=offered_trial_days,
        can_start_checkout=(
            entitlement.configuration_ready
            and not entitlement.subscription_access
            and email_verified
            and plan_prices_ready
        ),
        can_manage_billing=can_manage,
        email_verified=email_verified,
        founder_checkout_eligible=founder_checkout_eligible,
    )


def _plan_options(overview):
    prices = display_plan_prices(founder=overview.founder_checkout_eligible)
    descriptions = {
        BASIC: 'Commission tracking, pay-plan setup, and monthly sales visibility.',
        PRO: 'Everything in Basic, plus Activity & Goals and Stew Coach.',
    }
    labels = {BASIC: 'Basic', PRO: 'Pro'}
    interval_labels = {MONTH: 'Monthly', YEAR: 'Yearly'}

    def trial_days_for(tier, billing_interval):
        if overview.introductory_benefit_consumed:
            return 0
        if overview.founder_checkout_eligible:
            return overview.offered_trial_days
        if tier == BASIC and billing_interval == MONTH:
            return settings.BILLING_STANDARD_TRIAL_DAYS
        return 0

    return tuple(
        BillingPlanOption(
            tier,
            labels[tier],
            descriptions[tier],
            tuple(
                BillingIntervalOption(
                    billing_interval,
                    interval_labels[billing_interval],
                    prices[(tier, billing_interval)],
                    trial_days_for(tier, billing_interval),
                )
                for billing_interval in checkout_intervals()
            ),
        )
        for tier in checkout_tiers(founder=overview.founder_checkout_eligible)
    )


@billing_feature_required
def billing_overview(request):
    configuration = billing_configuration()
    entitlement, overview = _overview_projection(request.user)
    return render(request, 'SalesLogApp/billing/overview.html', {
        'configuration': configuration,
        'entitlement': entitlement,
        'billing': overview,
        'founder_form': FounderCodeRedemptionForm(),
        'standard_trial_days': settings.BILLING_STANDARD_TRIAL_DAYS,
        'founder_trial_days': settings.BILLING_FOUNDER_TRIAL_DAYS,
        'plan_options': _plan_options(overview),
    })


@billing_feature_required
@require_POST
def billing_checkout_start(request):
    configuration = billing_configuration()
    if not configuration.ready:
        messages.error(request, 'Checkout is unavailable until billing is configured.')
        return redirect('billing_overview')
    if synchronized_plan_price_errors():
        messages.error(request, 'Checkout is unavailable until plan pricing is synchronized.')
        return redirect('billing_overview')
    if not request.user.email or not request.user.email.strip():
        messages.error(request, 'Add an account email before starting billing.')
        return redirect('billing_overview')
    if not has_verified_canonical_email(request.user):
        messages.error(
            request,
            'Verify your account email before starting your trial.',
        )
        return redirect('account_email_verification_sent')
    access = BillingAccess.objects.select_related('founder_grant').filter(
        user=request.user,
    ).first()
    founder_checkout_eligible = bool(
        access
        and access.founder_grant_id
        and access.founder_grant.revoked_at is None
        and access.introductory_benefit_consumed_at is None
    )
    plan_data = request.POST.copy()
    if not settings.BILLING_TIERED_PRICING_ENABLED:
        if not plan_data.get('tier'):
            plan_data['tier'] = PRO
        if not plan_data.get('billing_interval'):
            plan_data['billing_interval'] = MONTH
    form = BillingPlanSelectionForm(
        plan_data,
        founder=founder_checkout_eligible,
    )
    if not form.is_valid():
        messages.error(request, 'Choose an available StewLog plan.')
        return redirect('billing_overview')
    try:
        attempt, _ = reserve_checkout_attempt(
            request.user,
            tier=form.cleaned_data['tier'],
            billing_interval=form.cleaned_data['billing_interval'],
        )
        customer = customer_for_user(request.user)
        hosted_url = create_checkout_session(
            user=request.user,
            customer=customer,
            attempt=attempt,
            success_url=request.build_absolute_uri(reverse('billing_checkout_success')),
            cancel_url=request.build_absolute_uri(reverse('billing_checkout_cancel')),
        )
    except BillingPolicyError as exc:
        messages.info(request, str(exc))
        return redirect('billing_overview')
    except BillingGatewayError:
        if 'attempt' in locals():
            mark_checkout_gateway_error(attempt)
        messages.error(request, 'Stripe Checkout is temporarily unavailable.')
        return redirect('billing_overview')
    mark_checkout_session_created(attempt)
    return redirect(hosted_url)


@billing_feature_required
def billing_checkout_success(request):
    entitlement = get_billing_entitlement(request.user)
    onboarding_marked = billing_onboarding_marked(request.user)
    if (
        onboarding_marked
        and entitlement.subscription_access
    ):
        return redirect(
            billing_onboarding_handoff_name(request.user) or 'my_pay_plan'
        )
    return render(request, 'SalesLogApp/billing/result.html', {
        'heading': 'Checkout received',
        'message': (
            'Stripe synchronization is authoritative. Your billing status will '
            'update after the signed webhook is processed.'
        ),
        'entitlement': entitlement,
        'onboarding_pending': onboarding_marked,
    })


@billing_feature_required
def billing_checkout_cancel(request):
    return render(request, 'SalesLogApp/billing/result.html', {
        'heading': 'Checkout canceled',
        'message': (
            'This page does not consume or grant an introductory benefit. '
            'An abandoned reservation expires automatically.'
        ),
        'entitlement': get_billing_entitlement(request.user),
    })


@billing_feature_required
@require_POST
def billing_founder_redeem(request):
    form = FounderCodeRedemptionForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Enter a valid founder code.')
        return redirect('billing_overview')
    try:
        redeem_founder_code(request.user, form.cleaned_data['founder_code'])
    except BillingPolicyError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            'Founder eligibility recorded. It will apply to your first eligible Checkout.',
        )
    return redirect('billing_overview')


@billing_feature_required
@require_POST
def billing_portal(request):
    try:
        customer = existing_customer_for_user(request.user)
        if customer is None:
            raise BillingGatewayError('No synchronized billing customer is available.')
        hosted_url = create_portal_session(
            customer=customer,
            return_url=request.build_absolute_uri(reverse('billing_overview')),
        )
    except BillingGatewayError:
        messages.error(request, 'The billing portal is temporarily unavailable.')
        return redirect('billing_overview')
    return redirect(hosted_url)
