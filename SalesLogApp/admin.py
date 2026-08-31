from django.contrib import admin, messages
from allauth.account.admin import EmailAddressAdmin as AllauthEmailAddressAdmin
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db import transaction
from django.db.utils import IntegrityError
from django.http import HttpResponseRedirect

from .auth_forms import (
    NormalizedAdminUserChangeForm,
    NormalizedAdminUserCreationForm,
    NormalizedEmailAddressAdminForm,
)
from .auth_identity import (
    EMAIL_UNAVAILABLE_MESSAGE,
    USERNAME_UNAVAILABLE_MESSAGE,
    email_addresses_matching_email,
    synchronize_primary_address_from_user,
    synchronize_user_from_addresses,
    users_matching_email,
    users_matching_username,
)
from .models.sales import DailyActivity, MonthlyGoal
from .models import (
    ArchivedVehicle,
    AskStewConversation,
    AskStewFeedback,
    AskStewTurn,
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
    Team,
    TeamInvitation,
    TeamMembership,
    Vehicle,
    VehicleMake,
    VehicleModel,
)


class NormalizedUserAdmin(DjangoUserAdmin):
    form = NormalizedAdminUserChangeForm
    add_form = NormalizedAdminUserCreationForm

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and 'email' in form.changed_data:
            synchronize_primary_address_from_user(obj)

    def changeform_view(
        self,
        request,
        object_id=None,
        form_url='',
        extra_context=None,
    ):
        try:
            return super().changeform_view(
                request,
                object_id=object_id,
                form_url=form_url,
                extra_context=extra_context,
            )
        except IntegrityError:
            users_by_name = users_matching_username(
                request.POST.get('username'),
            )
            users_by_email = users_matching_email(request.POST.get('email'))
            addresses = email_addresses_matching_email(
                request.POST.get('email'),
            )
            if object_id:
                users_by_name = users_by_name.exclude(pk=object_id)
                users_by_email = users_by_email.exclude(pk=object_id)
                addresses = addresses.exclude(user_id=object_id)
            username_collision = users_by_name.exists()
            email_collision = users_by_email.exists() or addresses.exists()
            if not username_collision and not email_collision:
                raise
            error = (
                USERNAME_UNAVAILABLE_MESSAGE
                if username_collision
                else EMAIL_UNAVAILABLE_MESSAGE
            )
            self.message_user(request, error, level=messages.ERROR)
            return HttpResponseRedirect(request.get_full_path())


class NormalizedEmailAddressAdmin(AllauthEmailAddressAdmin):
    form = NormalizedEmailAddressAdminForm

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        previous_user_id = None
        if change and obj.pk:
            previous_user_id = EmailAddress.objects.filter(
                pk=obj.pk,
            ).values_list('user_id', flat=True).first()
        super().save_model(request, obj, form, change)
        for user_id in {previous_user_id, obj.user_id} - {None}:
            synchronize_user_from_addresses(user_id)

    def changeform_view(
        self,
        request,
        object_id=None,
        form_url='',
        extra_context=None,
    ):
        try:
            return super().changeform_view(
                request,
                object_id=object_id,
                form_url=form_url,
                extra_context=extra_context,
            )
        except IntegrityError:
            addresses = email_addresses_matching_email(
                request.POST.get('email'),
            )
            if object_id:
                addresses = addresses.exclude(pk=object_id)
            if not addresses.exists():
                raise
            self.message_user(
                request,
                EMAIL_UNAVAILABLE_MESSAGE,
                level=messages.ERROR,
            )
            return HttpResponseRedirect(request.get_full_path())


user_model = get_user_model()
if admin.site.is_registered(user_model):
    admin.site.unregister(user_model)
admin.site.register(user_model, NormalizedUserAdmin)

if admin.site.is_registered(EmailAddress):
    admin.site.unregister(EmailAddress)
admin.site.register(EmailAddress, NormalizedEmailAddressAdmin)

admin.site.register(DailyActivity)
admin.site.register(MonthlyGoal)
admin.site.register(UserProfile)


class ReadOnlyOperationalAdmin(admin.ModelAdmin):
    """Expose operational state without permitting production data mutation."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Team)
class TeamAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        'name', 'owner', 'is_active', 'is_read_only', 'created_at', 'updated_at',
    )
    list_filter = ('is_active', 'is_read_only', 'display_mode')
    search_fields = ('name', 'owner__username', 'owner__email')
    readonly_fields = [field.name for field in Team._meta.fields]


@admin.register(TeamMembership)
class TeamMembershipAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        'team', 'user', 'role', 'status', 'updated_at',
    )
    list_filter = ('role', 'status')
    search_fields = (
        'team__name', 'user__username', 'user__email',
    )
    readonly_fields = [field.name for field in TeamMembership._meta.fields]


@admin.register(TeamInvitation)
class TeamInvitationAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        'token_prefix', 'team', 'intended_email', 'intended_user',
        'expires_at', 'accepted_at', 'revoked_at',
    )
    list_filter = ('accepted_at', 'revoked_at', 'expires_at')
    search_fields = (
        'token_prefix', 'team__name', 'intended_email',
        'intended_user__username',
    )
    exclude = ('token_digest',)
    readonly_fields = [
        field.name for field in TeamInvitation._meta.fields
        if field.name != 'token_digest'
    ]


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


class AskStewTurnInline(admin.TabularInline):
    model = AskStewTurn
    extra = 0
    can_delete = False
    fields = (
        'sequence', 'role', 'content', 'intent', 'route_source',
        'provider_status', 'provider_used', 'verified', 'duration_ms',
        'created_at',
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AskStewConversation)
class AskStewConversationAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        'public_id', 'user', 'status', 'last_intent', 'created_at', 'updated_at',
    )
    list_filter = ('status', 'last_intent', 'created_at')
    search_fields = ('public_id', 'user__username', 'user__email')
    readonly_fields = [field.name for field in AskStewConversation._meta.fields]
    inlines = (AskStewTurnInline,)


@admin.register(AskStewFeedback)
class AskStewFeedbackAdmin(ReadOnlyOperationalAdmin):
    list_display = ('updated_at', 'user', 'helpful', 'assistant_turn')
    list_filter = ('helpful', 'updated_at')
    search_fields = (
        'user__username', 'user__email',
        'assistant_turn__conversation__public_id',
    )
    readonly_fields = [field.name for field in AskStewFeedback._meta.fields]


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
        'user', 'onboarding_required_at', 'introductory_benefit_kind',
        'introductory_benefit_consumed_at', 'enforcement_enrolled_at',
        'enforcement_notice_sent_at', 'enforcement_grace_ends_at',
        'last_event_type', 'last_synchronized_at',
    )
    list_filter = ('introductory_benefit_kind',)
    search_fields = ('user__username',)
    readonly_fields = [field.name for field in BillingAccess._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BillingCheckoutAttempt)
class BillingCheckoutAttemptAdmin(admin.ModelAdmin):
    list_display = (
        'public_id', 'user', 'selected_tier', 'selected_billing_interval',
        'trial_kind', 'trial_days', 'status', 'reservation_expires_at',
        'confirmed_at',
    )
    list_filter = ('status', 'trial_kind', 'selected_billing_interval')
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
