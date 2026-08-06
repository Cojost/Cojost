from decimal import Decimal
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from ..commission_engine.exceptions import ConditionValidationError, RuleConfigurationError
from ..commission_engine.validators import (
    validate_condition,
    validate_configuration,
    validate_rule_scope,
    validate_rule_type,
)


class Industry(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'industries'

    def __str__(self):
        return self.name


class PayPlan(models.Model):
    industry = models.ForeignKey(
        Industry,
        on_delete=models.PROTECT,
        related_name='pay_plans',
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='pay_plans',
    )
    dealership_name = models.CharField(max_length=150, blank=True)
    is_template = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.CheckConstraint(
                condition=Q(is_template=True) | Q(owner_user__isnull=False),
                name='payplan_template_or_owner',
            ),
            models.UniqueConstraint(
                fields=['owner_user', 'industry', 'name'],
                condition=Q(owner_user__isnull=False),
                name='unique_owned_payplan_name',
            ),
            models.UniqueConstraint(
                fields=['industry', 'name'],
                condition=Q(owner_user__isnull=True, is_template=True),
                name='unique_system_template_name',
            ),
        ]
        indexes = [
            models.Index(
                fields=['owner_user', 'industry', 'is_active'],
                name='payplan_owner_ind_active_idx',
            ),
        ]

    def __str__(self):
        return self.name


