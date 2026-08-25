import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection
from django.db.migrations.recorder import MigrationRecorder

from SalesLogApp.ask_stew_entitlements import _configured_pilot_user_ids
from SalesLogApp.pay_plan_intents.openai_provider import provider_configuration


MIGRATION_NAME = '0064_ask_stew_ai1a_lab'


def _migration_applied():
    try:
        return MigrationRecorder(connection).migration_qs.filter(
            app='SalesLogApp',
            name=MIGRATION_NAME,
        ).exists()
    except DatabaseError:
        return False


def _readiness_payload():
    configuration = provider_configuration()
    migration_applied = _migration_applied()
    lab_only = bool(settings.ASK_STEW_AI_LAB_ONLY)
    provider_ready = bool(configuration.ready)
    return {
        'ai_routing_configuration_ready': provider_ready,
        'conversation_retention_days': (
            settings.ASK_STEW_AI_CONVERSATION_RETENTION_DAYS
        ),
        'conversation_ttl_hours': (
            settings.ASK_STEW_AI_CONVERSATION_TTL_HOURS
        ),
        'customer_access_blocked': lab_only,
        'daily_provider_request_limit': (
            configuration.daily_request_limit
        ),
        'internal_lab_ready': bool(
            migration_applied and lab_only and provider_ready
        ),
        'lab_only': lab_only,
        'migration_0064_applied': migration_applied,
        'model': configuration.model or 'not_configured',
        'paid_request_made': False,
        'pilot_user_count': len(_configured_pilot_user_ids()),
        'provider': configuration.provider or 'not_configured',
        'provider_state': configuration.state,
        'short_window_limit': settings.ASK_STEW_AI_SHORT_WINDOW_LIMIT,
        'short_window_seconds': settings.ASK_STEW_AI_SHORT_WINDOW_SECONDS,
    }


class Command(BaseCommand):
    help = (
        'Report Ask Stew AI-1A deployment readiness without making an API '
        'request or printing secrets.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', dest='as_json')
        parser.add_argument('--require-ready', action='store_true')

    def handle(self, *args, **options):
        payload = _readiness_payload()
        if options['as_json']:
            self.stdout.write(json.dumps(payload, sort_keys=True))
        else:
            for key, value in payload.items():
                if isinstance(value, bool):
                    value = str(value).lower()
                elif value is None:
                    value = 'not_configured'
                self.stdout.write(f'{key}={value}')
        if options['require_ready'] and not payload['internal_lab_ready']:
            raise CommandError('Ask Stew AI-1A internal lab is not ready.')
