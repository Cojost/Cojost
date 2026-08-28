import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Prefetch, Q, Sum
from django.http import Http404
from django.utils import timezone

from .models import (
    Sale,
    Team,
    TeamActivity,
    TeamComment,
    TeamInvitation,
    TeamMembership,
    TeamReaction,
)
from .team_entitlements import can_create_team, get_team_entitlement


REACTION_EMOJI = {
    TeamReaction.CELEBRATE: '🎉',
    TeamReaction.ON_FIRE: '🔥',
    TeamReaction.APPLAUSE: '👏',
    TeamReaction.STRONG_WORK: '💪',
    TeamReaction.GREAT_JOB: '⭐',
}


class InvitationDeliveryError(Exception):
    """Raised when an invitation cannot be delivered without leaking details."""


class InvalidInvitationResumeReference(Exception):
    """Raised for an invalid non-raw invitation resume reference."""


INVITATION_RESUME_REFERENCE_VERSION = 1
INVITATION_VERIFICATION_RESUME_SIGNING_SALT = (
    'SalesLogApp.team-invitation.email-verification-resume.v1'
)
INVITATION_REVIEW_SIGNING_SALT = (
    'SalesLogApp.team-invitation.review-action.v1'
)
INVITATION_REFERENCE_MAX_LENGTH = 256
_TOKEN_DIGEST_PATTERN = re.compile(r'\A[0-9a-f]{64}\Z')


@dataclass(frozen=True)
class TeamView:
    public_id: object
    name: str
    timezone: str
    monthly_unit_goal: Decimal | None
    display_mode: str


@dataclass(frozen=True)
class MemberView:
    public_id: object
    display_name: str
    initial: str
    avatar_url: str
    role: str
    role_label: str
    is_self: bool


@dataclass(frozen=True)
class CommentView:
    public_id: object
    author_name: str
    body: str
    created_at: datetime
    was_edited: bool
    can_edit: bool
    can_delete: bool
    can_moderate: bool


@dataclass(frozen=True)
class ReactionView:
    code: str
    label: str
    emoji: str
    count: int
    reacted_by_me: bool


@dataclass(frozen=True)
class ActivityView:
    public_id: object
    actor_name: str
    actor_initial: str
    actor_avatar_url: str
    unit_credit: Decimal
    activity_date: date
    comments: tuple
    reactions: tuple


@dataclass(frozen=True)
class MemberTotalView:
    public_id: object
    display_name: str
    initial: str
    avatar_url: str
    units: Decimal
    rank: int | None
    is_self: bool


@dataclass(frozen=True)
class InvitationView:
    public_id: object
    display_name: str
    token_prefix: str
    expires_at: datetime


def safe_display_name(user):
    return user.get_full_name().strip() or user.username


def safe_avatar_url(user):
    try:
        profile = user.sales_profile
    except ObjectDoesNotExist:
        return ''
    if not profile.avatar_available:
        return ''
    try:
        return profile.avatar.url
    except (OSError, ValueError):
        return ''


def as_team_view(team):
    return TeamView(
        public_id=team.public_id,
        name=team.name,
        timezone=team.timezone,
        monthly_unit_goal=team.monthly_unit_goal,
        display_mode=team.display_mode,
    )


def as_member_view(membership, current_user_id):
    name = safe_display_name(membership.user)
    return MemberView(
        public_id=membership.public_id,
        display_name=name,
        initial=(name[:1] or '?').upper(),
        avatar_url=safe_avatar_url(membership.user),
        role=membership.role,
        role_label=membership.get_role_display(),
        is_self=membership.user_id == current_user_id,
    )


def teams_are_enabled():
    return settings.TEAMS_FEATURE_ENABLED


def active_membership_for_user(user):
    return (
        TeamMembership.objects.select_related('team', 'team__owner')
        .filter(user=user, status=TeamMembership.ACTIVE, team__is_active=True)
        .first()
    )


