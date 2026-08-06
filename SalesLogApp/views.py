from decimal import Decimal
from datetime import datetime, timedelta
from functools import wraps
from django.http import Http404, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from .models.sales import (
    Sale,
    Commission,
    BonusLevel,
    CommissionAdjustment,
    calculate_bonus,
)
from .forms import (
    SaleForm,
    CommissionAdjustmentForm,
    BonusLevelForm,
    OtherCommissionAdjustmentForm,
    modelformset_factory,
    UserLoginForm,
    CustomUserCreationForm,
    ManualPayPlanRuleForm,
    PayPlanRuleConditionEditForm,
    PayPlanAssistantForm,
    PayPlanAssistantFollowUpForm,
    PayPlanReplacementForm,
    PayPlanSetupForm,
    SandboxActivationForm,
    SandboxComparisonForm,
    SandboxCreateForm,
    SandboxHypotheticalDealForm,
    SandboxReplayForm,
    SandboxRuleForm,
    ScenarioConversionForm,
    ScenarioDeleteForm,
    ScenarioRenameForm,
    ScenarioResetForm,
    ScenarioSaveAsForm,
    ScenarioSaveForm,
)
from .eligibility_forms import DashboardNPSProjectionForm, PayPlanEligibilityForm
from django.utils import timezone
from django.contrib.auth.views import LoginView
from django.contrib.auth import authenticate, login 
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.views.decorators.http import require_http_methods, require_POST
from django.urls import reverse
from django.utils.html import escape
from .models.sales import DailyActivity, MonthlyGoal
from .models.vehicles import VehicleMake, VehicleModel, normalize_catalog_name
from .forms import DailyActivityForm, MonthlyGoalForm
from .forms import AppearanceForm, AvatarForm
from .profile_context import get_user_profile
from .access import (
    get_or_create_onboarding,
    legacy_commission_only,
    pay_plan_onboarding_required,
    sync_active_onboarding_assignment,
    uses_new_engine,
)
from .services import (
    activity_history_context,
    activity_month_context,
    sales_month_context,
)
from .commission_service import CommissionEngineService, CommissionHelpContext
from .nps_projection import NPSSurveyProjectionService
from .plan_requirements import PlanRequirementService
from .pay_plan_management import (
    PayPlanActivationService,
    create_manual_draft,
    create_replacement_draft, create_pasted_replacement_draft,
    preview_version,
    recalculate_commissions,
    reload_existing_document,
)
from .pay_plan_imports import (
    apply_import_draft_to_version,
    build_upload_import_draft,
    mark_submission_review_state,
    parse_description_to_import_draft,
)
from .pay_plan_conversations import (
    ConversationStateError,
    PayPlanConversationService,
)
from .models import (
    PayPlanAssignment,
    PayPlanDescriptionSubmission,
    PayPlanDocument,
    PayPlanEligibility,
    PayPlanChangeRequest,
    PayPlanOnboarding,
    PayPlanRule,
    PayPlanVersion,
    CommissionSandbox,
    SandboxRun,
)
from .models.sales import SaleType
from .sale_types import get_sale_type_handler

def commission_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not Commission.objects.filter(user=request.user).exists():
            return redirect('adjust_commission')
        return view_func(request, *args, **kwargs)
    return wrapper


def _selected_month(request):
    raw = request.GET.get('month') or request.POST.get('month')
    if raw:
        try:
            return datetime.strptime(raw, '%Y-%m').date().replace(day=1)
        except ValueError:
            pass
    return timezone.localdate().replace(day=1)


def _previous_month(month):
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


def _history_range(request, selected_month):
    default_start = selected_month
    for _ in range(11):
        default_start = _previous_month(default_start)

    def parse(name, fallback):
        raw = request.GET.get(name)
        if not raw:
            return fallback
        try:
            return datetime.strptime(raw, '%Y-%m').date().replace(day=1)
        except ValueError:
            return fallback

    start = parse('history_start', default_start)
    end = parse('history_end', selected_month)
    if start > end:
        return default_start, selected_month
    # Bound reports to five years to avoid accidental unbounded work.
    cursor, count = end, 1
    while cursor > start and count <= 60:
        cursor = _previous_month(cursor)
        count += 1
    if count > 60:
        return default_start, selected_month
    return start, end


@pay_plan_onboarding_required
def activity_goals(request, activity_id=None):
    user = request.user
    selected_month = _selected_month(request)
    activity_instance = get_object_or_404(
        DailyActivity, pk=activity_id, user=user
    ) if activity_id is not None else None
    if request.method == 'POST' and request.POST.get('form_type') == 'activity':
        form = DailyActivityForm(request.POST, instance=activity_instance)
        if form.is_valid():
            cleaned = form.cleaned_data
            with transaction.atomic():
                DailyActivity.objects.update_or_create(
                    user=user, date=cleaned['date'],
                    defaults={'leads_taken': cleaned['leads_taken'],
                              'phone_calls_made': cleaned['phone_calls_made']},
                )
            messages.success(request, 'Daily activity saved.')
            return redirect(f"{reverse('activity_goals')}?month={selected_month:%Y-%m}")
    else:
        form = DailyActivityForm(instance=activity_instance)
    goal = MonthlyGoal.objects.filter(user=user, month_start=selected_month).first()
    if request.method == 'POST' and request.POST.get('form_type') == 'goal':
        goal_form = MonthlyGoalForm(request.POST, instance=goal, month_start=selected_month)
        if goal_form.is_valid():
            month = goal_form.cleaned_data['month']
            MonthlyGoal.objects.update_or_create(
                user=user, month_start=month,
                defaults={'target_units': goal_form.cleaned_data['target_units'],
                          'target_commission': goal_form.cleaned_data['target_commission']},
            )
            messages.success(request, 'Monthly goals saved.')
            return redirect(f"{reverse('activity_goals')}?month={month:%Y-%m}")
    else:
        goal_form = MonthlyGoalForm(instance=goal, month_start=selected_month)
    context = activity_month_context(user, selected_month)
    history_start, history_end = _history_range(request, selected_month)
    context.update(activity_history_context(user, history_start, history_end))
    context.update({
        'activity_form': form, 'goal_form': goal_form, 'selected_month': selected_month,
    })
    return render(request, 'activity_goals.html', context)

@pay_plan_onboarding_required
def view_sales(request):
    selected_month = _selected_month(request)
    projection_rules = NPSSurveyProjectionService.rules_for_user(
        request.user, selected_month,
    )
    nps_projection_visible = bool(projection_rules)
    eligibility = PayPlanEligibility.objects.filter(
        user=request.user,
        month_start=selected_month,
    ).first()
    is_projection_post = (
        request.method == 'POST'
        and request.POST.get('form_type') == 'nps_projection'
    )
    if is_projection_post and not nps_projection_visible:
        messages.error(
            request,
            'NPS survey projection is not available for this pay plan.',
        )
        return redirect(f"{reverse('view_sales')}?month={selected_month:%Y-%m}")
    if is_projection_post:
        nps_projection_form = DashboardNPSProjectionForm(
            request.POST, instance=eligibility,
        )
        if nps_projection_form.is_valid():
            eligibility = nps_projection_form.save(commit=False)
            eligibility.user = request.user
            eligibility.month_start = selected_month
            eligibility.updated_by = request.user
            eligibility.save()
            messages.success(
                request,
                'NPS survey projection updated for the selected month.',
            )
            return redirect(
                f"{reverse('view_sales')}?month={selected_month:%Y-%m}"
            )
    else:
        nps_projection_form = DashboardNPSProjectionForm(instance=eligibility)
    context = sales_month_context(request.user, selected_month)
    projection_source = eligibility or PayPlanEligibility()
    nps_projection = NPSSurveyProjectionService.calculate(
        projection_rules,
        selected_month,
        passing=projection_source.nps_projection_passing,
        good_surveys=projection_source.nps_projected_good_surveys,
        bad_surveys=projection_source.nps_projected_bad_surveys,
    )
    context.update({
        'nps_projection_visible': nps_projection_visible,
        'nps_projection_form': nps_projection_form,
        'nps_projection': nps_projection,
    })
    request.session['total_count'] = float(context['total_count'])
    return render(request, 'view_sales.html', context)


@pay_plan_onboarding_required
def print_sales(request):
    context = sales_month_context(request.user, _selected_month(request))
    context.update({
        'printed_on': timezone.localdate(),
        'report_user': request.user,
        'back_url': f"{reverse('view_sales')}?month={context['selected_month']:%Y-%m}",
    })
    return render(request, 'reports/print_sales.html', context)


