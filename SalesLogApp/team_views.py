from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .models import Team, TeamComment, TeamInvitation, TeamMembership, TeamReaction
from .team_entitlements import can_create_team, get_team_entitlement
from .team_forms import (
    ReactionForm,
    InvitationCodeForm,
    SharingPreferenceForm,
    TeamCommentForm,
    TeamCreateForm,
    TeamGoalForm,
    TeamInviteForm,
    TeamSettingsForm,
)
from .team_services import (
    InvitationDeliveryError,
    accept_invitation,
    active_membership_for_user,
    as_member_view,
    as_team_view,
    build_feed_queryset,
    build_month_totals,
    create_and_email_invitation,
    create_team,
    decline_invitation,
    get_team_membership_or_404,
    invitation_for_user_or_404,
    pending_invitation_views,
    project_activity,
    require_management,
    revoke_invitation,
    safe_display_name,
    team_is_effectively_read_only,
    update_sharing_preference,
    visible_activity_or_404,
)


def teams_feature_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not settings.TEAMS_FEATURE_ENABLED:
            raise Http404('Page not found.')
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse('account_login'))
        return view_func(request, *args, **kwargs)

    return wrapped


@teams_feature_required
def team_home(request):
    membership = active_membership_for_user(request.user)
    if membership:
        return redirect('team_detail', team_id=membership.team.public_id)
    verified_emails = tuple(
        email.lower()
        for email in request.user.emailaddress_set.filter(verified=True)
        .values_list('email', flat=True)
    )
    invitation_identity = Q(intended_user=request.user)
    if verified_emails:
        invitation_identity |= Q(intended_email__in=verified_emails)
    pending = TeamInvitation.objects.select_related('team', 'created_by').filter(
        invitation_identity,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
        team__is_active=True,
    )
    pending_views = tuple(
        {
            'team_name': invitation.team.name,
            'inviter_name': safe_display_name(invitation.created_by),
            'expires_at': invitation.expires_at,
        }
        for invitation in pending
    )
    entitlement = get_team_entitlement(request.user)
    return render(request, 'SalesLogApp/teams/home.html', {
        'can_create_team': can_create_team(request.user),
        'entitlement_tier': entitlement.tier,
        'pending_invitations': pending_views,
        'invitation_code_form': InvitationCodeForm(),
    })


@teams_feature_required
@require_http_methods(['GET', 'POST'])
def team_create(request):
    if not can_create_team(request.user):
        raise PermissionDenied
    form = TeamCreateForm(request.POST or None, initial={'timezone': settings.TIME_ZONE})
    if request.method == 'POST' and form.is_valid():
        try:
            team = create_team(
                request.user,
                name=form.cleaned_data['name'],
                timezone_name=form.cleaned_data['timezone'],
                monthly_unit_goal=form.cleaned_data['monthly_unit_goal'],
                display_mode=form.cleaned_data['display_mode'],
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, 'Your team is ready.')
            return redirect('team_detail', team_id=team.public_id)
    return render(request, 'SalesLogApp/teams/form.html', {
        'form': form,
        'heading': 'Create a team',
        'submit_label': 'Create team',
        'privacy_note': True,
    })