def get_team_membership_or_404(user, team_public_id):
    try:
        return (
            TeamMembership.objects.select_related('team', 'team__owner', 'user')
            .get(
                user=user,
                status=TeamMembership.ACTIVE,
                team__public_id=team_public_id,
                team__is_active=True,
            )
        )
    except TeamMembership.DoesNotExist as exc:
        raise Http404('Team not found.') from exc


def team_is_effectively_read_only(team):
    return team.is_read_only or not get_team_entitlement(team.owner).has_pro_access


def require_management(membership, *, owner_only=False):
    if membership.status != TeamMembership.ACTIVE:
        raise Http404('Team not found.')
    allowed_roles = {TeamMembership.OWNER}
    if not owner_only:
        allowed_roles.add(TeamMembership.ADMIN)
    if membership.role not in allowed_roles:
        raise PermissionDenied
    if team_is_effectively_read_only(membership.team):
        raise PermissionDenied


@transaction.atomic
def create_team(user, *, name, timezone_name, monthly_unit_goal, display_mode):
    if not can_create_team(user):
        raise PermissionDenied
    get_user_model().objects.select_for_update().get(pk=user.pk)
    if Team.objects.filter(owner=user, is_active=True).exists():
        raise ValidationError('You can own only one active team.')
    if TeamMembership.objects.select_for_update().filter(
        user=user,
        status=TeamMembership.ACTIVE,
        team__is_active=True,
    ).exists():
        raise ValidationError('Leave your current team before creating another one.')
    team = Team(
        owner=user,
        name=name,
        timezone=timezone_name,
        monthly_unit_goal=monthly_unit_goal,
        display_mode=display_mode,
    )
    team.full_clean()
    team.save()
    TeamMembership.objects.create(
        team=team,
        user=user,
        role=TeamMembership.OWNER,
        status=TeamMembership.ACTIVE,
        joined_at=timezone.now(),
    )
    return team


def _token_digest(raw_token):
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        raw_token.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def _resume_reference_max_age():
    value = getattr(
        settings,
        'TEAM_INVITATION_VERIFICATION_RESUME_MAX_AGE',
        None,
    )
    invitation_lifetime = settings.TEAMS_INVITATION_TTL_HOURS * 60 * 60
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > 7 * 24 * 60 * 60
        or value > invitation_lifetime
    ):
        raise InvalidInvitationResumeReference(
            'The invitation resume reference is invalid.'
        )
    return value


def _validated_token_digest(token_digest):
    if (
        not isinstance(token_digest, str)
        or not _TOKEN_DIGEST_PATTERN.fullmatch(token_digest)
    ):
        raise InvalidInvitationResumeReference(
            'The invitation resume reference is invalid.'
        )
    return token_digest


def _sign_invitation_reference(token_digest, *, salt):
    _resume_reference_max_age()
    payload = {
        'version': INVITATION_RESUME_REFERENCE_VERSION,
        'digest': _validated_token_digest(token_digest),
    }
    return signing.TimestampSigner(salt=salt).sign_object(
        payload,
        serializer=signing.JSONSerializer,
        compress=False,
    )


def _digest_from_invitation_reference(reference, *, salt):
    if (
        not isinstance(reference, str)
        or not reference
        or len(reference) > INVITATION_REFERENCE_MAX_LENGTH
    ):
        raise InvalidInvitationResumeReference(
            'The invitation resume reference is invalid.'
        )
    try:
        payload = signing.TimestampSigner(salt=salt).unsign_object(
            reference,
            serializer=signing.JSONSerializer,
            max_age=_resume_reference_max_age(),
        )
    except (signing.BadSignature, TypeError, ValueError):
        raise InvalidInvitationResumeReference(
            'The invitation resume reference is invalid.'
        ) from None
    if (
        type(payload) is not dict
        or set(payload) != {'version', 'digest'}
        or type(payload['version']) is not int
        or payload['version'] != INVITATION_RESUME_REFERENCE_VERSION
    ):
        raise InvalidInvitationResumeReference(
            'The invitation resume reference is invalid.'
        )
    return _validated_token_digest(payload['digest'])


def create_invitation_verification_resume_reference(raw_token):
    """Return a timestamped reference containing only the canonical digest."""
    return _sign_invitation_reference(
        _token_digest(raw_token),
        salt=INVITATION_VERIFICATION_RESUME_SIGNING_SALT,
    )


