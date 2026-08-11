import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models


def validate_timezone_name(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValidationError('Choose a valid IANA timezone.') from exc


class Team(models.Model):
    RANKED = 'ranked'
    ALPHABETICAL = 'alphabetical'
    DISPLAY_MODE_CHOICES = [
        (RANKED, 'Ranked by units'),
        (ALPHABETICAL, 'Alphabetical'),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=80)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='owned_sales_teams',
    )
    timezone = models.CharField(
        max_length=64,
        default='UTC',
        validators=[validate_timezone_name],
    )
    monthly_unit_goal = models.DecimalField(
        max_digits=8,
        decimal_places=1,
        null=True,
        blank=True,
    )
    display_mode = models.CharField(
        max_length=16,
        choices=DISPLAY_MODE_CHOICES,
        default=RANKED,
    )
    is_active = models.BooleanField(default=True)
    is_read_only = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['owner', 'is_active'], name='team_owner_active_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(monthly_unit_goal__isnull=True)
                    | models.Q(monthly_unit_goal__gt=0)
                ),
                name='team_monthly_goal_positive',
            ),
        ]

    def __str__(self):
        return self.name


class TeamMembership(models.Model):
    OWNER = 'owner'
    ADMIN = 'admin'
    MEMBER = 'member'
    ROLE_CHOICES = [
        (OWNER, 'Owner'),
        (ADMIN, 'Admin'),
        (MEMBER, 'Member'),
    ]

    INVITED = 'invited'
    ACTIVE = 'active'
    DECLINED = 'declined'
    REMOVED = 'removed'
    LEFT = 'left'
    STATUS_CHOICES = [
        (INVITED, 'Invited'),
        (ACTIVE, 'Active'),
        (DECLINED, 'Declined'),
        (REMOVED, 'Removed'),
        (LEFT, 'Left'),
    ]

    INDIVIDUAL_AND_TOTALS = 'individual_and_totals'
    TOTALS_ONLY = 'totals_only'
    PAUSED = 'paused'
    SHARING_CHOICES = [
        (INDIVIDUAL_AND_TOTALS, 'Individual activity and totals'),
        (TOTALS_ONLY, 'Totals only'),
        (PAUSED, 'Pause all sharing'),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sales_team_memberships',
    )
    role = models.CharField(max_length=12, choices=ROLE_CHOICES, default=MEMBER)
    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=INVITED,
    )
    sharing_preference = models.CharField(
        max_length=28,
        choices=SHARING_CHOICES,
        default=TOTALS_ONLY,
    )
    joined_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'user'],
                name='unique_team_user_membership',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'status'], name='team_member_user_status_idx'),
            models.Index(
                fields=['team', 'status', 'role'],
                name='team_member_status_role_idx',
            ),
        ]

    def __str__(self):
        return f'{self.team} / {self.user}'


class TeamInvitation(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='invitations',
    )
    intended_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sales_team_invitations',
    )
    intended_email = models.EmailField(blank=True)
    token_digest = models.CharField(max_length=64, unique=True)
    token_prefix = models.CharField(max_length=12)
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='created_sales_team_invitations',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='accepted_sales_team_invitations',
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=['intended_user', 'expires_at'],
                name='team_invitee_expiry_idx',
            ),
            models.Index(
                fields=['team', 'accepted_at', 'revoked_at'],
                name='team_invite_state_idx',
            ),
        ]

    def __str__(self):
        return f'Invitation {self.token_prefix}… to {self.team}'


class TeamActivity(models.Model):
    SALE = 'sale'
    MILESTONE = 'milestone'
    TYPE_CHOICES = [
        (SALE, 'Sale'),
        (MILESTONE, 'Milestone'),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='activities',
    )
    membership = models.ForeignKey(
        TeamMembership,
        on_delete=models.PROTECT,
        related_name='activities',
    )
    sale = models.OneToOneField(
        'SalesLogApp.Sale',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='team_activity',
    )
    activity_type = models.CharField(
        max_length=16,
        choices=TYPE_CHOICES,
        default=SALE,
    )
    unit_credit = models.DecimalField(max_digits=5, decimal_places=1)
    activity_date = models.DateField()
    is_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-activity_date', '-created_at', '-id']
        indexes = [
            models.Index(
                fields=['team', 'is_visible', 'activity_date'],
                name='team_activity_feed_idx',
            ),
        ]

    def __str__(self):
        return f'{self.get_activity_type_display()} activity for {self.membership}'


class TeamComment(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    activity = models.ForeignKey(
        TeamActivity,
        on_delete=models.CASCADE,
        related_name='comments',
    )
    author_membership = models.ForeignKey(
        TeamMembership,
        on_delete=models.PROTECT,
        related_name='comments',
    )
    body = models.TextField(validators=[MaxLengthValidator(500)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(
        TeamMembership,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='moderated_comments',
    )

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['activity', 'created_at'], name='team_comment_activity_idx'),
        ]

    @property
    def is_visible(self):
        return self.deleted_at is None and self.moderated_at is None

    def __str__(self):
        return f'Comment {self.public_id}'


class TeamReaction(models.Model):
    CELEBRATE = 'celebrate'
    ON_FIRE = 'on_fire'
    APPLAUSE = 'applause'
    STRONG_WORK = 'strong_work'
    GREAT_JOB = 'great_job'
    CODE_CHOICES = [
        (CELEBRATE, 'Celebrate'),
        (ON_FIRE, 'On fire'),
        (APPLAUSE, 'Applause'),
        (STRONG_WORK, 'Strong work'),
        (GREAT_JOB, 'Great job'),
    ]

    activity = models.ForeignKey(
        TeamActivity,
        on_delete=models.CASCADE,
        related_name='reactions',
    )
    membership = models.ForeignKey(
        TeamMembership,
        on_delete=models.PROTECT,
        related_name='reactions',
    )
    code = models.CharField(max_length=20, choices=CODE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['activity', 'membership', 'code'],
                name='unique_team_activity_reaction',
            ),
        ]
        indexes = [
            models.Index(fields=['activity', 'code'], name='team_reaction_code_idx'),
        ]

    def __str__(self):
        return f'{self.code} on {self.activity_id}'
