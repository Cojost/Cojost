import uuid

from django.conf import settings
from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator
from django.db import models


class FounderGrant(models.Model):
    FOUNDER_PRO = 'founder_pro'
    TIER_CHOICES = [(FOUNDER_PRO, 'Founder Pro')]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    code_digest = models.CharField(max_length=64, unique=True)
    code_prefix = models.CharField(max_length=12, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='created_founder_grants',
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    max_redemptions = models.PositiveSmallIntegerField(default=1)
    redemption_count = models.PositiveSmallIntegerField(default=0)
    redeemed_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='redeemed_founder_grant',
    )
    redeemed_at = models.DateTimeField(null=True, blank=True)
    trial_days = models.PositiveSmallIntegerField(
        default=90,
        validators=[MinValueValidator(1), MaxValueValidator(365)],
    )
    entitlement_tier = models.CharField(
        max_length=24,
        choices=TIER_CHOICES,
        default=FOUNDER_PRO,
    )
    administrative_note = models.TextField(
        blank=True,
        validators=[MaxLengthValidator(500)],
        help_text='Never enter payment, card, customer, or other billing data.',
    )

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_redemptions__gte=1),
                name='founder_grant_max_redemptions_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(redemption_count__lte=models.F('max_redemptions')),
                name='founder_grant_redemptions_within_max',
            ),
        ]

    def __str__(self):
        return f'Founder grant {self.code_prefix}...'


class BillingAccess(models.Model):
    NONE = ''
    STANDARD = 'standard'
    FOUNDER = 'founder'
    INTRODUCTORY_KIND_CHOICES = [
        (NONE, 'None'),
        (STANDARD, 'Standard'),
        (FOUNDER, 'Founder'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='billing_access',
    )
    onboarding_required_at = models.DateTimeField(null=True, blank=True)
    introductory_benefit_consumed_at = models.DateTimeField(null=True, blank=True)
    introductory_benefit_kind = models.CharField(
        max_length=12,
        choices=INTRODUCTORY_KIND_CHOICES,
        blank=True,
        default=NONE,
    )
    founder_grant = models.OneToOneField(
        FounderGrant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='billing_access',
    )
    authoritative_subscription = models.OneToOneField(
        'djstripe.Subscription',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='saleslog_billing_access',
    )
    founder_entitlement_expires_at = models.DateTimeField(null=True, blank=True)
    last_event_type = models.CharField(max_length=64, blank=True)
    last_event_created_at = models.DateTimeField(null=True, blank=True)
    last_synchronized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'billing access records'

    def __str__(self):
        return f'Billing access for user {self.user_id}'


class BillingCheckoutAttempt(models.Model):
    RESERVED = 'reserved'
    SESSION_CREATED = 'session_created'
    CHECKOUT_COMPLETED = 'checkout_completed'
    CONFIRMED = 'confirmed'
    FAILED = 'failed'
    EXPIRED = 'expired'
    STATUS_CHOICES = [
        (RESERVED, 'Reserved'),
        (SESSION_CREATED, 'Session created'),
        (CHECKOUT_COMPLETED, 'Checkout completed'),
        (CONFIRMED, 'Subscription confirmed'),
        (FAILED, 'Failed'),
        (EXPIRED, 'Expired'),
    ]
    ACTIVE_STATUSES = [RESERVED, SESSION_CREATED, CHECKOUT_COMPLETED]

    NONE = ''
    STANDARD = BillingAccess.STANDARD
    FOUNDER = BillingAccess.FOUNDER
    TRIAL_KIND_CHOICES = BillingAccess.INTRODUCTORY_KIND_CHOICES
    BASIC = 'basic'
    PRO = 'pro'
    PLAN_TIER_CHOICES = [(BASIC, 'Basic'), (PRO, 'Pro')]
    MONTH = 'month'
    YEAR = 'year'
    BILLING_INTERVAL_CHOICES = [(MONTH, 'Monthly'), (YEAR, 'Yearly')]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='billing_checkout_attempts',
    )
    founder_grant = models.ForeignKey(
        FounderGrant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='checkout_attempts',
    )
    trial_kind = models.CharField(
        max_length=12,
        choices=TRIAL_KIND_CHOICES,
        blank=True,
        default=NONE,
    )
    trial_days = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(365)],
    )
    selected_tier = models.CharField(
        max_length=12,
        choices=PLAN_TIER_CHOICES,
        default=PRO,
    )
    selected_billing_interval = models.CharField(
        max_length=5,
        choices=BILLING_INTERVAL_CHOICES,
        default=MONTH,
    )
    selected_price_id = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=RESERVED,
    )
    reservation_expires_at = models.DateTimeField()
    session_created_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                condition=models.Q(status__in=[
                    'reserved', 'session_created', 'checkout_completed',
                ]),
                name='one_active_billing_checkout_per_user',
            ),
            models.CheckConstraint(
                condition=models.Q(trial_days__lte=365),
                name='billing_checkout_trial_days_bounded',
            ),
        ]
        indexes = [
            models.Index(
                fields=['user', 'status', 'reservation_expires_at'],
                name='billing_attempt_user_state_idx',
            ),
        ]

    def __str__(self):
        return f'Checkout attempt {self.public_id}'