@pay_plan_onboarding_required
def print_activity_goals(request):
    selected_month = _selected_month(request)
    context = activity_month_context(request.user, selected_month)
    context.update({
        'printed_on': timezone.localdate(),
        'report_user': request.user,
        'back_url': f"{reverse('activity_goals')}?month={selected_month:%Y-%m}",
    })
    return render(request, 'reports/print_activity_goals.html', context)


@pay_plan_onboarding_required
def print_activity_history(request):
    selected_month = _selected_month(request)
    history_start, history_end = _history_range(request, selected_month)
    context = activity_history_context(request.user, history_start, history_end)
    context.update({
        'selected_month': selected_month,
        'printed_on': timezone.localdate(),
        'report_user': request.user,
        'back_url': (
            f"{reverse('activity_goals')}?month={selected_month:%Y-%m}"
            f"&history_start={history_start:%Y-%m}&history_end={history_end:%Y-%m}"
        ),
    })
    return render(request, 'reports/print_activity_history.html', context)


@login_required
def profile(request):
    user_profile = get_user_profile(request.user)
    password_form_class = (
        PasswordChangeForm if request.user.has_usable_password() else SetPasswordForm
    )
    if request.method == 'POST':
        action = request.POST.get('form_type')
        if action == 'appearance':
            appearance_form = AppearanceForm(request.POST, instance=user_profile)
            if appearance_form.is_valid():
                appearance_form.save()
                messages.success(request, 'Appearance settings saved.')
                return redirect('profile')
        elif action == 'reset_header_color':
            user_profile.reset_header_color()
            user_profile.full_clean()
            user_profile.save()
            messages.success(request, 'Header color reset to blue.')
            return redirect('profile')
        elif action == 'avatar':
            old_name = user_profile.avatar.name if user_profile.avatar else ''
            old_storage = user_profile.avatar.storage if user_profile.avatar else None
            avatar_form = AvatarForm(request.POST, request.FILES, instance=user_profile)
            if avatar_form.is_valid():
                avatar_form.save()
                if old_name and old_name != user_profile.avatar.name:
                    old_storage.delete(old_name)
                messages.success(request, 'Profile picture updated.')
                return redirect('profile')
        elif action == 'remove_avatar':
            if user_profile.avatar:
                storage = user_profile.avatar.storage
                old_name = user_profile.avatar.name
                user_profile.avatar = ''
                user_profile.save(update_fields=['avatar', 'updated_at'])
                storage.delete(old_name)
            messages.success(request, 'Profile picture removed.')
            return redirect('profile')
        elif action == 'password':
            password_form = password_form_class(request.user, request.POST)
            if password_form.is_valid():
                changed_user = password_form.save()
                update_session_auth_hash(request, changed_user)
                messages.success(request, 'Password updated securely.')
                return redirect('profile')

    appearance_form = locals().get(
        'appearance_form', AppearanceForm(instance=user_profile)
    )
    avatar_form = locals().get('avatar_form', AvatarForm(instance=user_profile))
    password_form = locals().get(
        'password_form', password_form_class(request.user)
    )
    return render(request, 'profile.html', {
        'profile': user_profile,
        'appearance_form': appearance_form,
        'avatar_form': avatar_form,
        'password_form': password_form,
        'password_is_set': request.user.has_usable_password(),
        'commission_instance': Commission.objects.filter(user=request.user).first(),
    })

@pay_plan_onboarding_required
def add_sale(request):
    commission_instance = Commission.objects.filter(user=request.user).first()
    handler = get_sale_type_handler(SaleType.AUTOMOTIVE)
    if request.method == 'POST':
        form = SaleForm(request.POST)
        vehicle_form = handler.build_form(
            request.POST, user=request.user, require_vehicle=True
        )
        sale_valid = form.is_valid()
        vehicle_valid = vehicle_form.is_valid()
        if sale_valid and vehicle_valid:
            with transaction.atomic():
                sale = form.save(commit=False)
                sale.user = request.user
                sale.save()
                handler.save_details(vehicle_form, sale)
            return redirect('view_sales')
    else:
        form = SaleForm()
        vehicle_form = handler.build_form(user=request.user, require_vehicle=True)
    return render(request, 'add_sale.html', {
        'form': form,
        'vehicle_form': vehicle_form,
        'commission_instance': commission_instance,
    })

@pay_plan_onboarding_required
def edit_sale(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id, user=request.user)
    handler = get_sale_type_handler(sale.sale_type)

    if request.method == 'POST':
        form = SaleForm(request.POST, instance=sale)
        vehicle_form = handler.build_form(
            request.POST, user=request.user, sale=sale, require_vehicle=False
        )
        sale_valid = form.is_valid()
        vehicle_valid = vehicle_form.is_valid()
        if sale_valid and vehicle_valid:
            with transaction.atomic():
                sale = form.save()
                handler.save_details(vehicle_form, sale)
            return redirect('view_sales')
    else:
        form = SaleForm(instance=sale)
        vehicle_form = handler.build_form(
            user=request.user, sale=sale, require_vehicle=False
        )

    return render(request, 'edit_sale.html', {
        'form': form, 'vehicle_form': vehicle_form, 'sale': sale,
    })


@login_required
def vehicle_make_search(request):
    term = request.GET.get('q', '').strip()
    if len(term) > 100:
        return JsonResponse({'results': []}, status=400)
    queryset = VehicleMake.objects.filter(active=True)
    if term:
        queryset = queryset.filter(normalized_name__contains=normalize_catalog_name(term))
    results = list(queryset.order_by('name').values('id', 'name')[:20])
    return JsonResponse({'results': results})


@login_required
def vehicle_model_search(request):
    term = request.GET.get('q', '').strip()
    try:
        make_id = int(request.GET.get('make_id', ''))
    except (TypeError, ValueError):
        return JsonResponse({'results': []}, status=400)
    if len(term) > 100 or not VehicleMake.objects.filter(pk=make_id, active=True).exists():
        return JsonResponse({'results': []}, status=400)
    queryset = VehicleModel.objects.filter(make_id=make_id, active=True)
    if term:
        queryset = queryset.filter(normalized_name__contains=normalize_catalog_name(term))
    return JsonResponse({'results': list(
        queryset.order_by('name').values('id', 'name')[:20]
    )})

@pay_plan_onboarding_required
def delete_sale(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id, user=request.user)

    if request.method == 'POST':
        sale.delete()
        return redirect('view_sales')

    return render(request, 'delete_sale.html', {'sale': sale})

@pay_plan_onboarding_required
def view_commission(request):
    user = request.user
    if request.method == 'POST':
        return redirect('add_sale')

    today = timezone.localdate()
    start_of_month = today.replace(day=1)
    if today.month == 12:
        start_of_next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        start_of_next_month = today.replace(month=today.month + 1, day=1)

    sales = list(
        Sale.objects.filter(
            user=user,
            date__gte=start_of_month,
            date__lt=start_of_next_month,
        ).select_related('vehicle__make', 'vehicle__model').order_by('date', 'dealNumber')
    )

    if uses_new_engine(user):
        context = sales_month_context(request.user, start_of_month)
        help_context = CommissionHelpContext.build(request.user)
        return render(request, 'new_view_commission.html', {
            **context,
            'help_context': help_context,
        })

    commission_instance = get_object_or_404(Commission, user=user)
    bonus_levels = BonusLevel.objects.filter(
        user=user,
        commission=commission_instance,
    )
    other_adjustments = CommissionAdjustment.objects.filter(
        user=user,
        commission=commission_instance,
        active=True,
    )
    total_adjustments = sum(
        (adjustment.signed_amount for adjustment in other_adjustments),
        Decimal('0'),
    )

    def calculate_totals_and_bonuses(current_sales):
        total_count = sum((s.unit_credit for s in current_sales), Decimal('0'))
        total_front_end = sum(s.calculate_frontEnd for s in current_sales)
        total_back_end = sum(s.calculate_backend for s in current_sales)
        total_bonus = calculate_bonus(current_sales, bonus_levels)
        return total_count, total_front_end, total_back_end, total_bonus

    #if request.method == 'POST':
     #   form = SaleForm(request.POST)
      #  if form.is_valid():
       #     sale = form.save(commit=False)
        #    sale.user = user
         #   sale.save()

          #  sales = list(
           #     Sale.objects.filter(
            #        user=user,
             #       date__gte=start_of_month,
              #      date__lt=start_of_next_month,
          #      )
          #  )
            #total_count, total_calculated_front_end, total_calculated_back_end, total_bonus = calculate_totals_and_bonuses(sales)

            #return render(request, 'view_commission.html', {
             #   'commission_instance': commission_instance,
              #  'form': SaleForm(),
               # 'total_count': total_count,
                #'total_front_end': total_calculated_front_end,
               # 'total_back_end': total_calculated_back_end,
                #'total_bonus': total_bonus,
                #'other_adjustments': other_adjustments,
               # 'total_adjustments': total_adjustments,
                #'total_commission': total_calculated_front_end + total_calculated_back_end + total_bonus + total_adjustments,
                #'sales': sales,
           # })
    #else:
    #    form = SaleForm()

    total_count, total_calculated_front_end, total_calculated_back_end, total_bonus = calculate_totals_and_bonuses(sales)

    return render(request, 'view_commission.html', {
        'commission_instance': commission_instance,
        'total_count': total_count,
        'total_front_end': total_calculated_front_end,
        'total_back_end': total_calculated_back_end,
        'total_bonus': total_bonus,
        'other_adjustments': other_adjustments,
        'total_adjustments': total_adjustments,
        'total_commission': total_calculated_front_end + total_calculated_back_end + total_bonus + total_adjustments,
        'sales': sales,
    })




