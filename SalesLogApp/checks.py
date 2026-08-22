from django.conf import settings
from django.core.checks import Error, Warning, register
from django.db import DatabaseError, connection

from .pay_plan_intents.openai_provider import provider_configuration
from .billing_configuration import billing_configuration
from .email_verification import (
    ProductionEmailConfigurationError,
    validate_production_email_delivery_configuration,
)


@register()
def pay_plan_assistant_provider_check(app_configs, **kwargs):
    configuration = provider_configuration()
    if configuration.state in {'disabled', 'ready'}:
        return []
    messages = {
        'missing_credentials': (
            'Pay Plan Assistant provider is enabled but credentials are missing.'
        ),
        'unsupported_provider': (
            'Pay Plan Assistant provider is enabled with an unsupported provider.'
        ),
        'invalid_configuration': (
            'Pay Plan Assistant provider settings are invalid: '
            + '; '.join(configuration.errors)
        ),
    }
    return [Warning(
        messages.get(
            configuration.state,
            'Pay Plan Assistant provider configuration is not ready.',
        ),
        hint=(
            'External requests remain disabled; deterministic assistance '
            'continues to work. Run assistant_provider_health for safe details.'
        ),
        id='SalesLogApp.W001',
    )]


@register()
def billing_configuration_check(app_configs, **kwargs):
    if not (
        settings.BILLING_FEATURE_ENABLED
        or settings.BILLING_ENFORCEMENT_ENABLED
        or settings.BILLING_ONBOARDING_ENABLED
        or settings.BILLING_TIERED_PRICING_ENABLED
    ):
        return []
    configuration = billing_configuration()
    if not configuration.ready:
        message = (
            'Billing configuration is not ready: '
            + '; '.join(configuration.errors)
            + '.'
        )
        if (
            settings.BILLING_ENFORCEMENT_ENABLED
            or settings.BILLING_ONBOARDING_ENABLED
            or settings.BILLING_TIERED_PRICING_ENABLED
        ):
            return [Error(
                message,
                hint=(
                    'Disable tiered pricing/onboarding/enforcement or configure '
                    'the selected Stripe mode.'
                ),
                id='SalesLogApp.E002',
            )]
        return [Warning(
            message,
            hint='Checkout remains unavailable until configuration is complete.',
            id='SalesLogApp.W002',
        )]

    if (
        settings.BILLING_ENFORCEMENT_ENABLED
        or settings.BILLING_ONBOARDING_ENABLED
        or settings.BILLING_TIERED_PRICING_ENABLED
    ):
        operational_errors = _billing_operational_errors()
        if operational_errors:
            return [Error(
                'Billing rollout prerequisites are incomplete: '
                + '; '.join(operational_errors)
                + '.',
                hint=(
                    'Disable tiered pricing/onboarding/enforcement until '
                    'migrations, synchronized Prices, and a signed '
                    'WebhookEndpoint are ready for the selected mode.'
                ),
                id='SalesLogApp.E003',
            )]
    if (
        settings.BILLING_ENFORCEMENT_ENABLED
        and settings.BILLING_ENFORCEMENT_EMERGENCY_BYPASS
    ):
        return [Warning(
            'Billing enforcement is enabled but the emergency bypass is active.',
            hint=(
                'Keep the bypass only for incident recovery, then disable it '
                'after verifying cohort enforcement is safe.'
            ),
            id='SalesLogApp.W003',
        )]
    return []


@register(deploy=True)
def email_verification_delivery_check(app_configs, **kwargs):
    if settings.DEBUG:
        return []
    try:
        validate_production_email_delivery_configuration()
    except ProductionEmailConfigurationError:
        return [Error(
            'Production email delivery configuration failed safe preflight.',
            hint=(
                'Configure the supported SMTP backend, a public SMTP host, '
                'encrypted transport, and a syntactically valid non-local sender.'
            ),
            id='SalesLogApp.E004',
        )]
    return []


def _billing_operational_errors():
    """Return non-secret reasons that enforcement cannot safely be enabled."""
    from django.db.migrations.recorder import MigrationRecorder
    from djstripe.models import WebhookEndpoint

    try:
        applied = set(
            MigrationRecorder(connection).migration_qs.filter(
                app__in=['SalesLogApp', 'djstripe']
            ).values_list('app', 'name')
        )
        errors = []
        if ('SalesLogApp', '0054_billing_foundation') not in applied:
            errors.append('the billing migration is not applied')
        if (
            settings.BILLING_ONBOARDING_ENABLED
            and ('SalesLogApp', '0060_billingaccess_onboarding_required_at')
            not in applied
        ):
            errors.append('the billing onboarding migration is not applied')
        if (
            settings.BILLING_ENFORCEMENT_ENABLED
            and ('SalesLogApp', '0063_bill4_staged_billing_enforcement')
            not in applied
        ):
            errors.append('the staged billing enforcement migration is not applied')
        if (
            settings.BILLING_TIERED_PRICING_ENABLED
            and ('SalesLogApp', '0061_billingcheckoutattempt_selected_plan')
            not in applied
        ):
            errors.append('the tiered-pricing migration is not applied')
        if (
            settings.BILLING_TIERED_PRICING_ENABLED
            and (
                'SalesLogApp',
                '0062_billingcheckoutattempt_selected_billing_interval',
            ) not in applied
        ):
            errors.append('the annual-billing migration is not applied')
        if ('djstripe', '0003_2_11') not in applied:
            errors.append('the required dj-stripe migration is not applied')
        webhook_ready = WebhookEndpoint.objects.filter(
            livemode=settings.STRIPE_LIVE_MODE,
            status='enabled',
            djstripe_validation_method='verify_signature',
        ).exclude(secret='').exists()
        if not webhook_ready:
            errors.append(
                'no enabled signature-verifying webhook endpoint exists for '
                'the selected mode'
            )
        if settings.BILLING_TIERED_PRICING_ENABLED:
            from .billing_pricing import synchronized_plan_price_errors

            errors.extend(synchronized_plan_price_errors())
        return errors
    except DatabaseError:
        return ['the billing database schema could not be inspected']
