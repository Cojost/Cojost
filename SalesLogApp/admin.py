from django.contrib import admin
from .models.sales import DailyActivity, MonthlyGoal
from .models import (
    ArchivedVehicle,
    Industry,
    PayPlan,
    PayPlanActivationEvent,
    PayPlanAssignment,
    PayPlanDescriptionSubmission,
    PayPlanDocument,
    PayPlanEligibility,
    PayPlanOnboarding,
    PayPlanRule,
    PayPlanRuleCondition,
    PayPlanVersion,
    PayPlanConversation,
    PayPlanConversationTurn,
    PayPlanAssistantUsageEvent,
    CommissionSandbox,
    ScenarioHistory,
    SandboxHypotheticalDeal,
    SandboxResult,
    SandboxRun,
    UserProfile,
    BillingAccess,
    BillingCheckoutAttempt,
    FounderGrant,
    Vehicle,
    VehicleMake,
    VehicleModel,
)

admin.site.register(DailyActivity)
admin.site.register(MonthlyGoal)
admin.site.register(UserProfile)


@admin.register(Industry)
class IndustryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'slug')


class PayPlanVersionInline(admin.TabularInline):
    model = PayPlanVersion
    extra = 0
    fields = (
        'version_name', 'effective_start_date', 'effective_end_date', 'status',
    )


class PayPlanRuleConditionInline(admin.TabularInline):
    model = PayPlanRuleCondition
    extra = 0
    fields = ('field_name', 'operator', 'value', 'sort_order')


class PayPlanRuleInline(admin.TabularInline):
    model = PayPlanRule
    extra = 0
    fields = (
        'name', 'rule_type', 'calculation_scope', 'condition_group_operator',
        'configuration', 'is_active', 'sort_order',
    )


@admin.register(PayPlan)
class PayPlanAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'industry', 'owner_user', 'dealership_name',
        'is_template', 'is_active',
    )
    list_filter = ('industry', 'is_template', 'is_active')
    search_fields = ('name', 'dealership_name', 'owner_user__username')
    autocomplete_fields = ('owner_user',)
    inlines = (PayPlanVersionInline,)


@admin.register(PayPlanVersion)
class PayPlanVersionAdmin(admin.ModelAdmin):
    list_display = (
        'version_name', 'pay_plan', 'status',
        'effective_start_date', 'effective_end_date',
    )
    list_filter = ('status', 'pay_plan__industry')
    search_fields = ('version_name', 'pay_plan__name', 'pay_plan__owner_user__username')
    autocomplete_fields = ('pay_plan',)
    inlines = (PayPlanRuleInline,)


class PayPlanConversationTurnInline(admin.TabularInline):
    model = PayPlanConversationTurn
    extra = 0
    readonly_fields = ('role', 'content', 'structured_intent', 'sequence', 'created_at')


@admin.register(PayPlanConversation)
class PayPlanConversationAdmin(admin.ModelAdmin):
    list_display = ('conversation_key', 'user', 'plan_version', 'status', 'updated_at')
    list_filter = ('status',)
    search_fields = ('conversation_key', 'user__username')
    inlines = (PayPlanConversationTurnInline,)


@admin.register(PayPlanAssistantUsageEvent)
class PayPlanAssistantUsageEventAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'user', 'route', 'status', 'duration_bucket',
        'model_name',
    )
    list_filter = ('route', 'status', 'duration_bucket', 'model_name')
    search_fields = ('conversation_ref', 'provider_request_id', 'user__username')
    readonly_fields = (
        'user', 'category', 'route', 'status', 'duration_ms',
        'duration_bucket', 'model_name', 'conversation_ref', 'input_tokens',
        'output_tokens', 'provider_request_id', 'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(CommissionSandbox)
class CommissionSandboxAdmin(admin.ModelAdmin):
    list_display = (
        'scenario_name', 'owner', 'source_version', 'draft_version',
        'status', 'revision', 'saved_revision', 'last_calculated_at',
        'updated_at',
    )
    list_filter = ('status',)
    search_fields = ('scenario_name', 'owner__username')
    readonly_fields = ('public_id', 'created_at', 'updated_at')


admin.site.register(SandboxHypotheticalDeal)
admin.site.register(SandboxRun)
admin.site.register(SandboxResult)
admin.site.register(ScenarioHistory)


@admin.register(FounderGrant)
class FounderGrantAdmin(admin.ModelAdmin):
    list_display = (
        'code_prefix', 'created_at', 'expires_at', 'revoked_at',
        'redemption_count', 'redeemed_user', 'trial_days',
    )
    list_filter = ('entitlement_tier', 'created_at', 'revoked_at')
    search_fields = ('code_prefix', 'redeemed_user__username')
    exclude = ('code_digest',)
    readonly_fields = (
        'public_id', 'code_prefix', 'created_at', 'created_by',
        'max_redemptions', 'redemption_count', 'redeemed_user',
        'redeemed_at', 'trial_days', 'entitlement_tier',
        'administrative_note',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BillingAccess)
class BillingAccessAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'introductory_benefit_kind',
        'introductory_benefit_consumed_at', 'last_event_type',
        'last_synchronized_at',
    )
    search_fields = ('user__username',)
    readonly_fields = [field.name for field in BillingAccess._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BillingCheckoutAttempt)