@legacy_commission_only
def adjust_commission(request, commission_id=None):
    user = request.user

    # Fetch the commission instance and associated bonus levels.
    # If the commission_id does not belong to the signed-in user, fall back to the user's own commission.
    if commission_id:
        try:
            commission_instance = Commission.objects.get(pk=commission_id, user=user)
        except Commission.DoesNotExist:
            commission_instance, _ = Commission.objects.get_or_create(user=user)
            return redirect('adjust_commission_by_id', commission_id=commission_instance.id)
    else:
        commission_instance, created = Commission.objects.get_or_create(user=user)
    
    # Query for existing bonus levels linked to this commission
    bonus_levels = BonusLevel.objects.filter(user=request.user, commission=commission_instance)
    other_adjustments = CommissionAdjustment.objects.filter(
        user=request.user,
        commission=commission_instance,
    )
    today = timezone.localdate()
    start_of_month = today.replace(day=1)
    if today.month == 12:
        start_of_next_month = today.replace(year=today.year + 1, month=1, day=1)
    else:
        start_of_next_month = today.replace(month=today.month + 1, day=1)
    sales = Sale.objects.filter(
        user=user,
        date__gte=start_of_month,
        date__lt=start_of_next_month,
    )
    
    total_count = sum((sale.unit_credit for sale in sales), Decimal('0'))


    # Define the formset for managing multiple BonusLevel entries
    BonusLevelFormSet = modelformset_factory(
        BonusLevel,
        fields=('count_threshold', 'amount', 'active'),
        form=BonusLevelForm,
        extra=1,  # Allows adding a new bonus level
        can_delete=True  # Allows deleting bonus levels
    )
    OtherAdjustmentFormSet = modelformset_factory(
        CommissionAdjustment,
        form=OtherCommissionAdjustmentForm,
        extra=1,
        can_delete=True,
    )

    if request.method == 'POST':
        # Handle form and formset submission
        form = CommissionAdjustmentForm(request.POST, instance=commission_instance)
        formset = BonusLevelFormSet(
            request.POST,
            queryset=bonus_levels,
            prefix='unit_bonus',
        )
        adjustment_formset = OtherAdjustmentFormSet(
            request.POST,
            queryset=other_adjustments,
            prefix='other_adjustment',
        )

        if form.is_valid() and formset.is_valid() and adjustment_formset.is_valid():
            # Save the commission adjustments
            form.save()

            # Save the bonus levels, linking them to the correct commission and user
            bonus_levels = formset.save(commit=False)  # Defer saving to add relationships
            for bonus_level in bonus_levels:
                bonus_level.commission = commission_instance  # Associate with commission
                bonus_level.user = request.user  # Ensure the bonus level is linked to the user
                bonus_level.save()

            for bonus_level in formset.deleted_objects:
                bonus_level.delete()

            adjustments = adjustment_formset.save(commit=False)
            for adjustment in adjustments:
                adjustment.commission = commission_instance
                adjustment.user = request.user
                adjustment.save()

            for adjustment in adjustment_formset.deleted_objects:
                adjustment.delete()

            return redirect('view_commission')  # Redirect after successful save
    else:
        # Handle GET request, initializing the form and formset with existing data
        form = CommissionAdjustmentForm(instance=commission_instance)
        formset = BonusLevelFormSet(
            queryset=bonus_levels,
            prefix='unit_bonus',
        )
        adjustment_formset = OtherAdjustmentFormSet(
            queryset=other_adjustments,
            prefix='other_adjustment',
        )

    chart_data = {
        "labels": [level.count_threshold for level in bonus_levels],
        "data": [float(level.amount) for level in bonus_levels],
    }
    # Query for current bonus levels for displaying in the template
    current_bonus_levels = bonus_levels

    return render(request, 'adjust_commission.html', {
        'form': form,
        'formset': formset,
        'adjustment_formset': adjustment_formset,
        'commission': commission_instance,
        'current_bonus_levels': current_bonus_levels,
        'total_count': total_count,
        'chart_data': chart_data,
    })
@legacy_commission_only
def add_bonus(request):
    user = request.user
    commission_instance = get_object_or_404(Commission, user=user)
    BonusLevelFormSet = modelformset_factory(
        BonusLevel,
        form=BonusLevelForm,
        extra=1,  # Allows adding a new bonus level
        can_delete=True  # Allows deleting bonus levels
    )
    
    if request.method == 'POST':
        owned_levels = BonusLevel.objects.filter(user=user, commission=commission_instance)
        formset = BonusLevelFormSet(request.POST, queryset=owned_levels)
        if formset.is_valid():
            bonus_levels = formset.save(commit=False)
            for bonus in bonus_levels:
                bonus.commission = commission_instance
                bonus.user = user
                bonus.save()
            return redirect('view_commission')
    else:
        formset = BonusLevelFormSet(queryset=BonusLevel.objects.filter(user=user, commission=commission_instance))

    return render(request, 'add_bonus.html', {'formset': formset})

class UserLoginView(LoginView):
    template_name = 'login.html'
    authentication_form = UserLoginForm


def register(request):
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            user.backend = 'django.contrib.auth.backends.ModelBackend'  # Set backend
            user = authenticate(username=user.username, password=form.cleaned_data['password1'])
            if user is not None:
                login(request, user)
                profile = get_user_profile(user)
                profile.commission_system = profile.PAY_PLAN_V2
                profile.save(update_fields=['commission_system', 'updated_at'])
                get_or_create_onboarding(user)
                return redirect('pay_plan_setup')
            else:
                form.add_error(None, 'Authentication failed.')
        else:
            form.add_error(None, 'Form is not valid.')
    else:
        form = CustomUserCreationForm()
    return render(request, 'registration/register.html', {'form': form})


@login_required
def pay_plan_setup(request):
    onboarding = get_or_create_onboarding(request.user)
    if not uses_new_engine(request.user):
        return redirect('view_sales')
    if uses_new_engine(request.user) and onboarding.status == PayPlanOnboarding.ACTIVE:
        return redirect('view_sales')

    if request.method == 'POST':
        form = PayPlanSetupForm(request.POST, request.FILES)
        if form.is_valid():
            setup_method = form.cleaned_data['setup_method']
            onboarding.setup_method = setup_method
            onboarding.status = PayPlanOnboarding.METHOD_SELECTED
            onboarding.started_at = onboarding.started_at or timezone.now()
            onboarding.save(update_fields=['setup_method', 'status', 'started_at', 'updated_at'])

            description_text = (form.cleaned_data['description'] or '').strip()
            if setup_method == PayPlanOnboarding.DESCRIBE:
                description = PayPlanDescriptionSubmission.objects.create(
                    user=request.user,
                    onboarding=onboarding,
                    pay_plan=onboarding.current_pay_plan,
                    description=description_text,
                    status=PayPlanDescriptionSubmission.SUBMITTED,
                    submitted_at=timezone.now(),
                    warnings=[],
                )
                onboarding.questionnaire = {
                    **onboarding.questionnaire,
                    'description': description_text,
                    'rule_import_draft': parse_description_to_import_draft(
                        description_text,
                        onboarding.current_pay_plan.name if onboarding.current_pay_plan else 'Imported Plan',
                    ),
                }
                onboarding.submitted_at = description.submitted_at

            if setup_method == PayPlanOnboarding.UPLOAD:
                created_documents = []
                for page_order, uploaded_file in enumerate(
                    form.cleaned_data['documents'],
                    start=1,
                ):
                    mime_type = uploaded_file.content_type
                    created_documents.append(PayPlanDocument.objects.create(
                        user=request.user,
                        onboarding=onboarding,
                        pay_plan=onboarding.current_pay_plan,
                        pay_plan_version=onboarding.current_version,
                        original_filename=uploaded_file.name,
                        file=uploaded_file,
                        mime_type=mime_type,
                        file_size=uploaded_file.size,
                        document_type=PayPlanSetupForm.ALLOWED_CONTENT_TYPES[mime_type],
                        status=PayPlanDocument.PENDING_REVIEW,
                        page_order=page_order,
                    ))
                onboarding.questionnaire = {
                    **onboarding.questionnaire,
                    'rule_import_draft': build_upload_import_draft(
                        created_documents,
                        onboarding.current_pay_plan.name if onboarding.current_pay_plan else 'Imported Plan',
                    ),
                }
                onboarding.submitted_at = timezone.now()

            if setup_method in {
                PayPlanOnboarding.DESCRIBE,
                PayPlanOnboarding.UPLOAD,
            }:
                onboarding.status = PayPlanOnboarding.SUBMITTED
            else:
                onboarding.status = PayPlanOnboarding.NEEDS_REVIEW
            onboarding.save(update_fields=[
                'status',
                'questionnaire',
                'submitted_at',
                'updated_at',
            ])
            return redirect('pay_plan_review')
    else:
        form = PayPlanSetupForm(initial={
            'setup_method': onboarding.setup_method or PayPlanOnboarding.ASSISTED,
            'description': onboarding.questionnaire.get('description', ''),
        })

    return render(request, 'pay_plan_setup.html', {
        'onboarding': onboarding,
        'form': form,
    })


