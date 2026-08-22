import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from djstripe.models import Customer, Price, Product, Subscription, WebhookEndpoint

from .billing_configuration import billing_configuration
from .billing_entitlements import get_billing_entitlement
from .billing_plans import (
    BASIC,
    MONTH,
    PRO,
    YEAR,
    PRICE_POLICY,
    classify_subscription_plan,
)
from .billing_pricing import synchronized_plan_price_errors
from .billing_services import (
    BillingPolicyError,
    finalize_introductory_benefit,
    generate_founder_grant,
    redeem_founder_code,
    reserve_checkout_attempt,
)
from .models import BillingAccess, BillingCheckoutAttempt


BILL3_SETTINGS = {
    'BILLING_FEATURE_ENABLED': True,
    'BILLING_ENFORCEMENT_ENABLED': False,
    'BILLING_ONBOARDING_ENABLED': False,
    'BILLING_TIERED_PRICING_ENABLED': True,
    'STRIPE_LIVE_MODE': False,
    'STRIPE_TEST_PUBLIC_KEY': 'pk_test_bill3_public_value',
    'STRIPE_TEST_SECRET_KEY': 'sk_test_bill3_private_value',
    'STRIPE_BASIC_MONTHLY_PRICE_ID': 'price_bill3basicmonth',
    'STRIPE_BASIC_YEARLY_PRICE_ID': 'price_bill3basicyear',
    'STRIPE_PRO_MONTHLY_PRICE_ID': 'price_bill3promonth',
    'STRIPE_PRO_YEARLY_PRICE_ID': 'price_bill3proyear',
    'STRIPE_LEGACY_PRO_PRICE_IDS': ['price_bill3legacypro'],
    'BILLING_STANDARD_TRIAL_DAYS': 30,
    'BILLING_FOUNDER_TRIAL_DAYS': 90,
    'DJSTRIPE_WEBHOOK_VALIDATION': 'verify_signature',
}

SELECTIONS = {
    (BASIC, MONTH): ('price_bill3basicmonth', 499, 'month'),
    (BASIC, YEAR): ('price_bill3basicyear', 4900, 'year'),
    (PRO, MONTH): ('price_bill3promonth', 999, 'month'),
    (PRO, YEAR): ('price_bill3proyear', 9900, 'year'),
}


def create_price(
    price_id,
    cents,
    interval,
    *,
    active=True,
    livemode=False,
    currency='usd',
    price_type='recurring',
    interval_count=1,
):
    product_id = f'prod_{price_id.removeprefix("price_")}'
    product = Product.objects.create(
        id=product_id,
        name=product_id,
        active=True,
        livemode=livemode,
        stripe_data={'id': product_id},
    )
    recurring = (
        {'interval': interval, 'interval_count': interval_count}
        if price_type == 'recurring'
        else None
    )
    return Price.objects.create(
        id=price_id,
        product=product,
        active=active,
        livemode=livemode,
        currency=currency,
        stripe_data={
            'id': price_id,
            'unit_amount': cents,
            'unit_amount_decimal': str(cents),
            'currency': currency,
            'type': price_type,
            'recurring': recurring,
        },
    )


def create_current_prices(*, override_selection=None, overrides=None, omit=None):
    overrides = overrides or {}
    for selection, (price_id, cents, interval) in SELECTIONS.items():
        if selection == omit:
            continue
        values = {
            'price_id': price_id,
            'cents': cents,
            'interval': interval,
        }
        if selection == override_selection:
            values.update(overrides)
        create_price(**values)


