import io
import json

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from SalesLogApp.management.commands.auth_identity_readiness import (
    build_auth_identity_readiness_report,
)


class AuthIdentityReadinessTests(TestCase):
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
        self.assertFalse(report['email_login_cutover_ready'])
        self.assertTrue(all(not ids for ids in report['blockers'].values()))
        self.assertEqual(user.pk, get_user_model().objects.get().pk)

    def test_reports_email_and_username_blockers_without_pii(self):
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
