from decimal import Decimal
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower, Trim

from .pay_plans import PayPlanVersion
from .sales import BaseSale, Sale


class CommissionSandbox(models.Model):
    DRAFT = 'draft'
    SHARED = 'shared'
    ARCHIVED = 'archived'
    CONVERTED = 'converted'
    STATUS_CHOICES = [
        (DRAFT, 'Draft'),
        (SHARED, 'Shared'),
        (ARCHIVED, 'Archived'),
        (CONVERTED, 'Converted to Pay Plan'),
    ]
    REPLAY = 'replay'
    PROJECTION = 'projection'
    MIXED = 'mixed'
    REPLAY_MODE_CHOICES = [
        (REPLAY, 'Historical replay'),
        (PROJECTION, 'Future projection'),
        (MIXED, 'Historical and hypothetical'),
    ]
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='commission_sandboxes',
    )
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source_version = models.ForeignKey(
        PayPlanVersion, on_delete=models.PROTECT,
        related_name='source_sandboxes',
    )
    draft_version = models.OneToOneField(
        PayPlanVersion, on_delete=models.PROTECT,
        related_name='sandbox_session',
    )
    scenario_name = models.CharField(max_length=150)
    scenario_notes = models.TextField(blank=True)
    source_scenario = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='duplicates',
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=DRAFT, db_index=True,
    )
    replay_mode = models.CharField(
        max_length=16,
        choices=REPLAY_MODE_CHOICES,
        default=REPLAY,
    )
    replay_start_date = models.DateField(null=True, blank=True)
    replay_end_date = models.DateField(null=True, blank=True)
    assumptions = models.JSONField(default=dict, blank=True)
    replay_filters = models.JSONField(default=dict, blank=True)
    calculation_summary = models.JSONField(default=dict, blank=True)
    validation_summary = models.JSONField(default=dict, blank=True)
    revision = models.PositiveIntegerField(default=1)
    saved_revision = models.PositiveIntegerField(default=1)
    last_calculated_revision = models.PositiveIntegerField(default=0)
    calculation_input_fingerprint = models.CharField(max_length=64, blank=True)
    calculation_source_fingerprint = models.CharField(max_length=64, blank=True)
    calculation_engine_version = models.CharField(max_length=32, blank=True)
    last_saved_at = models.DateTimeField(null=True, blank=True)
    last_calculated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(
                fields=['owner', 'status', 'updated_at'],
                name='sandbox_owner_status_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                Lower(Trim('scenario_name')),
                'owner',
                condition=~Q(status='archived'),
                name='unique_open_scenario_name_ci',
            ),
            models.CheckConstraint(
                condition=(
                    Q(replay_end_date__isnull=True)
                    | Q(replay_start_date__isnull=True)
                    | Q(replay_end_date__gte=models.F('replay_start_date'))
                ),
                name='scenario_valid_replay_dates',
            ),
            models.CheckConstraint(
                condition=Q(saved_revision__lte=models.F('revision')),
                name='scenario_saved_revision_lte_revision',
            ),
        ]

    def clean(self):
        super().clean()
        for field_name in ('source_version', 'draft_version'):
            version = getattr(self, field_name, None)
            if version and version.pay_plan.owner_user_id != self.owner_id:
                raise ValidationError({
                    field_name: 'Sandbox versions must belong to the owner.',
                })
        if (
            self.source_version_id and self.draft_version_id
            and self.source_version_id == self.draft_version_id
        ):
            raise ValidationError({
                'draft_version': 'The sandbox must use a separate draft version.',
            })
        if (
            self.draft_version_id
            and self.draft_version.status not in {
                PayPlanVersion.DRAFT, PayPlanVersion.REVIEW_REQUIRED,
            }
        ):
            raise ValidationError({
                'draft_version': 'A sandbox draft version cannot be active or historical.',
            })
        if self.draft_version_id and not self.draft_version.is_sandbox:
            raise ValidationError({
                'draft_version': 'The draft version must be marked for sandbox use.',
            })
        if (
            self.source_scenario_id
            and self.source_scenario.owner_id != self.owner_id
        ):
            raise ValidationError({
                'source_scenario': 'A source scenario must have the same owner.',
            })
        if (
            self.replay_start_date
            and self.replay_end_date
            and self.replay_end_date < self.replay_start_date
        ):
            raise ValidationError({
                'replay_end_date': 'Replay end date cannot precede start date.',
            })
        name = (self.scenario_name or '').strip()
        if not name:
            raise ValidationError({'scenario_name': 'Scenario name is required.'})
        self.scenario_name = name
        duplicate = type(self).objects.filter(
            owner_id=self.owner_id,
            scenario_name__iexact=name,
        ).exclude(status=self.ARCHIVED)
        if self.pk:
            duplicate = duplicate.exclude(pk=self.pk)
        if duplicate.exists():
            raise ValidationError({
                'scenario_name': 'You already have an active scenario with this name.',
            })

    @property
    def has_unsaved_changes(self):
        return self.revision != self.saved_revision


