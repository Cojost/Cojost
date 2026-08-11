from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from SalesLogApp.billing_services import generate_founder_grant


class Command(BaseCommand):
    help = 'Generate one founder code and display its plaintext exactly once.'

    def add_arguments(self, parser):
        parser.add_argument('--expires-in-days', type=int)
        parser.add_argument('--trial-days', type=int)
        parser.add_argument('--created-by')
        parser.add_argument('--note', default='')

    def handle(self, *args, **options):
        expires_in_days = options['expires_in_days']
        if expires_in_days is not None and not 1 <= expires_in_days <= 3650:
            raise CommandError('--expires-in-days must be between 1 and 3650.')
        expires_at = (
            timezone.now() + timedelta(days=expires_in_days)
            if expires_in_days is not None
            else None
        )
        created_by = None
        if options['created_by']:
            try:
                created_by = get_user_model().objects.get(
                    username=options['created_by']
                )
            except get_user_model().DoesNotExist as exc:
                raise CommandError('The creating user was not found.') from exc
        try:
            grant, raw_code = generate_founder_grant(
                created_by=created_by,
                expires_at=expires_at,
                trial_days=options['trial_days'],
                administrative_note=options['note'],
            )
        except Exception as exc:
            raise CommandError('Founder code generation failed validation.') from exc
        self.stdout.write(f'Founder grant: {grant.public_id}')
        self.stdout.write('Copy this code now; it cannot be recovered later:')
        self.stdout.write(raw_code)