class Bill3ConfigurationTests(SimpleTestCase):
    @override_settings(**BILL3_SETTINGS)
    def test_all_four_distinct_current_prices_are_ready(self):
        configuration = billing_configuration()
        self.assertTrue(configuration.ready)
        self.assertTrue(configuration.basic_yearly_price_valid)
        self.assertTrue(configuration.pro_yearly_price_valid)

    def test_each_current_price_is_required_and_valid(self):
        names = [policy.setting_name for policy in PRICE_POLICY.values()]
        for setting_name in names:
            with self.subTest(setting=setting_name), self.settings(**{
                **BILL3_SETTINGS,
                setting_name: '',
            }):
                self.assertFalse(billing_configuration().ready)

    @override_settings(**{
        **BILL3_SETTINGS,
        'STRIPE_BASIC_YEARLY_PRICE_ID': 'not-a-stripe-price',
    })
    def test_invalid_current_price_identifier_fails_closed(self):
        configuration = billing_configuration()
        self.assertFalse(configuration.ready)
        self.assertIn(
            'Basic yearly Price configuration is missing or invalid',
            configuration.errors,
        )

    @override_settings(**{
        **BILL3_SETTINGS,
        'STRIPE_PRO_YEARLY_PRICE_ID': 'price_bill3basicyear',
    })
    def test_duplicated_current_price_configuration_fails_closed(self):
        configuration = billing_configuration()
        self.assertFalse(configuration.ready)
        self.assertIn('all four current Prices must be different', configuration.errors)

    @override_settings(**{
        **BILL3_SETTINGS,
        'STRIPE_LEGACY_PRO_PRICE_IDS': ['price_bill3proyear'],
    })
    def test_current_price_cannot_overlap_legacy_allowlist(self):
        configuration = billing_configuration()
        self.assertFalse(configuration.ready)
        self.assertIn(
            'legacy Pro Prices must differ from current plan Prices',
            configuration.errors,
        )


@override_settings(**BILL3_SETTINGS)
class Bill3SynchronizedPriceTests(TestCase):
    def reset_prices(self):
        Price.objects.all().delete()
        Product.objects.all().delete()

    def test_exact_amount_currency_mode_and_intervals_pass(self):
        create_current_prices()
        self.assertEqual(synchronized_plan_price_errors(), ())

    def test_each_selection_rejects_wrong_amount(self):
        for selection, (_, cents, _) in SELECTIONS.items():
            with self.subTest(selection=selection):
                self.reset_prices()
                create_current_prices(
                    override_selection=selection,
                    overrides={'cents': cents + 1},
                )
                self.assertTrue(any(
                    'wrong amount' in error
                    for error in synchronized_plan_price_errors()
                ))

    def test_each_selection_rejects_wrong_interval(self):
        for selection, (_, _, interval) in SELECTIONS.items():
            with self.subTest(selection=selection):
                self.reset_prices()
                wrong_interval = 'year' if interval == 'month' else 'month'
                create_current_prices(
                    override_selection=selection,
                    overrides={'interval': wrong_interval},
                )
                self.assertTrue(any(
                    'wrong interval' in error
                    for error in synchronized_plan_price_errors()
                ))

    def test_missing_price_fails_closed(self):
        create_current_prices(omit=(BASIC, YEAR))
        self.assertTrue(any(
            'basic yearly Price is unavailable' in error
            for error in synchronized_plan_price_errors()
        ))

    def test_wrong_mode_price_fails_closed(self):
        create_current_prices(
            override_selection=(BASIC, MONTH),
            overrides={'livemode': True},
        )
        self.assertTrue(any(
            'wrong Stripe mode' in error
            for error in synchronized_plan_price_errors()
        ))

    def test_inactive_price_fails_closed(self):
        create_current_prices(
            override_selection=(BASIC, YEAR),
            overrides={'active': False},
        )
        self.assertTrue(any(
            'inactive' in error for error in synchronized_plan_price_errors()
        ))

    def test_wrong_currency_fails_closed(self):
        create_current_prices(
            override_selection=(PRO, MONTH),
            overrides={'currency': 'eur'},
        )
        self.assertTrue(any(
            'not USD' in error for error in synchronized_plan_price_errors()
        ))

    def test_non_recurring_price_fails_closed(self):
        create_current_prices(
            override_selection=(PRO, YEAR),
            overrides={'price_type': 'one_time'},
        )
        self.assertTrue(any(
            'not recurring' in error
            for error in synchronized_plan_price_errors()
        ))

    def test_missing_or_non_unit_interval_count_fails_closed(self):
        for interval_count in (None, 2):
            with self.subTest(interval_count=interval_count):
                self.reset_prices()
                create_current_prices(
                    override_selection=(PRO, YEAR),
                    overrides={'interval_count': interval_count},
                )
                self.assertTrue(any(
                    'wrong interval' in error
                    for error in synchronized_plan_price_errors()
                ))

    def test_readiness_reports_annual_migration_without_identifiers(self):
        create_current_prices()
        WebhookEndpoint.objects.create(
            id='we_test_bill3_readiness',
            livemode=False,
            url='https://example.test/stripe/webhook/bill3/',
            enabled_events=['customer.subscription.updated'],
            secret='test-only-signing-value',
            status='enabled',
            stripe_data={},
        )
        output = StringIO()
        call_command('billing_readiness', '--json', stdout=output)
        rendered = output.getvalue()
        report = json.loads(rendered)
        self.assertTrue(report['migrations']['annual_billing'])
        self.assertTrue(report['tiered_pricing_ready'])
        for price_id, _, _ in SELECTIONS.values():
            self.assertNotIn(price_id, rendered)

    @override_settings(BILLING_TIERED_PRICING_ENABLED=False)
    def test_readiness_validates_candidate_prices_before_rollout(self):
        create_current_prices()
        WebhookEndpoint.objects.create(
            id='we_test_bill3_preflight',
            livemode=False,
            url='https://example.test/stripe/webhook/bill3-preflight/',
            enabled_events=['customer.subscription.updated'],
            secret='test-only-preflight-signing-value',
            status='enabled',
            stripe_data={},
        )
        output = StringIO()
        call_command('billing_readiness', '--json', stdout=output)
        report = json.loads(output.getvalue())
        self.assertFalse(report['tiered_pricing_enabled'])
        self.assertTrue(report['tiered_pricing_configuration_ready'])
        self.assertTrue(report['tiered_pricing_ready'])


