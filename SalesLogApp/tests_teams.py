from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch
from zoneinfo import ZoneInfo

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from SalesLog.settings import env_strict_bool

from .models import (
    Commission,
    Sale,
    Team,
    TeamActivity,
    TeamComment,
    TeamInvitation,
    TeamMembership,
    TeamReaction,
)
from .team_services import (
    InvitationDeliveryError,
    accept_invitation,
    build_feed_queryset,
    build_month_totals,
    create_and_email_invitation,
    create_invitation,
    create_team,
    invitation_for_user_or_404,
    project_activity,
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
        }).status_code, 404)
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
        }).status_code, 404)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)

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

    def test_totals_only_hides_activity_and_paused_hides_all_sharing(self):
        self.make_sale(count='0.5')
        self.member_membership.sharing_preference = TeamMembership.TOTALS_ONLY
        self.member_membership.save()
        month, rows, total = build_month_totals(self.team, self.member.pk)
        self.assertEqual(total, Decimal('0.5'))
        self.login()
        response = self.client.get(reverse('team_detail', args=[self.team.public_id]))
        self.assertEqual(len(response.context['activity_page']), 0)

        self.client.post(reverse('team_sharing', args=[self.team.public_id]), {
            'sharing_preference': TeamMembership.PAUSED,
        })
        _, rows, total = build_month_totals(self.team, self.member.pk)
        self.assertEqual(total, Decimal('0'))
        self.assertFalse(any(row.public_id == self.member_membership.public_id for row in rows))

    def test_hidden_activity_is_inaccessible_by_direct_comment_or_reaction_url(self):
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
        self.assertEqual(self.client.post(comment_url, {'body': 'hidden'}).status_code, 404)
        self.assertEqual(self.client.post(
            reaction_url, {'code': TeamReaction.CELEBRATE}
        ).status_code, 404)
        self.assertFalse(TeamComment.objects.filter(activity=activity).exists())
        self.assertFalse(TeamReaction.objects.filter(activity=activity).exists())

    def test_sharing_request_cannot_forge_role(self):
        self.login()
        self.client.post(reverse('team_sharing', args=[self.team.public_id]), {
            'sharing_preference': TeamMembership.TOTALS_ONLY,
            'role': TeamMembership.OWNER,
            'status': TeamMembership.ACTIVE,
        })
        self.member_membership.refresh_from_db()
        self.assertEqual(self.member_membership.role, TeamMembership.MEMBER)
        self.assertEqual(
            self.member_membership.sharing_preference,
            TeamMembership.TOTALS_ONLY,
        )

    def test_sale_saved_while_totals_only_does_not_create_activity(self):
        self.member_membership.sharing_preference = TeamMembership.TOTALS_ONLY
        self.member_membership.save()
        sale = self.make_sale()
        self.assertFalse(TeamActivity.objects.filter(sale=sale).exists())
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
        response = csrf_client.post(reverse('team_sharing', args=[self.team.public_id]), {
            'sharing_preference': TeamMembership.PAUSED,
        })
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
