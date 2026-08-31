import io
import json

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from SalesLogApp.auth_identity_constraints import (
    IDENTITY_INDEX_SPECS,
    _postgresql_signature,
)
from SalesLogApp.management.commands.auth_identity_readiness import (
    build_auth_identity_readiness_report,
)


class AuthIdentityReadinessTests(TestCase):
    def _drop_identity_indexes(self, *keys):
        selected = set(keys) if keys else {
            spec['key'] for spec in IDENTITY_INDEX_SPECS
        }
        with connection.cursor() as cursor:
            for spec in reversed(IDENTITY_INDEX_SPECS):
                if spec['key'] in selected:
                    cursor.execute(
                        f'DROP INDEX {connection.ops.quote_name(spec["name"])}'
                    )

    def _create_identity_index(self, key):
        spec = next(
            spec for spec in IDENTITY_INDEX_SPECS if spec['key'] == key
        )
        with connection.cursor() as cursor:
            cursor.execute(spec['sqlite_sql'])

    def _user(self, username, email):
        user = get_user_model().objects.create_user(
            username=username,
            email=email,
            password='Readiness-test-password-482!',
        )
        if email:
            EmailAddress.objects.create(
                user=user,
                email=email,
                primary=True,
                verified=True,
            )
        return user

    def test_clean_identity_data_is_ready_for_enforcement(self):
        user = self._user('ReadyUser', 'ready@example.com')

        report = build_auth_identity_readiness_report()

        self.assertEqual(report['user_model'], 'auth.User')
        self.assertEqual(report['user_primary_key_field'], 'id')
        self.assertTrue(report['user_primary_key_is_numeric'])
        self.assertEqual(report['total_users'], 1)
        self.assertEqual(report['total_allauth_email_addresses'], 1)
        self.assertTrue(report['data_ready_for_enforcement'])
        self.assertTrue(report['normalized_constraints_ready'])
        self.assertTrue(report['email_login_cutover_ready'])
        self.assertTrue(all(not ids for ids in report['blockers'].values()))
        self.assertEqual(user.pk, get_user_model().objects.get().pk)

    def test_postgresql_catalog_fragments_have_stable_signatures(self):
        self.assertEqual(
            _postgresql_signature('lower(TRIM(BOTH FROM "email"))'),
            'lowertrimemail',
        )
        self.assertEqual(
            _postgresql_signature(
                "(btrim((email)::text) <> ''::text)"
            ),
            "btrimemail<>''",
        )

    def test_reports_email_and_username_blockers_without_pii(self):
        self._drop_identity_indexes()
        first = self._user('CaseName', 'Shared@Example.com')
        get_user_model().objects.filter(pk=first.pk).update(
            email='Shared@Example.com'
        )
        second = get_user_model().objects.create_user(
            username='casename',
            email=' shared@example.com ',
            password='Readiness-test-password-482!',
        )
        get_user_model().objects.filter(pk=second.pk).update(
            email=' shared@example.com '
        )
        missing = get_user_model().objects.create_user(
            username='missing-email',
            email='',
            password='Readiness-test-password-482!',
        )
        EmailAddress.objects.create(
            user=second,
            email='shared@example.com',
            primary=True,
            verified=False,
        )

        output = io.StringIO()
        call_command('auth_identity_readiness', '--json', stdout=output)
        report = json.loads(output.getvalue())

        self.assertEqual(report['missing_email_count'], 1)
        self.assertEqual(report['email_normalization_required_count'], 2)
        self.assertEqual(report['user_email_collision_group_count'], 1)
        self.assertEqual(report['combined_email_collision_group_count'], 1)
        self.assertEqual(report['username_collision_group_count'], 1)
        self.assertFalse(report['data_ready_for_enforcement'])
        self.assertEqual(
            report['blockers']['combined_email_collision_user_ids'],
            sorted([first.pk, second.pk]),
        )
        self.assertEqual(
            report['blockers']['username_collision_user_ids'],
            sorted([first.pk, second.pk]),
        )
        self.assertEqual(
            report['blockers']['missing_email_user_ids'],
            [missing.pk],
        )
        rendered = output.getvalue()
        self.assertNotIn('shared@example.com', rendered.casefold())
        self.assertNotIn('casename', rendered.casefold())

    def test_reports_same_user_allauth_row_collision(self):
        self._drop_identity_indexes()
        user = get_user_model().objects.create_user(
            username='same-owner',
            email='same@example.com',
            password='Readiness-test-password-482!',
        )
        EmailAddress.objects.create(
            user=user,
            email='same@example.com',
            primary=True,
            verified=False,
        )
        EmailAddress.objects.create(
            user=user,
            email='SAME@example.com',
            primary=False,
            verified=False,
        )

        report = build_auth_identity_readiness_report()

        self.assertEqual(report['allauth_email_collision_group_count'], 1)
        self.assertEqual(
            report['blockers']['allauth_email_collision_user_ids'],
            [user.pk],
        )
        self.assertFalse(report['data_ready_for_enforcement'])

    def test_reports_allauth_sync_and_primary_mismatch(self):
        user = get_user_model().objects.create_user(
            username='email-mismatch',
            email='canonical@example.com',
            password='Readiness-test-password-482!',
        )
        EmailAddress.objects.create(
            user=user,
            email='different@example.com',
            primary=True,
            verified=True,
        )

        report = build_auth_identity_readiness_report()

        self.assertEqual(report['missing_matching_allauth_address_count'], 1)
        self.assertEqual(report['primary_email_mismatch_count'], 1)
        self.assertEqual(
            report['blockers']['missing_matching_allauth_address_user_ids'],
            [user.pk],
        )
        self.assertEqual(
            report['blockers']['primary_email_mismatch_user_ids'],
            [user.pk],
        )

    def test_require_data_ready_fails_closed(self):
        get_user_model().objects.create_user(
            username='blocked-user',
            email='',
            password='Readiness-test-password-482!',
        )

        with self.assertRaisesMessage(
            CommandError,
            'AUTH-1 identity data is not ready for normalized uniqueness.',
        ):
            call_command(
                'auth_identity_readiness',
                '--require-data-ready',
                stdout=io.StringIO(),
            )

    def test_all_constraint_diagnostics_and_require_flags(self):
        self._user('constraint-ready', 'constraint-ready@example.com')

        report = build_auth_identity_readiness_report()

        self.assertTrue(report['normalized_email_constraint_present'])
        self.assertTrue(report['normalized_user_email_constraint_present'])
        self.assertTrue(report['normalized_username_constraint_present'])
        self.assertTrue(
            report['normalized_allauth_email_constraint_present']
        )
        self.assertTrue(report['normalized_constraints_ready'])
        for diagnostic in report['normalized_constraint_diagnostics'].values():
            self.assertTrue(diagnostic['present'])
            self.assertTrue(diagnostic['unique'])
            self.assertTrue(diagnostic['expression_index'])
            self.assertTrue(diagnostic['definition_matches'])
            self.assertTrue(diagnostic['database_valid'])
            self.assertTrue(diagnostic['enforced'])

        call_command(
            'auth_identity_readiness',
            '--require-data-ready',
            stdout=io.StringIO(),
        )
        call_command(
            'auth_identity_readiness',
            '--require-ready',
            stdout=io.StringIO(),
        )

        self._drop_identity_indexes('allauth_email')
        report = build_auth_identity_readiness_report()
        self.assertTrue(report['data_ready_for_enforcement'])
        self.assertFalse(
            report['normalized_allauth_email_constraint_present']
        )
        self.assertFalse(report['normalized_constraints_ready'])
        call_command(
            'auth_identity_readiness',
            '--require-data-ready',
            stdout=io.StringIO(),
        )
        with self.assertRaisesMessage(
            CommandError,
            'AUTH-1 email-login cutover is not ready.',
        ):
            call_command(
                'auth_identity_readiness',
                '--require-ready',
                stdout=io.StringIO(),
            )

    def test_each_missing_constraint_is_reported_individually(self):
        field_by_key = {
            'user_email': 'normalized_user_email_constraint_present',
            'username': 'normalized_username_constraint_present',
            'allauth_email': (
                'normalized_allauth_email_constraint_present'
            ),
        }
        for spec in IDENTITY_INDEX_SPECS:
            with self.subTest(key=spec['key']):
                self._drop_identity_indexes(spec['key'])
                report = build_auth_identity_readiness_report()
                self.assertFalse(report[field_by_key[spec['key']]])
                self.assertFalse(report['normalized_constraints_ready'])
                self.assertFalse(
                    report['normalized_constraint_diagnostics'][
                        spec['key']
                    ]['enforced']
                )
                self._create_identity_index(spec['key'])

    def test_expected_name_with_wrong_definition_is_not_ready(self):
        self._drop_identity_indexes('user_email')
        spec = next(
            spec
            for spec in IDENTITY_INDEX_SPECS
            if spec['key'] == 'user_email'
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f'CREATE UNIQUE INDEX '
                f'{connection.ops.quote_name(spec["name"])} '
                f'ON {connection.ops.quote_name(spec["table"])} '
                f'({connection.ops.quote_name("email")})'
            )

        report = build_auth_identity_readiness_report()
        diagnostic = report['normalized_constraint_diagnostics']['user_email']

        self.assertTrue(diagnostic['present'])
        self.assertTrue(diagnostic['unique'])
        self.assertFalse(diagnostic['expression_index'])
        self.assertFalse(diagnostic['definition_matches'])
        self.assertFalse(diagnostic['enforced'])
        self.assertFalse(report['normalized_email_constraint_present'])
        self.assertFalse(report['normalized_constraints_ready'])

    def test_command_does_not_modify_identity_rows(self):
        user = self._user('UnchangedUser', 'unchanged@example.com')
        before_user = list(
            get_user_model().objects.values_list('pk', 'username', 'email')
        )
        before_addresses = list(
            EmailAddress.objects.values_list(
                'pk', 'user_id', 'email', 'verified', 'primary'
            )
        )

        call_command('auth_identity_readiness', stdout=io.StringIO())

        self.assertEqual(
            before_user,
            list(get_user_model().objects.values_list('pk', 'username', 'email')),
        )
        self.assertEqual(
            before_addresses,
            list(
                EmailAddress.objects.values_list(
                    'pk', 'user_id', 'email', 'verified', 'primary'
                )
            ),
        )
        self.assertEqual(user.pk, get_user_model().objects.get().pk)