@override_settings(**BILL3_SETTINGS)
class Bill3CheckoutTests(TestCase):
    def setUp(self):
        create_current_prices()
        self.user = get_user_model().objects.create_user(
            username='bill3-checkout',
            email='bill3-checkout@example.test',
            password='safe-test-password',
        )
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )
        self.client.force_login(self.user)

    def test_all_four_checkout_selections_use_only_server_owned_prices(self):
        with (
            patch('SalesLogApp.billing_views.customer_for_user') as customer,
            patch(
                'SalesLogApp.billing_gateway.stripe.checkout.Session.create'
            ) as create_session,
        ):
            customer.return_value = SimpleNamespace(id='cus_test_bill3_checkout')
            create_session.return_value = SimpleNamespace(
                url='https://checkout.stripe.com/c/pay/test-bill3'
            )
            for selection, (price_id, _, _) in SELECTIONS.items():
                with self.subTest(selection=selection):
                    tier, billing_interval = selection
                    response = self.client.post(
                        reverse('billing_checkout_start'),
                        {
                            'tier': tier,
                            'billing_interval': billing_interval,
                        },
                    )
                    self.assertTrue(
                        response.url.startswith('https://checkout.stripe.com/')
                    )
                    attempt = BillingCheckoutAttempt.objects.filter(
                        user=self.user,
                    ).latest('id')
                    self.assertEqual(attempt.selected_tier, tier)
                    self.assertEqual(
                        attempt.selected_billing_interval, billing_interval,
                    )
                    self.assertEqual(attempt.selected_price_id, price_id)
                    kwargs = create_session.call_args.kwargs
                    self.assertEqual(kwargs['line_items'], [{
                        'price': price_id,
                        'quantity': 1,
                    }])
                    self.assertEqual(kwargs['metadata']['selected_tier'], tier)
                    self.assertEqual(
                        kwargs['metadata']['selected_billing_interval'],
                        billing_interval,
                    )
                    self.assertEqual(kwargs['payment_method_collection'], 'always')
                    expected_trial_days = (
                        30 if selection == (BASIC, MONTH) else 0
                    )
                    self.assertEqual(attempt.trial_days, expected_trial_days)
                    if expected_trial_days:
                        self.assertEqual(
                            kwargs['subscription_data']['trial_period_days'],
                            expected_trial_days,
                        )
                    else:
                        self.assertNotIn(
                            'trial_period_days', kwargs['subscription_data'],
                        )

    def test_browser_supplied_price_identifiers_are_rejected_before_stripe(self):
        payloads = (
            {
                'tier': BASIC,
                'billing_interval': MONTH,
                'price': 'price_browser_supplied',
            },
            {
                'tier': BASIC,
                'billing_interval': MONTH,
                'price_id': 'price_browser_supplied',
            },
            {
                'tier': BASIC,
                'billing_interval': MONTH,
                'selected_price_id': 'price_browser_supplied',
            },
        )
        with patch(
            'SalesLogApp.billing_gateway.stripe.checkout.Session.create'
        ) as create_session:
            for payload in payloads:
                with self.subTest(payload=payload):
                    response = self.client.post(
                        reverse('billing_checkout_start'), payload, follow=True,
                    )
                    self.assertContains(
                        response, 'Choose an available StewLog plan.',
                    )
        create_session.assert_not_called()
        self.assertFalse(BillingCheckoutAttempt.objects.exists())

    def test_unknown_mixed_and_unsupported_choices_fail_closed(self):
        payloads = (
            {},
            {'tier': 'enterprise', 'billing_interval': MONTH},
            {'tier': PRO, 'billing_interval': 'quarter'},
            {'tier': PRO, 'billing_interval': MONTH, 'plan': BASIC},
            {'tier': [BASIC, PRO], 'billing_interval': MONTH},
            {'tier': PRO, 'billing_interval': [MONTH, YEAR]},
        )
        with patch(
            'SalesLogApp.billing_gateway.stripe.checkout.Session.create'
        ) as create_session:
            for payload in payloads:
                with self.subTest(payload=payload):
                    response = self.client.post(
                        reverse('billing_checkout_start'), payload, follow=True,
                    )
                    self.assertContains(
                        response, 'Choose an available StewLog plan.',
                    )
        create_session.assert_not_called()
        self.assertFalse(BillingCheckoutAttempt.objects.exists())

    def test_switching_monthly_to_yearly_expires_incompatible_attempt(self):
        monthly, _ = reserve_checkout_attempt(
            self.user, tier=BASIC, billing_interval=MONTH,
        )
        yearly, created = reserve_checkout_attempt(
            self.user, tier=BASIC, billing_interval=YEAR,
        )
        monthly.refresh_from_db()
        self.assertTrue(created)
        self.assertEqual(monthly.status, BillingCheckoutAttempt.EXPIRED)
        self.assertEqual(yearly.selected_billing_interval, YEAR)
        self.assertEqual(yearly.selected_price_id, 'price_bill3basicyear')

    def test_standard_pro_annual_checkout_omits_trial(self):
        with (
            patch('SalesLogApp.billing_views.customer_for_user') as customer,
            patch(
                'SalesLogApp.billing_gateway.stripe.checkout.Session.create'
            ) as create_session,
        ):
            customer.return_value = SimpleNamespace(id='cus_test_bill3_standard')
            create_session.return_value = SimpleNamespace(
                url='https://checkout.stripe.com/c/pay/test-standard'
            )
            self.client.post(reverse('billing_checkout_start'), {
                'tier': PRO,
                'billing_interval': YEAR,
            })
        kwargs = create_session.call_args.kwargs
        self.assertNotIn('trial_period_days', kwargs['subscription_data'])
        self.assertEqual(kwargs['payment_method_collection'], 'always')

    def test_founder_can_choose_either_pro_interval_with_90_day_trial(self):
        _, raw_code = generate_founder_grant()
        redeem_founder_code(self.user, raw_code)
        with (
            patch('SalesLogApp.billing_views.customer_for_user') as customer,
            patch(
                'SalesLogApp.billing_gateway.stripe.checkout.Session.create'
            ) as create_session,
        ):
            customer.return_value = SimpleNamespace(id='cus_test_bill3_founder')
            create_session.return_value = SimpleNamespace(
                url='https://checkout.stripe.com/c/pay/test-founder'
            )
            for billing_interval in (MONTH, YEAR):
                with self.subTest(billing_interval=billing_interval):
                    self.client.post(reverse('billing_checkout_start'), {
                        'tier': PRO,
                        'billing_interval': billing_interval,
                    })
                    attempt = BillingCheckoutAttempt.objects.filter(
                        user=self.user,
                    ).latest('id')
                    self.assertEqual(
                        attempt.selected_billing_interval, billing_interval,
                    )
                    self.assertEqual(
                        create_session.call_args.kwargs['subscription_data'][
                            'trial_period_days'
                        ],
                        90,
                    )

    def test_founder_cannot_select_basic_on_either_interval(self):
        _, raw_code = generate_founder_grant()
        redeem_founder_code(self.user, raw_code)
        with patch(
            'SalesLogApp.billing_gateway.stripe.checkout.Session.create'
        ) as create_session:
            for billing_interval in (MONTH, YEAR):
                with self.subTest(billing_interval=billing_interval):
                    response = self.client.post(
                        reverse('billing_checkout_start'),
                        {
                            'tier': BASIC,
                            'billing_interval': billing_interval,
                        },
                        follow=True,
                    )
                    self.assertContains(
                        response, 'Choose an available StewLog plan.',
                    )
        create_session.assert_not_called()