@login_required
def pay_plan_review(request):
    onboarding = get_or_create_onboarding(request.user)
    if not uses_new_engine(request.user):
        return redirect('view_sales')
    if request.method == 'POST':
        action = request.POST.get('action')
        version = onboarding.current_version
        import_draft = onboarding.questionnaire.get('rule_import_draft') or {}
        version_belongs_to_user = bool(
            version
            and (
                version.pay_plan.owner_user_id == request.user.id
                or version.pay_plan.is_template
            )
        )
        if action == 'approve_import' and version_belongs_to_user:
            apply_result = apply_import_draft_to_version(version, import_draft, overwrite=True)
            if apply_result['created_rules'] == 0:
                onboarding.status = PayPlanOnboarding.NEEDS_REVIEW
                onboarding.last_error = 'No valid rules were imported from the draft.'
                onboarding.save(update_fields=['status', 'last_error', 'updated_at'])
                mark_submission_review_state(onboarding, approved=False)
                messages.error(
                    request,
                    'No valid rules were created from the import draft. Review required.',
                )
                return redirect('pay_plan_review')

            import_draft['approved'] = True
            import_draft['approved_at'] = timezone.now().isoformat()
            import_draft['apply_result'] = apply_result
            onboarding.questionnaire = {
                **onboarding.questionnaire,
                'rule_import_draft': import_draft,
            }
            onboarding.status = PayPlanOnboarding.READY_TO_ACTIVATE
            onboarding.last_error = ''
            onboarding.save(update_fields=['questionnaire', 'status', 'last_error', 'updated_at'])
            mark_submission_review_state(onboarding, approved=True)
            messages.success(
                request,
                f"Import draft approved with {apply_result['created_rules']} rule(s).",
            )
            return redirect('pay_plan_review')

        if action == 'activate' and version_belongs_to_user:
            active_rule_count = version.rules.filter(is_active=True).count()
            if active_rule_count == 0:
                onboarding.status = PayPlanOnboarding.NEEDS_REVIEW
                onboarding.last_error = 'No active rules are available on this plan version.'
                onboarding.save(update_fields=['status', 'last_error', 'updated_at'])
                messages.error(
                    request,
                    'Activation blocked: the selected plan has no active commission rules.',
                )
                return redirect('pay_plan_review')
            onboarding.status = PayPlanOnboarding.ACTIVE
            onboarding.completed_at = timezone.now()
            onboarding.last_error = ''
            onboarding.save(update_fields=['status', 'completed_at', 'last_error', 'updated_at'])
            sync_active_onboarding_assignment(request.user)
            return redirect('view_sales')
        if action == 'activate':
            onboarding.status = PayPlanOnboarding.FAILED
            onboarding.last_error = 'No valid pay-plan version is ready to activate.'
            onboarding.save(update_fields=['status', 'last_error', 'updated_at'])
            messages.error(
                request,
                'A valid pay-plan version is required before activation.',
            )
        if action == 'save_for_later':
            onboarding.status = PayPlanOnboarding.NEEDS_REVIEW
            onboarding.save(update_fields=['status', 'updated_at'])
            return redirect('pay_plan_setup')

    documents = onboarding.documents.all().order_by('page_order', 'id')
    descriptions = onboarding.description_submissions.all().order_by('-created_at')
    import_draft = onboarding.questionnaire.get('rule_import_draft') or {}
    return render(request, 'pay_plan_review.html', {
        'onboarding': onboarding,
        'documents': documents,
        'descriptions': descriptions,
        'import_draft': import_draft,
        'can_activate': bool(
            onboarding.current_version
            and onboarding.current_version.rules.filter(is_active=True).exists()
        ),
        'description_preview': escape(descriptions.first().description) if descriptions else '',
    })


@login_required
def replace_pay_plan(request):
    if not uses_new_engine(request.user):
        messages.error(request, 'Replacement plans are available only for the new pay-plan engine.')
        return redirect('view_commission')
    summary = CommissionEngineService.active_plan_summary(request.user)
    initial_name = summary['plan'].name if summary.get('plan') else 'Automotive Pay Plan'
    if request.method == 'POST':
        form = PayPlanReplacementForm(request.POST, request.FILES)
        if form.is_valid():
            if form.cleaned_data.get('documents'):
                version = create_replacement_draft(
                    request.user,
                    form.cleaned_data['documents'],
                    form.cleaned_data['plan_name'],
                    form.cleaned_data['effective_start_date'],
                )
            else:
                version = create_pasted_replacement_draft(
                    request.user,
                    form.cleaned_data['pasted_text'],
                    form.cleaned_data['plan_name'],
                    form.cleaned_data['effective_start_date'],
                )
            messages.success(
                request,
                'Replacement uploaded as a draft. Your current plan remains active.',
            )
            return redirect('replacement_pay_plan_review', version_id=version.id)
    else:
        form = PayPlanReplacementForm(initial={
            'plan_name': initial_name,
            'apply_from': PayPlanReplacementForm.FUTURE_ONLY,
        })
    return render(request, 'pay_plan_replace.html', {
        'form': form,
        'active_plan_summary': summary,
    })


@require_POST
@login_required
def reload_pay_plan(request):
    if not uses_new_engine(request.user):
        messages.error(request, 'Reload is available only for the new pay-plan engine.')
        return redirect('view_commission')
    document = (
        PayPlanDocument.objects.filter(user=request.user)
        .exclude(file='')
        .order_by('-uploaded_at', '-id')
        .first()
    )
    if document is None or not document.is_available:
        messages.error(
            request,
            'The original file is no longer available. Upload a replacement plan.',
        )
        return redirect('replace_pay_plan')
    raw_date = request.POST.get('effective_start_date')
    try:
        effective_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        effective_date = timezone.localdate().replace(day=1)
    try:
        version = reload_existing_document(request.user, document, effective_date)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return redirect('view_commission')
    messages.success(
        request,
        'The source file was reprocessed into a new draft. Your active plan was not changed.',
    )
    return redirect('replacement_pay_plan_review', version_id=version.id)


@require_POST
@login_required
def edit_pay_plan_manually(request):
    if not uses_new_engine(request.user):
        return redirect('view_commission')
    try:
        version = create_manual_draft(
            request.user, timezone.localdate().replace(day=1),
        )
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return redirect('view_commission')
    messages.success(
        request,
        'A manual-edit draft was created. The active plan remains unchanged.',
    )
    return redirect('replacement_pay_plan_review', version_id=version.id)