class PayPlanVersion(models.Model):
    DRAFT = 'draft'
    REVIEW_REQUIRED = 'review_required'
    ACTIVE = 'active'
    INACTIVE = 'inactive'
    FAILED = 'failed'
    ARCHIVED = 'archived'
    FUTURE_EFFECTIVE = 'future_effective'
    STATUS_CHOICES = [
        (DRAFT, 'Draft'),
        (REVIEW_REQUIRED, 'Review Required'),
        (ACTIVE, 'Active'),
        (INACTIVE, 'Inactive'),
        (FAILED, 'Processing Failed'),
        (ARCHIVED, 'Archived'),
        (FUTURE_EFFECTIVE, 'Future Effective'),
    ]
    SOURCE_LEGACY = 'legacy'
    SOURCE_UPLOAD = 'upload'
    SOURCE_RELOAD = 'reload'
    SOURCE_MANUAL = 'manual'
    SOURCE_PASTE = 'paste'
    SOURCE_CHOICES = [
        (SOURCE_LEGACY, 'Legacy'),
        (SOURCE_UPLOAD, 'Upload'),
        (SOURCE_RELOAD, 'Reload'),
        (SOURCE_MANUAL, 'Manual'),
        (SOURCE_PASTE, 'Pasted text'),
    ]

    pay_plan = models.ForeignKey(
        PayPlan,
        on_delete=models.CASCADE,
        related_name='versions',
    )
    version_name = models.CharField(max_length=100)
    version_number = models.PositiveIntegerField(null=True, blank=True)
    effective_start_date = models.DateField()
    effective_end_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=DRAFT,
    )
    source_type = models.CharField(
        max_length=16, choices=SOURCE_CHOICES, default=SOURCE_LEGACY,
    )
    source_filename = models.CharField(max_length=255, blank=True)
    previous_version = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='replacement_versions',
    )
    parser_version = models.CharField(max_length=64, blank=True)
    processing_status = models.CharField(max_length=32, blank=True)
    processing_errors = models.JSONField(default=list, blank=True)
    processing_warnings = models.JSONField(default=list, blank=True)
    canonical_schema_version = models.CharField(max_length=16, blank=True)
    canonical_payload = models.JSONField(default=dict, blank=True)
    canonical_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    compilation_report = models.JSONField(default=dict, blank=True)
    is_sandbox = models.BooleanField(default=False, db_index=True)
    origin_scenario = models.OneToOneField(
        'CommissionSandbox',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='converted_version',
    )
    default_backend_percentage = models.DecimalField(
        max_digits=7, decimal_places=6, null=True, blank=True,
        help_text='Canonical backend multiplier, for example 0.05 for 5%.',
    )
    default_backend_minimum = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    default_backend_maximum = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_pay_plan_versions',
    )
    activation_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pay_plan', '-effective_start_date', '-id']
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(effective_end_date__isnull=True)
                    | Q(effective_end_date__gte=models.F('effective_start_date'))
                ),
                name='payplanversion_valid_dates',
            ),
            models.UniqueConstraint(
                fields=['pay_plan', 'version_name'],
                name='unique_payplan_version_name',
            ),
            models.UniqueConstraint(
                fields=['pay_plan'],
                condition=Q(status='active', effective_end_date__isnull=True),
                name='unique_open_active_plan_version',
            ),
        ]
        indexes = [
            models.Index(
                fields=['pay_plan', 'status', 'effective_start_date'],
                name='planver_status_start_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.previous_version_id
            and self.previous_version.pay_plan.owner_user_id
            != self.pay_plan.owner_user_id
        ):
            raise ValidationError({
                'previous_version': 'Previous version must have the same owner.'
            })
        if (
            self.created_by_id
            and self.pay_plan.owner_user_id
            and self.created_by_id != self.pay_plan.owner_user_id
        ):
            raise ValidationError({
                'created_by': 'Version creator must match the user-owned plan.'
            })
        if (
            self.origin_scenario_id
            and self.origin_scenario.owner_id != self.pay_plan.owner_user_id
        ):
            raise ValidationError({
                'origin_scenario': 'Scenario origin must have the same owner.',
            })
        if (
            self.effective_start_date
            and self.effective_end_date
            and self.effective_end_date < self.effective_start_date
        ):
            raise ValidationError({
                'effective_end_date': 'End date cannot be before start date.'
            })
        if (
            self.default_backend_percentage is not None
            and not Decimal('0') < self.default_backend_percentage <= Decimal('1')
        ):
            raise ValidationError({
                'default_backend_percentage': 'Enter a multiplier above 0 and no more than 1.'
            })
        if (
            self.default_backend_minimum is not None
            and self.default_backend_maximum is not None
            and self.default_backend_minimum > self.default_backend_maximum
        ):
            raise ValidationError({
                'default_backend_maximum': 'Maximum must be greater than or equal to minimum.'
            })

        if not self.pay_plan_id or self.status != self.ACTIVE:
            return
        overlaps = type(self).objects.filter(
            pay_plan_id=self.pay_plan_id,
            status=self.ACTIVE,
        ).exclude(pk=self.pk)
        if self.effective_end_date:
            overlaps = overlaps.filter(
                effective_start_date__lte=self.effective_end_date
            )
        overlaps = overlaps.filter(
            Q(effective_end_date__isnull=True)
            | Q(effective_end_date__gte=self.effective_start_date)
        )
        if overlaps.exists():
            raise ValidationError(
                'Active versions of the same pay plan cannot have overlapping dates.'
            )

    def __str__(self):
        return f'{self.pay_plan} — {self.version_name}'


class PayPlanRule(models.Model):
    pay_plan_version = models.ForeignKey(
        PayPlanVersion,
        on_delete=models.CASCADE,
        related_name='rules',
    )
    semantic_key = models.UUIDField(default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    rule_type = models.CharField(max_length=64)
    calculation_scope = models.CharField(max_length=16)
    condition_group_operator = models.CharField(
        max_length=8,
        choices=(
            ('all', 'All'),
            ('any', 'Any'),
        ),
        default='all',
    )
    configuration = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['pay_plan_version', 'sort_order', 'id']
        indexes = [
            models.Index(fields=['pay_plan_version', 'is_active', 'sort_order'], name='payplanrule_active_sort_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['pay_plan_version', 'semantic_key'],
                name='unique_rule_semantic_key_per_version',
            ),
        ]

    def clean(self):
        super().clean()
        try:
            validate_rule_type(self.rule_type)
            validate_rule_scope(self.calculation_scope, self.rule_type)
            validate_configuration(self.rule_type, self.configuration)
        except (RuleConfigurationError, ConditionValidationError) as exc:
            raise ValidationError(str(exc))

    def __str__(self):
        return f'{self.name} ({self.rule_type})'


class PayPlanRuleCondition(models.Model):
    rule = models.ForeignKey(
        PayPlanRule,
        on_delete=models.CASCADE,
        related_name='conditions',
    )
    field_name = models.CharField(max_length=64)
    operator = models.CharField(max_length=32)
    value = models.JSONField()
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['rule', 'sort_order', 'id']
        indexes = [
            models.Index(fields=['rule', 'field_name', 'operator'], name='pprcondition_rule_field_idx'),
        ]

    def clean(self):
        super().clean()
        try:
            validate_condition({
                'field_name': self.field_name,
                'operator': self.operator,
                'value': self.value,
            })
        except ConditionValidationError as exc:
            raise ValidationError(str(exc))
        if (
            self.rule_id
            and self.rule.calculation_scope == 'period'
            and self.field_name in {
                'vehicle_condition', 'make', 'model', 'year', 'is_cpo',
                'deal_type', 'front_end_gross', 'back_end_gross',
                'total_gross', 'deal_credit', 'sale_date',
            }
        ):
            raise ValidationError(
                'Per-sale conditions cannot be attached to period rules. '
                'Use an appropriate monthly metric in the rule configuration.'
            )

    def as_dict(self):
        data = {
            'field_name': self.field_name,
            'operator': self.operator,
        }
        if self.operator not in {'is_true', 'is_false'}:
            data['value'] = self.value
        return data


class PayPlanAssignment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pay_plan_assignments',
    )
    pay_plan_version = models.ForeignKey(
        PayPlanVersion,
        on_delete=models.PROTECT,
        related_name='assignments',
    )
    effective_start_date = models.DateField()
    effective_end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['user', '-effective_start_date', '-id']
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(effective_end_date__isnull=True)
                    | Q(effective_end_date__gte=models.F('effective_start_date'))
                ),
                name='payplanassignment_valid_dates',
            ),
            models.UniqueConstraint(
                fields=['user', 'pay_plan_version', 'effective_start_date'],
                name='unique_user_plan_assignment_start',
            ),
            models.UniqueConstraint(
                fields=['user'],
                condition=Q(is_active=True, effective_end_date__isnull=True),
                name='unique_open_active_assignment',
            ),
        ]
        indexes = [
            models.Index(
                fields=['user', 'is_active', 'effective_start_date'],
                name='planassign_user_active_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.effective_start_date
            and self.effective_end_date
            and self.effective_end_date < self.effective_start_date
        ):
            raise ValidationError({
                'effective_end_date': 'End date cannot be before start date.'
            })

        if self.user_id and self.pay_plan_version_id:
            owner_id = self.pay_plan_version.pay_plan.owner_user_id
            if owner_id is not None and owner_id != self.user_id:
                raise ValidationError({
                    'pay_plan_version': 'A user cannot be assigned another user’s pay plan.'
                })

        if not self.user_id or not self.is_active or not self.effective_start_date:
            return
        overlaps = type(self).objects.filter(
            user_id=self.user_id,
            is_active=True,
        ).exclude(pk=self.pk)
        if self.effective_end_date:
            overlaps = overlaps.filter(
                effective_start_date__lte=self.effective_end_date
            )
        overlaps = overlaps.filter(
            Q(effective_end_date__isnull=True)
            | Q(effective_end_date__gte=self.effective_start_date)
        )
        if overlaps.exists():
            raise ValidationError(
                'A user cannot have overlapping active pay plan assignments.'
            )

    def __str__(self):
        return f'{self.user} — {self.pay_plan_version}'


class PayPlanOnboarding(models.Model):
    NOT_STARTED = 'not_started'
    METHOD_SELECTED = 'method_selected'
    SUBMITTED = 'submitted'
    DRAFT_CREATED = 'draft_created'
    NEEDS_REVIEW = 'needs_review'
    READY_TO_ACTIVATE = 'ready_to_activate'
    ACTIVE = 'active'
    REJECTED = 'rejected'
    FAILED = 'failed'

    STATUS_CHOICES = [
        (NOT_STARTED, 'Not started'),
        (METHOD_SELECTED, 'Method selected'),
        (SUBMITTED, 'Submitted'),
        (DRAFT_CREATED, 'Draft created'),
        (NEEDS_REVIEW, 'Needs review'),
        (READY_TO_ACTIVATE, 'Ready to activate'),
        (ACTIVE, 'Active'),
        (REJECTED, 'Rejected'),
        (FAILED, 'Failed'),
    ]

    UPLOAD = 'upload'
    DESCRIBE = 'describe'
    MANUAL_BUILDER = 'manual_builder'
    ASSISTED = 'assisted'
    SETUP_METHOD_CHOICES = [
        (UPLOAD, 'Upload'),
        (DESCRIBE, 'Describe'),
        (MANUAL_BUILDER, 'Manual builder'),
        (ASSISTED, 'Assisted'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pay_plan_onboarding',
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=NOT_STARTED, db_index=True)
    setup_method = models.CharField(max_length=32, choices=SETUP_METHOD_CHOICES, blank=True)
    current_pay_plan = models.ForeignKey(
        PayPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='onboarding_records',
    )
    current_version = models.ForeignKey(
        PayPlanVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='onboarding_records',
    )
    questionnaire = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'status'], name='pp_o_user_status_idx'),
            models.Index(fields=['status', 'setup_method'], name='pp_o_status_method_idx'),
        ]

    def __str__(self):
        return f'Pay-plan onboarding for {self.user}'

    def clean(self):
        super().clean()
        if self.current_pay_plan_id and self.current_pay_plan.owner_user_id != self.user_id:
            raise ValidationError('Current pay plan must belong to the onboarding user.')
        if self.current_version_id:
            if self.current_version.pay_plan.owner_user_id != self.user_id:
                raise ValidationError('Current version must belong to the onboarding user.')
            if (
                self.current_pay_plan_id
                and self.current_version.pay_plan_id != self.current_pay_plan_id
            ):
                raise ValidationError('Current version must belong to the current pay plan.')