@override_settings(**BILL3_SETTINGS)
class Bill3SubscriptionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='bill3-entitlement', email='bill3-entitlement@example.test'
        )
        self.customer = Customer.objects.create(
            id='cus_test_bill3_entitlement',
            subscriber=self.user,
            livemode=False,
            email=self.user.email,
            stripe_data={'id': 'cus_test_bill3_entitlement', 'livemode': False},
        )

    def subscription(self, price_ids, *, suffix, metadata=None):
        now = timezone.now()
        data = {
            'id': f'sub_test_bill3_{suffix}',
            'object': 'subscription',
            'customer': self.customer.id,
            'status': 'active',
            'livemode': False,
            'metadata': metadata or {},
            'current_period_end': int((now + timedelta(days=30)).timestamp()),
            'items': {
                'data': [
                    {'price': {'id': price_id}} for price_id in price_ids
                ],
            },
        }
        return Subscription.objects.create(
            id=data['id'],
            customer=self.customer,
            livemode=False,
            stripe_data=data,
        )

    def entitlement(self, subscription):
        BillingAccess.objects.update_or_create(
            user=self.user,
            defaults={'authoritative_subscription': subscription},
        )
        return get_billing_entitlement(self.user)

    def test_billing_interval_never_changes_entitlement_tier(self):
        for index, (selection, (price_id, _, _)) in enumerate(
            SELECTIONS.items(), start=1,
        ):
            with self.subTest(selection=selection):
                tier, billing_interval = selection
                subscription = self.subscription(
                    [price_id], suffix=f'current_{index}',
                )
                plan = classify_subscription_plan(subscription)
                entitlement = self.entitlement(subscription)
                self.assertTrue(plan.eligible)
                self.assertEqual(plan.tier, tier)
                self.assertEqual(plan.billing_interval, billing_interval)
                self.assertEqual(entitlement.tier, tier)
                self.assertEqual(entitlement.has_pro_access, tier == PRO)

    def test_basic_monthly_confirmation_consumes_standard_trial_once(self):
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )
        attempt, _ = reserve_checkout_attempt(
            self.user, tier=BASIC, billing_interval=MONTH,
        )
        self.assertEqual(attempt.trial_kind, BillingCheckoutAttempt.STANDARD)
        self.assertEqual(attempt.trial_days, 30)
        subscription = self.subscription(
            ['price_bill3basicmonth'], suffix='standard_trial',
            metadata={'billing_attempt': str(attempt.public_id)},
        )
        self.assertTrue(
            finalize_introductory_benefit(attempt.public_id, subscription)
        )
        consumed_at = BillingAccess.objects.get(
            user=self.user,
        ).introductory_benefit_consumed_at
        self.assertTrue(
            finalize_introductory_benefit(attempt.public_id, subscription)
        )
        access = BillingAccess.objects.get(user=self.user)
        self.assertEqual(access.introductory_benefit_consumed_at, consumed_at)
        self.assertEqual(
            access.introductory_benefit_kind, BillingAccess.STANDARD,
        )

    def test_allowlisted_legacy_price_remains_grandfathered_pro(self):
        subscription = self.subscription(
            ['price_bill3legacypro'], suffix='legacy',
        )
        entitlement = self.entitlement(subscription)
        self.assertTrue(entitlement.subscription_access)
        self.assertTrue(entitlement.has_pro_access)
        self.assertTrue(entitlement.grandfathered)

    def test_unknown_mixed_and_duplicate_subscriptions_fail_closed(self):
        cases = (
            ('unknown', ['price_bill3unknown']),
            ('mixed', ['price_bill3basicmonth', 'price_bill3proyear']),
            ('duplicate', ['price_bill3promonth', 'price_bill3promonth']),
            (
                'multi_interval',
                ['price_bill3basicmonth', 'price_bill3basicyear'],
            ),
        )
        for suffix, price_ids in cases:
            with self.subTest(suffix=suffix):
                subscription = self.subscription(price_ids, suffix=suffix)
                self.assertFalse(classify_subscription_plan(subscription).eligible)
                self.assertFalse(self.entitlement(subscription).subscription_access)

        malformed = self.subscription(
            ['price_bill3promonth'], suffix='malformed_extra',
        )
        malformed.stripe_data['items']['data'].append({'price': {}})
        malformed.save(update_fields=['stripe_data'])
        self.assertFalse(classify_subscription_plan(malformed).eligible)
        self.assertFalse(self.entitlement(malformed).subscription_access)

    def test_webhook_confirmation_matches_exact_tier_interval_and_price(self):
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )
        attempt, _ = reserve_checkout_attempt(
            self.user, tier=PRO, billing_interval=YEAR,
        )
        subscription = self.subscription(
            ['price_bill3proyear'],
            suffix='webhook_match',
            metadata={'billing_attempt': str(attempt.public_id)},
        )
        self.assertTrue(
            finalize_introductory_benefit(attempt.public_id, subscription)
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, BillingCheckoutAttempt.CONFIRMED)

    def test_webhook_confirmation_rejects_interval_mismatch(self):
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )
        attempt, _ = reserve_checkout_attempt(
            self.user, tier=PRO, billing_interval=YEAR,
        )
        subscription = self.subscription(
            ['price_bill3promonth'], suffix='webhook_wrong_interval',
        )
        with self.assertRaises(BillingPolicyError):
            finalize_introductory_benefit(attempt.public_id, subscription)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, BillingCheckoutAttempt.RESERVED)


