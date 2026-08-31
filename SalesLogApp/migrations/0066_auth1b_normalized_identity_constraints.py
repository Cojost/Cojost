from collections import Counter, defaultdict

from django.conf import settings
from django.db import migrations


USER_EMAIL_INDEX_NAME = 'auth_user_email_ci_unique'
USERNAME_INDEX_NAME = 'auth_user_username_ci_unique'
ALLAUTH_EMAIL_INDEX_NAME = 'account_emailaddress_email_ci_unique'


def _validate_supported_database(schema_editor):
    if schema_editor.connection.vendor not in {'postgresql', 'sqlite'}:
        raise RuntimeError(
            'AUTH-1B normalized identity indexes support PostgreSQL and '
            'SQLite only.'
        )


def _lock_identity_writes(schema_editor):
    """Keep PostgreSQL identity rows stable through the atomic index build."""
    if schema_editor.connection.vendor != 'postgresql':
        return
    quote = schema_editor.quote_name
    schema_editor.execute(
        f'LOCK TABLE {quote("auth_user")}, '
        f'{quote("account_emailaddress")} IN SHARE MODE'
    )


def _normalized(value):
    # Match allauth's stored-email policy and the SQL LOWER indexes while
    # leaving user-facing username casing intact.
    return (value or '').strip().lower()


def _duplicate_group_count(values):
    return sum(count > 1 for count in Counter(values).values())


def validate_normalized_identity_data(apps, schema_editor):
    """Fail closed without repairing or otherwise changing identity rows."""
    _validate_supported_database(schema_editor)
    # The migration is atomic. PostgreSQL holds these locks until all three
    # indexes exist; SQLite's read/DDL transaction either keeps its snapshot
    # stable or fails the write-lock upgrade without committing partial DDL.
    _lock_identity_writes(schema_editor)
    User = apps.get_model('auth', 'User')
    EmailAddress = apps.get_model('account', 'EmailAddress')
    if settings.AUTH_USER_MODEL != 'auth.User':
        raise RuntimeError(
            'AUTH-1B requires the unchanged auth.User model.'
        )
    if (
        User._meta.db_table != 'auth_user'
        or EmailAddress._meta.db_table != 'account_emailaddress'
    ):
        raise RuntimeError(
            'AUTH-1B requires the expected auth and allauth tables.'
        )

    issues = Counter()
    normalized_user_emails = []
    normalized_usernames = []
    normalized_address_emails = []
    email_owner_ids = defaultdict(set)

    for user_id, raw_email, raw_username in User.objects.order_by('pk').values_list(
        'pk', 'email', 'username'
    ).iterator():
        raw_email = raw_email or ''
        normalized_email = _normalized(raw_email)
        if raw_email:
            if raw_email != normalized_email:
                issues['user_email_normalization_rows'] += 1
            if normalized_email:
                normalized_user_emails.append(normalized_email)
                email_owner_ids[normalized_email].add(user_id)

        raw_username = raw_username or ''
        normalized_username = _normalized(raw_username)
        if not normalized_username:
            issues['blank_username_rows'] += 1
        else:
            normalized_usernames.append(normalized_username)
        if raw_username != raw_username.strip():
            issues['username_whitespace_rows'] += 1

    for _address_id, user_id, raw_email in EmailAddress.objects.order_by(
        'pk'
    ).values_list('pk', 'user_id', 'email').iterator():
        raw_email = raw_email or ''
        normalized_email = _normalized(raw_email)
        if not normalized_email:
            issues['blank_allauth_email_rows'] += 1
            continue
        if raw_email != normalized_email:
            issues['allauth_email_normalization_rows'] += 1
        normalized_address_emails.append(normalized_email)
        email_owner_ids[normalized_email].add(user_id)

    issues['user_email_collision_groups'] = _duplicate_group_count(
        normalized_user_emails
    )
    issues['username_collision_groups'] = _duplicate_group_count(
        normalized_usernames
    )
    issues['allauth_email_collision_groups'] = _duplicate_group_count(
        normalized_address_emails
    )
    issues['cross_owner_email_collision_groups'] = sum(
        len(owner_ids) > 1 for owner_ids in email_owner_ids.values()
    )
    issues = Counter({name: count for name, count in issues.items() if count})

    if issues:
        rendered = '; '.join(
            f'{name}={issues[name]}' for name in sorted(issues)
        )
        raise RuntimeError(
            'AUTH-1B identity preflight failed without changing data: '
            f'{rendered}.'
        )


def create_normalized_identity_indexes(apps, schema_editor):
    _validate_supported_database(schema_editor)

    quote = schema_editor.quote_name
    statements = (
        (
            USER_EMAIL_INDEX_NAME,
            'auth_user',
            'email',
            True,
        ),
        (
            USERNAME_INDEX_NAME,
            'auth_user',
            'username',
            False,
        ),
        (
            ALLAUTH_EMAIL_INDEX_NAME,
            'account_emailaddress',
            'email',
            False,
        ),
    )
    for name, table, column, exclude_normalized_blank in statements:
        sql = (
            f'CREATE UNIQUE INDEX {quote(name)} ON {quote(table)} '
            f'(LOWER(TRIM({quote(column)})))'
        )
        if exclude_normalized_blank:
            sql += f" WHERE TRIM({quote(column)}) <> ''"
        schema_editor.execute(sql)


def drop_normalized_identity_indexes(apps, schema_editor):
    _validate_supported_database(schema_editor)
    quote = schema_editor.quote_name
    for name in (
        ALLAUTH_EMAIL_INDEX_NAME,
        USERNAME_INDEX_NAME,
        USER_EMAIL_INDEX_NAME,
    ):
        schema_editor.execute(f'DROP INDEX IF EXISTS {quote(name)}')


class Migration(migrations.Migration):
    atomic = True

    dependencies = [
        ('SalesLogApp', '0065_team_activity_for_active_members'),
        ('account', '0009_emailaddress_unique_primary_email'),
        ('auth', '0012_alter_user_first_name_max_length'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            validate_normalized_identity_data,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            create_normalized_identity_indexes,
            drop_normalized_identity_indexes,
        ),
    ]
