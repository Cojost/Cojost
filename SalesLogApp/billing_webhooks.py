from django.db import transaction
from django.utils import timezone

from djstripe.models import Customer, Subscription

from .billing_services import (
    finalize_introductory_benefit,
    mark_checkout_completed,
)
from .models import BillingAccess


SUPPORTED_BILLING_EVENTS = {
    'checkout.session.completed',
    'customer.subscription.created',
    'customer.subscription.updated',
    'customer.subscription.deleted',
    'customer.subscription.trial_will_end',
    'invoice.paid',
    'invoice.payment_failed',
}


def _event_object(event):
    data = event.data or {}
    return data.get('object') or {}


def _attempt_reference(data):
    return (data.get('metadata') or {}).get('billing_attempt')


@transaction.atomic
def _record_latest_event(user_id, event, subscription=None):
    access, _ = BillingAccess.objects.select_for_update().get_or_create(
        user_id=user_id
    )
    if (
        access.last_event_created_at is not None
        and event.created <= access.last_event_created_at
    ):
        return False
    access.last_event_type = event.type
    access.last_event_created_at = event.created
    access.last_synchronized_at = timezone.now()
    update_fields = [
        'last_event_type',
        'last_event_created_at',
        'last_synchronized_at',
        'updated_at',
    ]
    if subscription is not None:
        access.authoritative_subscription = subscription
        update_fields.append('authoritative_subscription')
    access.save(update_fields=update_fields)
    return True


def reconcile_billing_event(event):
    if event.type not in SUPPORTED_BILLING_EVENTS:
        return False
    data = _event_object(event)
    if event.type == 'checkout.session.completed':
        mark_checkout_completed(_attempt_reference(data))
        customer_id = data.get('customer')
        customer = Customer.objects.filter(id=customer_id).first() if customer_id else None
        if customer and customer.subscriber_id:
            _record_latest_event(customer.subscriber_id, event)
        return True

    if event.type.startswith('customer.subscription.'):
        subscription_id = data.get('id')
        subscription = Subscription.objects.select_related('customer').filter(
            id=subscription_id
        ).first()
        if subscription is None or subscription.customer.subscriber_id is None:
            return False
        attempt_reference = _attempt_reference(subscription.stripe_data or data)
        if attempt_reference:
            finalize_introductory_benefit(
                attempt_reference,
                subscription,
                event_created_at=event.created,
            )
        _record_latest_event(
            subscription.customer.subscriber_id,
            event,
            subscription=subscription,
        )
        return True

    customer_id = data.get('customer')
    customer = Customer.objects.filter(id=customer_id).first() if customer_id else None
    if customer and customer.subscriber_id:
        _record_latest_event(customer.subscriber_id, event)
    return True
