from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.db.models import Min
from django.shortcuts import redirect

def get_commission_system(user: Any) -> str:
    from .models import UserProfile

    profile = getattr(user, 'sales_profile', None)
    if profile is not None:
        value = profile.commission_system or UserProfile.LEGACY
        if value == UserProfile.NEW_ENGINE:
            return UserProfile.PAY_PLAN_V2
        return value
    return UserProfile.LEGACY


def uses_new_engine(user: Any) -> bool:
    from .models import UserProfile

    return get_commission_system(user) == UserProfile.PAY_PLAN_V2


def get_or_create_onboarding(user: Any):
    from .models import PayPlanOnboarding

    onboarding, _ = PayPlanOnboarding.objects.get_or_create(user=user)
    return onboarding


def sync_active_onboarding_assignment(user: Any):
    from .models import ArchivedSale, PayPlanAssignment, PayPlanOnboarding, PayPlanVersion, Sale

    onboarding = getattr(user, 'pay_plan_onboarding', None)
    if onboarding is None:
        onboarding = PayPlanOnboarding.objects.filter(user=user).select_related('current_version').first()
    if onboarding is None or onboarding.status != PayPlanOnboarding.ACTIVE:
        return None

    version = onboarding.current_version
    if version is None or version.status != PayPlanVersion.ACTIVE:
        return None

    sale_dates = [
        Sale.objects.filter(user=user).aggregate(value=Min('date'))['value'],
        ArchivedSale.objects.filter(user=user).aggregate(value=Min('date'))['value'],
    ]
    earliest_sale_date = min((value for value in sale_dates if value), default=None)
    desired_start = min(version.effective_start_date, earliest_sale_date) if earliest_sale_date else version.effective_start_date
    changed = False

    with transaction.atomic():
        if version.effective_start_date != desired_start:
            version.effective_start_date = desired_start
            version.save(update_fields=['effective_start_date', 'updated_at'])
            changed = True

        assignments = PayPlanAssignment.objects.select_for_update().filter(user=user)
        assignment = assignments.filter(pay_plan_version=version).order_by('effective_start_date', 'id').first()
        if assignment is None:
            assignment = PayPlanAssignment.objects.create(
                user=user,
                pay_plan_version=version,
                effective_start_date=desired_start,
                effective_end_date=version.effective_end_date,
                is_active=True,
            )
            changed = True
        else:
            assignment_changed = False
            if assignment.effective_start_date != desired_start:
                assignment.effective_start_date = desired_start
                assignment_changed = True
            if assignment.effective_end_date != version.effective_end_date:
                assignment.effective_end_date = version.effective_end_date
                assignment_changed = True
            if not assignment.is_active:
                assignment.is_active = True
                assignment_changed = True
            if assignment_changed:
                assignment.save(update_fields=['effective_start_date', 'effective_end_date', 'is_active', 'updated_at'])
                changed = True

        # Preserve non-overlapping, effective-dated assignments for history.
        conflicting_assignments = (
            assignments.filter(is_active=True)
            .exclude(pk=assignment.pk)
            .filter(
                models.Q(effective_end_date__isnull=True)
                | models.Q(effective_end_date__gte=desired_start)
            )
        )
        conflicting_count = conflicting_assignments.count()
        if conflicting_count:
            conflicting_assignments.update(is_active=False)
            changed = True

    return {
        'onboarding': onboarding,
        'version': version,
        'assignment': assignment,
        'desired_start': desired_start,
        'changed': changed,
        'conflicting_assignments_deactivated': conflicting_count,
    }


def onboarding_is_complete(user: Any) -> bool:
    from .models import PayPlanOnboarding, PayPlanVersion

    onboarding = getattr(user, 'pay_plan_onboarding', None)
    if onboarding is None:
        onboarding = PayPlanOnboarding.objects.filter(user=user).first()
    if not onboarding or onboarding.status != PayPlanOnboarding.ACTIVE:
        return False

    version = onboarding.current_version
    if version is None or version.status != PayPlanVersion.ACTIVE:
        return False

    return bool(sync_active_onboarding_assignment(user))


def pay_plan_onboarding_required(view_func: Callable[..., Any]) -> Callable[..., Any]:
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if uses_new_engine(request.user) and not onboarding_is_complete(request.user):
            return redirect('pay_plan_setup')
        return view_func(request, *args, **kwargs)

    return wrapper


def legacy_commission_only(view_func: Callable[..., Any]) -> Callable[..., Any]:
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if uses_new_engine(request.user):
            return redirect('pay_plan_setup')
        return view_func(request, *args, **kwargs)

    return wrapper


def post_login_redirect(user: Any) -> str:
    if uses_new_engine(user) and not onboarding_is_complete(user):
        return 'pay_plan_setup'
    return 'view_sales'