def digest_from_invitation_verification_resume_reference(reference):
    return _digest_from_invitation_reference(
        reference,
        salt=INVITATION_VERIFICATION_RESUME_SIGNING_SALT,
    )


def create_invitation_review_reference(token_digest):
    return _sign_invitation_reference(
        token_digest,
        salt=INVITATION_REVIEW_SIGNING_SALT,
    )


def digest_from_invitation_review_reference(reference):
    return _digest_from_invitation_reference(
        reference,
        salt=INVITATION_REVIEW_SIGNING_SALT,
    )


@transaction.atomic
def create_invitation(membership, intended_email):
    require_management(membership)
    team = Team.objects.select_for_update().get(pk=membership.team_id)
    if not team.is_active:
        raise Http404('Team not found.')
    normalized_email = intended_email.strip().lower()
    if not normalized_email:
        raise ValidationError('Enter an email address.')
    validate_email(normalized_email)

    verified_address = (
        EmailAddress.objects.select_related('user')
        .filter(
            email__iexact=normalized_email,
            verified=True,
        )
        .order_by('pk')
        .first()
    )
    intended_user = verified_address.user if verified_address else None
    if intended_user and intended_user.pk == team.owner_id:
        raise ValidationError('That account already belongs to this team.')
    if intended_user and TeamMembership.objects.select_for_update().filter(
        user=intended_user,
        status=TeamMembership.ACTIVE,
        team__is_active=True,
    ).exists():
        raise ValidationError('That account already belongs to a team.')

    if intended_user and not EmailAddress.objects.filter(
        user=intended_user,
        email__iexact=normalized_email,
        verified=True,
    ).exists():
        raise ValidationError('Use a verified email address for that account.')

    if intended_user:
        pending_membership, _ = TeamMembership.objects.get_or_create(
            team=team,
            user=intended_user,
            defaults={
                'role': TeamMembership.MEMBER,
                'status': TeamMembership.INVITED,
            },
        )
        if pending_membership.status == TeamMembership.ACTIVE:
            raise ValidationError('That account already belongs to this team.')
        pending_membership.role = TeamMembership.MEMBER
        pending_membership.status = TeamMembership.INVITED
        pending_membership.joined_at = None
        pending_membership.save(
            update_fields=['role', 'status', 'joined_at', 'updated_at']
        )

    TeamInvitation.objects.filter(
        team=team,
        intended_email__iexact=normalized_email,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())
    raw_token = secrets.token_urlsafe(32)
    invitation = TeamInvitation.objects.create(
        team=team,
        intended_user=intended_user,
        intended_email=normalized_email,
        token_digest=_token_digest(raw_token),
        token_prefix=raw_token[:10],
        expires_at=timezone.now() + timedelta(hours=settings.TEAMS_INVITATION_TTL_HOURS),
        created_by=membership.user,
    )
    return invitation, raw_token


def _invitation_email_body(
    *, team, inviter_name, raw_token, signup_url, login_url, teams_url
):
    return (
        f'{inviter_name} invited you to join {team.name} on STEW Log.\n\n'
        'By joining, your profile name and picture, sale dates, and unit '
        'credits will be visible to active team members. Customer names, '
        'deal numbers, gross, commission, and pay-plan details remain private.\n\n'
        'If you do not have an account, register here:\n'
        f'{signup_url}\n\n'
        'If you already have an account, sign in here:\n'
        f'{login_url}\n\n'
        'Verify this email address, then open Teams and enter the one-time '
        'invitation code below. The code is intentionally not included in a URL.\n\n'
        f'Invitation code: {raw_token}\n\n'
        f'Teams: {teams_url}\n\n'
        'This invitation expires automatically. If you were not expecting it, '
        'you can ignore this email.'
    )


