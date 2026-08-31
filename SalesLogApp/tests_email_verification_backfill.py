from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from io import StringIO
from threading import Event, Lock
from unittest import skipUnless
from unittest.mock import patch

from allauth.account.models import EmailAddress, EmailConfirmation
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .checks import email_verification_delivery_check
from .email_verification import (
    _finalize_dispatch,
    _recipient_digest,
    _reserve_dispatch,
    build_verification_request,
    dispatch_verification_email,
)
from .models import (
    EmailVerificationDispatch,
    PayPlan,
    Sale,
    TeamInvitation,
)


class UnsafeImportableEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        return len(email_messages)


@override_settings(
    DEBUG=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    EMAIL_VERIFICATION_PUBLIC_BASE_URL='http://testserver',
    EMAIL_VERIFICATION_RESEND_COOLDOWN_MINUTES=60,
    ALLOWED_HOSTS=['testserver'],
)
class EmailVerificationBackfillCommandTests(TestCase):
    def setUp(self):
        cache.clear()

    def create_user(self, marker, *, email=None, is_active=True):
        return get_user_model().objects.create_user(
            username=f'verification-{marker}',
            email=email if email is not None else f'{marker}@example.com',
            password='pass',
            is_active=is_active,
        )

    def run_command(self, *arguments):
        stdout = StringIO()
        stderr = StringIO()
        call_command(
            'audit_and_resend_verification_emails',
            *arguments,
            stdout=stdout,
            stderr=stderr,
        )
        return stdout.getvalue(), stderr.getvalue()

    def production_settings(self, **overrides):
        configured = {
            'DEBUG': False,
            'EMAIL_BACKEND': 'django.core.mail.backends.smtp.EmailBackend',
            'EMAIL_HOST': 'smtp.resend.com',
            'EMAIL_PORT': 587,
            'EMAIL_USE_TLS': True,
            'EMAIL_USE_SSL': False,
            'EMAIL_TIMEOUT': 10,
            'DEFAULT_FROM_EMAIL': 'STEW Log <no-reply@mail.stewlog.com>',
            'EMAIL_VERIFICATION_PUBLIC_BASE_URL': 'https://stewlog.com',
            'EMAIL_VERIFICATION_PENDING_STALE_MINUTES': 15,
            'ALLOWED_HOSTS': ['stewlog.com'],
        }
        configured.update(overrides)
        return configured

    def database_state(self):
        return {
            'users': tuple(
                get_user_model().objects.order_by('pk').values_list(
                    'pk', 'email', 'is_active',
                )
            ),
            'addresses': tuple(
                EmailAddress.objects.order_by('pk').values_list(
                    'pk', 'user_id', 'email', 'verified', 'primary',
                )
            ),
            'confirmations': tuple(
                EmailConfirmation.objects.order_by('pk').values_list(
                    'pk', 'email_address_id', 'key',
                )
            ),
            'dispatches': tuple(
                EmailVerificationDispatch.objects.order_by('pk').values_list(
                    'pk', 'user_id', 'recipient_digest', 'status',
                )
            ),
            'invitations': tuple(
                TeamInvitation.objects.order_by('pk').values_list(
                    'pk', 'accepted_at', 'revoked_at',
                )
            ),
            'sales': tuple(Sale.objects.order_by('pk').values_list('pk')),
            'plans': tuple(PayPlan.objects.order_by('pk').values_list('pk')),
        }

    def assert_production_preflight_rejected(self, user, **overrides):
        before = self.database_state()
        outbox_size = len(mail.outbox)
        with override_settings(**self.production_settings(**overrides)):
            with self.assertRaises(CommandError) as captured:
                self.run_command(
                    '--send',
                    '--confirm-production-send',
                    '--user-id',
                    str(user.pk),
                )
        self.assertEqual(
            str(captured.exception),
            'Production email delivery preflight failed.',
        )
        self.assertEqual(self.database_state(), before)
        self.assertEqual(len(mail.outbox), outbox_size)

    def test_dry_run_reports_but_does_not_repair_or_send(self):
        user = self.create_user('dry-run')

        output, errors = self.run_command('--user-id', str(user.pk))

        self.assertIn('Email verification audit mode: DRY RUN', output)
        self.assertIn('Missing EmailAddress row: 1', output)
        self.assertIn('Eligible recipients: 1', output)
        self.assertIn('Sent: 0', output)
        self.assertEqual(errors, '')
        self.assertFalse(EmailAddress.objects.filter(user=user).exists())
        self.assertFalse(
            EmailVerificationDispatch.objects.filter(user=user).exists()
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_verified_user_is_skipped(self):
        user = self.create_user('verified')
        EmailAddress.objects.create(
            user=user,
            email=user.email,
            primary=True,
            verified=True,
        )

        output, _ = self.run_command('--send', '--user-id', str(user.pk))

        self.assertIn('Already verified: 1', output)
        self.assertIn('Eligible recipients: 0', output)
        self.assertIn('Skipped: 1', output)
        self.assertEqual(len(mail.outbox), 0)

    def test_unverified_address_receives_one_real_confirmation_email(self):
        user = self.create_user('existing-row')
        EmailAddress.objects.create(
            user=user,
            email=user.email,
            primary=True,
            verified=False,
        )

        output, _ = self.run_command('--send', '--user-id', str(user.pk))

        self.assertIn('Unverified with valid EmailAddress row: 1', output)
        self.assertIn('Sent: 1', output)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        self.assertIn('/accounts/confirm-email/', mail.outbox[0].body)
        self.assertFalse(EmailAddress.objects.get(user=user).verified)

    def test_send_repairs_missing_address_without_marking_it_verified(self):
        user = self.create_user('missing-row')

        output, _ = self.run_command('--send', '--user-id', str(user.pk))

        address = EmailAddress.objects.get(user=user)
        self.assertIn('Missing EmailAddress row: 1', output)
        self.assertTrue(address.primary)
        self.assertFalse(address.verified)
        self.assertEqual(address.email, user.email)
        self.assertEqual(len(mail.outbox), 1)

    def test_missing_address_is_not_made_primary_when_primary_exists(self):
        user = self.create_user('secondary-canonical')
        EmailAddress.objects.create(
            user=user,
            email='older-address@example.com',
            primary=True,
            verified=False,
        )

        self.run_command('--send', '--user-id', str(user.pk))

        repaired = EmailAddress.objects.get(user=user, email=user.email)
        self.assertFalse(repaired.primary)
        self.assertFalse(repaired.verified)

    def test_inactive_and_blank_email_users_are_skipped(self):
        inactive = self.create_user('inactive', is_active=False)
        blank = self.create_user('blank', email='')

        inactive_output, _ = self.run_command(
            '--send', '--user-id', str(inactive.pk),
        )
        blank_output, _ = self.run_command(
            '--send', '--user-id', str(blank.pk),
        )

        self.assertIn('Active users examined: 0', inactive_output)
        self.assertIn('Skipped: 1', inactive_output)
        self.assertIn('Blank or invalid user email: 1', blank_output)
        self.assertEqual(len(mail.outbox), 0)

    def test_case_insensitive_ownership_conflict_is_blocked(self):
        first = self.create_user('conflict-one', email='Shared@Example.com')
        second = self.create_user(
            'conflict-two',
            email='different-owner@example.com',
        )
        EmailAddress.objects.create(
            user=second,
            email='shared@example.com',
            primary=True,
            verified=False,
        )

        output, _ = self.run_command('--send', '--user-id', str(first.pk))

        self.assertIn('Duplicate/conflicting email ownership: 1', output)
        self.assertIn('Eligible recipients: 0', output)
        self.assertFalse(EmailAddress.objects.filter(user=first).exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_delivery_failure_does_not_stop_the_batch(self):
        first = self.create_user('delivery-one')
        second = self.create_user('delivery-two')
        for user in (first, second):
            EmailAddress.objects.create(
                user=user,
                email=user.email,
                primary=True,
                verified=False,
            )

        with patch(
            'SalesLogApp.email_verification.send_verification_email_to_address',
            side_effect=[OSError('mail provider unavailable'), True],
        ):
            output, errors = self.run_command('--send')

        self.assertIn('Sent: 1', output)
        self.assertIn('Failed: 1', output)
        self.assertIn(f'User ID {first.pk}: verification delivery failed.', errors)
        self.assertNotIn('mail provider unavailable', errors)
        self.assertEqual(
            set(EmailVerificationDispatch.objects.values_list(
                'status', flat=True,
            )),
            {
                EmailVerificationDispatch.FAILED,
                EmailVerificationDispatch.SENT,
            },
        )

    def test_successful_send_is_suppressed_during_cooldown(self):
        user = self.create_user('cooldown')
        EmailAddress.objects.create(
            user=user,
            email=user.email,
            primary=True,
            verified=False,
        )

        first_output, _ = self.run_command(
            '--send', '--user-id', str(user.pk),
        )
        second_output, _ = self.run_command(
            '--send', '--user-id', str(user.pk),
        )

        self.assertIn('Sent: 1', first_output)
        self.assertIn('Eligible recipients: 0', second_output)
        self.assertIn('Skipped: 1', second_output)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            EmailVerificationDispatch.objects.filter(user=user).count(),
            1,
        )

    def test_each_recipient_gets_a_distinct_allauth_confirmation_link(self):
        first = self.create_user('unique-link-one')
        second = self.create_user('unique-link-two')

        output, _ = self.run_command('--send')

        self.assertIn('Sent: 2', output)
        self.assertEqual(len(mail.outbox), 2)
        urls = [
            next(
                word for word in message.body.split()
                if '/accounts/confirm-email/' in word
            )
            for message in mail.outbox
        ]
        self.assertEqual(len(set(urls)), 2)
        self.assertTrue(all(url.startswith('http://testserver/') for url in urls))

    def test_user_verified_after_send_is_always_skipped(self):
        user = self.create_user('verified-after-send')

        self.run_command('--send', '--user-id', str(user.pk))
        address = EmailAddress.objects.get(user=user)
        address.verified = True
        address.save(update_fields=['verified'])
        output, _ = self.run_command(
            '--send', '--user-id', str(user.pk),
        )

        self.assertIn('Already verified: 1', output)
        self.assertIn('Eligible recipients: 0', output)
        self.assertEqual(len(mail.outbox), 1)

    def test_limit_and_batch_size_bound_the_send(self):
        users = [
            self.create_user(f'limited-{index}')
            for index in range(3)
        ]

        output, _ = self.run_command(
            '--send', '--limit', '2', '--batch-size', '1',
        )

        self.assertIn('Eligible recipients: 2', output)
        self.assertIn('Sent: 2', output)
        self.assertEqual(len(mail.outbox), 2)
        self.assertFalse(
            EmailAddress.objects.filter(user=users[2]).exists()
        )

    def test_command_output_never_contains_address_or_confirmation_key(self):
        user = self.create_user('private-output')

        output, errors = self.run_command('--send', '--email', user.email)

        combined_output = output + errors
        self.assertNotIn(user.email, combined_output)
        self.assertEqual(len(mail.outbox), 1)
        confirmation_url = next(
            word for word in mail.outbox[0].body.split()
            if '/accounts/confirm-email/' in word
        )
        confirmation_key = confirmation_url.rstrip('/').rsplit('/', 1)[-1]
        self.assertNotIn(confirmation_key, combined_output)

    def test_contradictory_or_unsafe_command_options_fail(self):
        user = self.create_user('options')
        with self.assertRaises(CommandError):
            self.run_command(
                '--user-id', str(user.pk), '--email', user.email,
            )
        with self.assertRaises(CommandError):
            self.run_command('--confirm-production-send')
        with override_settings(
            DEBUG=False,
            EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
            EMAIL_VERIFICATION_PUBLIC_BASE_URL='https://stewlog.com',
            DEFAULT_FROM_EMAIL='STEW Log <no-reply@mail.stewlog.com>',
        ):
            with self.assertRaises(CommandError):
                self.run_command('--send', '--user-id', str(user.pk))
        with override_settings(
            DEBUG=False,
            EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
            EMAIL_VERIFICATION_PUBLIC_BASE_URL='https://wrong.example.com',
            DEFAULT_FROM_EMAIL='STEW Log <no-reply@mail.stewlog.com>',
        ):
            with self.assertRaises(CommandError):
                self.run_command(
                    '--send',
                    '--confirm-production-send',
                    '--user-id',
                    str(user.pk),
                )

    def test_production_sender_preflight_fails_closed_before_writes(self):
        user = self.create_user('sender-preflight')
        rejected_senders = (
            '',
            'not-an-email',
            'one@mail.stewlog.com, two@mail.stewlog.com',
            'user@localhost',
            'user@sub.localhost',
            'user@127.0.0.1',
            'user@[127.0.0.1]',
            'user@[::1]',
            'user@10.0.0.1',
            'user@[fe80::1]',
            'user@workstation.local',
            'Display Name <user@localhost>',
            'Display Name <user@LOCALHOST.>',
        )
        for sender in rejected_senders:
            with self.subTest(sender=sender):
                self.assert_production_preflight_rejected(
                    user,
                    DEFAULT_FROM_EMAIL=sender,
                )

    def test_production_backend_preflight_fails_closed_before_writes(self):
        user = self.create_user('backend-preflight')
        rejected_backends = (
            'example.missing.DoesNotExist',
            'django.core.mail.backends.console.EmailBackend',
            'django.core.mail.backends.dummy.EmailBackend',
            'django.core.mail.backends.locmem.EmailBackend',
            'django.core.mail.backends.filebased.EmailBackend',
            (
                'SalesLogApp.tests_email_verification_backfill.'
                'UnsafeImportableEmailBackend'
            ),
        )
        for backend in rejected_backends:
            with self.subTest(backend=backend):
                self.assert_production_preflight_rejected(
                    user,
                    EMAIL_BACKEND=backend,
                )

        before = self.database_state()
        with (
            override_settings(**self.production_settings()),
            patch(
                'SalesLogApp.email_verification.get_connection',
                side_effect=RuntimeError('secret initialization failure'),
            ),
            self.assertRaises(CommandError) as captured,
        ):
            self.run_command(
                '--send', '--confirm-production-send',
                '--user-id', str(user.pk),
            )
        self.assertEqual(
            str(captured.exception),
            'Production email delivery preflight failed.',
        )
        self.assertNotIn('secret initialization failure', str(captured.exception))
        self.assertEqual(self.database_state(), before)
        self.assertEqual(len(mail.outbox), 0)

    def test_production_smtp_preflight_fails_closed_before_writes(self):
        user = self.create_user('smtp-preflight')
        rejected_settings = (
            {'EMAIL_HOST': ''},
            {'EMAIL_HOST': 'localhost'},
            {'EMAIL_HOST': 'localhost.localdomain'},
            {'EMAIL_HOST': '127.0.0.1'},
            {'EMAIL_HOST': '::1'},
            {'EMAIL_HOST': '[::1]'},
            {'EMAIL_HOST': '0.0.0.0'},
            {'EMAIL_HOST': '10.0.0.1'},
            {'EMAIL_HOST': '169.254.10.20'},
            {'EMAIL_HOST': '224.0.0.1'},
            {'EMAIL_HOST': '[fe80::1]'},
            {'EMAIL_HOST': 'bad host.example'},
            {'EMAIL_HOST': 'relay.local'},
            {'EMAIL_HOST': '203.0.113.10'},
            {'EMAIL_PORT': 0},
            {'EMAIL_PORT': 65536},
            {'EMAIL_PORT': 'not-a-port'},
            {'EMAIL_USE_TLS': True, 'EMAIL_USE_SSL': True},
            {'EMAIL_USE_TLS': False, 'EMAIL_USE_SSL': False},
        )
        for overrides in rejected_settings:
            with self.subTest(overrides=overrides):
                self.assert_production_preflight_rejected(user, **overrides)

    def test_supported_production_smtp_preflight_allows_mocked_delivery(self):
        user = self.create_user('valid-production-preflight')
        with (
            override_settings(**self.production_settings(
                EMAIL_HOST='SMTP.RESEND.COM.',
            )),
            patch(
                'SalesLogApp.email_verification.'
                'send_verification_email_to_address',
                return_value=True,
            ) as send,
        ):
            output, errors = self.run_command(
                '--send', '--confirm-production-send',
                '--user-id', str(user.pk),
            )

        self.assertIn('Sent: 1', output)
        self.assertEqual(errors, '')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(EmailAddress.objects.get(user=user).verified)
        self.assertEqual(
            EmailVerificationDispatch.objects.get(user=user).status,
            EmailVerificationDispatch.SENT,
        )

    def test_deployment_check_reports_sanitized_preflight_failure(self):
        unsafe_sender = 'private-address@localhost'
        with override_settings(**self.production_settings(
            DEFAULT_FROM_EMAIL=unsafe_sender,
        )):
            issues = email_verification_delivery_check(None)

        self.assertEqual([issue.id for issue in issues], ['SalesLogApp.E004'])
        rendered = ' '.join(
            f'{issue.msg} {issue.hint}' for issue in issues
        )
        self.assertNotIn(unsafe_sender, rendered)

    def test_failed_delivery_can_retry_immediately_without_cooldown(self):
        user = self.create_user('failed-retry')
        request = build_verification_request(
            user=user,
            base_url='http://testserver',
        )
        with patch(
            'SalesLogApp.email_verification.send_verification_email_to_address',
            side_effect=[OSError('provider unavailable'), True],
        ) as send:
            failed = dispatch_verification_email(
                user=user,
                request=request,
                source=EmailVerificationDispatch.BACKFILL,
            )
            dispatch = EmailVerificationDispatch.objects.get(user=user)
            self.assertEqual(failed.outcome, 'failed')
            self.assertEqual(dispatch.status, EmailVerificationDispatch.FAILED)

            retried = dispatch_verification_email(
                user=user,
                request=request,
                source=EmailVerificationDispatch.BACKFILL,
            )

        dispatch.refresh_from_db()
        self.assertEqual(retried.outcome, 'sent')
        self.assertEqual(send.call_count, 2)
        self.assertEqual(dispatch.status, EmailVerificationDispatch.SENT)
        self.assertEqual(EmailAddress.objects.filter(user=user).count(), 1)
        self.assertFalse(EmailAddress.objects.get(user=user).verified)

    def test_stale_pending_reservation_recovers_but_fresh_pending_blocks(self):
        stale_user = self.create_user('stale-pending')
        stale_address = EmailAddress.objects.create(
            user=stale_user,
            email=stale_user.email,
            primary=True,
            verified=False,
        )
        stale_dispatch = EmailVerificationDispatch.objects.create(
            user=stale_user,
            recipient_digest=_recipient_digest(stale_user.email),
            source=EmailVerificationDispatch.BACKFILL,
            status=EmailVerificationDispatch.PENDING,
        )
        EmailVerificationDispatch.objects.filter(pk=stale_dispatch.pk).update(
            attempted_at=timezone.now() - timedelta(minutes=16),
        )
        stale_request = build_verification_request(
            user=stale_user,
            base_url='http://testserver',
        )

        with patch(
            'SalesLogApp.email_verification.send_verification_email_to_address',
            return_value=True,
        ) as send:
            recovered = dispatch_verification_email(
                user=stale_user,
                request=stale_request,
                source=EmailVerificationDispatch.BACKFILL,
            )

        stale_dispatch.refresh_from_db()
        self.assertEqual(recovered.outcome, 'sent')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(stale_dispatch.status, EmailVerificationDispatch.SENT)
        self.assertEqual(EmailAddress.objects.get(pk=stale_address.pk), stale_address)

        fresh_user = self.create_user('fresh-pending')
        EmailAddress.objects.create(
            user=fresh_user,
            email=fresh_user.email,
            primary=True,
            verified=False,
        )
        EmailVerificationDispatch.objects.create(
            user=fresh_user,
            recipient_digest=_recipient_digest(fresh_user.email),
            source=EmailVerificationDispatch.BACKFILL,
            status=EmailVerificationDispatch.PENDING,
        )
        fresh_request = build_verification_request(
            user=fresh_user,
            base_url='http://testserver',
        )
        with patch(
            'SalesLogApp.email_verification.send_verification_email_to_address',
        ) as send:
            blocked = dispatch_verification_email(
                user=fresh_user,
                request=fresh_request,
                source=EmailVerificationDispatch.BACKFILL,
            )
        self.assertEqual(blocked.outcome, 'skipped')
        send.assert_not_called()

    def test_reclaimed_reservation_rejects_stale_worker_finalization(self):
        user = self.create_user('reservation-generation')
        address = EmailAddress.objects.create(
            user=user,
            email=user.email,
            primary=True,
            verified=False,
        )
        original = EmailVerificationDispatch.objects.create(
            user=user,
            recipient_digest=_recipient_digest(user.email),
            source=EmailVerificationDispatch.BACKFILL,
            status=EmailVerificationDispatch.PENDING,
        )
        EmailVerificationDispatch.objects.filter(pk=original.pk).update(
            attempted_at=timezone.now() - timedelta(minutes=16),
        )

        reservation, _, _ = _reserve_dispatch(
            user.pk,
            EmailVerificationDispatch.BACKFILL,
        )
        current, reserved_address = reservation
        self.assertEqual(reserved_address.pk, address.pk)
        newer_attempt = current.attempted_at + timedelta(seconds=1)
        EmailVerificationDispatch.objects.filter(pk=current.pk).update(
            attempted_at=newer_attempt,
        )

        self.assertFalse(_finalize_dispatch(
            current,
            status=EmailVerificationDispatch.SENT,
        ))
        original.refresh_from_db()
        self.assertEqual(original.status, EmailVerificationDispatch.PENDING)
        self.assertEqual(original.attempted_at, newer_attempt)


@override_settings(
    DEBUG=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    EMAIL_VERIFICATION_PUBLIC_BASE_URL='http://testserver',
    EMAIL_VERIFICATION_RESEND_COOLDOWN_MINUTES=60,
    EMAIL_VERIFICATION_PENDING_STALE_MINUTES=15,
    ALLOWED_HOSTS=['testserver'],
)
class EmailVerificationConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username='concurrent-verification',
            email='Concurrent@Example.com',
            password='pass',
        )
        EmailAddress.objects.create(
            user=self.user,
            email='concurrent@example.com',
            primary=True,
            verified=False,
        )

    def worker(self):
        close_old_connections()
        try:
            user = get_user_model().objects.get(pk=self.user.pk)
            request = build_verification_request(
                user=user,
                base_url='http://testserver',
            )
            return dispatch_verification_email(
                user=user,
                request=request,
                source=EmailVerificationDispatch.BACKFILL,
            )
        finally:
            close_old_connections()

    def exercise_inflight_duplicate(self):
        delivery_started = Event()
        release_delivery = Event()
        call_lock = Lock()
        delivery_calls = []

        def controlled_delivery(*args, **kwargs):
            with call_lock:
                delivery_calls.append(1)
            delivery_started.set()
            if not release_delivery.wait(timeout=10):
                raise TimeoutError('test delivery release timed out')
            return True

        with (
            patch(
                'SalesLogApp.email_verification.'
                'send_verification_email_to_address',
                side_effect=controlled_delivery,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            winner = executor.submit(self.worker)
            self.assertTrue(delivery_started.wait(timeout=10))
            loser = executor.submit(self.worker)
            try:
                loser_result = loser.result(timeout=10)
            finally:
                release_delivery.set()
            winner_result = winner.result(timeout=10)

        self.assertEqual(winner_result.outcome, 'sent')
        self.assertEqual(loser_result.outcome, 'skipped')
        self.assertEqual(len(delivery_calls), 1)
        dispatch = EmailVerificationDispatch.objects.get()
        self.assertEqual(dispatch.status, EmailVerificationDispatch.SENT)
        self.assertEqual(EmailVerificationDispatch.objects.count(), 1)
        self.assertEqual(EmailAddress.objects.filter(user=self.user).count(), 1)

    def test_inflight_reservation_prevents_a_second_delivery(self):
        self.exercise_inflight_duplicate()

    @skipUnless(
        connection.vendor == 'postgresql',
        'PostgreSQL row-lock execution requires PostgreSQL.',
    )
    def test_postgresql_concurrent_initial_reservation_sends_only_once(self):
        start_workers = Event()
        delivery_started = Event()
        release_delivery = Event()
        call_lock = Lock()
        delivery_calls = []

        def simultaneous_worker():
            if not start_workers.wait(timeout=10):
                raise TimeoutError('test workers did not start')
            return self.worker()

        def controlled_delivery(*args, **kwargs):
            with call_lock:
                delivery_calls.append(1)
            delivery_started.set()
            if not release_delivery.wait(timeout=10):
                raise TimeoutError('test delivery release timed out')
            return True

        with (
            patch(
                'SalesLogApp.email_verification.'
                'send_verification_email_to_address',
                side_effect=controlled_delivery,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = (
                executor.submit(simultaneous_worker),
                executor.submit(simultaneous_worker),
            )
            start_workers.set()
            self.assertTrue(delivery_started.wait(timeout=10))
            try:
                completed, _ = wait(
                    futures,
                    timeout=10,
                    return_when=FIRST_COMPLETED,
                )
                self.assertEqual(len(completed), 1)
                self.assertEqual(next(iter(completed)).result().outcome, 'skipped')
            finally:
                release_delivery.set()
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual(
            sorted(result.outcome for result in results),
            ['sent', 'skipped'],
        )
        self.assertEqual(len(delivery_calls), 1)
        self.assertEqual(EmailVerificationDispatch.objects.count(), 1)
        self.assertEqual(
            EmailVerificationDispatch.objects.get().status,
            EmailVerificationDispatch.SENT,
        )
