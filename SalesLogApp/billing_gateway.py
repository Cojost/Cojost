from urllib.parse import urlparse

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from djstripe.models import Customer
from djstripe.settings import djstripe_settings

from .billing_configuration import billing_configuration, selected_secret_key


class BillingGatewayError(Exception):
    pass


def _require_ready_configuration():
    configuration = billing_configuration()
    if not configuration.ready:
        raise BillingGatewayError('Billing configuration is unavailable.')
    return configuration


def existing_customer_for_user(user):
    customers = list(Customer.objects.filter(
        subscriber=user,
        livemode=settings.STRIPE_LIVE_MODE,
    )[:2])
    if len(customers) > 1:
        raise BillingGatewayError('Billing customer ownership requires support review.')
    return customers[0] if customers else None


@transaction.atomic
def customer_for_user(user):
    _require_ready_configuration()
    if not user.email or not user.email.strip():
        raise BillingGatewayError('Add an account email before starting billing.')
    get_user_model().objects.select_for_update().get(pk=user.pk)
    customer = existing_customer_for_user(user)
    if customer is not None:
        return customer
    try:
        customer, _ = Customer.get_or_create(
            subscriber=user,
            livemode=settings.STRIPE_LIVE_MODE,
            api_key=selected_secret_key(),
        )
    except stripe.StripeError as exc:
        raise BillingGatewayError('Stripe customer setup is temporarily unavailable.') from exc
    return customer


def _validated_hosted_url(value, expected_host):
    parsed = urlparse(value or '')
    if parsed.scheme != 'https' or parsed.hostname != expected_host:
        raise BillingGatewayError('Stripe returned an invalid hosted destination.')
    return value


def create_checkout_session(*, user, customer, attempt, success_url, cancel_url):
    _require_ready_configuration()
    if not attempt.selected_price_id:
        raise BillingGatewayError('The selected plan is unavailable.')
    subscriber_key = djstripe_settings.SUBSCRIBER_CUSTOMER_KEY
    metadata = {
        subscriber_key: str(user.pk),
        'billing_attempt': str(attempt.public_id),
        'intro_trial_kind': attempt.trial_kind or 'none',
        'selected_tier': attempt.selected_tier,
        'billing_policy': 'tiered_v1',
    }
    subscription_data = {'metadata': metadata}
    if attempt.trial_days:
        subscription_data['trial_period_days'] = attempt.trial_days
    try:
        session = stripe.checkout.Session.create(
            api_key=selected_secret_key(),
            idempotency_key=f'saleslog-checkout-{attempt.public_id}',
            mode='subscription',
            customer=customer.id,
            line_items=[{
                'price': attempt.selected_price_id,
                'quantity': 1,
            }],
            payment_method_collection='always',
            expires_at=int(attempt.reservation_expires_at.timestamp()),
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(user.pk),
            metadata=metadata,
            subscription_data=subscription_data,
        )
    except stripe.StripeError as exc:
        raise BillingGatewayError('Stripe Checkout is temporarily unavailable.') from exc
    return _validated_hosted_url(session.url, 'checkout.stripe.com')


def create_portal_session(*, customer, return_url):
    _require_ready_configuration()
    try:
        session = stripe.billing_portal.Session.create(
            api_key=selected_secret_key(),
            customer=customer.id,
            return_url=return_url,
        )
    except stripe.StripeError as exc:
        raise BillingGatewayError('The billing portal is temporarily unavailable.') from exc
    return _validated_hosted_url(session.url, 'billing.stripe.com')
