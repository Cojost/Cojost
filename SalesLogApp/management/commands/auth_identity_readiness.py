import json
from collections import defaultdict

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, models


EMAIL_CONSTRAINT_NAME = 'auth_user_email_ci_unique'
USERNAME_CONSTRAINT_NAME = 'auth_user_username_ci_unique'


def normalize_email(value):
    return (value or '').strip().casefold()


def normalize_username(value):
    return (value or '').strip().casefold()


def _collision_groups(entries):
    grouped = defaultdict(set)
    for normalized_value, user_id in entries:
        if normalized_value:
            grouped[normalized_value].add(user_id)
    return [
        sorted(user_ids)
        for user_ids in grouped.values()
        if len(user_ids) > 1
    ]


def _user_ids(groups):
    return sorted({user_id for group in groups for user_id in group})


def _constraint_names(user_model):
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor,
            user_model._meta.db_table,
        )
    return set(constraints)


def build_auth_identity_readiness_report():
    """Inspect identity data without changing users or allauth records."""
    user_model = get_user_model()
    users = list(
        user_model._default_manager.order_by('pk').values(
            'pk',
            user_model.USERNAME_FIELD,
            user_model.get_email_field_name(),
        )
    )
    addresses = list(
        EmailAddress.objects.order_by('pk').values(
            'pk', 'user_id', 'email', 'verified', 'primary'
        )
    )

    username_field = user_model.USERNAME_FIELD
    email_field = user_model.get_email_field_name()
    missing_email_user_ids = []
    email_normalization_user_ids = []
    missing_username_user_ids = []
    username_whitespace_user_ids = []
    user_email_entries = []
    username_entries = []
    canonical_email_by_user = {}

    for user in users:
        user_id = user['pk']
        raw_email = user[email_field] or ''
        normalized_email = normalize_email(raw_email)
        canonical_email_by_user[user_id] = normalized_email
        if not normalized_email:
            missing_email_user_ids.append(user_id)
        else:
            user_email_entries.append((normalized_email, user_id))
            if raw_email != normalized_email:
                email_normalization_user_ids.append(user_id)

        raw_username = user[username_field] or ''
        normalized_username = normalize_username(raw_username)
        if not normalized_username:
            missing_username_user_ids.append(user_id)
        else:
            username_entries.append((normalized_username, user_id))
            if raw_username != raw_username.strip():
                username_whitespace_user_ids.append(user_id)

    address_entries = []
    address_normalization_user_ids = set()
    addresses_by_user = defaultdict(list)
    for address in addresses:
        user_id = address['user_id']
        raw_email = address['email'] or ''
        normalized_email = normalize_email(raw_email)
        addresses_by_user[user_id].append((normalized_email, address))
        if normalized_email:
            address_entries.append((normalized_email, user_id))
        if raw_email != normalized_email:
            address_normalization_user_ids.add(user_id)

    user_email_collision_groups = _collision_groups(user_email_entries)
    allauth_email_collision_groups = _collision_groups(address_entries)
    combined_email_collision_groups = _collision_groups(
        user_email_entries + address_entries
    )
    username_collision_groups = _collision_groups(username_entries)

    missing_matching_address_user_ids = []
    primary_email_mismatch_user_ids = []
    multiple_primary_address_user_ids = []
    unverified_canonical_email_user_ids = []
    for user in users:
        user_id = user['pk']
        canonical_email = canonical_email_by_user[user_id]
        user_addresses = addresses_by_user[user_id]
        matching = [
            address
            for normalized_email, address in user_addresses
            if canonical_email and normalized_email == canonical_email
        ]
        if canonical_email and not matching:
            missing_matching_address_user_ids.append(user_id)
        if canonical_email and matching and not any(
            address['verified'] for address in matching
        ):
            unverified_canonical_email_user_ids.append(user_id)

        primary_addresses = [
            (normalized_email, address)
            for normalized_email, address in user_addresses
            if address['primary']
        ]
        if len(primary_addresses) > 1:
            multiple_primary_address_user_ids.append(user_id)
        if primary_addresses and any(
            normalized_email != canonical_email
            for normalized_email, _address in primary_addresses
        ):
            primary_email_mismatch_user_ids.append(user_id)

    constraint_names = _constraint_names(user_model)
    normalized_email_constraint_present = (
        EMAIL_CONSTRAINT_NAME in constraint_names
    )
    normalized_username_constraint_present = (
        USERNAME_CONSTRAINT_NAME in constraint_names
    )

    blockers = {
        'missing_email_user_ids': sorted(missing_email_user_ids),
        'email_normalization_user_ids': sorted(email_normalization_user_ids),
        'combined_email_collision_user_ids': _user_ids(
            combined_email_collision_groups
        ),
        'allauth_email_normalization_user_ids': sorted(
            address_normalization_user_ids
        ),
        'missing_matching_allauth_address_user_ids': sorted(
            missing_matching_address_user_ids
        ),
        'primary_email_mismatch_user_ids': sorted(
            primary_email_mismatch_user_ids
        ),
        'multiple_primary_address_user_ids': sorted(
            multiple_primary_address_user_ids
        ),
        'missing_username_user_ids': sorted(missing_username_user_ids),
        'username_whitespace_user_ids': sorted(username_whitespace_user_ids),
        'username_collision_user_ids': _user_ids(username_collision_groups),
    }
    data_ready = not any(blockers.values())
    constraints_ready = (
        normalized_email_constraint_present
        and normalized_username_constraint_present
    )
    pk_field = user_model._meta.pk

    return {
        'user_model': user_model._meta.label,
        'user_primary_key_field': pk_field.name,
        'user_primary_key_type': pk_field.get_internal_type(),
        'user_primary_key_is_numeric': isinstance(pk_field, models.IntegerField),
        'total_users': len(users),
        'total_allauth_email_addresses': len(addresses),
        'missing_email_count': len(missing_email_user_ids),
        'email_normalization_required_count': len(
            email_normalization_user_ids
        ),
        'user_email_collision_group_count': len(
            user_email_collision_groups
        ),
        'allauth_email_collision_group_count': len(
            allauth_email_collision_groups
        ),
        'combined_email_collision_group_count': len(
            combined_email_collision_groups
        ),
        'allauth_email_normalization_required_count': len(
            address_normalization_user_ids
        ),
        'missing_matching_allauth_address_count': len(
            missing_matching_address_user_ids
        ),
        'unverified_canonical_email_count': len(
            unverified_canonical_email_user_ids
        ),
        'primary_email_mismatch_count': len(
            primary_email_mismatch_user_ids
        ),
        'multiple_primary_address_count': len(
            multiple_primary_address_user_ids
        ),
        'missing_username_count': len(missing_username_user_ids),
        'username_whitespace_count': len(username_whitespace_user_ids),
        'username_collision_group_count': len(username_collision_groups),
        'normalized_email_constraint_present': (
            normalized_email_constraint_present
        ),
        'normalized_username_constraint_present': (
            normalized_username_constraint_present
        ),
        'data_ready_for_enforcement': data_ready,
        'normalized_constraints_ready': constraints_ready,
        'email_login_cutover_ready': data_ready and constraints_ready,
        'blockers': blockers,
    }


class Command(BaseCommand):
    help = (
        'Read identity readiness without modifying users, email addresses, '
        'passwords, or ownership records.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', dest='as_json')
        parser.add_argument('--require-data-ready', action='store_true')
        parser.add_argument('--require-ready', action='store_true')

    def handle(self, *args, **options):
        report = build_auth_identity_readiness_report()
        if options['as_json']:
            self.stdout.write(json.dumps(report, sort_keys=True))
        else:
            for name, value in report.items():
                if name == 'blockers':
                    self.stdout.write('blockers:')
                    for blocker_name, user_ids in value.items():
                        rendered_ids = ','.join(str(user_id) for user_id in user_ids)
                        self.stdout.write(
                            f'  {blocker_name}: [{rendered_ids}]'
                        )
                else:
                    if isinstance(value, bool):
                        value = str(value).lower()
                    self.stdout.write(f'{name}={value}')

        if (
            options['require_data_ready']
            and not report['data_ready_for_enforcement']
        ):
            raise CommandError(
                'AUTH-1 identity data is not ready for normalized uniqueness.'
            )
        if options['require_ready'] and not report['email_login_cutover_ready']:
            raise CommandError('AUTH-1 email-login cutover is not ready.')
