from django.core.management.base import BaseCommand

from SalesLogApp.pay_plan_intents.openai_provider import provider_configuration


class Command(BaseCommand):
    help = 'Report Pay Plan Assistant provider readiness without making an API request.'

    def handle(self, *args, **options):
        configuration = provider_configuration()
        self.stdout.write(f'state={configuration.state}')
        self.stdout.write(f'provider={configuration.provider or "not_configured"}')
        self.stdout.write(f'model={configuration.model or "not_configured"}')
        if configuration.timeout_seconds is not None:
            self.stdout.write(
                f'timeout_seconds={configuration.timeout_seconds}'
            )
        if configuration.rollout_percent is not None:
            self.stdout.write(
                f'rollout_percent={configuration.rollout_percent}'
            )
        self.stdout.write(
            f'allowlist_user_count={len(configuration.allowed_user_ids)}'
        )
        if configuration.daily_request_limit is not None:
            self.stdout.write(
                f'daily_request_limit={configuration.daily_request_limit}'
            )
        if configuration.errors:
            for error in configuration.errors:
                self.stdout.write(f'configuration_error={error}')
        self.stdout.write('paid_request_made=false')
        self.stdout.write('credentials_present=' + (
            'true' if configuration.state == 'ready' else 'not_confirmed'
        ))