class PayPlanDocument(models.Model):
    UPLOADED = 'uploaded'
    PENDING_REVIEW = 'pending_review'
    PROCESSING = 'processing'
    EXTRACTED = 'extracted'
    NEEDS_REVIEW = 'needs_review'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    FAILED = 'failed'

    STATUS_CHOICES = [
        (UPLOADED, 'Uploaded'),
        (PENDING_REVIEW, 'Pending review'),
        (PROCESSING, 'Processing'),
        (EXTRACTED, 'Extracted'),
        (NEEDS_REVIEW, 'Needs review'),
        (APPROVED, 'Approved'),
        (REJECTED, 'Rejected'),
        (FAILED, 'Failed'),
    ]

    PDF = 'pdf'
    IMAGE = 'image'
    DOCUMENT_TYPE_CHOICES = [
        (PDF, 'PDF'),
        (IMAGE, 'Image'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pay_plan_documents',
    )
    onboarding = models.ForeignKey(
        PayPlanOnboarding,
        on_delete=models.CASCADE,
        related_name='documents',
    )
    pay_plan = models.ForeignKey(
        PayPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='documents',
    )
    pay_plan_version = models.ForeignKey(
        PayPlanVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='documents',
    )
    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to='pay_plan_documents/%Y/%m/')
    mime_type = models.CharField(max_length=100)
    file_size = models.PositiveIntegerField()
    document_type = models.CharField(max_length=16, choices=DOCUMENT_TYPE_CHOICES)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=UPLOADED, db_index=True)
    page_order = models.PositiveIntegerField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    last_processed_at = models.DateTimeField(null=True, blank=True)
    parser_version = models.CharField(max_length=64, blank=True)
    processing_errors = models.JSONField(default=list, blank=True)
    processing_warnings = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'status'], name='payplan_doc_user_status_idx'),
            models.Index(fields=['onboarding', 'document_type'], name='payplan_doc_onboard_type_idx'),
        ]

    def __str__(self):
        return self.original_filename

    def clean(self):
        super().clean()
        if self.onboarding_id and self.onboarding.user_id != self.user_id:
            raise ValidationError('Document onboarding must belong to the document user.')
        if self.pay_plan_id and self.pay_plan.owner_user_id != self.user_id:
            raise ValidationError('Document pay plan must belong to the document user.')
        if self.pay_plan_version_id:
            if self.pay_plan_version.pay_plan.owner_user_id != self.user_id:
                raise ValidationError('Document version must belong to the document user.')
            if (
                self.pay_plan_id
                and self.pay_plan_version.pay_plan_id != self.pay_plan_id
            ):
                raise ValidationError('Document version must belong to the selected pay plan.')

    @property
    def is_available(self):
        if not self.file or not self.file.name:
            return False
        try:
            return self.file.storage.exists(self.file.name)
        except OSError:
            return False