@transaction.atomic
def create_and_email_invitation(
    membership,
    intended_email,
    *,
    signup_url,
    login_url,
    teams_url,
):
    invitation, raw_token = create_invitation(membership, intended_email)
    try:
        sent = send_mail(
            subject=f'Invitation to join {invitation.team.name} on STEW Log',
            message=_invitation_email_body(
                team=invitation.team,
                inviter_name=safe_display_name(membership.user),
                raw_token=raw_token,
                signup_url=signup_url,
                login_url=login_url,
                teams_url=teams_url,
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[invitation.intended_email],
            fail_silently=False,
        )
    except Exception as exc:
        raise InvitationDeliveryError(
            'The invitation email could not be sent. No invitation was created.'
        ) from exc
    if sent != 1:
        raise InvitationDeliveryError(
            'The invitation email could not be sent. No invitation was created.'
        )
    return invitation, raw_token


def _invitation_for_digest_or_404(token_digest, user, *, lock=False):
    queryset = TeamInvitation.objects.select_related('team', 'intended_user')
    if lock:
        queryset = queryset.select_for_update(of=('self',))
    try:
        invitation = queryset.get(token_digest=token_digest)
    except TeamInvitation.DoesNotExist as exc:
        raise Http404('Invitation not found.') from exc
    now = timezone.now()
    if (
        not invitation.team.is_active
        or invitation.revoked_at is not None
        or invitation.accepted_at is not None
        or invitation.expires_at <= now
    ):
        raise Http404('Invitation not found.')
    if (
        invitation.intended_user_id is not None
        and invitation.intended_user_id != user.pk
    ):
        raise Http404('Invitation not found.')
    if invitation.intended_email and not EmailAddress.objects.filter(
        user=user,
        email__iexact=invitation.intended_email,
        verified=True,
    ).exists():
        raise Http404('Invitation not found.')
    if TeamMembership.objects.filter(
        team=invitation.team,
        user=user,
        status=TeamMembership.ACTIVE,
    ).exists():
        raise Http404('Invitation not found.')
    return invitation


def invitation_for_user_or_404(raw_token, user, *, lock=False):
    return _invitation_for_digest_or_404(
        _token_digest(raw_token),
        user,
        lock=lock,
    )


def invitation_for_resume_digest_or_404(token_digest, user, *, lock=False):
    try:
        token_digest = _validated_token_digest(token_digest)
    except InvalidInvitationResumeReference as exc:
        raise Http404('Invitation not found.') from exc
    return _invitation_for_digest_or_404(token_digest, user, lock=lock)


def _accept_locked_invitation(invitation, user):
    get_user_model().objects.select_for_update().get(pk=user.pk)
    Team.objects.select_for_update().get(pk=invitation.team_id)
    if TeamMembership.objects.select_for_update().filter(
        user=user,
        status=TeamMembership.ACTIVE,
        team__is_active=True,
    ).exclude(team=invitation.team).exists():
        raise ValidationError('Leave your current team before joining another one.')
    membership, _ = TeamMembership.objects.select_for_update().get_or_create(
        team=invitation.team,
        user=user,
        defaults={
            'role': TeamMembership.MEMBER,
            'status': TeamMembership.INVITED,
        },
    )
    membership.status = TeamMembership.ACTIVE
    membership.role = TeamMembership.MEMBER
    membership.joined_at = timezone.now()
    membership.save(update_fields=['status', 'role', 'joined_at', 'updated_at'])
    invitation.intended_user = user
    invitation.accepted_by = user
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=['intended_user', 'accepted_by', 'accepted_at'])
    TeamInvitation.objects.filter(
        team=invitation.team,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
    ).filter(
        Q(intended_user=user)
        | Q(intended_email__iexact=invitation.intended_email)
    ).exclude(pk=invitation.pk).update(revoked_at=timezone.now())
    return membership


@transaction.atomic
def accept_invitation(raw_token, user):
    invitation = invitation_for_user_or_404(raw_token, user, lock=True)
    return _accept_locked_invitation(invitation, user)


@transaction.atomic
def accept_invitation_from_resume_digest(token_digest, user):
    invitation = invitation_for_resume_digest_or_404(
        token_digest,
        user,
        lock=True,
    )
    return _accept_locked_invitation(invitation, user)