@override_settings(**BILL3_SETTINGS)
class Bill3PricingPageTests(TestCase):
    def setUp(self):
        create_current_prices()
        self.user = get_user_model().objects.create_user(
            username='bill3-pricing',
            email='bill3-pricing@example.test',
            password='safe-test-password',
        )
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )

    def test_billing_page_displays_monthly_yearly_and_post_trial_charges(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('billing_overview'))
        self.assertContains(response, '$4.99 USD per month')
        self.assertContains(response, '$49.00 USD per year')
        self.assertContains(response, '$4.08 USD per month equivalent')
        self.assertContains(response, '$9.99 USD per month')
        self.assertContains(response, '$99.00 USD per year')
        self.assertContains(response, '$8.25 USD per month equivalent')
        self.assertContains(response, 'billed yearly', count=2)
        self.assertContains(response, 'after your 30-day trial', count=1)
        self.assertContains(response, 'No trial.', count=3)
        self.assertContains(
            response,
            'charge the full $49.00 USD per year when you subscribe',
        )
        for price_id, _, _ in SELECTIONS.values():
            self.assertNotContains(response, price_id)

    def test_public_pricing_page_displays_both_intervals(self):
        response = self.client.get(reverse('landing_page'))
        self.assertContains(response, '$4.99 USD per month')
        self.assertContains(response, '$49.00 USD per year')
        self.assertContains(response, '$9.99 USD per month')
        self.assertContains(response, '$99.00 USD per year')
        self.assertContains(response, 'billed yearly', count=2)
        self.assertContains(
            response,
            'Basic Monthly includes an eligible 30-day trial',
        )
        self.assertContains(
            response,
            'The full $99.00 USD per year is charged when you subscribe',
        )

    def test_founder_pricing_is_pro_only_with_both_intervals_and_90_days(self):
        _, raw_code = generate_founder_grant()
        redeem_founder_code(self.user, raw_code)
        self.client.force_login(self.user)
        response = self.client.get(reverse('billing_overview'))
        self.assertNotContains(response, '$4.99 USD per month')
        self.assertNotContains(response, '$49.00 USD per year')
        self.assertContains(response, '$9.99 USD per month')
        self.assertContains(response, '$99.00 USD per year')
        self.assertContains(response, 'After your 90-day trial')