class PayPlanActivationEvent(models.Model):
    ACTIVATED = 'activated'
    RECALCULATED = 'recalculated'
    ACTION_CHOICES = [
        (ACTIVATED, 'Activated'),
        (RECALCULATED, 'Recalculated'),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='pay_plan_activation_events',
    )
    version = models.ForeignKey(
        PayPlanVersion, on_delete=models.PROTECT, related_name='activation_events',
    )
    previous_version = models.ForeignKey(
        PayPlanVersion, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='replacement_activation_events',
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    reason = models.TextField(blank=True)
    report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def clean(self):
        super().clean()
        if self.version_id and self.version.pay_plan.owner_user_id != self.user_id:
            raise ValidationError('Activation version must belong to the event user.')
        if (
            self.previous_version_id
            and self.previous_version.pay_plan.owner_user_id != self.user_id
        ):
            raise ValidationError('Previous version must belong to the event user.')


class PayPlanEligibility(models.Model):
    NPS_ELIGIBLE = 'eligible'
    NPS_INELIGIBLE = 'ineligible'
    NPS_EXEMPT = 'exempt'
    NPS_PENDING = 'pending'
    NPS_CHOICES = [
        (NPS_ELIGIBLE, 'Eligible'),
        (NPS_INELIGIBLE, 'Not eligible'),
        (NPS_EXEMPT, 'Exempt'),
        (NPS_PENDING, 'Pending or unknown'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pay_plan_eligibilities',
    )
    month_start = models.DateField()
    green_pea = models.BooleanField(
        null=True, blank=True,
        help_text='Whether the Green Pea program applies for this month.',
    )
    ar_requirement_met = models.BooleanField(
        null=True, blank=True,
        help_text='Whether the active plan’s AR requirement is met this month.',
    )
    nps_status = models.CharField(
        max_length=16, choices=NPS_CHOICES, default=NPS_PENDING,
    )
    nps_projection_passing = models.BooleanField(
        null=True,
        blank=True,
        help_text='Private user-entered assumption for the monthly NPS projection.',
    )
    nps_projected_good_surveys = models.PositiveIntegerField(
        default=0,
        help_text='Private projected count of good surveys for this month.',
    )
    nps_projected_bad_surveys = models.PositiveIntegerField(
        default=0,
        help_text='Private projected count of bad surveys for this month.',
    )
    training_requirements_met = models.BooleanField(null=True, blank=True)
    call_requirement_met = models.BooleanField(null=True, blank=True)
    video_requirement_met = models.BooleanField(null=True, blank=True)
    nps_qualifying_surveys = models.PositiveIntegerField(
        default=0,
        help_text='Number of returned NPS surveys with a qualifying score.',
    )
    nps_low_score_surveys = models.PositiveIntegerField(
        default=0,
        help_text='Number of returned NPS surveys scored 8 or below.',
    )
    holiday_bonus_eligible = models.BooleanField(
        null=True,
        blank=True,
        help_text='Whether Holiday Bonus Fund accrual is currently eligible.',
    )
    holiday_bonus_forfeited = models.BooleanField(
        default=False,
        help_text='Whether accrued Holiday Bonus Fund money has been forfeited.',
    )
    custom_values = models.JSONField(
        default=dict,
        blank=True,
        help_text='Additional monthly facts requested by an imported pay plan.',
    )
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_pay_plan_eligibilities',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-month_start', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'month_start'],
                name='unique_payplan_eligibility_month',
            ),
            models.CheckConstraint(
                condition=models.Q(month_start__day=1),
                name='payplan_eligibility_month_first',
            ),
        ]
        indexes = [
            models.Index(
                fields=['user', 'month_start'],
                name='ppeligibility_user_month_idx',
            ),
        ]

    @property
    def nps_finance_eligible(self):
        if self.nps_status in {self.NPS_ELIGIBLE, self.NPS_EXEMPT}:
            return True
        if self.nps_status == self.NPS_INELIGIBLE:
            return False
        return None

    def clean(self):
        super().clean()
        if self.month_start and self.month_start.day != 1:
            raise ValidationError({'month_start': 'Eligibility must use the first day of a month.'})

    def save(self, *args, **kwargs):
        if self.month_start:
            self.month_start = self.month_start.replace(day=1)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.user} eligibility for {self.month_start:%B %Y}'