class BillingCheckoutAttemptAdmin(admin.ModelAdmin):
    list_display = (
        'public_id', 'user', 'trial_kind', 'trial_days', 'status',
        'reservation_expires_at', 'confirmed_at',
    )
    list_filter = ('status', 'trial_kind')
    search_fields = ('user__username',)
    readonly_fields = [field.name for field in BillingCheckoutAttempt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PayPlanRule)
class PayPlanRuleAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'pay_plan_version', 'rule_type', 'calculation_scope',
        'is_active', 'sort_order',
    )
    list_filter = ('is_active', 'calculation_scope', 'rule_type')
    search_fields = ('name', 'pay_plan_version__version_name', 'pay_plan_version__pay_plan__name')
    autocomplete_fields = ('pay_plan_version',)
    inlines = (PayPlanRuleConditionInline,)


@admin.register(PayPlanAssignment)
class PayPlanAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'pay_plan_version', 'effective_start_date',
        'effective_end_date', 'is_active',
    )
    list_filter = ('is_active', 'pay_plan_version__pay_plan__industry')
    search_fields = (
        'user__username', 'pay_plan_version__pay_plan__name',
        'pay_plan_version__version_name',
    )
    autocomplete_fields = ('user', 'pay_plan_version')


@admin.register(PayPlanActivationEvent)
class PayPlanActivationEventAdmin(admin.ModelAdmin):
    list_display = ('user', 'version', 'action', 'created_at')
    list_filter = ('action', 'created_at')
    search_fields = ('user__username', 'version__pay_plan__name', 'reason')
    readonly_fields = (
        'user', 'version', 'previous_version', 'action', 'reason', 'report', 'created_at',
    )


@admin.register(PayPlanEligibility)
class PayPlanEligibilityAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'month_start', 'green_pea', 'nps_status', 'ar_requirement_met',
        'training_requirements_met', 'call_requirement_met',
        'video_requirement_met',
    )
    list_filter = ('month_start', 'green_pea', 'nps_status')
    search_fields = ('user__username', 'user__email')
    autocomplete_fields = ('user', 'updated_by')


@admin.register(PayPlanOnboarding)
class PayPlanOnboardingAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'setup_method', 'current_pay_plan', 'current_version', 'updated_at')
    list_filter = ('status', 'setup_method')
    search_fields = ('user__username', 'current_pay_plan__name', 'current_version__version_name')
    autocomplete_fields = ('user', 'current_pay_plan', 'current_version')


@admin.register(PayPlanDocument)
class PayPlanDocumentAdmin(admin.ModelAdmin):
    list_display = ('original_filename', 'user', 'status', 'document_type', 'uploaded_at')
    list_filter = ('status', 'document_type')
    search_fields = ('original_filename', 'user__username')
    autocomplete_fields = ('user', 'onboarding', 'pay_plan', 'pay_plan_version')


@admin.register(PayPlanDescriptionSubmission)
class PayPlanDescriptionSubmissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'pay_plan', 'submitted_at', 'updated_at')
    list_filter = ('status',)
    search_fields = ('user__username', 'description', 'pay_plan__name')
    autocomplete_fields = ('user', 'onboarding', 'pay_plan')


@admin.register(VehicleMake)
class VehicleMakeAdmin(admin.ModelAdmin):
    list_display = ('name', 'verified', 'active', 'created_by')
    list_filter = ('verified', 'active')
    search_fields = ('name', 'normalized_name')


@admin.register(VehicleModel)
class VehicleModelAdmin(admin.ModelAdmin):
    list_display = ('name', 'make', 'verified', 'active', 'created_by')
    list_filter = ('verified', 'active', 'make')
    search_fields = ('name', 'normalized_name', 'make__name')


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ('year', 'make', 'model', 'stock_number', 'vin', 'sale')
    list_filter = ('year', 'make', 'model')
    search_fields = ('vin', 'stock_number')


@admin.register(ArchivedVehicle)
class ArchivedVehicleAdmin(admin.ModelAdmin):
    list_display = ('year', 'make_name', 'model_name', 'stock_number', 'vin')
    list_filter = ('year', 'make_name', 'model_name')
    search_fields = ('vin', 'stock_number')
