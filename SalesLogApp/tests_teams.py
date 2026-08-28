import base64
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import re
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from allauth.account.models import EmailAddress, EmailConfirmation
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core import signing
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from SalesLog.settings import env_strict_bool

from .account_adapter import TEAM_INVITATION_RESUME_SESSION_KEY
from .models import (
    Commission,
    EmailVerificationDispatch,
    Sale,
    Team,
    TeamActivity,
    TeamComment,
    TeamInvitation,
    TeamMembership,
    TeamReaction,
)
from .team_services import (
    INVITATION_RESUME_REFERENCE_VERSION,
    INVITATION_REVIEW_SIGNING_SALT,
    INVITATION_VERIFICATION_RESUME_SIGNING_SALT,
    InvalidInvitationResumeReference,
    InvitationDeliveryError,
    accept_invitation,
    build_feed_queryset,
    build_month_totals,
    create_and_email_invitation,
    create_invitation,
    create_invitation_review_reference,
    create_invitation_verification_resume_reference,
    create_team,
    digest_from_invitation_review_reference,
    digest_from_invitation_verification_resume_reference,
    invitation_for_resume_digest_or_404,
    invitation_for_user_or_404,
    project_activity,
)
from .team_views import (
    INVITATION_RESUME_REFERENCE_SESSION_KEY,
    INVITATION_REVIEW_REFERENCE_SESSION_KEY,
    LEGACY_INVITATION_RESUME_CODE_SESSION_KEY,
)
from .team_entitlements import (
    TeamEntitlement,
    can_use_teams,
    get_team_entitlement,
)


def test_team_entitlement(user):
    """Test-only seam; production founder access is billing-owned."""
    founder_ids = {str(value) for value in settings.TEAMS_FOUNDER_USER_IDS}
    if user.is_authenticated and str(user.pk) in founder_ids:
        return TeamEntitlement(tier='founder_pro', source='teams_test')
    return TeamEntitlement(tier='basic', source='teams_test')


class StrictTeamsFlagTests(SimpleTestCase):
    def test_teams_feature_is_disabled_by_default(self):
        self.assertFalse(settings.TEAMS_FEATURE_ENABLED)

    def test_strict_boolean_parser_accepts_only_documented_values(self):
        with patch.dict('os.environ', {'TEST_TEAMS_FLAG': 'true'}):
            self.assertIs(env_strict_bool('TEST_TEAMS_FLAG'), True)
        with patch.dict('os.environ', {'TEST_TEAMS_FLAG': '0'}):
            self.assertIs(env_strict_bool('TEST_TEAMS_FLAG'), False)
        with patch.dict('os.environ', {'TEST_TEAMS_FLAG': 'yes'}):
            with self.assertRaises(ImproperlyConfigured):
                env_strict_bool('TEST_TEAMS_FLAG')