class PayPlanChangeRequest(models.Model):
    NEEDS_REVIEW = 'needs_review'
    APPLIED = 'applied'
    REJECTED = 'rejected'
    STATUS_CHOICES = [
        (NEEDS_REVIEW, 'Needs review'),
        (APPLIED, 'Applied'),
        (REJECTED, 'Rejected'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pay_plan_change_requests',
    )
    source_version = models.ForeignKey(
        PayPlanVersion,
        on_delete=models.PROTECT,
        related_name='plain_text_source_requests',
    )
    draft_version = models.OneToOneField(
        PayPlanVersion,
        on_delete=models.CASCADE,
        related_name='plain_text_change_request',
    )
    request_text = models.TextField()
    parsed_actions = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    preview = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=NEEDS_REVIEW,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user', 'status'], name='ppchange_user_status_idx'),
        ]


class PayPlanChangePattern(models.Model):
    action_type = models.CharField(max_length=64)
    target_key = models.CharField(max_length=128)
    approved_count = models.PositiveIntegerField(default=0)
    example_request = models.TextField(blank=True)
    last_approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['action_type', 'target_key'],
                name='unique_payplan_change_pattern',
            ),
        ]


class PayPlanDescriptionSubmission(models.Model):
    DRAFT = 'draft'
    SUBMITTED = 'submitted'
    PARSED = 'parsed'
    NEEDS_REVIEW = 'needs_review'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    FAILED = 'failed'

    STATUS_CHOICES = [
        (DRAFT, 'Draft'),
        (SUBMITTED, 'Submitted'),
        (PARSED, 'Parsed'),
        (NEEDS_REVIEW, 'Needs review'),
        (APPROVED, 'Approved'),
        (REJECTED, 'Rejected'),
        (FAILED, 'Failed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pay_plan_descriptions',
    )
    onboarding = models.ForeignKey(
        PayPlanOnboarding,
        on_delete=models.CASCADE,
        related_name='description_submissions',
    )
    pay_plan = models.ForeignKey(
        PayPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='description_submissions',
    )
    description = models.TextField()
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    warnings = models.JSONField(default=list, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'status'], name='payplan_desc_user_status_idx'),
            models.Index(fields=['onboarding', 'status'], name='pp_desc_onboard_status_idx'),
        ]

    def __str__(self):
        return f'Description submission for {self.user}'


