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
    CommissionSandbox,
    ScenarioHistory,
    SandboxHypotheticalDeal,
    SandboxResult,
    SandboxRun,
    UserProfile,
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
