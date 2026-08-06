from django.core.checks import Warning, register

from .pay_plan_intents.openai_provider import provider_configuration


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