@login_required
def pay_plan_assistant(request):
    if not uses_new_engine(request.user):
        return redirect('view_commission')
    conversation = None
    resolution = None
    follow_up_form = PayPlanAssistantFollowUpForm()
    initial_date = timezone.localdate() + timedelta(days=1)
    form = PayPlanAssistantForm(initial={'effective_date': initial_date})

    if request.method == 'POST':
        action = request.POST.get('assistant_action') or 'start'
        conversation_key = request.POST.get('conversation_key', '').strip()
        if action == 'follow_up':
            follow_up_form = PayPlanAssistantFollowUpForm(request.POST)
            if follow_up_form.is_valid():
                try:
                    outcome = PayPlanConversationService.follow_up(
                        request.user,
                        conversation_key,
                        response_text=follow_up_form.cleaned_data['response_text'],
                        candidate_index=request.POST.get('candidate_choice'),
                    )
                except ObjectDoesNotExist as exc:
                    raise Http404('Conversation not found.') from exc
                except ValidationError as exc:
                    follow_up_form.add_error(None, '; '.join(exc.messages))
                else:
                    conversation = outcome.conversation
                    resolution = outcome.resolution
                    follow_up_form = PayPlanAssistantFollowUpForm()
            if conversation is None and conversation_key:
                try:
                    outcome = PayPlanConversationService.resume(
                        request.user, conversation_key,
                    )
                except ObjectDoesNotExist as exc:
                    raise Http404('Conversation not found.') from exc
                else:
                    conversation = outcome.conversation
                    resolution = outcome.resolution
        elif action == 'cancel':
            try:
                outcome = PayPlanConversationService.cancel(
                    request.user, conversation_key,
                )
            except ObjectDoesNotExist as exc:
                raise Http404('Conversation not found.') from exc
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
            else:
                messages.info(request, 'The conversation was cancelled. No draft was created.')
                conversation = outcome.conversation
        elif action == 'start_over':
            try:
                outcome = PayPlanConversationService.start_over(
                    request.user, conversation_key,
                )
            except ObjectDoesNotExist as exc:
                raise Http404('Conversation not found.') from exc
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
            else:
                return redirect(
                    f"{reverse('pay_plan_assistant')}?conversation="
                    f'{outcome.conversation.conversation_key}'
                )
        elif action == 'create_draft' and conversation_key:
            try:
                change_request = PayPlanConversationService.create_draft(
                    request.user, conversation_key,
                )
            except ObjectDoesNotExist as exc:
                raise Http404('Conversation not found.') from exc
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
                try:
                    outcome = PayPlanConversationService.resume(
                        request.user, conversation_key,
                    )
                except ObjectDoesNotExist as resume_exc:
                    raise Http404('Conversation not found.') from resume_exc
                conversation = outcome.conversation
                resolution = outcome.resolution
            else:
                messages.success(
                    request,
                    'The inactive draft was created from your confirmed '
                    'interpretation. Review it before activation.',
                )
                return redirect(
                    'replacement_pay_plan_review',
                    version_id=change_request.draft_version_id,
                )
        else:
            # The no-key create_draft branch preserves the pre-Phase 1D trusted
            # POST contract while still passing through a scoped conversation.
            form = PayPlanAssistantForm(request.POST)
            if form.is_valid():
                try:
                    if conversation_key:
                        outcome = PayPlanConversationService.begin_existing(
                            request.user,
                            conversation_key,
                            form.cleaned_data['request_text'],
                            form.cleaned_data['effective_date'],
                        )
                    else:
                        outcome = PayPlanConversationService.start(
                            request.user,
                            form.cleaned_data['request_text'],
                            form.cleaned_data['effective_date'],
                        )
                except ObjectDoesNotExist as exc:
                    raise Http404('Conversation not found.') from exc
                except ValidationError as exc:
                    form.add_error('request_text', '; '.join(exc.messages))
                else:
                    conversation = outcome.conversation
                    resolution = outcome.resolution
                    if action == 'create_draft':
                        try:
                            change_request = PayPlanConversationService.create_draft(
                                request.user,
                                conversation.conversation_key,
                            )
                        except ValidationError as exc:
                            form.add_error('request_text', '; '.join(exc.messages))
                        else:
                            messages.success(
                                request,
                                'The inactive draft was created from your '
                                'confirmed interpretation. Review it before '
                                'activation.',
                            )
                            return redirect(
                                'replacement_pay_plan_review',
                                version_id=change_request.draft_version_id,
                            )
    elif request.GET.get('conversation'):
        try:
            outcome = PayPlanConversationService.resume(
                request.user,
                request.GET['conversation'],
            )
        except ObjectDoesNotExist as exc:
            raise Http404('Conversation not found.') from exc
        except ConversationStateError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            conversation = outcome.conversation
            resolution = outcome.resolution

    history = PayPlanChangeRequest.objects.filter(user=request.user)[:10]
    open_conversations = PayPlanConversationService.open_for_user(request.user)
    return render(request, 'pay_plan_assistant.html', {
        'form': form,
        'follow_up_form': follow_up_form,
        'history': history,
        'open_conversations': open_conversations,
        'conversation': conversation,
        'conversation_turns': (
            conversation.turns.order_by('sequence') if conversation else ()
        ),
        'resolution': resolution,
    })


@login_required
def replacement_pay_plan_review(request, version_id):
    version = get_object_or_404(
        PayPlanVersion.objects.select_related('pay_plan', 'previous_version'),
        id=version_id,
        pay_plan__owner_user=request.user,
        is_sandbox=False,
    )
    if version.status not in {
        PayPlanVersion.DRAFT, PayPlanVersion.REVIEW_REQUIRED,
    }:
        messages.error(request, 'Only draft versions can be reviewed for activation.')
        return redirect('pay_plan_history')

    manual_form = ManualPayPlanRuleForm()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_rule':
            manual_form = ManualPayPlanRuleForm(request.POST)
            if manual_form.is_valid():
                with transaction.atomic():
                    rule = PayPlanRule(
                        pay_plan_version=version,
                        name=manual_form.cleaned_data['name'],
                        rule_type=manual_form.cleaned_data['rule_type'],
                        calculation_scope=manual_form.cleaned_data['calculation_scope'],
                        configuration=manual_form.cleaned_data['configuration'],
                        sort_order=version.rules.count() + 1,
                    )
                    try:
                        rule.full_clean()
                        rule.save()
                        for order, condition in enumerate(
                            manual_form.cleaned_data.get('conditions') or [], start=1,
                        ):
                            rule.conditions.create(sort_order=order, **condition)
                    except (ValidationError, TypeError, KeyError) as exc:
                        transaction.set_rollback(True)
                        manual_form.add_error(None, str(exc))
                    else:
                        version.processing_errors = []
                        version.processing_status = 'needs_review'
                        version.save(update_fields=[
                            'processing_errors', 'processing_status', 'updated_at',
                        ])
                        messages.success(request, 'Manual rule added to the draft.')
                        return redirect('replacement_pay_plan_review', version_id=version.id)
        elif action == 'activate':
            warnings_approved = request.POST.get('approve_warnings') == 'on'
            try:
                report = PayPlanActivationService.activate(
                    request.user, version, warnings_approved=warnings_approved,
                    reason='User confirmed replacement after review',
                )
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
            else:
                messages.success(
                    request,
                    f"Pay plan {version.version_name} activated. "
                    f"{report['sales_tested']} sales reviewed; "
                    f"{report['calculated_count']} calculated; "
                    f"{report['excluded_count']} need attention.",
                )
                return redirect('view_commission')

    preview = preview_version(request.user, version)
    try:
        plain_text_change = version.plain_text_change_request
    except PayPlanChangeRequest.DoesNotExist:
        plain_text_change = None
    return render(request, 'pay_plan_replacement_review.html', {
        'version': version,
        'rules': version.rules.prefetch_related('conditions').all(),
        'documents': version.documents.filter(user=request.user),
        'preview': preview,
        'manual_rule_form': manual_form,
        'plain_text_change': plain_text_change,
    })


@login_required
def pay_plan_history(request):
    versions = PayPlanVersion.objects.filter(
        pay_plan__owner_user=request.user,
        is_sandbox=False,
    ).select_related('pay_plan', 'previous_version').prefetch_related('documents')
    return render(request, 'pay_plan_history.html', {'versions': versions})


@login_required
def pay_plan_rules(request, version_id):
    version = get_object_or_404(
        PayPlanVersion,
        id=version_id,
        pay_plan__owner_user=request.user,
        is_sandbox=False,
    )
    return render(request, 'pay_plan_rules.html', {
        'version': version,
        'rules': version.rules.prefetch_related('conditions').all(),
        'can_edit_rules': True,
    })