def _decline_locked_invitation(invitation, user):
    invitation.intended_user = user
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=['intended_user', 'revoked_at'])
    TeamMembership.objects.filter(
        team=invitation.team,
        user=user,
        status=TeamMembership.INVITED,
    ).update(status=TeamMembership.DECLINED)


@transaction.atomic
def decline_invitation(raw_token, user):
    invitation = invitation_for_user_or_404(raw_token, user, lock=True)
    _decline_locked_invitation(invitation, user)


@transaction.atomic
def decline_invitation_from_resume_digest(token_digest, user):
    invitation = invitation_for_resume_digest_or_404(
        token_digest,
        user,
        lock=True,
    )
    _decline_locked_invitation(invitation, user)


@transaction.atomic
def revoke_invitation(actor_membership, invitation_public_id):
    require_management(actor_membership)
    try:
        invitation = TeamInvitation.objects.select_for_update().get(
            public_id=invitation_public_id,
            team=actor_membership.team,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        )
    except TeamInvitation.DoesNotExist as exc:
        raise Http404('Invitation not found.') from exc
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=['revoked_at'])
    if invitation.intended_user_id:
        TeamMembership.objects.filter(
            team=invitation.team,
            user=invitation.intended_user,
            status=TeamMembership.INVITED,
        ).update(status=TeamMembership.REMOVED)


def sync_sale_activity(sale):
    if not settings.TEAMS_FEATURE_ENABLED:
        return
    membership = (
        TeamMembership.objects.select_related('team')
        .filter(
            user_id=sale.user_id,
            status=TeamMembership.ACTIVE,
            team__is_active=True,
        )
        .first()
    )
    if not membership:
        TeamActivity.objects.filter(sale=sale).update(is_visible=False)
        return
    TeamActivity.objects.update_or_create(
        sale=sale,
        defaults={
            'team': membership.team,
            'membership': membership,
            'activity_type': TeamActivity.SALE,
            'unit_credit': sale.unit_credit,
            'activity_date': sale.date,
            'is_visible': True,
        },
    )


def withdraw_sale_activity(sale):
    TeamActivity.objects.filter(sale=sale).update(is_visible=False)


def _month_bounds(team, month_value=None):
    team_zone = ZoneInfo(team.timezone)
    current_local = timezone.now().astimezone(team_zone).date().replace(day=1)
    if month_value:
        try:
            selected = datetime.strptime(month_value, '%Y-%m').date().replace(day=1)
        except ValueError:
            selected = current_local
    else:
        selected = current_local
    if selected > current_local:
        selected = current_local
    if selected.month == 12:
        month_end = date(selected.year + 1, 1, 1)
    else:
        month_end = date(selected.year, selected.month + 1, 1)
    return selected, month_end


def build_month_totals(team, current_user_id, month_value=None):
    month_start, month_end = _month_bounds(team, month_value)
    memberships = list(
        TeamMembership.objects.select_related('user', 'user__sales_profile')
        .filter(team=team, status=TeamMembership.ACTIVE)
    )
    eligible = Q(pk__in=[])
    by_user = {}
    team_zone = ZoneInfo(team.timezone)
    for membership in memberships:
        joined_date = (
            membership.joined_at.astimezone(team_zone).date()
            if membership.joined_at
            else month_start
        )
        joined_month = joined_date.replace(day=1)
        # Joining authorizes the member's full month-to-date unit total. This
        # matches the leaderboard's "where are we this month?" purpose while
        # still excluding reporting months that predate membership.
        if month_start >= joined_month:
            eligible |= Q(user_id=membership.user_id, date__gte=month_start)
        by_user[membership.user_id] = membership
    totals = {
        row['user_id']: row['units'] or Decimal('0')
        for row in Sale.objects.filter(
            eligible,
            date__lt=month_end,
        ).values('user_id').annotate(units=Sum('count'))
    } if memberships else {}
    rows = []
    for membership in memberships:
        name = safe_display_name(membership.user)
        rows.append({
            'membership': membership,
            'name': name,
            'units': totals.get(membership.user_id, Decimal('0')),
        })
    ranked = sorted(rows, key=lambda row: (-row['units'], row['name'].casefold(), row['membership'].user_id))
    rank_by_member = {}
    previous_units = None
    current_rank = 0
    for index, row in enumerate(ranked, start=1):
        if previous_units is None or row['units'] != previous_units:
            current_rank = index
            previous_units = row['units']
        rank_by_member[row['membership'].pk] = current_rank
    if team.display_mode == Team.ALPHABETICAL:
        rows.sort(key=lambda row: (row['name'].casefold(), row['membership'].user_id))
    else:
        rows = ranked
    result = tuple(
        MemberTotalView(
            public_id=row['membership'].public_id,
            display_name=row['name'],
            initial=(row['name'][:1] or '?').upper(),
            avatar_url=safe_avatar_url(row['membership'].user),
            units=row['units'],
            rank=rank_by_member[row['membership'].pk] if team.display_mode == Team.RANKED else None,
            is_self=row['membership'].user_id == current_user_id,
        )
        for row in rows
    )
    team_total = sum((row.units for row in result), Decimal('0'))
    return month_start, result, team_total


