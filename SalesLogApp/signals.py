from django.dispatch import receiver
from allauth.account.signals import user_signed_up
from django.shortcuts import redirect
from django.urls import reverse
from .models import Commission, Sale
from .models import UserProfile, PayPlanOnboarding
from django.conf import settings
from django.db.models.signals import post_save, pre_delete
from django.utils import timezone
from djstripe.signals import webhook_post_process


@receiver(user_signed_up)
def redirect_to_commission_setup(request, user, **kwargs):
    # Create a commission entry for the newly registered user, if not already present
    commission, created = Commission.objects.get_or_create(user=user)
    UserProfile.objects.filter(user=user).update(
        commission_system=UserProfile.PAY_PLAN_V2,
    )
    PayPlanOnboarding.objects.get_or_create(
        user=user,
        defaults={
            'status': PayPlanOnboarding.NOT_STARTED,
            'setup_method': PayPlanOnboarding.ASSISTED,
            'started_at': timezone.now(),
        },
    )
    
    # Redirect to the adjust commission page with the `commission_id`
    return redirect(reverse('adjust_commission_by_id', kwargs={'commission_id': commission.id}))


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_sales_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(
            user=instance,
            defaults={'commission_system': UserProfile.LEGACY},
        )


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_automotive_pay_plan(sender, instance, created, **kwargs):
    if not created:
        return

    from .models import (
        Industry,
        PayPlan,
        PayPlanAssignment,
        PayPlanOnboarding,
        PayPlanVersion,
        UserProfile,
    )

    automotive, _ = Industry.objects.get_or_create(
        slug='automotive',
        defaults={'name': 'Automotive', 'is_active': True},
    )
    plan = PayPlan.objects.create(
        industry=automotive,
        owner_user=instance,
        name='Legacy Automotive Pay Plan',
        description=(
            'Compatibility plan created from the existing automotive commission '
            'foundation. Rules will be migrated in a later stage.'
        ),
    )
    joined_date = instance.date_joined
    if hasattr(joined_date, 'date') and not isinstance(joined_date, str):
        joined_date = joined_date.date()
    start_date = joined_date
    version = PayPlanVersion.objects.create(
        pay_plan=plan,
        version_name='Imported Legacy Settings',
        effective_start_date=start_date,
        status=PayPlanVersion.ACTIVE,
    )
    PayPlanAssignment.objects.create(
        user=instance,
        pay_plan_version=version,
        effective_start_date=start_date,
    )

    # Keep background-created users on their existing engine selection.
    PayPlanOnboarding.objects.get_or_create(
        user=instance,
        defaults={
            'status': PayPlanOnboarding.NOT_STARTED,
            'setup_method': PayPlanOnboarding.ASSISTED,
            'current_pay_plan': plan,
            'current_version': version,
            'started_at': timezone.now(),
            'questionnaire': {},
        },
    )


@receiver(post_save, sender=Sale, dispatch_uid='teams_sync_sale_activity')
def sync_team_activity_after_sale_save(sender, instance, **kwargs):
    from .team_services import sync_sale_activity

    sync_sale_activity(instance)


@receiver(pre_delete, sender=Sale, dispatch_uid='teams_withdraw_sale_activity')
def withdraw_team_activity_before_sale_delete(sender, instance, **kwargs):
    from .team_services import withdraw_sale_activity

    withdraw_sale_activity(instance)


@receiver(
    webhook_post_process,
    dispatch_uid='saleslog_reconcile_billing_webhook',
)
def reconcile_billing_webhook(sender, instance, **kwargs):
    if instance.event is None:
        return
    from .billing_webhooks import reconcile_billing_event

    reconcile_billing_event(instance.event)