ROLLBACK_SETTINGS = {
    **BILL3_SETTINGS,
    'BILLING_TIERED_PRICING_ENABLED': False,
    'STRIPE_BASIC_YEARLY_PRICE_ID': '',
    'STRIPE_PRO_MONTHLY_PRICE_ID': '',
    'STRIPE_PRO_YEARLY_PRICE_ID': '',
    'STRIPE_LEGACY_PRO_PRICE_IDS': [],
}


@override_settings(**ROLLBACK_SETTINGS)
class Bill3RollbackTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='bill3-rollback', email='bill3-rollback@example.test'
        )
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )

    def test_rollout_off_preserves_original_single_price_as_pro(self):
        customer = Customer.objects.create(
            id='cus_test_bill3_rollback',
            subscriber=self.user,
            livemode=False,
            email=self.user.email,
            stripe_data={'id': 'cus_test_bill3_rollback', 'livemode': False},
        )
        data = {
            'id': 'sub_test_bill3_rollback',
            'customer': customer.id,
            'status': 'active',
            'livemode': False,
            'current_period_end': int(
                (timezone.now() + timedelta(days=30)).timestamp()
            ),
            'items': {'data': [{
                'price': {'id': settings.STRIPE_BASIC_MONTHLY_PRICE_ID},
            }]},
        }
        subscription = Subscription.objects.create(
            id=data['id'], customer=customer, livemode=False, stripe_data=data,
        )
        BillingAccess.objects.create(
            user=self.user, authoritative_subscription=subscription,
        )
        entitlement = get_billing_entitlement(self.user)
        self.assertTrue(billing_configuration().ready)
        self.assertFalse(
            billing_configuration().tiered_pricing_configuration_ready
        )
        self.assertEqual(synchronized_plan_price_errors(), ())
        self.assertTrue(entitlement.has_pro_access)
        self.assertTrue(entitlement.grandfathered)

    def test_rollout_off_checkout_defaults_to_original_monthly_policy(self):
        attempt, created = reserve_checkout_attempt(self.user)
        self.assertTrue(created)
        self.assertEqual(attempt.selected_tier, PRO)
        self.assertEqual(attempt.selected_billing_interval, MONTH)
        self.assertEqual(
            attempt.selected_price_id,
            settings.STRIPE_BASIC_MONTHLY_PRICE_ID,
        )
