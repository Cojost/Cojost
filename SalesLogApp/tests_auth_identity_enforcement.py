from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase

from SalesLogApp.auth_identity_constraints import (
    ALLAUTH_EMAIL_CONSTRAINT_NAME,
    USER_EMAIL_CONSTRAINT_NAME,
    USERNAME_CONSTRAINT_NAME,
    inspect_normalized_identity_constraints,
)


MIGRATION_MODULE = import_module(
    'SalesLogApp.migrations.0066_auth1b_normalized_identity_constraints'
)


class AuthIdentityMigrationLockTests(SimpleTestCase):
    def _schema_editor(self, vendor):
        return SimpleNamespace(
            connection=SimpleNamespace(vendor=vendor),
            quote_name=lambda value: f'"{value}"',
            execute=Mock(),
        )

    def test_postgresql_preflight_locks_both_identity_tables(self):
        schema_editor = self._schema_editor('postgresql')

        MIGRATION_MODULE._lock_identity_writes(schema_editor)

        schema_editor.execute.assert_called_once_with(
            'LOCK TABLE "auth_user", "account_emailaddress" IN SHARE MODE'
        )

    def test_sqlite_relies_on_the_atomic_read_ddl_transaction(self):
        schema_editor = self._schema_editor('sqlite')

        MIGRATION_MODULE._lock_identity_writes(schema_editor)

        schema_editor.execute.assert_not_called()


