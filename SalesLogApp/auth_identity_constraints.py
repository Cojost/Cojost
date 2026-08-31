"""Database metadata for AUTH-1B normalized identity indexes."""

import re


USER_EMAIL_CONSTRAINT_NAME = 'auth_user_email_ci_unique'
USERNAME_CONSTRAINT_NAME = 'auth_user_username_ci_unique'
ALLAUTH_EMAIL_CONSTRAINT_NAME = 'account_emailaddress_email_ci_unique'


IDENTITY_INDEX_SPECS = (
    {
        'key': 'user_email',
        'table': 'auth_user',
        'name': USER_EMAIL_CONSTRAINT_NAME,
        'sqlite_sql': (
            'CREATE UNIQUE INDEX "auth_user_email_ci_unique" '
            'ON "auth_user" (LOWER(TRIM("email"))) '
            'WHERE TRIM("email") <> \'\''
        ),
        'postgresql_expression_signatures': {
            'lowertrimemail',
            'lowerbtrimemail',
        },
        'postgresql_predicate_signatures': {
            "trimemail<>''",
            "btrimemail<>''",
        },
    },
    {
        'key': 'username',
        'table': 'auth_user',
        'name': USERNAME_CONSTRAINT_NAME,
        'sqlite_sql': (
            'CREATE UNIQUE INDEX "auth_user_username_ci_unique" '
            'ON "auth_user" (LOWER(TRIM("username")))'
        ),
        'postgresql_expression_signatures': {
            'lowertrimusername',
            'lowerbtrimusername',
        },
        'postgresql_predicate_signatures': {None},
    },
    {
        'key': 'allauth_email',
        'table': 'account_emailaddress',
        'name': ALLAUTH_EMAIL_CONSTRAINT_NAME,
        'sqlite_sql': (
            'CREATE UNIQUE INDEX "account_emailaddress_email_ci_unique" '
            'ON "account_emailaddress" (LOWER(TRIM("email")))'
        ),
        'postgresql_expression_signatures': {
            'lowertrimemail',
            'lowerbtrimemail',
        },
        'postgresql_predicate_signatures': {None},
    },
)


def _compact_sql(value):
    if value is None:
        return None
    return re.sub(r'[\s"`\[\]]+', '', value).casefold().rstrip(';')


def _postgresql_signature(value):
    if value is None:
        return None
    signature = value.casefold()
    signature = re.sub(
        r'::(?:pg_catalog\.)?(?:text|character varying|varchar)',
        '',
        signature,
    )
    signature = signature.replace('pg_catalog.', '')
    signature = re.sub(r'both\s+from', '', signature)
    signature = re.sub(r'[\s"()]', '', signature)
    return signature


def _sqlite_catalog_entry(connection, spec):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index' AND tbl_name = %s AND name = %s
            """,
            [spec['table'], spec['name']],
        )
        row = cursor.fetchone()
    if not row:
        return None
    return {
        'definition': row[0],
        'definition_matches': (
            _compact_sql(row[0]) == _compact_sql(spec['sqlite_sql'])
        ),
        'catalog_unique': None,
        'valid': True,
        'ready': True,
        'access_method': 'btree',
        'key_column_count': 1,
    }


def _postgresql_catalog_entry(connection, spec):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                index_meta.indisunique,
                index_meta.indisvalid,
                index_meta.indisready,
                index_meta.indnkeyatts,
                access_method.amname,
                pg_catalog.pg_get_indexdef(index_meta.indexrelid),
                pg_catalog.pg_get_indexdef(
                    index_meta.indexrelid, 1, false
                ),
                pg_catalog.pg_get_expr(
                    index_meta.indpred, index_meta.indrelid, false
                )
            FROM pg_catalog.pg_class AS table_class
            JOIN pg_catalog.pg_index AS index_meta
              ON index_meta.indrelid = table_class.oid
            JOIN pg_catalog.pg_class AS index_class
              ON index_class.oid = index_meta.indexrelid
            JOIN pg_catalog.pg_am AS access_method
              ON access_method.oid = index_class.relam
            WHERE table_class.relname = %s
              AND index_class.relname = %s
              AND pg_catalog.pg_table_is_visible(table_class.oid)
            """,
            [spec['table'], spec['name']],
        )
        row = cursor.fetchone()
    if not row:
        return None

    (
        unique,
        valid,
        ready,
        key_column_count,
        access_method,
        definition,
        expression,
        predicate,
    ) = row
    expression_matches = (
        _postgresql_signature(expression)
        in spec['postgresql_expression_signatures']
    )
    predicate_matches = (
        _postgresql_signature(predicate)
        in spec['postgresql_predicate_signatures']
    )
    return {
        'definition': definition,
        'definition_matches': expression_matches and predicate_matches,
        'catalog_unique': bool(unique),
        'valid': bool(valid),
        'ready': bool(ready),
        'access_method': access_method,
        'key_column_count': key_column_count,
    }


def inspect_normalized_identity_constraints(connection):
    """Return read-only, JSON-serializable diagnostics for every AUTH-1B index."""
    diagnostics = {}
    introspected_by_table = {}

    for spec in IDENTITY_INDEX_SPECS:
        table_constraints = introspected_by_table.get(spec['table'])
        if table_constraints is None:
            with connection.cursor() as cursor:
                table_constraints = connection.introspection.get_constraints(
                    cursor,
                    spec['table'],
                )
            introspected_by_table[spec['table']] = table_constraints

        metadata = table_constraints.get(spec['name'])
        if connection.vendor == 'sqlite':
            catalog = _sqlite_catalog_entry(connection, spec)
        elif connection.vendor == 'postgresql':
            catalog = _postgresql_catalog_entry(connection, spec)
        else:
            catalog = None

        present = metadata is not None and catalog is not None
        unique = bool(metadata and metadata.get('unique'))
        expression_index = bool(
            metadata
            and metadata.get('index')
            and metadata.get('columns') in ([None], [])
        )
        definition_matches = bool(
            catalog and catalog['definition_matches']
        )
        catalog_valid = bool(
            catalog
            and catalog['valid']
            and catalog['ready']
            and catalog['access_method'] == 'btree'
            and catalog['key_column_count'] == 1
            and catalog['catalog_unique'] in (None, True)
        )
        enforced = bool(
            present
            and unique
            and expression_index
            and definition_matches
            and catalog_valid
        )
        diagnostics[spec['key']] = {
            'table': spec['table'],
            'name': spec['name'],
            'present': present,
            'unique': unique,
            'expression_index': expression_index,
            'definition_matches': definition_matches,
            'database_valid': catalog_valid,
            'enforced': enforced,
        }

    return diagnostics