def build_feed_queryset(team):
    visible_comments = TeamComment.objects.select_related(
        'author_membership__user',
    ).filter(deleted_at__isnull=True, moderated_at__isnull=True)
    return (
        TeamActivity.objects.filter(
            team=team,
            is_visible=True,
            membership__status=TeamMembership.ACTIVE,
        )
        .select_related('membership__user', 'membership__user__sales_profile')
        .prefetch_related(
            Prefetch('comments', queryset=visible_comments, to_attr='visible_comments'),
            'reactions',
        )
    )


def project_activity(activity, current_membership):
    actor_name = safe_display_name(activity.membership.user)
    can_moderate = current_membership.role in {
        TeamMembership.OWNER,
        TeamMembership.ADMIN,
    }
    comments = tuple(
        CommentView(
            public_id=comment.public_id,
            author_name=safe_display_name(comment.author_membership.user),
            body=comment.body,
            created_at=comment.created_at,
            was_edited=comment.edited_at is not None,
            can_edit=comment.author_membership_id == current_membership.pk,
            can_delete=comment.author_membership_id == current_membership.pk,
            can_moderate=(
                can_moderate
                and comment.author_membership_id != current_membership.pk
            ),
        )
        for comment in activity.visible_comments
    )
    reaction_counts = {code: 0 for code, _ in TeamReaction.CODE_CHOICES}
    mine = set()
    for reaction in activity.reactions.all():
        reaction_counts[reaction.code] += 1
        if reaction.membership_id == current_membership.pk:
            mine.add(reaction.code)
    reactions = tuple(
        ReactionView(
            code=code,
            label=label,
            emoji=REACTION_EMOJI[code],
            count=reaction_counts[code],
            reacted_by_me=code in mine,
        )
        for code, label in TeamReaction.CODE_CHOICES
    )
    return ActivityView(
        public_id=activity.public_id,
        actor_name=actor_name,
        actor_initial=(actor_name[:1] or '?').upper(),
        actor_avatar_url=safe_avatar_url(activity.membership.user),
        unit_credit=activity.unit_credit,
        activity_date=activity.activity_date,
        comments=comments,
        reactions=reactions,
    )


def visible_activity_or_404(membership, activity_public_id):
    try:
        return TeamActivity.objects.get(
            public_id=activity_public_id,
            team=membership.team,
            team__is_active=True,
            is_visible=True,
            membership__status=TeamMembership.ACTIVE,
        )
    except TeamActivity.DoesNotExist as exc:
        raise Http404('Activity not found.') from exc


def pending_invitation_views(team):
    now = timezone.now()
    invitations = TeamInvitation.objects.select_related('intended_user').filter(
        team=team,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=now,
    )
    return tuple(
        InvitationView(
            public_id=invitation.public_id,
            display_name=(
                safe_display_name(invitation.intended_user)
                if invitation.intended_user is not None
                else invitation.intended_email
            ),
            token_prefix=invitation.token_prefix,
            expires_at=invitation.expires_at,
        )
        for invitation in invitations
    )