@login_required
def edit_pay_plan_rule(request, version_id, rule_id):
    can_edit_any = request.user.has_perm('SalesLogApp.change_payplanrule')
    queryset = PayPlanRule.objects.select_related(
        'pay_plan_version__pay_plan',
    ).prefetch_related('conditions').filter(
        pay_plan_version__is_sandbox=False,
    )
    if not can_edit_any:
        queryset = queryset.filter(
            pay_plan_version__pay_plan__owner_user=request.user,
        )
    rule = get_object_or_404(
        queryset,
        pk=rule_id,
        pay_plan_version_id=version_id,
    )
    if request.method == 'POST':
        form = PayPlanRuleConditionEditForm(request.POST, rule=rule)
        if form.is_valid():
            with transaction.atomic():
                locked_queryset = PayPlanRule.objects.select_for_update().select_related(
                    'pay_plan_version__pay_plan',
                ).filter(pay_plan_version__is_sandbox=False)
                if not can_edit_any:
                    locked_queryset = locked_queryset.filter(
                        pay_plan_version__pay_plan__owner_user=request.user,
                    )
                locked_rule = get_object_or_404(
                    locked_queryset,
                    pk=rule.pk,
                    pay_plan_version_id=version_id,
                )
                locked_rule.full_clean()
                vehicle_conditions = locked_rule.conditions.filter(
                    field_name='vehicle_condition',
                )
                existing_order = (
                    vehicle_conditions.order_by('sort_order', 'id')
                    .values_list('sort_order', flat=True)
                    .first()
                )
                vehicle_conditions.delete()
                selected = form.cleaned_data['vehicle_condition']
                if selected:
                    from .commission_engine.validators import validate_condition
                    condition_data = {
                        'field_name': 'vehicle_condition',
                        'operator': 'equals',
                        'value': selected,
                    }
                    validate_condition(condition_data)
                    locked_rule.conditions.create(
                        **condition_data,
                        sort_order=(
                            existing_order
                            if existing_order is not None
                            else locked_rule.conditions.count() + 1
                        ),
                    )
            messages.success(
                request,
                f'{rule.name} vehicle condition updated. Calculations now use '
                'the saved condition.',
            )
            return redirect(
                'edit_pay_plan_rule',
                version_id=version_id,
                rule_id=rule_id,
            )
    else:
        form = PayPlanRuleConditionEditForm(rule=rule)
    return render(request, 'pay_plan_rule_edit.html', {
        'rule': rule,
        'version': rule.pay_plan_version,
        'form': form,
    })


@require_POST
@login_required
def recalculate_pay_plan_commissions(request):
    if not uses_new_engine(request.user):
        messages.error(request, 'Legacy users continue to use legacy commission settings.')
        return redirect('view_commission')
    try:
        report = recalculate_commissions(request.user)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    else:
        messages.success(
            request,
            f"Recalculation reviewed {report['sales_tested']} sales: "
            f"{report['calculated_count']} calculated, "
            f"{report['excluded_count']} need attention. "
            f"Total commission: ${Decimal(report['new_total']):.2f}.",
        )
    return redirect('view_commission')


@login_required
def pay_plan_eligibility(request):
    if not uses_new_engine(request.user):
        messages.error(request, 'Monthly eligibility applies only to the new pay-plan engine.')
        return redirect('view_commission')
    plan_requirements = PlanRequirementService.get_for_user(request.user)
    enabled_requirements = [
        key for key in (
            'nps', 'nps_bonus', 'ar', 'green_pea', 'training', 'calls',
            'video', 'holiday',
        )
        if plan_requirements.get(key)
    ]
    if not plan_requirements['has_monthly_requirements']:
        messages.info(
            request,
            'Your active pay plan does not contain monthly eligibility requirements.',
        )
        return redirect('view_commission')
    raw_month = request.GET.get('month') or request.POST.get('month_start')
    try:
        selected_month = datetime.strptime(raw_month, '%Y-%m').date()
    except (TypeError, ValueError):
        selected_month = timezone.localdate().replace(day=1)
    instance = PayPlanEligibility.objects.filter(
        user=request.user, month_start=selected_month,
    ).first()
    if request.method == 'POST':
        form = PayPlanEligibilityForm(
            request.POST, instance=instance,
            enabled_requirements=enabled_requirements,
        )
        if form.is_valid():
            eligibility = form.save(commit=False)
            eligibility.user = request.user
            eligibility.updated_by = request.user
            eligibility.save()
            messages.success(
                request,
                f'Eligibility settings saved for {eligibility.month_start:%B %Y}. '
                'Commission explanations now use this monthly status.',
            )
            return redirect(
                f"{reverse('pay_plan_eligibility')}?month={eligibility.month_start:%Y-%m}"
            )
    else:
        form = PayPlanEligibilityForm(
            instance=instance,
            initial={'month_start': selected_month},
            enabled_requirements=enabled_requirements,
        )
    history = PayPlanEligibility.objects.filter(user=request.user)[:12]
    return render(request, 'pay_plan_eligibility.html', {
        'form': form,
        'selected_month': selected_month,
        'eligibility': instance,
        'history': history,
        'plan_requirements': plan_requirements,
    })

def create_default_bonus_levels(user):
    commission = Commission.objects.filter(user=user).first()
    if commission:
        BonusLevel.objects.create(user=user, level=1, amount=1000.00, commission=commission)
        BonusLevel.objects.create(user=user, level=2, amount=2000.00, commission=commission)
        # Add more levels as required

def update_bonus_level(request, level_id):
    if request.method == 'POST':
        level = get_object_or_404(BonusLevel, id=level_id)
        new_amount = request.POST.get('bonus-amount')
        level.amount = new_amount
        level.save()

        # Send back the updated bonus level to refresh the chart
        updated_level = {
            'id': level.id,
            'level': level.level,
            'amount': level.amount,
        }
        return JsonResponse({'success': True, 'updated_level': updated_level})

    return JsonResponse({'success': False}, status=400)


def _sandbox_or_404(request, sandbox_id):
    from .sandbox_services import SandboxManager
    try:
        return SandboxManager.get_for_user(request.user, sandbox_id)
    except PermissionDenied:
        from django.http import Http404
        raise Http404('Sandbox not found.')


@login_required
def commission_sandbox_index(request):
    if not uses_new_engine(request.user):
        messages.error(request, 'Commission Sandbox requires the pay-plan engine.')
        return redirect('view_commission')
    if request.method == 'POST':
        form = SandboxCreateForm(request.POST, user=request.user)
        if form.is_valid():
            from .sandbox_services import SandboxManager
            sandbox = SandboxManager.create(
                request.user,
                form.cleaned_data['source_version'],
                form.cleaned_data['scenario_name'],
                form.cleaned_data['scenario_notes'],
            )
            messages.success(request, 'Private sandbox created.')
            return redirect('commission_sandbox_detail', sandbox_id=sandbox.public_id)
    else:
        form = SandboxCreateForm(user=request.user)
    all_scenarios = list(CommissionSandbox.objects.filter(
        owner=request.user,
    ).select_related('source_version', 'draft_version'))
    for sandbox in all_scenarios:
        try:
            difference = Decimal(str(
                (sandbox.calculation_summary or {}).get('difference', '0')
            ))
        except Exception:
            difference = Decimal('0')
        sandbox.difference_value = difference
        sandbox.difference_class = (
            'higher' if difference > 0
            else 'lower' if difference < 0
            else 'unchanged'
        )
        sandbox.warning_count_value = int(
            (sandbox.calculation_summary or {}).get('warning_count', 0) or 0
        )
    scenario_sort = request.GET.get('sort', 'recent')
    active_scenarios = [
        item for item in all_scenarios
        if item.status != CommissionSandbox.ARCHIVED
    ]
    if scenario_sort == 'name':
        active_scenarios.sort(key=lambda item: item.scenario_name.casefold())
    elif scenario_sort == 'increase':
        active_scenarios.sort(
            key=lambda item: item.difference_value, reverse=True,
        )
    elif scenario_sort == 'decrease':
        active_scenarios.sort(key=lambda item: item.difference_value)
    elif scenario_sort == 'warnings':
        active_scenarios.sort(
            key=lambda item: item.warning_count_value, reverse=True,
        )
    else:
        scenario_sort = 'recent'
        active_scenarios.sort(key=lambda item: item.updated_at, reverse=True)
    archived_scenarios = [
        item for item in all_scenarios
        if item.status == CommissionSandbox.ARCHIVED
    ]
    archived_scenarios.sort(key=lambda item: item.updated_at, reverse=True)
    return render(request, 'commission_sandbox_index.html', {
        'form': form,
        'comparison_form': SandboxComparisonForm(user=request.user),
        'sandboxes': active_scenarios,
        'active_scenarios': active_scenarios,
        'archived_scenarios': archived_scenarios,
        'scenario_sort': scenario_sort,
    })