@teams_feature_required
def team_detail(request, team_id):
    membership = get_team_membership_or_404(request.user, team_id)
    team = membership.team
    read_only = team_is_effectively_read_only(team)
    can_manage = (
        membership.role in {TeamMembership.OWNER, TeamMembership.ADMIN}
        and not read_only
    )
    month_start, totals, team_total = build_month_totals(
        team,
        request.user.pk,
        request.GET.get('month'),
    )
    page = Paginator(build_feed_queryset(team), 25).get_page(request.GET.get('page'))
    page.object_list = [project_activity(item, membership) for item in page.object_list]
    member_models = TeamMembership.objects.select_related('user').filter(
        team=team,
        status=TeamMembership.ACTIVE,
    ).order_by('role', 'user__username')
    members = tuple(as_member_view(item, request.user.pk) for item in member_models)
    goal = team.monthly_unit_goal
    goal_percent = None
    units_remaining = None
    if goal:
        goal_percent = (team_total / goal * Decimal('100')) if goal else Decimal('0')
        units_remaining = max(goal - team_total, Decimal('0'))

    one_time_invitation_code = None
    invitation_once = request.session.pop('team_invitation_once', None)
    if invitation_once and invitation_once.get('team') == str(team.public_id):
        one_time_invitation_code = invitation_once.get('code')

    return render(request, 'SalesLogApp/teams/detail.html', {
        'team': as_team_view(team),
        'membership': as_member_view(membership, request.user.pk),
        'members': members,
        'totals': totals,
        'team_total': team_total,
        'month_start': month_start,
        'goal_percent': goal_percent,
        'goal_progress_value': min(goal_percent or Decimal('0'), Decimal('100')),
        'units_remaining': units_remaining,
        'activity_page': page,
        'comment_form': TeamCommentForm(),
        'sharing_form': SharingPreferenceForm(initial={
            'sharing_preference': membership.sharing_preference,
        }),
        'invite_form': TeamInviteForm() if can_manage else None,
        'pending_invitations': pending_invitation_views(team) if can_manage else (),
        'can_manage': can_manage,
        'can_manage_roles': membership.role == TeamMembership.OWNER and not read_only,
        'read_only': read_only,
        'show_owner_read_only': membership.role == TeamMembership.OWNER and read_only,
        'one_time_invitation_code': one_time_invitation_code,
    })


@teams_feature_required
@require_http_methods(['GET', 'POST'])
def team_settings(request, team_id):
    membership = get_team_membership_or_404(request.user, team_id)
    if membership.role not in {TeamMembership.OWNER, TeamMembership.ADMIN}:
        raise PermissionDenied
    read_only = team_is_effectively_read_only(membership.team)
    if request.method == 'POST' and read_only:
        raise PermissionDenied
    form_class = (
        TeamSettingsForm
        if membership.role == TeamMembership.OWNER
        else TeamGoalForm
    )
    form = form_class(request.POST or None, instance=membership.team)
    if read_only:
        for field in form.fields.values():
            field.disabled = True
    if request.method == 'POST' and form.is_valid():
        require_management(membership)
        form.save()
        messages.success(request, 'Team settings updated.')
        return redirect('team_detail', team_id=membership.team.public_id)
    return render(request, 'SalesLogApp/teams/form.html', {
        'form': form,
        'heading': (
            'Team settings'
            if membership.role == TeamMembership.OWNER
            else 'Team goal'
        ),
        'submit_label': 'Save settings',
        'read_only': read_only,
        'show_owner_read_only': membership.role == TeamMembership.OWNER and read_only,
        'team': as_team_view(membership.team),
    })


@teams_feature_required
@require_POST
def team_invite(request, team_id):
    membership = get_team_membership_or_404(request.user, team_id)
    require_management(membership)
    form = TeamInviteForm(request.POST)
    if form.is_valid():
        try:
            _, raw_token = create_and_email_invitation(
                membership,
                form.cleaned_data['intended_email'],
                signup_url=request.build_absolute_uri(reverse('account_signup')),
                login_url=request.build_absolute_uri(reverse('account_login')),
                teams_url=request.build_absolute_uri(
                    reverse('team_invitation_accept')
                ),
            )
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        except InvitationDeliveryError as exc:
            messages.error(request, str(exc))
        else:
            request.session['team_invitation_once'] = {
                'team': str(membership.team.public_id),
                'code': raw_token,
            }
            messages.success(
                request,
                'Invitation email sent. The one-time code is also shown here as a backup.',
            )
    else:
        messages.error(request, 'Enter a valid email address.')
    return redirect('team_detail', team_id=membership.team.public_id)


@teams_feature_required
@require_POST
def team_invitation_revoke(request, team_id, invitation_id):
    membership = get_team_membership_or_404(request.user, team_id)
    revoke_invitation(membership, invitation_id)
    messages.success(request, 'Invitation revoked.')
    return redirect('team_detail', team_id=membership.team.public_id)