class PayPlanConversation(models.Model):
    OPEN = 'open'
    RESOLVED = 'resolved'
    CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (OPEN, 'Open'), (RESOLVED, 'Resolved'), (CANCELLED, 'Cancelled'),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='pay_plan_conversations',
    )
    plan_version = models.ForeignKey(
        PayPlanVersion, on_delete=models.PROTECT, null=True, blank=True,
        related_name='conversations',
    )
    conversation_key = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=OPEN)
    selected_rule_key = models.CharField(max_length=150, blank=True)
    pending_intent = models.JSONField(default=dict, blank=True)
    context = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'conversation_key'],
                name='unique_payplan_conversation_key_per_user',
            ),
        ]
        indexes = [
            models.Index(
                fields=['user', 'status', 'updated_at'],
                name='pp_conv_user_status_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.plan_version_id
            and self.plan_version.pay_plan.owner_user_id != self.user_id
        ):
            raise ValidationError('Conversation plan version must belong to the user.')


class PayPlanConversationTurn(models.Model):
    USER = 'user'
    ASSISTANT = 'assistant'
    SYSTEM = 'system'
    ROLE_CHOICES = [
        (USER, 'User'), (ASSISTANT, 'Assistant'), (SYSTEM, 'System'),
    ]
    conversation = models.ForeignKey(
        PayPlanConversation, on_delete=models.CASCADE, related_name='turns',
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.TextField()
    structured_intent = models.JSONField(default=dict, blank=True)
    sequence = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['conversation', 'sequence']
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'sequence'],
                name='unique_payplan_conversation_turn_sequence',
            ),
        ]