@login_required
def commission_sandbox_compare(request):
    if not uses_new_engine(request.user):
        messages.error(request, 'Commission Sandbox requires the pay-plan engine.')
        return redirect('view_commission')
    if request.method == 'GET':
        initial = {}
        scenario_id = request.GET.get('scenario')
        if (
            scenario_id
            and scenario_id.isdigit()
            and CommissionSandbox.objects.filter(
                owner=request.user,
                pk=scenario_id,
            ).exists()
        ):
            initial['sandboxes'] = [scenario_id]
        return render(request, 'commission_sandbox_compare.html', {
            'form': SandboxComparisonForm(
                user=request.user,
                initial=initial,
            ),
            'comparison': None,
        })
    form = SandboxComparisonForm(request.POST, user=request.user)
    if not form.is_valid():
        return render(request, 'commission_sandbox_compare.html', {
            'form': form,
            'comparison': None,
        })
    start, end = form.date_range()
    from .scenario_services import ScenarioComparisonService
    try:
        comparison = ScenarioComparisonService.compare(
            request.user,
            list(form.cleaned_data['sandboxes']),
            start=start,
            end=end,
        )
    except ValidationError as exc:
        form.add_error(None, exc)
        return render(request, 'commission_sandbox_compare.html', {
            'form': form,
            'comparison': None,
        })
    for deal in comparison['deals']:
        deal['cells'] = [
            {
                'scenario': entry['scenario'],
                'result': deal['scenarios'].get(
                    str(entry['scenario'].public_id)
                ),
            }
            for entry in comparison['scenarios']
        ]
    return render(request, 'commission_sandbox_compare.html', {
        'form': form,
        'comparison': comparison,
        'runs': [item['run'] for item in comparison['scenarios']],
        'period_start': start,
        'period_end': end,
    })


