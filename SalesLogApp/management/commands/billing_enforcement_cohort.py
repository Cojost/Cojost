import json
from collections import Counter
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from SalesLogApp.billing_enforcement import cohort_enforcement_state
from SalesLogApp.billing_entitlements import get_billing_entitlement
from SalesLogApp.models import BillingAccess


CONFIRMATION = 'APPLY_BILLING_ENFORCEMENT_COHORT'


class Command(BaseCommand):
    help = (
        'Audit or safely enroll an existing-user billing enforcement cohort. '
        'The command never sends email or calls Stripe.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--action',
            choices=('audit', 'enroll', 'mark-notice'),
            default='audit',
        )
        parser.add_argument('--user-id', action='append', type=int, dest='user_ids')
        parser.add_argument('--all-existing', action='store_true')
        parser.add_argument('--grace-days', type=int, default=30)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--confirm', default='')
        parser.add_argument('--json', action='store_true')
        parser.add_argument('--details', action='store_true')

    def handle(self, *args, **options):
        action = options['action']
        if action == 'audit':
            if options['apply']:
                raise CommandError('Audit is read-only; do not use --apply.')
            users = self._audit_users(options)
            report = self._audit(users, details=options['details'])
            return self._write(report, as_json=options['json'])

        users = self._mutation_targets(options)
        if not 1 <= options['grace_days'] <= 365:
            raise CommandError('--grace-days must be between 1 and 365.')
        if not options['apply']:
            report = {
                'action': action,
                'applied': False,
                'target_count': users.count(),
                'confirmation_required': CONFIRMATION,
                'emails_sent': 0,
                'network_calls': False,
            }
            return self._write(report, as_json=options['json'])
        if options['confirm'] != CONFIRMATION:
            raise CommandError(
                f'--confirm must exactly equal {CONFIRMATION}.'
            )
        if action == 'enroll':
            report = self._enroll(users)
        else:
            report = self._mark_notice(users, options['grace_days'])
        return self._write(report, as_json=options['json'])

    def _base_users(self):
        return get_user_model().objects.filter(is_active=True).order_by('pk')

    def _audit_users(self, options):
        user_ids = options['user_ids'] or []
        if user_ids and options['all_existing']:
            raise CommandError('Choose --user-id or --all-existing, not both.')
        users = self._base_users()
        if user_ids:
            users = users.filter(pk__in=set(user_ids))
        elif options['all_existing']:
            users = users.filter(
                Q(billing_access__isnull=True)
                | Q(billing_access__onboarding_required_at__isnull=True)
            )
        return users

    def _mutation_targets(self, options):
        user_ids = options['user_ids'] or []
        if bool(user_ids) == bool(options['all_existing']):
            raise CommandError(
                'Choose exactly one target mode: --user-id or --all-existing.'
            )
        users = self._base_users().filter(is_superuser=False)
        if user_ids:
            requested = set(user_ids)
            users = users.filter(pk__in=requested)
            found = set(users.values_list('pk', flat=True))
            if found != requested:
                raise CommandError(
                    'Every requested user must exist, be active, and not be a superuser.'
                )
            return users
        return users.filter(
            Q(billing_access__isnull=True)
            | Q(billing_access__onboarding_required_at__isnull=True)
        )

    def _audit(self, users, *, details):
        counts = Counter()
        rows = []
        for user in users:
            access = BillingAccess.objects.filter(user=user).first()
            entitlement = get_billing_entitlement(user)
            state = cohort_enforcement_state(
                user,
                access,
                subscription_access=entitlement.subscription_access,
            )
            counts[state.code] += 1
            if details:
                rows.append({
                    'user_id': user.pk,
                    'username': user.get_username(),
                    'state': state.code,
                })
        report = {
            'action': 'audit',
            'applied': False,
            'network_calls': False,
            'user_count': sum(counts.values()),
            'states': dict(sorted(counts.items())),
        }
        if details:
            report['details'] = rows
        return report

    @transaction.atomic
    def _enroll(self, users):
        now = timezone.now()
        enrolled = 0
        unchanged = 0
        for user in users.select_for_update():
            access, _ = BillingAccess.objects.select_for_update().get_or_create(
                user=user,
            )
            if access.enforcement_enrolled_at is not None:
                unchanged += 1
                continue
            access.enforcement_enrolled_at = now
            access.save(update_fields=['enforcement_enrolled_at', 'updated_at'])
            enrolled += 1
        return {
            'action': 'enroll',
            'applied': True,
            'enrolled': enrolled,
            'unchanged': unchanged,
            'emails_sent': 0,
            'network_calls': False,
        }

    @transaction.atomic
    def _mark_notice(self, users, grace_days):
        now = timezone.now()
        grace_ends_at = now + timedelta(days=grace_days)
        marked = 0
        unchanged = 0
        for user in users.select_for_update():
            try:
                access = BillingAccess.objects.select_for_update().get(user=user)
            except BillingAccess.DoesNotExist as exc:
                raise CommandError(
                    'Every target must be enrolled before notice is recorded.'
                ) from exc
            if access.enforcement_enrolled_at is None:
                raise CommandError(
                    'Every target must be enrolled before notice is recorded.'
                )
            if access.enforcement_notice_sent_at is not None:
                unchanged += 1
                continue
            access.enforcement_notice_sent_at = now
            access.enforcement_grace_ends_at = grace_ends_at
            access.save(update_fields=[
                'enforcement_notice_sent_at',
                'enforcement_grace_ends_at',
                'updated_at',
            ])
            marked += 1
        return {
            'action': 'mark-notice',
            'applied': True,
            'notice_recorded': marked,
            'unchanged': unchanged,
            'grace_days': grace_days,
            'emails_sent': 0,
            'network_calls': False,
        }

    def _write(self, report, *, as_json):
        if as_json:
            self.stdout.write(json.dumps(report, sort_keys=True, default=str))
            return
        for key, value in report.items():
            self.stdout.write(f'{key}: {value}')
