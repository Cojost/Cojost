from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from SalesLogApp.email_verification import (
    ALREADY_VERIFIED,
    INVALID_EMAIL,
    MISSING_ADDRESS,
    OWNERSHIP_CONFLICT,
    UNVERIFIED_ADDRESS,
    assess_verification_user,
    build_verification_request,
    dispatch_verification_email,
    ProductionEmailConfigurationError,
    validate_production_email_delivery_configuration,
)
from SalesLogApp.models import EmailVerificationDispatch


def positive_integer(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CommandError('Batch controls must be positive integers.') from exc
    if parsed < 1:
        raise CommandError('Batch controls must be positive integers.')
    return parsed


class Command(BaseCommand):
    help = (
        'Audit existing users for missing email verification and, only with '
        '--send, repair safe EmailAddress rows and resend confirmation email.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--send',
            action='store_true',
            help='Repair eligible records and send verification email.',
        )
        parser.add_argument(
            '--limit',
            type=positive_integer,
            help='Stop after this many eligible recipients.',
        )
        parser.add_argument(
            '--user-id',
            type=positive_integer,
            help='Audit or send to exactly one user ID.',
        )
        parser.add_argument(
            '--email',
            help='Audit or send to exactly one account email address.',
        )
        parser.add_argument(
            '--batch-size',
            type=positive_integer,
            default=100,
            help='Database iterator batch size (default: 100).',
        )
        parser.add_argument(
            '--confirm-production-send',
            action='store_true',
            help='Required with --send whenever DEBUG is false.',
        )

    def _validate_options(self, options):
        if options['user_id'] and options['email']:
            raise CommandError('Use only one of --user-id or --email.')
        if options['confirm_production_send'] and not options['send']:
            raise CommandError('--confirm-production-send requires --send.')
        if options['email']:
            candidate = options['email'].strip()
            try:
                validate_email(candidate)
            except ValidationError as exc:
                raise CommandError('The --email selector is invalid.') from exc
        if options['send'] and not settings.DEBUG:
            if not options['confirm_production_send']:
                raise CommandError(
                    'Production sending requires --confirm-production-send.'
                )
            try:
                validate_production_email_delivery_configuration()
            except ProductionEmailConfigurationError as exc:
                raise CommandError(
                    'Production email delivery preflight failed.'
                ) from exc

    def _users(self, options):
        user_model = get_user_model()
        if options['user_id']:
            users = user_model.objects.filter(pk=options['user_id'])
            if not users.exists():
                raise CommandError('The --user-id selector matched no user.')
            return users.order_by('pk')
        if options['email']:
            users = user_model.objects.filter(
                email__iexact=options['email'].strip(),
            )
            if users.count() != 1:
                raise CommandError(
                    'The --email selector must match exactly one user.'
                )
            return users.order_by('pk')
        return user_model.objects.filter(is_active=True).order_by('pk')

    def handle(self, *args, **options):
        self._validate_options(options)
        users = self._users(options)
        send_mode = options['send']
        mode = 'SEND' if send_mode else 'DRY RUN'
        self.stdout.write(f'Email verification audit mode: {mode}')

        counts = {
            'active_examined': 0,
            ALREADY_VERIFIED: 0,
            UNVERIFIED_ADDRESS: 0,
            MISSING_ADDRESS: 0,
            INVALID_EMAIL: 0,
            OWNERSHIP_CONFLICT: 0,
            'eligible': 0,
            'sent': 0,
            'skipped': 0,
            'failed': 0,
        }
        for user in users.iterator(chunk_size=options['batch_size']):
            assessment = assess_verification_user(user)
            if not user.is_active:
                counts['skipped'] += 1
                continue
            counts['active_examined'] += 1
            counts[assessment.category] += 1
            if not assessment.eligible:
                if assessment.category == OWNERSHIP_CONFLICT:
                    self.stderr.write(
                        self.style.WARNING(
                            f'User ID {user.pk}: email ownership conflict; '
                            'manual review required.'
                        )
                    )
                counts['skipped'] += 1
                continue

            counts['eligible'] += 1
            if send_mode:
                request = build_verification_request(
                    user=user,
                    base_url=settings.EMAIL_VERIFICATION_PUBLIC_BASE_URL,
                )
                result = dispatch_verification_email(
                    user=user,
                    request=request,
                    source=EmailVerificationDispatch.BACKFILL,
                )
                counts[result.outcome] += 1
                if result.outcome == 'failed':
                    self.stderr.write(
                        self.style.ERROR(
                            f'User ID {user.pk}: verification delivery failed.'
                        )
                    )
            else:
                counts['skipped'] += 1

            if options['limit'] and counts['eligible'] >= options['limit']:
                break

        self.stdout.write('Final summary')
        labels = (
            ('active_examined', 'Active users examined'),
            (ALREADY_VERIFIED, 'Already verified'),
            (UNVERIFIED_ADDRESS, 'Unverified with valid EmailAddress row'),
            (MISSING_ADDRESS, 'Missing EmailAddress row'),
            (INVALID_EMAIL, 'Blank or invalid user email'),
            (OWNERSHIP_CONFLICT, 'Duplicate/conflicting email ownership'),
            ('eligible', 'Eligible recipients'),
            ('sent', 'Sent'),
            ('skipped', 'Skipped'),
            ('failed', 'Failed'),
        )
        for key, label in labels:
            self.stdout.write(f'{label}: {counts[key]}')
