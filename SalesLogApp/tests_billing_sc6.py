from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from djstripe.models import Price, Product

from .billing_pricing import UNAVAILABLE_PRICE, display_price
from .models import Commission

BILLING_READY_SETTINGS = {
    'BILLING_FEATURE_ENABLED': True,
    'BILLING_ENFORCEMENT_ENABLED': False,
    'BILLING_ONBOARDING_ENABLED': False,
    'STRIPE_LIVE_MODE': False,
    'STRIPE_TEST_PUBLIC_KEY': 'pk_test_unitpublic123',
    'STRIPE_TEST_SECRET_KEY': 'sk_test_unitsecret123',
    'STRIPE_LIVE_PUBLIC_KEY': 'pk_live_unitpublic123',
    'STRIPE_LIVE_SECRET_KEY': 'sk_live_unitsecret123',
    'STRIPE_BASIC_MONTHLY_PRICE_ID': 'price_unitmonthly123',
    'BILLING_STANDARD_TRIAL_DAYS': 30,
    'BILLING_FOUNDER_TRIAL_DAYS': 90,
    'DJSTRIPE_WEBHOOK_VALIDATION': 'verify_signature',
}


def _create_price(
    *,
    price_id='price_unitmonthly123',
    unit_amount=199,
    currency='usd',
    interval='month',
    interval_count=1,
    price_type='recurring',
    active=True,
    livemode=False,
):
    product = Product.objects.filter(id='prod_unitbasic123').first()
    if product is None:
        product = Product.objects.create(
            id='prod_unitbasic123',
            name='STEW Log subscription',
            active=True,
            livemode=livemode,
            stripe_data={'id': 'prod_unitbasic123'},
        )
    return Price.objects.create(
        id=price_id,
        product=product,
        active=active,
        livemode=livemode,
        currency=currency,
        stripe_data={
            'id': price_id,
            'unit_amount': unit_amount,
            'unit_amount_decimal': str(unit_amount),
            'currency': currency,
            'type': price_type,
            'recurring': (
                {'interval': interval, 'interval_count': interval_count}
                if price_type == 'recurring' else None
            ),
        },
    )


@override_settings(**BILLING_READY_SETTINGS)
class DisplayPriceTests(TestCase):
    def test_unavailable_when_no_price_row_is_synchronized(self):
        self.assertEqual(display_price(), UNAVAILABLE_PRICE)

    def test_monthly_price_formats_from_local_row(self):
        _create_price(unit_amount=199)
        price = display_price()
        self.assertTrue(price.available)
        self.assertEqual(price.formatted, '$1.99 USD per month')
        self.assertEqual(price.amount, Decimal('1.99'))
        self.assertEqual(price.currency, 'USD')
        self.assertEqual(price.interval, 'month')

    def test_price_is_not_hardcoded(self):
        _create_price(unit_amount=299)
        self.assertEqual(display_price().formatted, '$2.99 USD per month')

    @override_settings(STRIPE_BASIC_MONTHLY_PRICE_ID='')
    def test_unavailable_when_price_id_not_configured(self):
        _create_price()
        self.assertEqual(display_price(), UNAVAILABLE_PRICE)

    def test_unavailable_when_price_id_does_not_match(self):
        _create_price(price_id='price_othermonthly456')
        self.assertEqual(display_price(), UNAVAILABLE_PRICE)

    def test_unavailable_when_price_is_inactive(self):
        _create_price(active=False)
        self.assertEqual(display_price(), UNAVAILABLE_PRICE)

    def test_unavailable_when_livemode_mismatches(self):
        _create_price(livemode=True)
        self.assertEqual(display_price(), UNAVAILABLE_PRICE)

    def test_unavailable_when_price_is_not_recurring(self):
        _create_price(price_type='one_time')
        self.assertEqual(display_price(), UNAVAILABLE_PRICE)

    def test_unavailable_when_interval_count_is_not_one(self):
        _create_price(interval_count=3)
        self.assertEqual(display_price(), UNAVAILABLE_PRICE)

    def test_unmapped_currency_omits_symbol(self):
        _create_price(currency='eur')
        self.assertEqual(display_price().formatted, '1.99 EUR per month')


@override_settings(**BILLING_READY_SETTINGS)
class BillingOverviewPricingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            'owner', email='owner@example.com', password='pw',
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse('billing_overview')

    def test_overview_defers_synchronized_legacy_price_to_checkout(self):
        _create_price(unit_amount=299)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '$2.99 USD per month')
        self.assertNotContains(response, '$1.99')
        self.assertContains(
            response, 'shown at Stripe Checkout before you confirm',
        )
        self.assertContains(
            response, 'Stripe Checkout shows the current price',
        )

    def test_overview_falls_back_without_synchronized_price(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '$1.99')
        self.assertContains(
            response, 'shown at Stripe Checkout before you confirm',
        )
        self.assertContains(
            response, 'Stripe Checkout shows the current price',
        )


class ProUpgradePromptTests(TestCase):
    PROMPT_TEXT = 'Try STEW Log Pro'

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            'owner', email='owner@example.com', password='pw',
        )
        Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('.10'),
            total_calculated_back_end=Decimal('.10'),
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.dashboard = reverse('view_sales')
        self.profile = reverse('profile')

    def test_prompt_hidden_while_billing_flags_are_off(self):
        response = self.client.get(self.dashboard)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.PROMPT_TEXT)

    @override_settings(**BILLING_READY_SETTINGS)
    def test_prompt_shown_to_non_pro_user_on_dashboard(self):
        _create_price(unit_amount=199)
        response = self.client.get(self.dashboard)
        self.assertContains(response, self.PROMPT_TEXT)
        self.assertContains(response, 'Standard Pro subscriptions start without a trial')
        self.assertNotContains(response, '$1.99 USD per month')
        self.assertContains(
            response, 'Stripe Checkout shows the current price',
        )
        self.assertContains(response, reverse('billing_overview'))

    @override_settings(**BILLING_READY_SETTINGS)
    def test_prompt_shown_on_profile(self):
        response = self.client.get(self.profile)
        self.assertContains(response, self.PROMPT_TEXT)

    @override_settings(**BILLING_READY_SETTINGS)
    def test_prompt_omits_price_when_unavailable(self):
        response = self.client.get(self.dashboard)
        self.assertContains(response, self.PROMPT_TEXT)
        self.assertNotContains(response, '$1.99')
        self.assertContains(
            response, 'Stripe Checkout shows the current price',
        )

    @override_settings(**BILLING_READY_SETTINGS)
    def test_prompt_hidden_for_pro_user(self):
        with patch(
            'SalesLogApp.billing_entitlements.get_billing_entitlement',
            return_value=SimpleNamespace(has_pro_access=True),
        ):
            response = self.client.get(self.dashboard)
        self.assertNotContains(response, self.PROMPT_TEXT)

    @override_settings(**BILLING_READY_SETTINGS)
    def test_prompt_hidden_for_staff(self):
        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        response = self.client.get(self.dashboard)
        self.assertNotContains(response, self.PROMPT_TEXT)