@teams_feature_required
@require_http_methods(['GET', 'POST'])
def team_invitation_accept(request):
    form = InvitationCodeForm(request.POST or None)
    invitation = None
    raw_token = None
    if request.method == 'POST' and form.is_valid():
        raw_token = form.cleaned_data['invitation_code']
        invitation = invitation_for_user_or_404(raw_token, request.user)
    if invitation is not None and request.POST.get('action') == 'accept':
        try:
            membership = accept_invitation(raw_token, request.user)
        except ValidationError as exc:
            return render(request, 'SalesLogApp/teams/invitation.html', {
                'team': as_team_view(invitation.team),
                'inviter_name': safe_display_name(invitation.created_by),
                'error': '; '.join(exc.messages),
                'form': form,
                'invitation_code': raw_token,
            }, status=409)
        messages.success(request, f'You joined {membership.team.name}.')
        return redirect('team_detail', team_id=membership.team.public_id)
    if invitation is not None and request.POST.get('action') == 'decline':
        decline_invitation(raw_token, request.user)
        messages.success(request, 'Invitation declined.')
        return redirect('team_home')
    context = {'form': form}
    if invitation is not None:
        context.update({
            'team': as_team_view(invitation.team),
            'inviter_name': safe_display_name(invitation.created_by),
            'invitation_code': raw_token,
        })
    return render(request, 'SalesLogApp/teams/invitation.html', context)


@teams_feature_required
@require_POST
def team_sharing(request, team_id):
    membership = get_team_membership_or_404(request.user, team_id)
    form = SharingPreferenceForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Invalid sharing preference.')
    update_sharing_preference(membership, form.cleaned_data['sharing_preference'])
    messages.success(request, 'Your sharing preference was updated immediately.')
    return redirect('team_detail', team_id=membership.team.public_id)


@teams_feature_required
@require_POST
def team_comment_add(request, team_id, activity_id):
    membership = get_team_membership_or_404(request.user, team_id)
    activity = visible_activity_or_404(membership, activity_id)
    form = TeamCommentForm(request.POST)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.activity = activity
        comment.author_membership = membership
        comment.save()
        messages.success(request, 'Comment posted.')
    else:
        messages.error(request, 'Comments must be between 1 and 500 characters.')
    return redirect('team_detail', team_id=membership.team.public_id)


@teams_feature_required
@require_http_methods(['GET', 'POST'])
def team_comment_edit(request, team_id, activity_id, comment_id):
    membership = get_team_membership_or_404(request.user, team_id)
    activity = visible_activity_or_404(membership, activity_id)
    try:
        comment = TeamComment.objects.get(
            public_id=comment_id,
            activity=activity,
            author_membership=membership,
            deleted_at__isnull=True,
            moderated_at__isnull=True,
        )
    except TeamComment.DoesNotExist as exc:
        raise Http404('Comment not found.') from exc
    form = TeamCommentForm(request.POST or None, instance=comment)
    if request.method == 'POST' and form.is_valid():
        comment = form.save(commit=False)
        comment.edited_at = timezone.now()
        comment.save(update_fields=['body', 'edited_at', 'updated_at'])
        messages.success(request, 'Comment updated.')
        return redirect('team_detail', team_id=membership.team.public_id)
    return render(request, 'SalesLogApp/teams/form.html', {
        'form': form,
        'heading': 'Edit comment',
        'submit_label': 'Save comment',
        'team': as_team_view(membership.team),
    })


@teams_feature_required
@require_POST
def team_comment_delete(request, team_id, activity_id, comment_id):
    membership = get_team_membership_or_404(request.user, team_id)
    activity = visible_activity_or_404(membership, activity_id)
    updated = TeamComment.objects.filter(
        public_id=comment_id,
        activity=activity,
        author_membership=membership,
        deleted_at__isnull=True,
        moderated_at__isnull=True,
    ).update(body='', deleted_at=timezone.now())
    if not updated:
        raise Http404('Comment not found.')
    messages.success(request, 'Comment deleted.')
    return redirect('team_detail', team_id=membership.team.public_id)


@teams_feature_required
@require_POST
def team_comment_hide(request, team_id, activity_id, comment_id):
    membership = get_team_membership_or_404(request.user, team_id)
    if membership.role not in {TeamMembership.OWNER, TeamMembership.ADMIN}:
        raise PermissionDenied
    activity = visible_activity_or_404(membership, activity_id)
    updated = TeamComment.objects.filter(
        public_id=comment_id,
        activity=activity,
        deleted_at__isnull=True,
        moderated_at__isnull=True,
    ).exclude(author_membership=membership).update(
        body='',
        moderated_at=timezone.now(),
        moderated_by=membership,
    )
    if not updated:
        raise Http404('Comment not found.')
    messages.success(request, 'Comment hidden.')
    return redirect('team_detail', team_id=membership.team.public_id)