class ScenarioHistory(models.Model):
    scenario = models.ForeignKey(
        CommissionSandbox,
        on_delete=models.CASCADE,
        related_name='history',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='commission_scenario_history',
    )
    action = models.CharField(max_length=50)
    summary = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(
                fields=['scenario', 'created_at'],
                name='scenario_history_date_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.scenario_id
            and self.actor_id
            and self.scenario.owner_id != self.actor_id
        ):
            raise ValidationError(
                'Scenario history actor must own the scenario.',
            )


class SandboxHypotheticalDeal(BaseSale):
    sandbox = models.ForeignKey(
        CommissionSandbox, on_delete=models.CASCADE,
        related_name='hypothetical_deals',
    )
    label = models.CharField(max_length=150, blank=True)
    dealNumber = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['sandbox', 'dealNumber'],
                name='unique_sandbox_hypothetical_deal',
            ),
        ]

    @property
    def user(self):
        return self.sandbox.owner

    @property
    def user_id(self):
        return self.sandbox.owner_id

    @property
    def unit_credit(self):
        return Decimal(str(self.count or 0))

    @property
    def commission_credit_multiplier(self):
        if Decimal(str(self.count or 0)) == Decimal('0.5'):
            return Decimal('0.5')
        return Decimal('1.0')


class SandboxRun(models.Model):
    REPLAY = 'replay'
    PROJECTION = 'projection'
    MIXED = 'mixed'
    MODE_CHOICES = [
        (REPLAY, 'Historical replay'),
        (PROJECTION, 'Future projection'),
        (MIXED, 'Historical and hypothetical'),
    ]
    sandbox = models.ForeignKey(
        CommissionSandbox, on_delete=models.CASCADE, related_name='runs',
    )
    mode = models.CharField(max_length=16, choices=MODE_CHOICES)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    sandbox_revision = models.PositiveIntegerField()
    input_fingerprint = models.CharField(max_length=64, db_index=True)
    engine_version = models.CharField(max_length=32, blank=True)
    schema_version = models.CharField(max_length=16, blank=True)
    source_fingerprint = models.CharField(max_length=64, blank=True)
    live_version_fingerprint = models.CharField(max_length=64, blank=True)
    actual_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sandbox_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    difference = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    percent_change = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
    )
    statistics = models.JSONField(default=dict, blank=True)
    validation_report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(
                fields=['sandbox', 'sandbox_revision', 'created_at'],
                name='sandbox_run_revision_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.period_start and self.period_end
            and self.period_end < self.period_start
        ):
            raise ValidationError('Sandbox run end date cannot precede start date.')


class SandboxResult(models.Model):
    PRODUCTION = 'production'
    HYPOTHETICAL = 'hypothetical'
    DEAL_KIND_CHOICES = [
        (PRODUCTION, 'Production sale'),
        (HYPOTHETICAL, 'Hypothetical sale'),
    ]
    HIGHER = 'higher'
    LOWER = 'lower'
    UNCHANGED = 'unchanged'
    COMPARISON_CHOICES = [
        (HIGHER, 'Higher'), (LOWER, 'Lower'), (UNCHANGED, 'Unchanged'),
    ]
    run = models.ForeignKey(
        SandboxRun, on_delete=models.CASCADE, related_name='results',
    )
    deal_kind = models.CharField(
        max_length=16, choices=DEAL_KIND_CHOICES,
    )
    source_key = models.CharField(max_length=100, blank=True)
    sale_snapshot = models.JSONField(default=dict, blank=True)
    production_sale = models.ForeignKey(
        Sale, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sandbox_results',
    )
    hypothetical_deal = models.ForeignKey(
        SandboxHypotheticalDeal, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='results',
    )
    actual_commission = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sandbox_commission = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    difference = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    percent_change = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
    )
    comparison = models.CharField(max_length=16, choices=COMPARISON_CHOICES)
    actual_explanation = models.JSONField(default=dict, blank=True)
    explanation = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        deal_kind='production',
                        hypothetical_deal__isnull=True,
                    )
                    | Q(
                        deal_kind='hypothetical',
                        production_sale__isnull=True,
                    )
                ),
                name='sandbox_result_valid_deal_kind',
            ),
        ]

    def clean(self):
        super().clean()
        sandbox = self.run.sandbox if self.run_id else None
        if (
            sandbox and self.production_sale_id
            and self.production_sale.user_id != sandbox.owner_id
        ):
            raise ValidationError('Production sale must belong to the sandbox owner.')
        if (
            sandbox and self.hypothetical_deal_id
            and self.hypothetical_deal.sandbox_id != sandbox.id
        ):
            raise ValidationError('Hypothetical deal must belong to this sandbox.')
