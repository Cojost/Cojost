import json
import uuid

from django.core.management.base import BaseCommand
from django.db import DatabaseError, connection
from django.db.migrations.recorder import MigrationRecorder
from django.urls import NoReverseMatch, reverse

from djstripe.models import WebhookEndpoint

from SalesLogApp.billing_configuration import billing_configuration


class Command(BaseCommand):
    help = 'Report billing readiness without network calls or secret values.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true')

    def handle(self, *args, **options):
        configuration = billing_configuration()
        try:
            webhook_path = reverse(
                'djstripe:djstripe_webhook_by_uuid',
                kwargs={'uuid': uuid.uuid4()},
            )
            webhook_route_present = webhook_path.startswith('/stripe/webhook/')
        except NoReverseMatch:
            webhook_route_present = False
        try:
            endpoints = WebhookEndpoint.objects.filter(
                livemode=(configuration.mode == 'live')
            )
            webhook_endpoint_present = endpoints.exists()
            webhook_secret_present = endpoints.exclude(secret='').exists()
            webhook_endpoint_ready = endpoints.filter(
                status='enabled',
                djstripe_validation_method='verify_signature',
            ).exclude(secret='').exists()
        except DatabaseError:
            webhook_endpoint_present = False
            webhook_secret_present = False
            webhook_endpoint_ready = False
        try:
            applied = set(MigrationRecorder(connection).applied_migrations())
            migrations = {
                'djstripe_0003': ('djstripe', '0003_2_11') in applied,
                'billing_foundation': (
                    'SalesLogApp', '0054_billing_foundation'
                ) in applied,
                'billing_onboarding': (
                    'SalesLogApp',
                    '0060_billingaccess_onboarding_required_at',
                ) in applied,
            }
        except DatabaseError:
            migrations = {
                'djstripe_0003': False,
                'billing_foundation': False,
                'billing_onboarding': False,
            }
        report = {
            'mode': configuration.mode,
            'feature_enabled': configuration.feature_enabled,
            'enforcement_enabled': configuration.enforcement_enabled,
            'onboarding_enabled': configuration.onboarding_enabled,
            'publishable_credential_configured': configuration.public_key_configured,
            'publishable_credential_valid': configuration.public_key_valid,
            'server_credential_configured': configuration.secret_key_configured,
            'server_credential_valid': configuration.secret_key_valid,
            'price_configured': configuration.price_configured,
            'price_valid': configuration.price_valid,
            'signature_verification': (
                configuration.webhook_validation == 'verify_signature'
            ),
            'webhook_route_present': webhook_route_present,
            'webhook_endpoint_present': webhook_endpoint_present,
            'webhook_signing_secret_present': webhook_secret_present,
            'webhook_endpoint_ready': webhook_endpoint_ready,
            'portal_configuration': 'manual_verification_required',
            'migrations': migrations,
            'configuration_ready': configuration.ready,
            'enforcement_ready': (
                configuration.ready
                and webhook_route_present
                and webhook_endpoint_ready
                and all(migrations.values())
            ),
        }
        if options['json']:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return
        for name, value in report.items():
            if name == 'migrations':
                self.stdout.write('migrations:')
                for migration, applied in value.items():
                    self.stdout.write(f'  {migration}: {applied}')
            else:
                self.stdout.write(f'{name}: {value}')