@teams_feature_required
@require_POST
def team_reaction_toggle(request, team_id, activity_id):
    membership = get_team_membership_or_404(request.user, team_id)
    activity = visible_activity_or_404(membership, activity_id)
    form = ReactionForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Invalid reaction.')
    code = form.cleaned_data['code']
    with transaction.atomic():
        reaction, created = TeamReaction.objects.get_or_create(
            activity=activity,
            membership=membership,
            code=code,
        )
        if not created:
            reaction.delete()
    return redirect('team_detail', team_id=membership.team.public_id)


def _target_membership_or_404(actor_membership, membership_id):
    try:
        return TeamMembership.objects.select_related('user').get(
            public_id=membership_id,
            team=actor_membership.team,
            status=TeamMembership.ACTIVE,
        )
    except TeamMembership.DoesNotExist as exc:
        raise Http404('Member not found.') from exc


@teams_feature_required
@require_POST
def team_member_remove(request, team_id, membership_id):
    actor = get_team_membership_or_404(request.user, team_id)
    require_management(actor)
    target = _target_membership_or_404(actor, membership_id)
    if target.role == TeamMembership.OWNER:
        raise PermissionDenied
    target.status = TeamMembership.REMOVED
    target.save(update_fields=['status', 'updated_at'])
    target.activities.update(is_visible=False)
    messages.success(request, 'Member removed.')
    return redirect('team_detail', team_id=actor.team.public_id)


@teams_feature_required
@require_POST
def team_member_role(request, team_id, membership_id, action):
    actor = get_team_membership_or_404(request.user, team_id)
    require_management(actor, owner_only=True)
    target = _target_membership_or_404(actor, membership_id)
    if target.role == TeamMembership.OWNER or target.pk == actor.pk:
        raise PermissionDenied
    if action == 'promote' and target.role == TeamMembership.MEMBER:
        target.role = TeamMembership.ADMIN
    elif action == 'demote' and target.role == TeamMembership.ADMIN:
        target.role = TeamMembership.MEMBER
    else:
        return HttpResponseBadRequest('Invalid role transition.')
    target.save(update_fields=['role', 'updated_at'])
    messages.success(request, 'Member role updated.')
    return redirect('team_detail', team_id=actor.team.public_id)


@teams_feature_required
@require_POST
def team_transfer_ownership(request, team_id, membership_id):
    actor = get_team_membership_or_404(request.user, team_id)
    require_management(actor, owner_only=True)
    target = _target_membership_or_404(actor, membership_id)
    if target.pk == actor.pk or not get_team_entitlement(target.user).has_pro_access:
        raise PermissionDenied
    with transaction.atomic():
        type(target.user).objects.select_for_update().get(pk=target.user_id)
        team = Team.objects.select_for_update().get(pk=actor.team_id)
        if Team.objects.filter(owner=target.user, is_active=True).exclude(pk=team.pk).exists():
            return HttpResponseBadRequest('That member already owns an active team.')
        team.owner = target.user
        team.save(update_fields=['owner', 'updated_at'])
        actor.role = TeamMembership.ADMIN
        actor.save(update_fields=['role', 'updated_at'])
        target.role = TeamMembership.OWNER
        target.save(update_fields=['role', 'updated_at'])
    messages.success(request, 'Team ownership transferred.')
    return redirect('team_detail', team_id=team.public_id)


@teams_feature_required
@require_POST
def team_leave(request, team_id):
    membership = get_team_membership_or_404(request.user, team_id)
    if membership.role == TeamMembership.OWNER:
        return HttpResponseBadRequest('Transfer ownership or deactivate the team first.')
    membership.status = TeamMembership.LEFT
    membership.save(update_fields=['status', 'updated_at'])
    membership.activities.update(is_visible=False)
    messages.success(request, 'You left the team.')
    return redirect('team_home')


@teams_feature_required
@require_POST
def team_deactivate(request, team_id):
    membership = get_team_membership_or_404(request.user, team_id)
    require_management(membership, owner_only=True)
    membership.team.is_active = False
    membership.team.save(update_fields=['is_active', 'updated_at'])
    messages.success(request, 'Team deactivated. Its history was preserved.')
    return redirect('team_home')