@override_settings(
    TEAMS_FEATURE_ENABLED=True,
    TEAMS_ENTITLEMENT_BACKEND='SalesLogApp.tests_teams.test_team_entitlement',
)
class Phase2ATeamsTests(TestCase):
    sale_number = 800000

    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username='owner', email='owner@example.com', password='pass',
            first_name='Olivia', last_name='Owner'
        )
        self.member = user_model.objects.create_user(
            username='member', email='member@example.com', password='pass',
            first_name='Mina', last_name='Member'
        )
        self.outsider = user_model.objects.create_user(
            username='outsider', email='outside@example.com', password='pass',
            first_name='Otto', last_name='Outside'
        )
        for user in (self.owner, self.member, self.outsider):
            EmailAddress.objects.create(
                user=user,
                email=user.email,
                verified=True,
                primary=True,
            )
        self.entitlement_override = override_settings(
            TEAMS_FOUNDER_USER_IDS=[str(self.owner.pk)]
        )
        self.entitlement_override.enable()
        self.addCleanup(self.entitlement_override.disable)
        self.team = create_team(
            self.owner,
            name='North Store',
            timezone_name='America/Chicago',
            monthly_unit_goal=Decimal('20.0'),
            display_mode=Team.RANKED,
        )
        self.owner_membership = TeamMembership.objects.get(
            team=self.team, user=self.owner
        )
        self.member_membership = TeamMembership.objects.create(
            team=self.team,
            user=self.member,
            status=TeamMembership.ACTIVE,
            role=TeamMembership.MEMBER,
            joined_at=timezone.now() - timedelta(days=60),
            sharing_preference=TeamMembership.INDIVIDUAL_AND_TOTALS,
        )

    @classmethod
    def next_sale_number(cls):
        cls.sale_number += 1
        return cls.sale_number

    def make_sale(self, user=None, *, count='1.0', sale_date=None, marker='private-customer'):
        return Sale.objects.create(
            user=user or self.member,
            customer=marker,
            dealNumber=self.next_sale_number(),
            count=Decimal(count),
            frontEnd=Decimal('7654.32'),
            backend=Decimal('1234.56'),
            date=sale_date or timezone.localdate(),
        )

    def login(self, user=None):
        self.client.force_login(user or self.member)

    def test_feature_disabled_hides_navigation_and_returns_404(self):
        self.login()
        with override_settings(TEAMS_FEATURE_ENABLED=False):
            response = self.client.get(reverse('team_home'))
            self.assertEqual(response.status_code, 404)
            profile = self.client.get(reverse('profile'))
            self.assertNotContains(profile, reverse('team_home'))

    def test_anonymous_user_is_redirected_before_invitation_lookup(self):
        response = self.client.get(reverse('team_invitation_accept'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('account_login'), response.url)

    def test_invitation_lock_targets_only_the_invitation_row(self):
        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email,
        )
        with patch.object(
            TeamInvitation.objects, 'select_related',
        ) as select_related:
            selected = select_related.return_value
            locked = selected.select_for_update.return_value
            locked.get.return_value = invitation

            result = invitation_for_user_or_404(
                raw, self.outsider, lock=True,
            )

        self.assertEqual(result.pk, invitation.pk)
        select_related.assert_called_once_with('team', 'intended_user')
        selected.select_for_update.assert_called_once_with(of=('self',))
        locked.get.assert_called_once_with(
            token_digest=invitation.token_digest,
        )

        with patch.object(
            TeamInvitation.objects, 'select_related',
        ) as select_related:
            selected = select_related.return_value
            locked = selected.select_for_update.return_value
            locked.get.return_value = invitation

            resumed = invitation_for_resume_digest_or_404(
                invitation.token_digest,
                self.outsider,
                lock=True,
            )

        self.assertEqual(resumed.pk, invitation.pk)
        select_related.assert_called_once_with('team', 'intended_user')
        selected.select_for_update.assert_called_once_with(of=('self',))
        locked.get.assert_called_once_with(
            token_digest=invitation.token_digest,
        )

    @skipUnless(
        connection.vendor == 'postgresql',
        'PostgreSQL is required for the nullable outer-join locking regression.',
    )
    def test_postgresql_locks_pending_invitation_with_null_intended_user(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            'postgresql.pending@example.com',
        )
        self.assertIsNone(invitation.intended_user_id)
        invitee = get_user_model().objects.create_user(
            username='postgresql-pending-invitee',
            email=invitation.intended_email,
            password='pass',
        )
        EmailAddress.objects.create(
            user=invitee,
            email=invitee.email,
            verified=True,
            primary=True,
        )

        locked = invitation_for_user_or_404(raw, invitee, lock=True)

        self.assertEqual(locked.pk, invitation.pk)
        self.assertIsNone(locked.intended_user)
        self.assertIn('team', locked._state.fields_cache)
        resumed = invitation_for_resume_digest_or_404(
            invitation.token_digest,
            invitee,
            lock=True,
        )
        self.assertEqual(resumed.pk, invitation.pk)
        self.assertIsNone(resumed.intended_user)

    def test_nullable_invitation_accept_view_activates_exact_membership_only(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            'nullable.accept@example.com',
        )
        self.assertIsNone(invitation.intended_user_id)
        invitee = get_user_model().objects.create_user(
            username='nullable-accept-invitee',
            email=invitation.intended_email,
            password='pass',
        )
        EmailAddress.objects.create(
            user=invitee,
            email=invitee.email,
            verified=True,
            primary=True,
        )
        private_sale = self.make_sale(marker='PRIVATE-ACCEPTANCE-CUSTOMER')
        commission, _ = Commission.objects.get_or_create(user=self.member)
        commission.frontend_minimum = Decimal('987654.32')
        commission.save(update_fields=['frontend_minimum'])
        private_plan = self.member.pay_plans.first()
        private_plan.name = 'PRIVATE ACCEPTANCE PAY PLAN'
        private_plan.save(update_fields=['name', 'updated_at'])
        unrelated_owner = get_user_model().objects.create_user(
            username='unrelated-team-owner',
        )
        Team.objects.create(
            owner=unrelated_owner,
            name='PRIVATE UNRELATED TEAM',
            timezone='UTC',
        )
        self.login(invitee)

        response = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'accept',
        })

        self.assertRedirects(
            response,
            reverse('team_detail', args=[self.team.public_id]),
            fetch_redirect_response=False,
        )
        invitation.refresh_from_db()
        membership = TeamMembership.objects.get(
            team=self.team,
            user=invitee,
        )
        self.assertEqual(membership.status, TeamMembership.ACTIVE)
        self.assertEqual(membership.role, TeamMembership.MEMBER)
        self.assertEqual(invitation.intended_user, invitee)
        self.assertEqual(invitation.accepted_by, invitee)
        self.assertIsNotNone(invitation.accepted_at)
        self.assertEqual(
            TeamMembership.objects.filter(
                user=invitee,
                status=TeamMembership.ACTIVE,
            ).count(),
            1,
        )

        detail = self.client.get(response.url)
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, private_sale.customer)
        self.assertNotContains(detail, str(private_sale.dealNumber))
        self.assertNotContains(detail, '987654.32')
        self.assertNotContains(detail, private_plan.name)
        self.assertNotContains(detail, 'PRIVATE UNRELATED TEAM')

    def test_nullable_invitation_decline_view_revokes_without_membership(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            'nullable.decline@example.com',
        )
        self.assertIsNone(invitation.intended_user_id)
        invitee = get_user_model().objects.create_user(
            username='nullable-decline-invitee',
            email=invitation.intended_email,
            password='pass',
        )
        EmailAddress.objects.create(
            user=invitee,
            email=invitee.email,
            verified=True,
            primary=True,
        )
        self.login(invitee)

        response = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'decline',
        })

        self.assertRedirects(
            response,
            reverse('team_home'),
            fetch_redirect_response=False,
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.intended_user, invitee)
        self.assertIsNotNone(invitation.revoked_at)
        self.assertFalse(TeamMembership.objects.filter(
            team=self.team,
            user=invitee,
        ).exists())

    def test_unavailable_invitation_uses_generic_non_enumerating_guidance(self):
        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email,
        )
        self.login(self.member)

        for submitted_code in ('invalid-invitation-code', raw):
            with self.subTest(valid_token=submitted_code == raw):
                response = self.client.post(
                    reverse('team_invitation_accept'),
                    {
                        'invitation_code': submitted_code,
                        'action': 'accept',
                    },
                )
                self.assertEqual(response.status_code, 404)
                self.assertContains(
                    response,
                    'This invitation could not be verified.',
                    status_code=404,
                )
                self.assertNotContains(
                    response,
                    submitted_code,
                    status_code=404,
                )
                self.assertNotContains(
                    response,
                    invitation.intended_email,
                    status_code=404,
                )
                self.assertNotContains(
                    response,
                    self.team.name,
                    status_code=404,
                )

    def test_unexpected_accept_and_decline_errors_are_logged_without_secrets(self):
        self.login(self.outsider)
        for action, service_name in (
            ('accept', 'accept_invitation'),
            ('decline', 'decline_invitation'),
        ):
            invitation, raw = create_invitation(
                self.owner_membership, self.outsider.email,
            )
            with (
                patch(
                    f'SalesLogApp.team_views.{service_name}',
                    side_effect=RuntimeError(
                        f'secret failure {raw} {invitation.intended_email}'
                    ),
                ),
                self.assertLogs(
                    'SalesLogApp.team_views', level='ERROR',
                ) as captured,
            ):
                response = self.client.post(
                    reverse('team_invitation_accept'),
                    {
                        'invitation_code': raw,
                        'action': action,
                    },
                )

            logs = '\n'.join(captured.output)
            self.assertEqual(response.status_code, 500)
            self.assertContains(
                response,
                'The invitation could not be processed. Please try again.',
                status_code=500,
            )
            self.assertNotContains(response, raw, status_code=500)
            self.assertNotContains(
                response, invitation.intended_email, status_code=500,
            )
            self.assertNotIn(raw, logs)
            self.assertNotIn(invitation.intended_email, logs)
            invitation.refresh_from_db()
            self.assertIsNone(invitation.accepted_at)
            self.assertIsNone(invitation.revoked_at)

    def test_basic_member_cannot_create_but_can_use_an_invitation(self):
        self.assertTrue(can_use_teams(self.member))
        self.assertEqual(get_team_entitlement(self.member).tier, 'basic')
        self.assertTrue(get_team_entitlement(self.owner).has_pro_access)
        self.login(self.outsider)
        self.assertEqual(self.client.get(reverse('team_create')).status_code, 403)
        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email
        )
        response = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'accept',
        })
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        self.assertEqual(invitation.accepted_by, self.outsider)
        self.assertTrue(TeamMembership.objects.filter(
            team=self.team, user=self.outsider, status=TeamMembership.ACTIVE
        ).exists())

    def test_invited_user_can_decline(self):
        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email
        )
        self.login(self.outsider)
        response = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'decline',
        })
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        membership = TeamMembership.objects.get(team=self.team, user=self.outsider)
        self.assertIsNotNone(invitation.revoked_at)
        self.assertEqual(membership.status, TeamMembership.DECLINED)

    def test_founder_can_create_only_one_team(self):
        with self.assertRaises(ValidationError):
            create_team(
                self.owner,
                name='Second Team',
                timezone_name='UTC',
                monthly_unit_goal=None,
                display_mode=Team.ALPHABETICAL,
            )

    def test_invitation_stores_digest_only_and_is_intended_user_only(self):
        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email
        )
        self.assertNotEqual(invitation.token_digest, raw)
        self.assertNotIn(raw, invitation.token_digest)
        self.assertEqual(invitation.token_prefix, raw[:10])
        self.login(self.member)
        response = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'accept',
        })
        self.assertEqual(response.status_code, 404)

    def test_invitation_requires_verification_at_acceptance(self):
        EmailAddress.objects.filter(user=self.outsider).update(verified=False)
        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email
        )
        self.assertIsNone(invitation.intended_user)
        self.login(self.outsider)
        self.assertEqual(self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'accept',
        }).status_code, 403)
        EmailAddress.objects.filter(user=self.outsider).update(verified=True)
        self.assertEqual(self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'accept',
        }).status_code, 302)
        invitation.refresh_from_db()
        self.assertEqual(invitation.intended_user, self.outsider)

    @patch('SalesLogApp.team_services.send_mail', return_value=1)
    def test_invite_view_emails_unregistered_recipient_without_code_in_url(
        self, send_mail
    ):
        self.login(self.owner)
        response = self.client.post(
            reverse('team_invite', args=[self.team.public_id]),
            {'intended_email': 'New.Person@Example.com'},
        )
        self.assertEqual(response.status_code, 302)
        invitation = TeamInvitation.objects.get(
            team=self.team,
            intended_email='new.person@example.com',
        )
        self.assertIsNone(invitation.intended_user)
        raw_code = self.client.session['team_invitation_once']['code']
        mail = send_mail.call_args.kwargs
        self.assertEqual(mail['recipient_list'], ['new.person@example.com'])
        self.assertIn('http://testserver/accounts/signup/', mail['message'])
        self.assertIn('http://testserver/accounts/login/', mail['message'])
        self.assertIn('http://testserver/SalesLogApp/teams/invitations/', mail['message'])
        self.assertIn('sale dates, and unit credits', mail['message'])
        self.assertIn('Customer names', mail['message'])
        self.assertIn(raw_code, mail['message'])
        for line in mail['message'].splitlines():
            if line.startswith(('http://', 'https://')):
                self.assertNotIn(raw_code, line)

    @patch('SalesLogApp.team_services.send_mail', return_value=1)
    def test_admin_can_send_an_email_invitation(self, send_mail):
        self.member_membership.role = TeamMembership.ADMIN
        self.member_membership.save(update_fields=['role', 'updated_at'])
        self.login(self.member)

        response = self.client.post(
            reverse('team_invite', args=[self.team.public_id]),
            {'intended_email': 'admin.invitee@example.com'},
        )

        self.assertEqual(response.status_code, 302)
        invitation = TeamInvitation.objects.get(
            team=self.team,
            intended_email='admin.invitee@example.com',
        )
        self.assertEqual(invitation.created_by, self.member)
        self.assertEqual(
            send_mail.call_args.kwargs['recipient_list'],
            ['admin.invitee@example.com'],
        )

    def test_unregistered_recipient_can_verify_and_accept(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            'future.member@example.com',
        )
        self.assertIsNone(invitation.intended_user)
        future_member = get_user_model().objects.create_user(
            username='future-member',
            email='future.member@example.com',
            password='pass',
        )
        EmailAddress.objects.create(
            user=future_member,
            email=future_member.email,
            verified=True,
            primary=True,
        )
        self.assertTrue(can_use_teams(future_member))
        membership = accept_invitation(raw, future_member)
        invitation.refresh_from_db()
        self.assertEqual(invitation.intended_user, future_member)
        self.assertEqual(invitation.accepted_by, future_member)
        self.assertEqual(membership.status, TeamMembership.ACTIVE)

    @patch('SalesLogApp.team_services.send_mail', side_effect=OSError('offline'))
    def test_email_failure_rolls_back_invitation_and_membership(self, _send_mail):
        with self.assertRaises(InvitationDeliveryError):
            create_and_email_invitation(
                self.owner_membership,
                self.outsider.email,
                signup_url='https://example.test/accounts/signup/',
                login_url='https://example.test/accounts/login/',
                teams_url='https://example.test/SalesLogApp/teams/invitations/',
            )
        self.assertFalse(TeamInvitation.objects.filter(
            team=self.team,
            intended_email=self.outsider.email,
        ).exists())
        self.assertFalse(TeamMembership.objects.filter(
            team=self.team,
            user=self.outsider,
        ).exists())

    def test_expired_revoked_and_replayed_invitations_are_rejected(self):
        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email
        )
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save(update_fields=['expires_at'])
        self.login(self.outsider)
        self.assertEqual(self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw, 'action': 'accept'
        }).status_code, 404)

        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email
        )
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=['revoked_at'])
        self.assertEqual(self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw, 'action': 'accept'
        }).status_code, 404)

        invitation, raw = create_invitation(
            self.owner_membership, self.outsider.email
        )
        self.assertEqual(self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw, 'action': 'accept'
        }).status_code, 302)
        self.assertEqual(self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw, 'action': 'accept'
        }).status_code, 404)
        self.assertEqual(TeamMembership.objects.filter(
            team=self.team, user=self.outsider
        ).count(), 1)

    def test_verified_email_is_rechecked_during_acceptance(self):
        address = EmailAddress.objects.get(user=self.outsider)
        invitation, raw = create_invitation(
            self.owner_membership, address.email
        )
        address.verified = False
        address.save(update_fields=['verified'])
        self.login(self.outsider)
        self.assertEqual(self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw, 'action': 'accept'
        }).status_code, 403)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)

    @override_settings(
        DEBUG=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        EMAIL_VERIFICATION_RESEND_COOLDOWN_MINUTES=60,
    )
    def test_unverified_join_resends_safely_and_resumes_after_verification(self):
        cache.clear()
        address = EmailAddress.objects.get(user=self.outsider)
        address.verified = False
        address.save(update_fields=['verified'])
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)

        response = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'accept',
        })

        self.assertEqual(response.status_code, 403)
        self.assertContains(
            response,
            'Please verify your email before joining a team. '
            'We\u2019ve sent you a new verification link.',
            status_code=403,
        )
        self.assertNotContains(response, raw, status_code=403)
        self.assertNotContains(response, self.outsider.email, status_code=403)
        self.assertNotContains(response, self.team.name, status_code=403)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn(raw, mail.outbox[0].body)
        self.assertIn('next=', mail.outbox[0].body)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)
        self.assertFalse(TeamMembership.objects.filter(
            team=self.team,
            user=self.outsider,
            status=TeamMembership.ACTIVE,
        ).exists())

        address.verified = True
        address.save(update_fields=['verified'])
        resume = self.client.get(reverse('team_invitation_accept'))

        self.assertEqual(resume.status_code, 200)
        self.assertContains(resume, self.team.name)
        self.assertNotContains(resume, raw)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)

    @override_settings(
        DEBUG=True,
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        EMAIL_VERIFICATION_RESEND_COOLDOWN_MINUTES=60,
    )
    def test_unverified_join_does_not_disclose_invitation_validity(self):
        cache.clear()
        EmailAddress.objects.filter(user=self.outsider).update(verified=False)
        _, raw = create_invitation(self.owner_membership, self.outsider.email)
        self.login(self.outsider)

        review = self.client.get(reverse('team_invitation_accept'))
        valid = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'accept',
        })
        invalid = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': 'invalid-invitation-code',
            'action': 'accept',
        })
        decline = self.client.post(reverse('team_invitation_accept'), {
            'invitation_code': raw,
            'action': 'decline',
        })

        self.assertEqual(review.status_code, 403)
        self.assertEqual(valid.status_code, 403)
        self.assertEqual(invalid.status_code, 403)
        self.assertEqual(decline.status_code, 403)
        self.assertEqual(review.content, valid.content)
        self.assertEqual(valid.content, invalid.content)
        self.assertEqual(valid.content, decline.content)
        self.assertNotContains(invalid, raw, status_code=403)
        self.assertNotContains(
            invalid,
            self.outsider.email,
            status_code=403,
        )

    def production_email_settings(self, **overrides):
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
            'ALLOWED_HOSTS': ['stewlog.com', 'testserver'],
        }
        configured.update(overrides)
        return configured

    def verification_business_state(self):
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
                    'pk', 'intended_user_id', 'accepted_at', 'revoked_at',
                )
            ),
            'memberships': tuple(
                TeamMembership.objects.order_by('pk').values_list(
                    'pk', 'team_id', 'user_id', 'status',
                )
            ),
            'sales': tuple(Sale.objects.order_by('pk').values_list('pk')),
            'commissions': tuple(
                Commission.objects.order_by('pk').values_list('pk')
            ),
        }

    def store_resume_reference(self, reference):
        session = self.client.session
        session.pop(LEGACY_INVITATION_RESUME_CODE_SESSION_KEY, None)
        session.pop(INVITATION_RESUME_REFERENCE_SESSION_KEY, None)
        session.pop(INVITATION_REVIEW_REFERENCE_SESSION_KEY, None)
        session.pop(TEAM_INVITATION_RESUME_SESSION_KEY, None)
        session[INVITATION_RESUME_REFERENCE_SESSION_KEY] = reference
        session[TEAM_INVITATION_RESUME_SESSION_KEY] = True
        session.save()

    def assert_generic_resume_failure(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertContains(
            response,
            'This invitation could not be resumed. Reopen the original '
            'invitation and try again.',
            status_code=404,
        )
        session = self.client.session
        self.assertNotIn(LEGACY_INVITATION_RESUME_CODE_SESSION_KEY, session)
        self.assertNotIn(INVITATION_RESUME_REFERENCE_SESSION_KEY, session)
        self.assertNotIn(INVITATION_REVIEW_REFERENCE_SESSION_KEY, session)
        self.assertNotIn(TEAM_INVITATION_RESUME_SESSION_KEY, session)

    def test_rejected_production_preflight_leaves_resume_session_unmodified(self):
        EmailAddress.objects.filter(user=self.outsider).delete()
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        session = self.client.session
        session['unrelated_session_state'] = 'preserve-me'
        session.save()
        before = self.verification_business_state()

        with (
            override_settings(**self.production_email_settings(
                DEFAULT_FROM_EMAIL='unsafe-sender@localhost',
            )),
            patch(
                'SalesLogApp.email_verification.'
                'send_verification_email_to_address',
            ) as deliver,
            self.assertLogs('SalesLogApp.team_views', level='ERROR') as captured,
        ):
            response = self.client.post(reverse('team_invitation_accept'), {
                'invitation_code': raw,
                'action': 'accept',
            })

        logs = '\n'.join(captured.output)
        session = self.client.session
        self.assertEqual(response.status_code, 403)
        self.assertContains(
            response,
            'Please verify your email before joining a team.',
            status_code=403,
        )
        self.assertNotContains(response, raw, status_code=403)
        self.assertNotContains(response, self.team.name, status_code=403)
        self.assertNotContains(response, self.outsider.email, status_code=403)
        self.assertNotIn(raw, logs)
        self.assertNotIn(self.outsider.email, logs)
        self.assertNotIn('unsafe-sender@localhost', logs)
        self.assertNotIn(LEGACY_INVITATION_RESUME_CODE_SESSION_KEY, session)
        self.assertNotIn(INVITATION_RESUME_REFERENCE_SESSION_KEY, session)
        self.assertNotIn(INVITATION_REVIEW_REFERENCE_SESSION_KEY, session)
        self.assertNotIn(TEAM_INVITATION_RESUME_SESSION_KEY, session)
        self.assertNotIn(raw, session.values())
        self.assertEqual(session['unrelated_session_state'], 'preserve-me')
        deliver.assert_not_called()
        self.assertEqual(self.verification_business_state(), before)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)

    def test_rejected_preflight_does_not_disclose_invitation_validity(self):
        EmailAddress.objects.filter(user=self.outsider).delete()
        _, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)

        responses = []
        with override_settings(**self.production_email_settings(
            DEFAULT_FROM_EMAIL='unsafe-sender@localhost',
        )):
            for code in (raw, 'invalid-invitation-code'):
                with self.assertLogs('SalesLogApp.team_views', level='ERROR'):
                    responses.append(self.client.post(
                        reverse('team_invitation_accept'),
                        {'invitation_code': code, 'action': 'accept'},
                    ))
                session = self.client.session
                self.assertNotIn(
                    LEGACY_INVITATION_RESUME_CODE_SESSION_KEY,
                    session,
                )
                self.assertNotIn(
                    INVITATION_RESUME_REFERENCE_SESSION_KEY,
                    session,
                )
                self.assertNotIn(
                    INVITATION_REVIEW_REFERENCE_SESSION_KEY,
                    session,
                )
                self.assertNotIn(TEAM_INVITATION_RESUME_SESSION_KEY, session)

        self.assertEqual(responses[0].status_code, 403)
        self.assertEqual(responses[0].content, responses[1].content)
        self.assertNotContains(responses[0], raw, status_code=403)
        self.assertNotContains(
            responses[1],
            self.outsider.email,
            status_code=403,
        )

    def test_valid_preflight_stores_only_timestamped_non_raw_reference(self):
        cache.clear()
        address = EmailAddress.objects.get(user=self.outsider)
        address.verified = False
        address.save(update_fields=['verified'])
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        delivered_messages = []

        def capture_delivery(messages):
            delivered_messages.extend(messages)
            return len(messages)

        with (
            override_settings(**self.production_email_settings()),
            patch(
                'django.core.mail.backends.smtp.EmailBackend.send_messages',
                side_effect=capture_delivery,
            ),
            self.assertNoLogs('SalesLogApp.team_views', level='ERROR'),
        ):
            response = self.client.post(reverse('team_invitation_accept'), {
                'invitation_code': raw,
                'action': 'accept',
            })

        session = self.client.session
        session_record = Session.objects.get(session_key=session.session_key)
        decoded_session = session_record.get_decoded()
        resume_reference = decoded_session[
            INVITATION_RESUME_REFERENCE_SESSION_KEY
        ]
        payload = signing.TimestampSigner(
            salt=INVITATION_VERIFICATION_RESUME_SIGNING_SALT,
        ).unsign_object(
            resume_reference,
            serializer=signing.JSONSerializer,
            max_age=settings.TEAM_INVITATION_VERIFICATION_RESUME_MAX_AGE,
        )
        encoded_raw = base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')
        self.assertEqual(response.status_code, 403)
        self.assertContains(
            response,
            'We’ve sent you a new verification link.',
            status_code=403,
        )
        self.assertNotIn(LEGACY_INVITATION_RESUME_CODE_SESSION_KEY, session)
        self.assertNotIn(INVITATION_REVIEW_REFERENCE_SESSION_KEY, session)
        self.assertIs(session[TEAM_INVITATION_RESUME_SESSION_KEY], True)
        self.assertNotEqual(resume_reference, raw)
        self.assertNotIn(raw, resume_reference)
        self.assertNotIn(encoded_raw, resume_reference)
        self.assertLessEqual(len(resume_reference), 256)
        self.assertEqual(
            set(payload),
            {'version', 'digest'},
        )
        self.assertEqual(
            payload['version'],
            INVITATION_RESUME_REFERENCE_VERSION,
        )
        self.assertEqual(payload['digest'], invitation.token_digest)
        self.assertRegex(payload['digest'], r'\A[0-9a-f]{64}\Z')
        self.assertEqual(
            digest_from_invitation_verification_resume_reference(
                resume_reference
            ),
            invitation.token_digest,
        )
        with self.assertRaises(signing.BadSignature):
            signing.TimestampSigner(
                salt='SalesLogApp.unrelated-purpose',
            ).unsign_object(
                resume_reference,
                serializer=signing.JSONSerializer,
                max_age=settings.TEAM_INVITATION_VERIFICATION_RESUME_MAX_AGE,
            )
        self.assertNotIn(raw, json.dumps(decoded_session, sort_keys=True))
        self.assertNotIn(raw, session_record.session_data)
        self.assertNotIn(encoded_raw, session_record.session_data)
        self.assertEqual(len(delivered_messages), 1)
        message = delivered_messages[0]
        message_text = message.subject + message.body + ' '.join(
            content for content, _ in delivered_messages[0].alternatives
        )
        self.assertIn('/accounts/confirm-email/', message_text)
        self.assertIn('next=', message_text)
        self.assertNotIn(raw, message_text)
        confirmation_url = re.search(
            r'https://stewlog\.com/accounts/confirm-email/[^\s<]+',
            message.body,
        ).group(0)
        self.assertNotIn(raw, confirmation_url)
        self.assertNotIn(resume_reference, response.content.decode())
        self.assertNotIn(resume_reference, message_text)
        ledger_text = json.dumps(list(
            EmailVerificationDispatch.objects.values(
                'user_id', 'recipient_digest', 'source', 'status',
            )
        ), sort_keys=True)
        self.assertNotIn(raw, ledger_text)
        self.assertNotIn(resume_reference, ledger_text)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)
        self.assertFalse(TeamMembership.objects.filter(
            team=self.team,
            user=self.outsider,
            status=TeamMembership.ACTIVE,
        ).exists())

    def test_public_invitation_form_rejects_digest_and_signed_reference(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        reference = create_invitation_verification_resume_reference(raw)
        self.login(self.outsider)
        before = self.verification_business_state()

        for submitted in (invitation.token_digest, reference):
            with self.subTest(submitted_type=(
                'digest' if submitted == invitation.token_digest else 'reference'
            )):
                response = self.client.post(reverse('team_invitation_accept'), {
                    'invitation_code': submitted,
                    'action': 'accept',
                })
                self.assertEqual(response.status_code, 404)
                self.assertNotContains(response, submitted, status_code=404)
                self.assertEqual(self.verification_business_state(), before)

    def test_native_allauth_confirmation_resumes_review_without_raw_token(self):
        cache.clear()
        address = EmailAddress.objects.get(user=self.outsider)
        address.verified = False
        address.save(update_fields=['verified'])
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        delivered_messages = []

        def capture_delivery(messages):
            delivered_messages.extend(messages)
            return len(messages)

        with (
            override_settings(**self.production_email_settings()),
            patch(
                'django.core.mail.backends.smtp.EmailBackend.send_messages',
                side_effect=capture_delivery,
            ),
            self.assertNoLogs('SalesLogApp.team_views', level='ERROR'),
        ):
            verification_required = self.client.post(
                reverse('team_invitation_accept'),
                {'invitation_code': raw, 'action': 'accept'},
            )
            self.assertEqual(verification_required.status_code, 403)
            confirmation_url = re.search(
                r'https://stewlog\.com/accounts/confirm-email/[^\s<]+',
                delivered_messages[0].body,
            ).group(0)
            parsed = urlsplit(confirmation_url)
            self.assertEqual(
                parse_qs(parsed.query).get('next'),
                [reverse('team_invitation_accept')],
            )

            confirmation_page = self.client.get(confirmation_url)
            self.assertEqual(confirmation_page.status_code, 200)
            confirmation = self.client.post(confirmation_url)
            self.assertEqual(confirmation.status_code, 302)
            self.assertEqual(confirmation.url, reverse('team_invitation_accept'))
            address.refresh_from_db()
            self.assertTrue(address.verified)

            review = self.client.get(reverse('team_invitation_accept'))
            self.assertEqual(review.status_code, 200)
            self.assertContains(review, self.team.name)
            self.assertNotContains(review, raw)
            self.assertNotContains(review, invitation.token_digest)
            session = self.client.session
            self.assertNotIn(INVITATION_RESUME_REFERENCE_SESSION_KEY, session)
            self.assertNotIn(TEAM_INVITATION_RESUME_SESSION_KEY, session)
            review_reference = session[
                INVITATION_REVIEW_REFERENCE_SESSION_KEY
            ]
            self.assertEqual(
                digest_from_invitation_review_reference(review_reference),
                invitation.token_digest,
            )
            self.assertNotContains(review, review_reference)
            invitation.refresh_from_db()
            self.assertIsNone(invitation.accepted_at)

            accepted = self.client.post(
                reverse('team_invitation_accept'),
                {'action': 'accept'},
            )
            self.assertEqual(accepted.status_code, 302)

        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)
        self.assertEqual(invitation.accepted_by, self.outsider)
        self.assertTrue(TeamMembership.objects.filter(
            team=self.team,
            user=self.outsider,
            status=TeamMembership.ACTIVE,
        ).exists())
        session = self.client.session
        self.assertNotIn(INVITATION_REVIEW_REFERENCE_SESSION_KEY, session)
        self.assertNotIn(raw, json.dumps(dict(session), sort_keys=True))

    def test_valid_non_raw_resume_can_be_explicitly_declined(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        self.store_resume_reference(
            create_invitation_verification_resume_reference(raw)
        )
        review = self.client.get(reverse('team_invitation_accept'))
        self.assertEqual(review.status_code, 200)
        self.assertNotContains(review, raw)

        declined = self.client.post(
            reverse('team_invitation_accept'),
            {'action': 'decline'},
        )

        self.assertEqual(declined.status_code, 302)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.revoked_at)
        self.assertFalse(TeamMembership.objects.filter(
            team=self.team,
            user=self.outsider,
            status=TeamMembership.ACTIVE,
        ).exists())
        self.assertNotIn(
            INVITATION_REVIEW_REFERENCE_SESSION_KEY,
            self.client.session,
        )

    def test_malformed_or_tampered_resume_references_fail_without_lookup(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        payload = {
            'version': INVITATION_RESUME_REFERENCE_VERSION,
            'digest': invitation.token_digest,
        }
        signer = signing.TimestampSigner(
            salt=INVITATION_VERIFICATION_RESUME_SIGNING_SALT,
        )
        valid_reference = signer.sign_object(
            payload,
            serializer=signing.JSONSerializer,
        )
        payload_parts = valid_reference.split(':')
        payload_parts[0] = (
            ('A' if payload_parts[0][0] != 'A' else 'B')
            + payload_parts[0][1:]
        )
        altered_payload = ':'.join(payload_parts)
        altered_signature = valid_reference[:-1] + (
            'A' if valid_reference[-1] != 'A' else 'B'
        )
        variants = {
            'altered_payload': altered_payload,
            'altered_signature': altered_signature,
            'altered_version': signer.sign_object(
                {'version': 2, 'digest': invitation.token_digest},
                serializer=signing.JSONSerializer,
            ),
            'missing_field': signer.sign_object(
                {'version': INVITATION_RESUME_REFERENCE_VERSION},
                serializer=signing.JSONSerializer,
            ),
            'extra_field': signer.sign_object(
                {**payload, 'invitation': str(invitation.public_id)},
                serializer=signing.JSONSerializer,
            ),
            'malformed_digest': signer.sign_object(
                {**payload, 'digest': 'not-a-digest'},
                serializer=signing.JSONSerializer,
            ),
            'wrong_salt': signing.TimestampSigner(
                salt='SalesLogApp.team-invitation.wrong-purpose',
            ).sign_object(payload, serializer=signing.JSONSerializer),
        }
        before = self.verification_business_state()

        for label, reference in variants.items():
            with self.subTest(label=label):
                self.store_resume_reference(reference)
                session = self.client.session
                session['unrelated_session_state'] = 'preserve-me'
                session.save()
                with (
                    patch(
                        'SalesLogApp.team_views.'
                        'invitation_for_resume_digest_or_404',
                    ) as lookup,
                    self.assertNoLogs(
                        'SalesLogApp.team_views',
                        level='ERROR',
                    ),
                ):
                    response = self.client.get(
                        reverse('team_invitation_accept')
                    )
                lookup.assert_not_called()
                self.assert_generic_resume_failure(response)
                self.assertEqual(
                    self.client.session['unrelated_session_state'],
                    'preserve-me',
                )
                self.assertNotContains(response, raw, status_code=404)
                self.assertNotContains(
                    response,
                    invitation.token_digest,
                    status_code=404,
                )
                self.assertNotContains(response, reference, status_code=404)
                self.assertEqual(self.verification_business_state(), before)

    def test_tampered_review_reference_fails_without_mutation(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        self.store_resume_reference(
            create_invitation_verification_resume_reference(raw)
        )
        review = self.client.get(reverse('team_invitation_accept'))
        self.assertEqual(review.status_code, 200)
        session = self.client.session
        review_reference = session[INVITATION_REVIEW_REFERENCE_SESSION_KEY]
        session[INVITATION_REVIEW_REFERENCE_SESSION_KEY] = (
            review_reference[:-1]
            + ('A' if review_reference[-1] != 'A' else 'B')
        )
        session.save()
        before = self.verification_business_state()

        response = self.client.post(
            reverse('team_invitation_accept'),
            {'action': 'accept'},
        )

        self.assert_generic_resume_failure(response)
        self.assertEqual(self.verification_business_state(), before)
        self.assertNotContains(response, raw, status_code=404)
        self.assertNotContains(
            response,
            invitation.token_digest,
            status_code=404,
        )

    @override_settings(TEAM_INVITATION_VERIFICATION_RESUME_MAX_AGE=60)
    def test_resume_reference_expiry_is_shorter_than_session_and_invitation(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        issued_at = 2_000_000_000
        with patch('django.core.signing.time.time', return_value=issued_at):
            reference = create_invitation_verification_resume_reference(raw)

        self.store_resume_reference(reference)
        self.assertGreater(self.client.session.get_expiry_age(), 60)
        with patch('django.core.signing.time.time', return_value=issued_at):
            immediate = self.client.get(reverse('team_invitation_accept'))
        self.assertEqual(immediate.status_code, 200)
        self.assertContains(immediate, self.team.name)

        self.store_resume_reference(reference)
        with patch(
            'django.core.signing.time.time',
            return_value=issued_at + 61,
        ):
            expired_reference = self.client.get(
                reverse('team_invitation_accept')
            )
        self.assert_generic_resume_failure(expired_reference)

        with patch('django.core.signing.time.time', return_value=issued_at):
            invitation_reference = (
                create_invitation_verification_resume_reference(raw)
            )
        TeamInvitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.store_resume_reference(invitation_reference)
        with patch('django.core.signing.time.time', return_value=issued_at):
            expired_invitation = self.client.get(
                reverse('team_invitation_accept')
            )
        self.assert_generic_resume_failure(expired_invitation)

    def test_invalid_resume_max_age_configuration_fails_closed(self):
        _, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        invalid_values = (
            0,
            True,
            '60',
            settings.TEAMS_INVITATION_TTL_HOURS * 60 * 60 + 1,
            7 * 24 * 60 * 60 + 1,
        )
        for value in invalid_values:
            with (
                self.subTest(value=value),
                override_settings(
                    TEAM_INVITATION_VERIFICATION_RESUME_MAX_AGE=value,
                ),
                self.assertRaises(InvalidInvitationResumeReference),
            ):
                create_invitation_verification_resume_reference(raw)

    def test_lifecycle_and_owner_changes_after_storage_fail_closed(self):
        cases = (
            'consumed',
            'revoked',
            'expired',
            'deleted',
            'recipient_changed',
            'exact_email_unverified',
            'active_membership',
            'wrong_user',
        )

        for index, case in enumerate(cases):
            with self.subTest(case=case):
                user = get_user_model().objects.create_user(
                    username=f'resume-lifecycle-{index}',
                    email=f'resume-lifecycle-{index}@example.com',
                    password='pass',
                )
                invited_address = EmailAddress.objects.create(
                    user=user,
                    email=user.email,
                    verified=True,
                    primary=True,
                )
                invitation, raw = create_invitation(
                    self.owner_membership,
                    user.email,
                )
                reference = create_invitation_verification_resume_reference(raw)
                signed_in_user = user

                if case == 'consumed':
                    accept_invitation(raw, user)
                elif case == 'revoked':
                    TeamInvitation.objects.filter(pk=invitation.pk).update(
                        revoked_at=timezone.now()
                    )
                elif case == 'expired':
                    TeamInvitation.objects.filter(pk=invitation.pk).update(
                        expires_at=timezone.now() - timedelta(seconds=1)
                    )
                elif case == 'deleted':
                    invitation.delete()
                elif case == 'recipient_changed':
                    TeamInvitation.objects.filter(pk=invitation.pk).update(
                        intended_email=f'changed-{index}@example.com'
                    )
                elif case == 'exact_email_unverified':
                    invited_address.verified = False
                    invited_address.save(update_fields=['verified'])
                    EmailAddress.objects.create(
                        user=user,
                        email=f'other-{index}@example.com',
                        verified=True,
                        primary=False,
                    )
                elif case == 'active_membership':
                    TeamMembership.objects.filter(
                        team=self.team,
                        user=user,
                    ).update(status=TeamMembership.ACTIVE)
                elif case == 'wrong_user':
                    signed_in_user = get_user_model().objects.create_user(
                        username=f'wrong-resume-user-{index}',
                        email=f'wrong-resume-user-{index}@example.com',
                        password='pass',
                    )
                    EmailAddress.objects.create(
                        user=signed_in_user,
                        email=signed_in_user.email,
                        verified=True,
                        primary=True,
                    )

                self.client.force_login(signed_in_user)
                self.store_resume_reference(reference)
                before = self.verification_business_state()
                with self.assertNoLogs(
                    'SalesLogApp.team_views',
                    level='ERROR',
                ):
                    response = self.client.get(
                        reverse('team_invitation_accept')
                    )
                self.assert_generic_resume_failure(response)
                self.assertNotContains(response, raw, status_code=404)
                self.assertNotContains(
                    response,
                    invitation.token_digest,
                    status_code=404,
                )
                self.assertEqual(self.verification_business_state(), before)

    def test_consumed_resume_reference_cannot_be_replayed(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        reference = create_invitation_verification_resume_reference(raw)
        self.store_resume_reference(reference)
        review = self.client.get(reverse('team_invitation_accept'))
        self.assertEqual(review.status_code, 200)
        accepted = self.client.post(
            reverse('team_invitation_accept'),
            {'action': 'accept'},
        )
        self.assertEqual(accepted.status_code, 302)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)
        before = self.verification_business_state()

        self.store_resume_reference(reference)
        replay = self.client.get(reverse('team_invitation_accept'))

        self.assert_generic_resume_failure(replay)
        self.assertEqual(self.verification_business_state(), before)
        self.assertNotContains(replay, raw, status_code=404)

    def test_legacy_raw_resume_state_is_rejected_and_cleared(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        self.login(self.outsider)
        session = self.client.session
        session[LEGACY_INVITATION_RESUME_CODE_SESSION_KEY] = raw
        session[TEAM_INVITATION_RESUME_SESSION_KEY] = True
        session['unrelated_session_state'] = 'preserve-me'
        session.save()
        before = self.verification_business_state()

        with self.assertNoLogs('SalesLogApp.team_views', level='ERROR'):
            response = self.client.get(reverse('team_invitation_accept'))

        self.assert_generic_resume_failure(response)
        self.assertEqual(
            self.client.session['unrelated_session_state'],
            'preserve-me',
        )
        self.assertNotContains(response, raw, status_code=404)
        self.assertNotContains(
            response,
            invitation.token_digest,
            status_code=404,
        )
        self.assertEqual(self.verification_business_state(), before)

    def test_unverified_legacy_raw_state_is_cleared_without_resend(self):
        invitation, raw = create_invitation(
            self.owner_membership,
            self.outsider.email,
        )
        EmailAddress.objects.filter(user=self.outsider).update(verified=False)
        self.login(self.outsider)
        session = self.client.session
        session[LEGACY_INVITATION_RESUME_CODE_SESSION_KEY] = raw
        session[TEAM_INVITATION_RESUME_SESSION_KEY] = True
        session['unrelated_session_state'] = 'preserve-me'
        session.save()
        before = self.verification_business_state()

        with (
            patch(
                'SalesLogApp.team_views.'
                'validate_production_email_delivery_configuration',
            ) as preflight,
            patch(
                'SalesLogApp.team_views.dispatch_verification_email',
            ) as dispatch,
            self.assertNoLogs('SalesLogApp.team_views', level='ERROR'),
        ):
            response = self.client.get(reverse('team_invitation_accept'))

        preflight.assert_not_called()
        dispatch.assert_not_called()
        self.assert_generic_resume_failure(response)
        self.assertEqual(
            self.client.session['unrelated_session_state'],
            'preserve-me',
        )
        self.assertNotContains(response, raw, status_code=404)
        self.assertNotContains(
            response,
            invitation.token_digest,
            status_code=404,
        )
        self.assertEqual(self.verification_business_state(), before)

    @override_settings(DEBUG=True)
    def test_unexpected_verification_resend_error_is_sanitized(self):
        EmailAddress.objects.filter(user=self.outsider).update(verified=False)
        _, raw = create_invitation(self.owner_membership, self.outsider.email)
        self.login(self.outsider)

        with (
            patch(
                'SalesLogApp.team_views.dispatch_verification_email',
                side_effect=RuntimeError(
                    f'provider failure {raw} {self.outsider.email}'
                ),
            ),
            self.assertLogs('SalesLogApp.team_views', level='ERROR') as captured,
        ):
            response = self.client.post(reverse('team_invitation_accept'), {
                'invitation_code': raw,
                'action': 'accept',
            })

        logs = '\n'.join(captured.output)
        self.assertEqual(response.status_code, 403)
        self.assertContains(
            response,
            'Please verify your email before joining a team.',
            status_code=403,
        )
        self.assertNotContains(response, raw, status_code=403)
        self.assertNotContains(response, self.outsider.email, status_code=403)
        self.assertNotIn(raw, logs)
        self.assertNotIn(self.outsider.email, logs)

    def test_active_member_cannot_join_a_second_team(self):
        other_owner = get_user_model().objects.create_user(username='other_owner')
        with override_settings(TEAMS_FOUNDER_USER_IDS=[
            str(self.owner.pk), str(other_owner.pk)
        ]):
            other_team = create_team(
                other_owner,
                name='Other Team',
                timezone_name='UTC',
                monthly_unit_goal=None,
                display_mode=Team.RANKED,
            )
            other_owner_membership = TeamMembership.objects.get(
                team=other_team, user=other_owner
            )
            _, first_raw = create_invitation(
                self.owner_membership, self.outsider.email
            )
            second_invitation, second_raw = create_invitation(
                other_owner_membership, self.outsider.email
            )
            accept_invitation(first_raw, self.outsider)
            with self.assertRaises(ValidationError):
                accept_invitation(second_raw, self.outsider)
            second_invitation.refresh_from_db()
            self.assertIsNone(second_invitation.accepted_at)

    def test_sale_activity_uses_only_safe_projection_and_syncs_edits_and_delete(self):
        sale = self.make_sale(marker='SECRET-CUSTOMER-ALPHA')
        activity = TeamActivity.objects.get(sale=sale)
        self.assertEqual(activity.unit_credit, Decimal('1.0'))
        self.login()
        response = self.client.get(reverse('team_detail', args=[self.team.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'SECRET-CUSTOMER-ALPHA')
        self.assertNotContains(response, '7654.32')
        self.assertNotContains(response, '1234.56')
        self.assertNotContains(response, str(sale.dealNumber))
        self.assertFalse(any(isinstance(item, Sale) for item in response.context['activity_page']))
        self.assertContains(response, '5.0% of team goal')

        self.client.force_login(self.owner)
        owner_response = self.client.get(
            reverse('team_detail', args=[self.team.public_id])
        )
        self.assertNotContains(owner_response, 'SECRET-CUSTOMER-ALPHA')
        self.assertNotContains(owner_response, '7654.32')

        sale.count = Decimal('0.5')
        sale.date = sale.date - timedelta(days=1)
        sale.save()
        activity.refresh_from_db()
        self.assertEqual(activity.unit_credit, Decimal('0.5'))
        self.assertEqual(activity.activity_date, sale.date)
        _, _, total = build_month_totals(self.team, self.member.pk)
        self.assertEqual(total, Decimal('0.5'))

        sale.delete()
        activity.refresh_from_db()
        self.assertFalse(activity.is_visible)
        self.assertIsNone(activity.sale)
        _, _, total = build_month_totals(self.team, self.member.pk)
        self.assertEqual(total, Decimal('0'))

    def test_legacy_sharing_preferences_do_not_hide_activity_or_totals(self):
        self.make_sale(count='0.5')
        self.member_membership.sharing_preference = TeamMembership.TOTALS_ONLY
        self.member_membership.save()
        month, rows, total = build_month_totals(self.team, self.member.pk)
        self.assertEqual(total, Decimal('0.5'))
        self.login()
        response = self.client.get(reverse('team_detail', args=[self.team.public_id]))
        self.assertEqual(len(response.context['activity_page']), 1)

        self.member_membership.sharing_preference = TeamMembership.PAUSED
        self.member_membership.save(update_fields=['sharing_preference'])
        _, rows, total = build_month_totals(self.team, self.member.pk)
        self.assertEqual(total, Decimal('0.5'))
        self.assertTrue(any(
            row.public_id == self.member_membership.public_id for row in rows
        ))
        self.assertEqual(build_feed_queryset(self.team).count(), 1)

    def test_legacy_preference_does_not_block_comment_or_reaction(self):
        activity = TeamActivity.objects.get(sale=self.make_sale())
        self.member_membership.sharing_preference = TeamMembership.TOTALS_ONLY
        self.member_membership.save(update_fields=['sharing_preference'])
        self.login()
        comment_url = reverse('team_comment_add', args=[
            self.team.public_id, activity.public_id
        ])
        reaction_url = reverse('team_reaction_toggle', args=[
            self.team.public_id, activity.public_id
        ])
        self.assertEqual(self.client.post(comment_url, {'body': 'visible'}).status_code, 302)
        self.assertEqual(self.client.post(
            reaction_url, {'code': TeamReaction.CELEBRATE}
        ).status_code, 302)
        self.assertTrue(TeamComment.objects.filter(activity=activity).exists())
        self.assertTrue(TeamReaction.objects.filter(activity=activity).exists())

    def test_team_page_has_no_sharing_control_or_management_sections(self):
        self.login()
        response = self.client.get(reverse('team_detail', args=[self.team.public_id]))
        self.assertNotContains(response, 'Your sharing')
        self.assertNotContains(response, 'Update sharing')
        self.assertNotContains(response, 'Invite by email')
        self.assertNotContains(response, '<h3>Members</h3>', html=True)
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.role, TeamMembership.MEMBER)

    def test_team_page_orders_progress_before_activity_and_settings_owns_management(self):
        self.login(self.owner)
        detail = self.client.get(reverse('team_detail', args=[self.team.public_id]))
        content = detail.content.decode()
        self.assertLess(content.index('progress</h3>'), content.index('Team activity'))
        self.assertNotContains(detail, 'Invite by email')
        self.assertContains(detail, 'Team settings')

        settings_page = self.client.get(
            reverse('team_settings', args=[self.team.public_id])
        )
        self.assertContains(settings_page, '<h3>Members</h3>', html=True)
        self.assertContains(settings_page, 'Invite by email')
        self.assertContains(settings_page, 'Pending invitations')

    def test_regular_member_can_open_settings_and_leave_without_management_access(self):
        self.login()
        response = self.client.get(
            reverse('team_settings', args=[self.team.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Leave team')
        self.assertNotContains(response, 'Invite by email')
        self.assertNotContains(response, 'Deactivate team')

    def test_profile_picture_is_used_in_progress_activity_and_member_management(self):
        self.make_sale()
        self.login(self.owner)
        with patch(
            'SalesLogApp.team_services.safe_avatar_url',
            return_value='/media/profile_avatars/member/avatar.png',
        ):
            detail = self.client.get(
                reverse('team_detail', args=[self.team.public_id])
            )
            settings_page = self.client.get(
                reverse('team_settings', args=[self.team.public_id])
            )
        self.assertContains(
            detail, 'src="/media/profile_avatars/member/avatar.png"', count=3
        )
        self.assertContains(
            settings_page,
            'src="/media/profile_avatars/member/avatar.png"',
            count=2,
        )

    def test_sale_saved_with_legacy_totals_only_creates_activity(self):
        self.member_membership.sharing_preference = TeamMembership.TOTALS_ONLY
        self.member_membership.save()
        sale = self.make_sale()
        self.assertTrue(TeamActivity.objects.filter(sale=sale, is_visible=True).exists())
        _, _, total = build_month_totals(self.team, self.member.pk)
        self.assertEqual(total, Decimal('1.0'))

    def test_fractional_month_totals_ties_previous_month_and_authoritative_source(self):
        today = timezone.localdate()
        previous_end = today.replace(day=1)
        previous_month_day = previous_end - timedelta(days=1)
        self.owner_membership.joined_at = timezone.now() - timedelta(days=60)
        self.owner_membership.save(update_fields=['joined_at'])
        self.make_sale(count='0.5', sale_date=previous_month_day)
        self.make_sale(count='1.0', sale_date=previous_month_day)
        self.make_sale(user=self.owner, count='1.5', sale_date=previous_month_day)
        activity = TeamActivity.objects.filter(membership=self.member_membership).first()
        activity.unit_credit = Decimal('99.0')
        activity.save(update_fields=['unit_credit'])
        _, rows, total = build_month_totals(
            self.team, self.member.pk, previous_month_day.strftime('%Y-%m')
        )
        self.assertEqual(total, Decimal('3.0'))
        self.assertEqual([row.rank for row in rows], [1, 1])
        self.assertEqual({row.units for row in rows}, {Decimal('1.5')})

    def test_join_month_includes_full_month_but_excludes_earlier_months(self):
        self.member_membership.joined_at = datetime(
            2026, 8, 10, 12, tzinfo=ZoneInfo(self.team.timezone)
        )
        self.member_membership.save(update_fields=['joined_at'])
        self.make_sale(sale_date=date(2026, 8, 6))
        self.make_sale(count='2.0', sale_date=date(2026, 7, 31))

        _, rows, total = build_month_totals(self.team, self.member.pk, '2026-08')
        member_row = next(row for row in rows if row.public_id == self.member_membership.public_id)
        self.assertEqual(member_row.units, Decimal('1.0'))
        self.assertEqual(total, Decimal('1.0'))

        _, rows, total = build_month_totals(self.team, self.member.pk, '2026-07')
        member_row = next(row for row in rows if row.public_id == self.member_membership.public_id)
        self.assertEqual(member_row.units, Decimal('0'))
        self.assertEqual(total, Decimal('0'))

    def test_comment_lifecycle_escaping_limits_and_moderation(self):
        activity = TeamActivity.objects.get(sale=self.make_sale())
        self.login()
        add_url = reverse('team_comment_add', args=[self.team.public_id, activity.public_id])
        self.assertEqual(self.client.post(add_url, {
            'body': '<script>alert("comment")</script>'
        }).status_code, 302)
        comment = TeamComment.objects.get(activity=activity)
        response = self.client.get(reverse('team_detail', args=[self.team.public_id]))
        self.assertContains(response, '&lt;script&gt;', html=False)
        self.assertNotContains(response, '<script>alert')
        self.client.post(add_url, {'body': 'x' * 501})
        self.assertEqual(TeamComment.objects.filter(activity=activity).count(), 1)

        self.client.force_login(self.outsider)
        other_team_member = TeamMembership.objects.create(
            team=self.team,
            user=self.outsider,
            status=TeamMembership.ACTIVE,
            joined_at=timezone.now(),
        )
        edit_url = reverse('team_comment_edit', args=[
            self.team.public_id, activity.public_id, comment.public_id
        ])
        self.assertEqual(self.client.post(edit_url, {'body': 'hijack'}).status_code, 404)

        self.client.force_login(self.owner)
        hide_url = reverse('team_comment_hide', args=[
            self.team.public_id, activity.public_id, comment.public_id
        ])
        self.assertEqual(self.client.post(hide_url).status_code, 302)
        comment.refresh_from_db()
        self.assertEqual(comment.body, '')
        self.assertEqual(comment.moderated_by, self.owner_membership)

    def test_member_can_edit_and_delete_own_comment(self):
        activity = TeamActivity.objects.get(sale=self.make_sale())
        comment = TeamComment.objects.create(
            activity=activity,
            author_membership=self.member_membership,
            body='first',
        )
        self.login()
        edit_url = reverse('team_comment_edit', args=[
            self.team.public_id, activity.public_id, comment.public_id
        ])
        self.client.post(edit_url, {'body': 'second'})
        comment.refresh_from_db()
        self.assertEqual(comment.body, 'second')
        self.assertIsNotNone(comment.edited_at)
        delete_url = reverse('team_comment_delete', args=[
            self.team.public_id, activity.public_id, comment.public_id
        ])
        self.client.post(delete_url)
        comment.refresh_from_db()
        self.assertEqual(comment.body, '')
        self.assertIsNotNone(comment.deleted_at)

    def test_reaction_codes_are_curated_unique_and_toggle(self):
        activity = TeamActivity.objects.get(sale=self.make_sale())
        self.login()
        url = reverse('team_reaction_toggle', args=[self.team.public_id, activity.public_id])
        self.assertEqual(self.client.post(url, {'code': TeamReaction.ON_FIRE}).status_code, 302)
        self.assertEqual(TeamReaction.objects.filter(activity=activity).count(), 1)
        self.assertEqual(self.client.post(url, {'code': TeamReaction.ON_FIRE}).status_code, 302)
        self.assertEqual(TeamReaction.objects.filter(activity=activity).count(), 0)
        self.assertEqual(self.client.post(url, {'code': 'arbitrary_emoji'}).status_code, 400)
        TeamReaction.objects.create(
            activity=activity, membership=self.member_membership, code=TeamReaction.APPLAUSE
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeamReaction.objects.create(
                    activity=activity,
                    membership=self.member_membership,
                    code=TeamReaction.APPLAUSE,
                )

    def test_cross_team_routes_do_not_disclose_or_mutate_objects(self):
        activity = TeamActivity.objects.get(sale=self.make_sale())
        other_owner = get_user_model().objects.create_user(username='isolated_owner')
        other_team = Team.objects.create(owner=other_owner, name='Isolated', timezone='UTC')
        TeamMembership.objects.create(
            team=other_team,
            user=other_owner,
            role=TeamMembership.OWNER,
            status=TeamMembership.ACTIVE,
            joined_at=timezone.now(),
        )
        self.client.force_login(other_owner)
        self.assertEqual(self.client.get(
            reverse('team_detail', args=[self.team.public_id])
        ).status_code, 404)
        self.assertEqual(self.client.post(
            reverse('team_reaction_toggle', args=[self.team.public_id, activity.public_id]),
            {'code': TeamReaction.CELEBRATE},
        ).status_code, 404)
        self.assertEqual(TeamReaction.objects.count(), 0)

    def test_mutating_routes_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.member)
        activity = TeamActivity.objects.get(sale=self.make_sale())
        response = csrf_client.post(reverse('team_comment_add', args=[
            self.team.public_id, activity.public_id,
        ]), {'body': 'blocked'})
        self.assertEqual(response.status_code, 403)

    def test_role_remove_leave_and_deactivate_lifecycle(self):
        self.login(self.owner)
        role_url = reverse('team_member_role', args=[
            self.team.public_id, self.member_membership.public_id, 'promote'
        ])
        self.client.post(role_url)
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.role, TeamMembership.ADMIN)
        role_url = reverse('team_member_role', args=[
            self.team.public_id, self.member_membership.public_id, 'demote'
        ])
        self.client.post(role_url)
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.role, TeamMembership.MEMBER)

        sale = self.make_sale()
        activity = TeamActivity.objects.get(sale=sale)
        remove_url = reverse('team_member_remove', args=[
            self.team.public_id, self.member_membership.public_id
        ])
        self.client.post(remove_url)
        self.member_membership.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(self.member_membership.status, TeamMembership.REMOVED)
        self.assertFalse(activity.is_visible)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(
            reverse('team_detail', args=[self.team.public_id])
        ).status_code, 404)

        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(
            reverse('team_leave', args=[self.team.public_id])
        ).status_code, 400)
        self.assertEqual(self.client.post(
            reverse('team_deactivate', args=[self.team.public_id])
        ).status_code, 302)
        self.team.refresh_from_db()
        self.assertFalse(self.team.is_active)
        self.assertTrue(Sale.objects.filter(pk=sale.pk).exists())

    def test_admin_cannot_modify_owner(self):
        self.member_membership.role = TeamMembership.ADMIN
        self.member_membership.save(update_fields=['role'])
        self.login()
        url = reverse('team_member_remove', args=[
            self.team.public_id, self.owner_membership.public_id
        ])
        self.assertEqual(self.client.post(url).status_code, 403)

    def test_transfer_is_explicit_and_requires_pro_target(self):
        self.login(self.owner)
        url = reverse('team_transfer_ownership', args=[
            self.team.public_id, self.member_membership.public_id
        ])
        self.assertEqual(self.client.post(url).status_code, 403)
        with override_settings(TEAMS_FOUNDER_USER_IDS=[
            str(self.owner.pk), str(self.member.pk)
        ]):
            self.assertEqual(self.client.post(url).status_code, 302)
        self.team.refresh_from_db()
        self.owner_membership.refresh_from_db()
        self.member_membership.refresh_from_db()
        self.assertEqual(self.team.owner, self.member)
        self.assertEqual(self.member_membership.role, TeamMembership.OWNER)
        self.assertEqual(self.owner_membership.role, TeamMembership.ADMIN)

    def test_owner_entitlement_loss_makes_management_read_only_without_data_loss(self):
        activity = TeamActivity.objects.get(sale=self.make_sale())
        comment = TeamComment.objects.create(
            activity=activity,
            author_membership=self.member_membership,
            body='preserve me',
        )
        TeamReaction.objects.create(
            activity=activity,
            membership=self.owner_membership,
            code=TeamReaction.STRONG_WORK,
        )
        self.client.force_login(self.owner)
        with override_settings(TEAMS_FOUNDER_USER_IDS=[]):
            response = self.client.get(reverse('team_detail', args=[self.team.public_id]))
            self.assertContains(response, 'Team management is read-only')
            self.assertEqual(self.client.post(
                reverse('team_invite', args=[self.team.public_id]),
                {'username': self.outsider.username},
            ).status_code, 403)
            settings_response = self.client.get(
                reverse('team_settings', args=[self.team.public_id])
            )
            self.assertContains(settings_response, 'Team management is read-only')
        self.assertTrue(Team.objects.filter(pk=self.team.pk, is_active=True).exists())
        self.assertTrue(TeamComment.objects.filter(pk=comment.pk).exists())
        self.assertEqual(TeamReaction.objects.filter(activity=activity).count(), 1)

    def test_feed_paginates_and_ignores_private_search_terms(self):
        for index in range(26):
            self.make_sale(marker=f'NEVER-LEAK-{index}')
        self.login()
        response = self.client.get(
            reverse('team_detail', args=[self.team.public_id]),
            {'q': 'NEVER-LEAK-25'},
        )
        self.assertEqual(len(response.context['activity_page']), 25)
        self.assertTrue(response.context['activity_page'].has_next())
        self.assertNotContains(response, 'NEVER-LEAK')

    def test_feed_projection_query_count_is_constant(self):
        activities = []
        for _ in range(8):
            activities.append(TeamActivity.objects.get(sale=self.make_sale()))
        for activity in activities:
            TeamComment.objects.create(
                activity=activity,
                author_membership=self.member_membership,
                body='safe',
            )
            TeamReaction.objects.create(
                activity=activity,
                membership=self.owner_membership,
                code=TeamReaction.GREAT_JOB,
            )
        with self.assertNumQueries(3):
            projected = [
                project_activity(item, self.member_membership)
                for item in build_feed_queryset(self.team)
            ]
            self.assertEqual(len(projected), 8)