class AuthIdentityEnforcementMigrationTests(TransactionTestCase):
    migrate_from = ('SalesLogApp', '0065_team_activity_for_active_members')
    migrate_to = (
        'SalesLogApp',
        '0066_auth1b_normalized_identity_constraints',
    )

    def setUp(self):
        super().setUp()
        self.old_apps = self._migrate(self.migrate_from)

    def tearDown(self):
        # Leave the shared disposable test database at the current leaf even
        # when a test intentionally leaves 0066 unapplied.
        EmailAddress.objects.all().delete()
        get_user_model().objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([
            target,
            ('account', '0009_emailaddress_unique_primary_email'),
            ('auth', '0012_alter_user_first_name_max_length'),
        ]).apps

    def _historical_models(self):
        return (
            self.old_apps.get_model('auth', 'User'),
            self.old_apps.get_model('account', 'EmailAddress'),
        )

    def _constraint_names(self, table):
        with connection.cursor() as cursor:
            return set(
                connection.introspection.get_constraints(cursor, table)
            )

    def test_preflight_blocks_dirty_data_without_modifying_rows(self):
        User, HistoricalEmailAddress = self._historical_models()
        first = User.objects.create(
            username='CaseName',
            email='clean@example.com',
            password='!',
        )
        second = User.objects.create(
            username=' casename ',
            email=' CLEAN@example.com ',
            password='!',
        )
        HistoricalEmailAddress.objects.create(
            user_id=first.pk,
            email='clean@example.com',
            primary=True,
            verified=False,
        )
        HistoricalEmailAddress.objects.create(
            user_id=second.pk,
            email='CLEAN@example.com',
            primary=True,
            verified=False,
        )
        before_users = list(
            User.objects.order_by('pk').values_list('pk', 'username', 'email')
        )
        before_addresses = list(
            HistoricalEmailAddress.objects.order_by('pk').values_list(
                'pk', 'user_id', 'email', 'verified', 'primary'
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            'AUTH-1B identity preflight failed without changing data',
        ) as raised:
            self._migrate(self.migrate_to)

        rendered = str(raised.exception)
        self.assertIn('user_email_normalization_rows=', rendered)
        self.assertIn('allauth_email_normalization_rows=', rendered)
        self.assertIn('username_whitespace_rows=', rendered)
        self.assertIn('user_email_collision_groups=', rendered)
        self.assertIn('username_collision_groups=', rendered)
        self.assertIn('allauth_email_collision_groups=', rendered)
        self.assertIn('cross_owner_email_collision_groups=', rendered)
        self.assertEqual(
            before_users,
            list(
                User.objects.order_by('pk').values_list(
                    'pk', 'username', 'email'
                )
            ),
        )
        self.assertEqual(
            before_addresses,
            list(
                HistoricalEmailAddress.objects.order_by('pk').values_list(
                    'pk', 'user_id', 'email', 'verified', 'primary'
                )
            ),
        )
        self.assertTrue(
            all(
                not diagnostic['present']
                for diagnostic in inspect_normalized_identity_constraints(
                    connection
                ).values()
            )
        )

    def test_preflight_blocks_same_user_allauth_collision(self):
        User, HistoricalEmailAddress = self._historical_models()
        user = User.objects.create(
            username='same-address-owner',
            email='same-address@example.com',
            password='!',
        )
        first = HistoricalEmailAddress.objects.create(
            user_id=user.pk,
            email='same-address@example.com',
            primary=True,
            verified=False,
        )
        second = HistoricalEmailAddress.objects.create(
            user_id=user.pk,
            email='SAME-ADDRESS@example.com',
            primary=False,
            verified=False,
        )
        before = list(
            HistoricalEmailAddress.objects.order_by('pk').values_list(
                'pk', 'user_id', 'email', 'verified', 'primary'
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            'allauth_email_collision_groups=1',
        ):
            self._migrate(self.migrate_to)

        self.assertEqual(
            before,
            list(
                HistoricalEmailAddress.objects.order_by('pk').values_list(
                    'pk', 'user_id', 'email', 'verified', 'primary'
                )
            ),
        )
        self.assertEqual({first.pk, second.pk}, {row[0] for row in before})

    def test_clean_migration_enforces_all_identities_and_allows_blank_user_email(self):
        User, HistoricalEmailAddress = self._historical_models()
        HistoricalUserProfile = self.old_apps.get_model(
            'SalesLogApp',
            'UserProfile',
        )
        blank_one = User.objects.create(
            username='blank-one', email='', password='!'
        )
        blank_two = User.objects.create(
            username='blank-two', email='', password='!'
        )
        owner = User.objects.create(
            username='PreservedOwner',
            email='preserved@example.com',
            password='!',
        )
        address = HistoricalEmailAddress.objects.create(
            user_id=owner.pk,
            email='preserved@example.com',
            primary=True,
            verified=True,
        )
        profile = HistoricalUserProfile.objects.create(user_id=owner.pk)
        original_user_rows = list(
            User.objects.order_by('pk').values_list('pk', 'username', 'email')
        )
        original_address = (
            address.pk,
            address.user_id,
            address.email,
            address.verified,
            address.primary,
        )
        original_profile = (profile.pk, profile.user_id)

        self._migrate(self.migrate_to)

        diagnostics = inspect_normalized_identity_constraints(connection)
        self.assertTrue(
            all(diagnostic['enforced'] for diagnostic in diagnostics.values())
        )
        self.assertEqual(
            original_user_rows,
            list(
                User.objects.order_by('pk').values_list(
                    'pk', 'username', 'email'
                )
            ),
        )
        persisted_profile = HistoricalUserProfile.objects.get(pk=profile.pk)
        self.assertEqual(
            original_profile,
            (persisted_profile.pk, persisted_profile.user_id),
        )
        persisted_address = HistoricalEmailAddress.objects.get(pk=address.pk)
        self.assertEqual(
            original_address,
            (
                persisted_address.pk,
                persisted_address.user_id,
                persisted_address.email,
                persisted_address.verified,
                persisted_address.primary,
            ),
        )
        self.assertEqual(
            {blank_one.pk, blank_two.pk, owner.pk},
            set(User.objects.values_list('pk', flat=True)),
        )

        third_blank = User.objects.create(
            username='blank-three', email='', password='!'
        )
        self.assertEqual('', third_blank.email)

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(
                username='email-case-collision',
                email='PRESERVED@example.com',
                password='!',
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(
                username='email-space-collision',
                email=' preserved@example.com ',
                password='!',
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(
                username='preservedowner',
                email='different-case@example.com',
                password='!',
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(
                username=' PreservedOwner ',
                email='different-space@example.com',
                password='!',
            )

        address_owner = User.objects.create(
            username='unverified-address-owner',
            email='unrelated@example.com',
            password='!',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            HistoricalEmailAddress.objects.create(
                user_id=address_owner.pk,
                email='PRESERVED@example.com',
                primary=False,
                verified=False,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            HistoricalEmailAddress.objects.create(
                user_id=address_owner.pk,
                email=' preserved@example.com ',
                primary=False,
                verified=False,
            )

    def test_reverse_removes_only_auth1b_indexes(self):
        User, HistoricalEmailAddress = self._historical_models()
        user = User.objects.create(
            username='reverse-owner',
            email='reverse@example.com',
            password='!',
        )
        HistoricalEmailAddress.objects.create(
            user_id=user.pk,
            email='reverse@example.com',
            primary=True,
            verified=True,
        )
        self._migrate(self.migrate_to)
        auth_before = self._constraint_names('auth_user')
        allauth_before = self._constraint_names('account_emailaddress')

        self._migrate(self.migrate_from)

        auth_after = self._constraint_names('auth_user')
        allauth_after = self._constraint_names('account_emailaddress')
        self.assertEqual(
            auth_before - {
                USER_EMAIL_CONSTRAINT_NAME,
                USERNAME_CONSTRAINT_NAME,
            },
            auth_after,
        )
        self.assertEqual(
            allauth_before - {ALLAUTH_EMAIL_CONSTRAINT_NAME},
            allauth_after,
        )
        self.assertIn('unique_verified_email', allauth_after)
        self.assertIn('unique_primary_email', allauth_after)
        self.assertTrue(
            all(
                not diagnostic['present']
                for diagnostic in inspect_normalized_identity_constraints(
                    connection
                ).values()
            )
        )