@login_required
def commission_sandbox_detail(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    latest_run = sandbox.runs.prefetch_related(
        'results__production_sale', 'results__hypothetical_deal',
    ).first()
    from .scenario_services import ScenarioCalculationService
    try:
        stale_reasons = ScenarioCalculationService.stale_reasons(
            request.user, sandbox,
        )
    except (ValidationError, PermissionDenied):
        stale_reasons = ['The scenario calculation state could not be verified.']
    return render(request, 'commission_sandbox_detail.html', {
        'sandbox': sandbox,
        'scenario_selector': CommissionSandbox.objects.filter(
            owner=request.user,
        ).order_by('-updated_at'),
        'rules': sandbox.draft_version.rules.prefetch_related(
            'conditions'
        ).order_by('sort_order', 'id'),
        'hypothetical_deals': sandbox.hypothetical_deals.all(),
        'replay_form': SandboxReplayForm(),
        'hypothetical_form': SandboxHypotheticalDealForm(
            initial={'date': timezone.localdate(), 'count': Decimal('1')},
        ),
        'save_form': ScenarioSaveForm(initial={
            'description': sandbox.scenario_notes,
            'assumptions': sandbox.assumptions,
        }),
        'latest_run': latest_run,
        'calculation_summary': sandbox.calculation_summary,
        'stale_reasons': stale_reasons,
        'history': sandbox.history.select_related('actor')[:50],
    })


@login_required
def commission_sandbox_rule(request, sandbox_id, rule_id=None):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if sandbox.status != CommissionSandbox.DRAFT:
        messages.error(
            request,
            'Archived and converted scenarios are read-only. '
            'Restore or duplicate the scenario to edit its rules.',
        )
        return redirect(
            'commission_sandbox_detail', sandbox_id=sandbox.public_id,
        )
    rule = None
    if rule_id is not None:
        rule = get_object_or_404(
            PayPlanRule,
            pk=rule_id,
            pay_plan_version=sandbox.draft_version,
            pay_plan_version__is_sandbox=True,
        )
    if request.method == 'POST':
        form = SandboxRuleForm(request.POST, rule=rule)
        if form.is_valid():
            from .sandbox_services import SandboxRuleEditor
            try:
                SandboxRuleEditor.save(
                    sandbox, rule=rule, data=form.cleaned_data,
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, 'Sandbox rule saved.')
                return redirect(
                    'commission_sandbox_detail', sandbox_id=sandbox.public_id,
                )
    else:
        form = SandboxRuleForm(rule=rule)
    return render(request, 'commission_sandbox_rule.html', {
        'sandbox': sandbox, 'rule': rule, 'form': form,
    })


@login_required
@require_POST
def commission_sandbox_rule_action(request, sandbox_id, rule_id, action):
    sandbox = _sandbox_or_404(request, sandbox_id)
    from .sandbox_services import SandboxRuleEditor
    actions = {
        'duplicate': lambda: SandboxRuleEditor.duplicate(sandbox, rule_id),
        'toggle': lambda: SandboxRuleEditor.toggle(sandbox, rule_id),
        'delete': lambda: SandboxRuleEditor.delete(sandbox, rule_id),
        'up': lambda: SandboxRuleEditor.move(sandbox, rule_id, 'up'),
        'down': lambda: SandboxRuleEditor.move(sandbox, rule_id, 'down'),
    }
    if action not in actions:
        messages.error(request, 'Unsupported sandbox rule action.')
    else:
        try:
            actions[action]()
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Sandbox rule updated.')
    return redirect('commission_sandbox_detail', sandbox_id=sandbox.public_id)


@login_required
@require_POST
def commission_sandbox_replay(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    form = SandboxReplayForm(request.POST)
    if form.is_valid():
        start, end = form.date_range()
        from .scenario_services import (
            ScenarioCalculationService, ScenarioHistoryService,
        )
        try:
            run = ScenarioCalculationService.recalculate(
                request.user, sandbox, mode=SandboxRun.REPLAY,
                start=start, end=end,
            )
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            ScenarioHistoryService.record(
                request.user,
                sandbox,
                'historical_replay',
                'Historical replay range calculated.',
                {'start': start, 'end': end},
            )
            messages.success(
                request,
                f'Replayed {run.statistics["sales_tested"]} deals. '
                f'Difference: ${run.difference:.2f}.',
            )
    else:
        messages.error(request, 'Choose a valid replay range.')
    return redirect('commission_sandbox_detail', sandbox_id=sandbox.public_id)


@login_required
@require_POST
def commission_sandbox_hypothetical(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if sandbox.status != CommissionSandbox.DRAFT:
        messages.error(request, 'Only a draft sandbox can be edited.')
        return redirect(
            'commission_sandbox_detail', sandbox_id=sandbox.public_id,
        )
    form = SandboxHypotheticalDealForm(request.POST)
    if form.is_valid():
        deal = form.save(sandbox=sandbox)
        from .sandbox_services import SandboxCompiler
        SandboxCompiler.invalidate(sandbox)
        from .scenario_services import ScenarioHistoryService
        ScenarioHistoryService.record(
            request.user,
            sandbox,
            'hypothetical_sale_added',
            'Hypothetical sale added.',
            {'hypothetical_sale_id': deal.pk},
        )
        messages.success(request, 'Hypothetical deal added without creating a sale.')
    else:
        messages.error(
            request,
            'Hypothetical deal was not added: '
            + '; '.join(
                error for errors in form.errors.values() for error in errors
            ),
        )
    return redirect('commission_sandbox_detail', sandbox_id=sandbox.public_id)


@login_required
@require_POST
def commission_sandbox_project(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    from .scenario_services import ScenarioCalculationService
    try:
        run = ScenarioCalculationService.recalculate(
            request.user, sandbox, mode=SandboxRun.PROJECTION,
        )
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    else:
        messages.success(
            request,
            f'Projected {run.statistics["hypothetical_deals"]} hypothetical deals.',
        )
    return redirect('commission_sandbox_detail', sandbox_id=sandbox.public_id)


@login_required
@require_POST
def commission_sandbox_activate(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    messages.error(
        request,
        'Direct sandbox activation is disabled. Create a pay-plan draft, '
        'review it, and activate it through the normal confirmation workflow.',
    )
    return redirect('commission_sandbox_convert', sandbox_id=sandbox.public_id)


@login_required
@require_POST
def commission_sandbox_archive(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    from .scenario_services import ScenarioService
    try:
        ScenarioService.archive(request.user, sandbox, confirmed=True)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    else:
        messages.success(
            request,
            'Scenario archived. Production data was not changed.',
        )
    return redirect('commission_sandbox_index')


@login_required
@require_http_methods(['GET', 'POST'])
def commission_sandbox_delete(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if request.method == 'POST':
        form = ScenarioDeleteForm(request.POST, scenario=sandbox)
        if form.is_valid():
            from .scenario_services import ScenarioService
            try:
                ScenarioService.delete(
                    request.user, sandbox, confirmed=True,
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(
                    request,
                    'Scenario permanently deleted. Production data was not changed.',
                )
                return redirect('commission_sandbox_index')
    else:
        form = ScenarioDeleteForm(scenario=sandbox)
    return render(request, 'commission_scenario_delete.html', {
        'sandbox': sandbox,
        'form': form,
    })


@login_required
@require_POST
def commission_sandbox_save(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    form = ScenarioSaveForm(request.POST)
    if form.is_valid():
        from .scenario_services import ScenarioService
        try:
            ScenarioService.save(
                request.user,
                sandbox,
                description=form.cleaned_data['description'],
                assumptions=form.cleaned_data['assumptions'],
            )
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, 'Scenario saved and calculation refreshed.')
    else:
        messages.error(
            request,
            'Scenario was not saved: ' + '; '.join(
                error for errors in form.errors.values() for error in errors
            ),
        )
    return redirect('commission_sandbox_detail', sandbox_id=sandbox.public_id)


@login_required
@require_http_methods(['GET', 'POST'])
def commission_sandbox_save_as(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if request.method == 'POST':
        form = ScenarioSaveAsForm(
            request.POST, user=request.user, scenario=None,
        )
        if form.is_valid():
            from .scenario_services import ScenarioService
            try:
                clone = ScenarioService.save_as(
                    request.user,
                    sandbox,
                    form.cleaned_data['name'],
                    form.cleaned_data['description'],
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(
                    request,
                    'Independent scenario snapshot saved.',
                )
                return redirect(
                    'commission_sandbox_detail',
                    sandbox_id=clone.public_id,
                )
    else:
        form = ScenarioSaveAsForm(
            user=request.user,
            initial={
                'name': f'{sandbox.scenario_name} Copy',
                'description': sandbox.scenario_notes,
            },
        )
    from .scenario_services import ScenarioComparisonService
    differences = ScenarioComparisonService.compare_rules(
        sandbox.source_version,
        sandbox.draft_version,
    )
    modified_rule_count = sum(
        len(differences[key]) for key in ('added', 'removed', 'modified')
    )
    return render(request, 'commission_scenario_save_as.html', {
        'sandbox': sandbox,
        'form': form,
        'rule_count': sandbox.draft_version.rules.count(),
        'modified_rule_count': modified_rule_count,
        'hypothetical_count': sandbox.hypothetical_deals.count(),
    })


@login_required
@require_POST
def commission_sandbox_duplicate(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    from .scenario_services import ScenarioCloneService
    try:
        duplicate = ScenarioCloneService.duplicate(request.user, sandbox)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return redirect(
            'commission_sandbox_detail', sandbox_id=sandbox.public_id,
        )
    messages.success(
        request,
        f'{duplicate.scenario_name} created as an independent scenario.',
    )
    return redirect(
        'commission_sandbox_detail', sandbox_id=duplicate.public_id,
    )


@login_required
@require_http_methods(['GET', 'POST'])
def commission_sandbox_rename(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if request.method == 'POST':
        form = ScenarioRenameForm(
            request.POST, user=request.user, scenario=sandbox,
        )
        if form.is_valid():
            from .scenario_services import ScenarioService
            try:
                sandbox = ScenarioService.rename(
                    request.user, sandbox, form.cleaned_data['name'],
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, 'Scenario renamed.')
                return redirect(
                    'commission_sandbox_detail',
                    sandbox_id=sandbox.public_id,
                )
    else:
        form = ScenarioRenameForm(
            user=request.user,
            scenario=sandbox,
            initial={'name': sandbox.scenario_name},
        )
    return render(request, 'commission_scenario_rename.html', {
        'sandbox': sandbox,
        'form': form,
    })


@login_required
@require_POST
def commission_sandbox_restore(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    from .scenario_services import ScenarioService
    try:
        restored = ScenarioService.restore(
            request.user, sandbox, confirmed=True,
        )
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return redirect('commission_sandbox_index')
    messages.success(request, 'Scenario restored as an editable draft.')
    return redirect(
        'commission_sandbox_detail', sandbox_id=restored.public_id,
    )


@login_required
@require_POST
def commission_sandbox_recalculate(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    from .scenario_services import ScenarioCalculationService
    try:
        run = ScenarioCalculationService.recalculate(
            request.user, sandbox,
        )
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    else:
        messages.success(
            request,
            f'Scenario recalculated for {run.statistics["sales_tested"]} deals. '
            f'Difference: ${run.difference:.2f}.',
        )
    return redirect(
        'commission_sandbox_detail', sandbox_id=sandbox.public_id,
    )


@login_required
@require_http_methods(['GET', 'POST'])
def commission_sandbox_reset(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if request.method == 'POST':
        form = ScenarioResetForm(request.POST)
        if form.is_valid():
            from .scenario_services import ScenarioService
            try:
                reset = ScenarioService.reset(
                    request.user,
                    sandbox,
                    retain_hypothetical_sales=form.cleaned_data[
                        'retain_hypothetical_sales'
                    ],
                    retain_replay_settings=form.cleaned_data[
                        'retain_replay_settings'
                    ],
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(
                    request,
                    'Scenario rules reset to a fresh isolated source copy.',
                )
                return redirect(
                    'commission_sandbox_detail',
                    sandbox_id=reset.public_id,
                )
    else:
        form = ScenarioResetForm(initial={
            'retain_hypothetical_sales': True,
            'retain_replay_settings': True,
        })
    return render(request, 'commission_scenario_reset.html', {
        'sandbox': sandbox,
        'form': form,
    })


@login_required
@require_http_methods(['GET', 'POST'])
def commission_sandbox_convert(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if request.method == 'POST':
        form = ScenarioConversionForm(request.POST)
        if form.is_valid():
            from .scenario_services import ScenarioConversionService
            try:
                version = ScenarioConversionService.convert(
                    request.user,
                    sandbox,
                    form.cleaned_data['effective_start_date'],
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(
                    request,
                    f'{version.version_name} was created as a review-only '
                    'pay-plan draft. Your active plan was not changed.',
                )
                return redirect(
                    'replacement_pay_plan_review',
                    version_id=version.id,
                )
    else:
        form = ScenarioConversionForm(initial={
            'effective_start_date': timezone.localdate(),
        })
    from .scenario_services import ScenarioValidationService
    try:
        ScenarioValidationService.validate(request.user, sandbox)
        sandbox.refresh_from_db()
    except (ValidationError, PermissionDenied):
        pass
    return render(request, 'commission_scenario_convert.html', {
        'sandbox': sandbox,
        'form': form,
        'rule_count': sandbox.draft_version.rules.count(),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def commission_sandbox_hypothetical_edit(
    request, sandbox_id, hypothetical_id,
):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if sandbox.status != CommissionSandbox.DRAFT:
        messages.error(request, 'Only a draft scenario can be edited.')
        return redirect(
            'commission_sandbox_detail', sandbox_id=sandbox.public_id,
        )
    deal = get_object_or_404(
        sandbox.hypothetical_deals,
        pk=hypothetical_id,
    )
    if request.method == 'POST':
        form = SandboxHypotheticalDealForm(
            request.POST, instance=deal,
        )
        if form.is_valid():
            form.save(sandbox=sandbox)
            from .sandbox_services import SandboxCompiler
            SandboxCompiler.invalidate(sandbox)
            from .scenario_services import ScenarioHistoryService
            ScenarioHistoryService.record(
                request.user,
                sandbox,
                'hypothetical_sale_updated',
                'Hypothetical sale updated.',
                {'hypothetical_sale_id': deal.pk},
            )
            messages.success(request, 'Hypothetical deal updated.')
            return redirect(
                'commission_sandbox_detail',
                sandbox_id=sandbox.public_id,
            )
    else:
        form = SandboxHypotheticalDealForm(instance=deal)
    return render(request, 'commission_scenario_hypothetical_form.html', {
        'sandbox': sandbox,
        'deal': deal,
        'form': form,
    })


@login_required
@require_POST
def commission_sandbox_hypothetical_delete(
    request, sandbox_id, hypothetical_id,
):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if sandbox.status != CommissionSandbox.DRAFT:
        messages.error(request, 'Only a draft scenario can be edited.')
        return redirect(
            'commission_sandbox_detail', sandbox_id=sandbox.public_id,
        )
    deal = get_object_or_404(
        sandbox.hypothetical_deals,
        pk=hypothetical_id,
    )
    deal_id = deal.pk
    deal.delete()
    from .sandbox_services import SandboxCompiler
    SandboxCompiler.invalidate(sandbox)
    from .scenario_services import ScenarioHistoryService
    ScenarioHistoryService.record(
        request.user,
        sandbox,
        'hypothetical_sale_deleted',
        'Hypothetical sale deleted.',
        {'hypothetical_sale_id': deal_id},
    )
    messages.success(
        request,
        'Hypothetical deal deleted. Production sales were not changed.',
    )
    return redirect(
        'commission_sandbox_detail', sandbox_id=sandbox.public_id,
    )
