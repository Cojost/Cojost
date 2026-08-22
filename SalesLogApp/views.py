from decimal import Decimal
from datetime import datetime, timedelta
from functools import wraps
import logging
import mimetypes
from uuid import uuid4
from django.http import FileResponse, Http404, JsonResponse
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
    BasicPayPlanActivationForm,
    BasicPayPlanReplacementForm,
    BasicPayPlanRuleForm,
    AskStewQuestionForm,
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
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired
from django.db import IntegrityError, connection, transaction
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
    require_safe,
)
from django.urls import reverse
from django.utils.html import escape
from .models.sales import DailyActivity, MonthlyGoal
from .models.vehicles import VehicleMake, VehicleModel, normalize_catalog_name
from .forms import DailyActivityForm, MonthlyGoalForm, SellingDayClosureForm
from .forms import AppearanceForm, AvatarForm
from .profile_context import get_user_profile
from django.conf import settings

from .ask_stew import AskStewAnswer, AskStewService
from .ask_stew_entitlements import ask_stew_ai_required
from .ask_stew_provider import ask_stew_provider_availability
from .billing_entitlements import get_billing_entitlement
from .billing_plans import BASIC, MONTH, PRO, YEAR
from .billing_pricing import (
    display_plan_prices,
    display_price,
    synchronized_plan_price_errors,
)
from .access import (
    activity_goals_authorized,
    activity_goals_pro_required,
    get_or_create_onboarding,
    internal_pay_plan_tool_required,
    legacy_commission_only,
    pay_plan_onboarding_required,
    sync_active_onboarding_assignment,
    uses_new_engine,
)
from .services import (
    activity_history_context,
    activity_month_context,
    month_bounds,
    sales_month_context,
)
from .selling_calendar import SellingDayCalendarError
from .stew_coach_calendar import owner_selling_calendar
from .stew_coach_nudges import active_nudges
from .stew_coach_phrasing import (
    StewCoachPhrasingError,
    deterministic_coach_message,
    phrase_coach_message,
)
from .stew_coach_presentation import (
    present_projection,
    unavailable_projection_context,
)
from .stew_coach_projection import (
    StewCoachProjectionError,
    StewCoachProjectionService,
)
from .commission_service import CommissionEngineService, CommissionHelpContext
from .nps_projection import NPSSurveyProjectionService
from .plan_requirements import ActivePayPlanService, PlanRequirementService
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
    PayPlanConversationService,
)
from .pay_plan_intents.openai_provider import provider_availability_for_user
from .models import (
    PayPlanAssignment,
    PayPlanDescriptionSubmission,
    PayPlanDocument,
    PayPlanEligibility,
    PayPlanChangeRequest,
    PayPlanOnboarding,
    PayPlanRule,
    PayPlanRuleCondition,
    PayPlanVersion,
    CommissionSandbox,
    SandboxRun,
    SellingDayClosure,
    StewCoachNudgeDismissal,
)
from .models.nudges import NUDGE_KEYS
from .models.sales import SaleType
from .sale_types import get_sale_type_handler


logger = logging.getLogger(__name__)


@require_safe
def landing_page(request):
    """Show the public StewLog story and send signed-in users home."""
    if request.user.is_authenticated:
        return redirect('view_sales')
    public_plans = ()
    if (
        settings.BILLING_FEATURE_ENABLED
        and settings.BILLING_TIERED_PRICING_ENABLED
        and not synchronized_plan_price_errors()
    ):
        prices = display_plan_prices()
        if all(price.available for price in prices.values()):
            public_plans = (
                {
                    'name': 'Basic',
                    'monthly_price': prices[(BASIC, MONTH)],
                    'yearly_price': prices[(BASIC, YEAR)],
                    'description': 'Track sales, pay-plan rules, and commission.',
                },
                {
                    'name': 'Pro',
                    'monthly_price': prices[(PRO, MONTH)],
                    'yearly_price': prices[(PRO, YEAR)],
                    'description': 'Add Activity & Goals and Stew Coach.',
                },
            )
    return render(request, 'landing_page.html', {
        'public_plans': public_plans,
        'standard_trial_days': settings.BILLING_STANDARD_TRIAL_DAYS,
    })


def _is_internal_pay_plan_user(user):
    return user.is_staff or user.is_superuser


def _resolved_manual_rule_errors(errors):
    """Remove only parser errors that a valid manual rule can resolve."""

    resolved_phrases = (
        'no usable rules were extracted',
        'active version had no rules to clone',
    )
    return [
        error for error in (errors or [])
        if not any(phrase in str(error).lower() for phrase in resolved_phrases)
    ]


def _create_guided_conditions(rule, conditions):
    for order, values in enumerate(conditions, start=1):
        condition = PayPlanRuleCondition(
            rule=rule,
            sort_order=order,
            **values,
        )
        condition.full_clean()
        condition.save()


def _pay_plan_changes(version):
    previous = version.previous_version
    if previous is None:
        return {'added': [rule.name for rule in version.rules.all()], 'changed': [], 'removed': []}

    current_rules = {
        str(rule.semantic_key): rule
        for rule in version.rules.prefetch_related('conditions').all()
    }
    previous_rules = {
        str(rule.semantic_key): rule
        for rule in previous.rules.prefetch_related('conditions').all()
    }
    added = [rule.name for key, rule in current_rules.items() if key not in previous_rules]
    removed = [rule.name for key, rule in previous_rules.items() if key not in current_rules]
    changed = []
    for key in current_rules.keys() & previous_rules.keys():
        current = current_rules[key]
        old = previous_rules[key]
        current_conditions = [condition.as_dict() for condition in current.conditions.all()]
        old_conditions = [condition.as_dict() for condition in old.conditions.all()]
        if (
            current.name != old.name
            or current.rule_type != old.rule_type
            or current.configuration != old.configuration
            or current.is_active != old.is_active
            or current_conditions != old_conditions
        ):
            changed.append(current.name)
    return {'added': added, 'changed': changed, 'removed': removed}


def _customer_draft_messages(version):
    errors = []
    for error in version.processing_errors or []:
        lowered = str(error).lower()
        if 'no usable rules' in lowered or 'no rules to clone' in lowered:
            errors.append(
                'No usable commission rules were found. Add the missing rules '
                'below or upload a clearer document.'
            )
        else:
            errors.append(
                'A rule could not be validated. Compare each draft rule with '
                'your document and correct the affected rule.'
            )
    warnings = []
    for warning in version.processing_warnings or []:
        lowered = str(warning).lower()
        if 'ocr is not available' in lowered:
            message = (
                'Text could not be read automatically from an image. Add the '
                'rules below or upload a text-based PDF.'
            )
        elif 'contains no extractable text' in lowered:
            message = 'A document page had no readable text. Check the rules below carefully.'
        elif 'pdf could not be read' in lowered:
            message = 'A PDF could not be read safely. Try a clearer file or add the rules below.'
        elif 'no commission rules were recognized' in lowered:
            message = 'No commission rules were recognized. Add the missing rules below.'
        elif 'confidence is below' in lowered:
            message = 'The document needs a careful review before activation.'
        elif lowered.startswith('compilation:'):
            message = 'Some plan wording could not be converted into a rule. Compare the draft with your document.'
        elif any(
            phrase in lowered
            for phrase in (
                'nps survey-count', 'used-vehicle monthly deduction',
                'draw recovery', 'holiday bonus fund',
            )
        ):
            message = str(warning)
        else:
            message = (
                'Some plan wording needs manual review. Compare the draft '
                'rules with your document before activation.'
            )
        if message not in warnings:
            warnings.append(message)
    return {'errors': errors, 'warnings': warnings}


def _is_hypothetical_deal_number_conflict(exc):
    cause = getattr(exc, '__cause__', None)
    constraint_name = getattr(
        getattr(cause, 'diag', None), 'constraint_name', None,
    )
    if constraint_name is not None:
        return constraint_name == 'unique_sandbox_hypothetical_deal'
    if connection.vendor == 'sqlite':
        message = str(exc).lower()
        return (
            'unique constraint failed:' in message
            and 'sandboxhypotheticaldeal.sandbox_id' in message
            and 'sandboxhypotheticaldeal.dealnumber' in message
        )
    return False


def _add_validation_error(form, exc):
    if hasattr(exc, 'error_dict'):
        for field_name, errors in exc.error_dict.items():
            target = field_name if field_name in form.fields else None
            for error in errors:
                form.add_error(target, error)
        return
    form.add_error(None, exc)


def _form_error_message(form, prefix):
    return prefix + '; '.join(
        str(error)
        for errors in form.errors.values()
        for error in errors
    )

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


def _stew_coach_context(user, selected_month):
    """Build the SC-3 presentation context; fail closed on any error."""

    month_start, next_month = month_bounds(selected_month)
    month_end = next_month - timedelta(days=1)
    closures = list(
        SellingDayClosure.objects.filter(
            user=user, date__gte=month_start, date__lte=month_end,
        ).order_by('date')
    )
    try:
        calendar = owner_selling_calendar(
            user, month_start=month_start, month_end=month_end,
        )
        result = StewCoachProjectionService.calculate(
            owner=user,
            month_start=month_start,
            as_of_date=timezone.localdate(),
            calendar=calendar,
        )
        projection = present_projection(result)
    except (SellingDayCalendarError, StewCoachProjectionError):
        projection = unavailable_projection_context()
    return {
        'stew_coach': projection,
        'closures': closures,
        'stew_coach_message': _stew_coach_message_context(user, projection),
    }


def _coach_phrase_token_salt(user):
    return f'stew-coach-phrase:{user.pk}'


def _new_coach_phrase_token(user):
    return signing.dumps(
        uuid4().hex, salt=_coach_phrase_token_salt(user), compress=True,
    )


def _stew_coach_message_context(user, projection):
    """SC-4 deterministic coach message; the provider is never called on GET."""

    text = None
    if projection.get('available'):
        try:
            text = deterministic_coach_message(projection)
        except StewCoachPhrasingError:
            text = None
    availability = ask_stew_provider_availability(user)
    ai_available = bool(text) and availability['available']
    return {
        'text': text,
        'notice': '',
        'provider_used': False,
        'ai_available': ai_available,
        'token': _new_coach_phrase_token(user) if ai_available else '',
    }


def _apply_coach_phrase_post(request, context):
    """Apply an SC-4 AI-wording request; every failure keeps verified text."""

    coach = context.get('stew_coach_message') or {}
    projection = context.get('stew_coach') or {}
    if not coach.get('text') or not projection.get('available'):
        return
    token = str(request.POST.get('submission_token', ''))[:256]
    try:
        signing.loads(
            token,
            salt=_coach_phrase_token_salt(request.user),
            max_age=3600,
        )
    except (BadSignature, SignatureExpired):
        coach['notice'] = (
            'This coaching request expired. Refresh the page and try again.'
        )
        return
    if request.session.get('stew_coach_last_phrase_token') == token:
        coach['notice'] = (
            'That coaching request was already processed. No duplicate AI '
            'request was sent.'
        )
        return
    request.session['stew_coach_last_phrase_token'] = token
    try:
        result = phrase_coach_message(
            request.user, projection, submission_token=token,
        )
    except Exception as exc:
        logger.error(
            'Unexpected Stew Coach phrasing failure for user_id=%s error_type=%s',
            request.user.pk,
            type(exc).__name__,
        )
        coach['notice'] = (
            'AI wording is temporarily unavailable. This coaching note comes '
            'directly from StewLog\u2019s verified projections.'
        )
        return
    coach['text'] = result.message
    coach['notice'] = result.notice
    coach['provider_used'] = result.provider_used
    if coach.get('ai_available'):
        coach['token'] = _new_coach_phrase_token(request.user)


def _stew_nudges_context(user, projection, next_name):
    """SC-5 in-app nudges; fail closed to no nudges on any error."""

    try:
        nudges = active_nudges(user, projection)
    except Exception as exc:
        logger.warning(
            'Stew Coach nudges unavailable for user_id=%s error_type=%s',
            getattr(user, 'pk', None),
            type(exc).__name__,
        )
        nudges = ()
    return {'stew_nudges': nudges, 'stew_nudges_next': next_name}


def _pro_upgrade_prompt_context(user):
    """SC-6 upgrade prompt for all current users; fails closed to hidden.

    Shown only while the staged billing flags expose billing pages, and only
    to authenticated users without Pro access. No per-user cohort gating by
    owner decision (rollout to all current users).
    """

    prompt = None
    try:
        if (
            (
                settings.BILLING_FEATURE_ENABLED
                or settings.BILLING_ENFORCEMENT_ENABLED
            )
            and getattr(user, 'is_authenticated', False)
            and not activity_goals_authorized(user)
        ):
            price = (
                display_price()
                if settings.BILLING_TIERED_PRICING_ENABLED
                else None
            )
            prompt = {
                'price_available': bool(price and price.available),
                'price_formatted': price.formatted if price else '',
            }
    except Exception as exc:
        logger.warning(
            'Pro upgrade prompt unavailable for user_id=%s error_type=%s',
            getattr(user, 'pk', None),
            type(exc).__name__,
        )
        prompt = None
    return {'pro_upgrade_prompt': prompt}


@login_required
@require_http_methods(['POST'])
def dismiss_stew_nudge(request):
    """Dismiss one owner-scoped nudge for one month."""

    nudge_key = str(request.POST.get('nudge_key', ''))[:32]
    month = _selected_month(request)
    destination = (
        'view_sales'
        if request.POST.get('next') == 'view_sales' else 'activity_goals'
    )
    if nudge_key in NUDGE_KEYS:
        try:
            StewCoachNudgeDismissal.objects.get_or_create(
                user=request.user,
                nudge_key=nudge_key,
                month_start=month,
            )
        except (IntegrityError, ValidationError):
            pass
    return redirect(f"{reverse(destination)}?month={month:%Y-%m}")


@activity_goals_pro_required
@pay_plan_onboarding_required
@require_http_methods(['GET', 'POST'])
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
            with transaction.atomic():
                MonthlyGoal.objects.update_or_create(
                    user=user, month_start=month,
                    defaults={
                        'target_units': goal_form.cleaned_data['target_units'],
                        'target_total_gross': (
                            goal_form.cleaned_data['target_total_gross']
                        ),
                        'target_commission': (
                            goal_form.cleaned_data['target_commission']
                        ),
                    },
                )
            messages.success(request, 'Monthly goals saved.')
            return redirect(f"{reverse('activity_goals')}?month={month:%Y-%m}")
    else:
        goal_form = MonthlyGoalForm(instance=goal, month_start=selected_month)
    if request.method == 'POST' and request.POST.get('form_type') == 'closure':
        closure_form = SellingDayClosureForm(request.POST)
        if closure_form.is_valid():
            closure = closure_form.save(commit=False)
            closure.user = user
            try:
                closure.save()
            except (IntegrityError, ValidationError):
                messages.error(
                    request,
                    'That closure date is already on your selling calendar.',
                )
            else:
                messages.success(request, 'Selling-day closure added.')
            return redirect(
                f"{reverse('activity_goals')}?month={selected_month:%Y-%m}"
            )
    else:
        closure_form = SellingDayClosureForm()
    if (
        request.method == 'POST'
        and request.POST.get('form_type') == 'closure_delete'
    ):
        try:
            closure_id = int(request.POST.get('closure_id', ''))
        except (TypeError, ValueError):
            closure_id = None
        deleted = 0
        if closure_id is not None:
            deleted, _unused = SellingDayClosure.objects.filter(
                pk=closure_id, user=user,
            ).delete()
        if deleted:
            messages.success(request, 'Selling-day closure removed.')
        else:
            messages.error(request, 'That closure could not be removed.')
        return redirect(
            f"{reverse('activity_goals')}?month={selected_month:%Y-%m}"
        )
    context = activity_month_context(user, selected_month)
    history_start, history_end = _history_range(request, selected_month)
    context.update(activity_history_context(user, history_start, history_end))
    context.update(_stew_coach_context(user, selected_month))
    if (
        request.method == 'POST'
        and request.POST.get('form_type') == 'coach_phrase'
    ):
        _apply_coach_phrase_post(request, context)
    context.update(_stew_nudges_context(
        user, context['stew_coach'], 'activity_goals',
    ))
    context.update({
        'activity_form': form, 'goal_form': goal_form, 'selected_month': selected_month,
        'closure_form': closure_form,
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
    if activity_goals_authorized(request.user):
        stew_context = _stew_coach_context(request.user, selected_month)
        context.update(_stew_nudges_context(
            request.user, stew_context['stew_coach'], 'view_sales',
        ))
    else:
        context.update({'stew_nudges': (), 'stew_nudges_next': 'view_sales'})
    context.update(_pro_upgrade_prompt_context(request.user))
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


@activity_goals_pro_required
@pay_plan_onboarding_required
@require_GET
def print_activity_goals(request):
    selected_month = _selected_month(request)
    context = activity_month_context(request.user, selected_month)
    context.update({
        'printed_on': timezone.localdate(),
        'report_user': request.user,
        'back_url': f"{reverse('activity_goals')}?month={selected_month:%Y-%m}",
    })
    return render(request, 'reports/print_activity_goals.html', context)


@activity_goals_pro_required
@pay_plan_onboarding_required
@require_GET
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
        **_pro_upgrade_prompt_context(request.user),
    })


@login_required
def profile_avatar_file(request, user_id, filename):
    """Serve only the authenticated user's exact stored avatar file."""
    if request.user.pk != user_id:
        raise Http404('Profile picture not found.')
    if not filename or '/' in filename or '\\' in filename:
        raise Http404('Profile picture not found.')
    user_profile = get_user_profile(request.user)
    expected_name = f'profile_avatars/{user_id}/{filename}'
    if not user_profile.avatar or user_profile.avatar.name != expected_name:
        raise Http404('Profile picture not found.')
    content_type = mimetypes.guess_type(filename)[0]
    if content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
        raise Http404('Profile picture not found.')
    try:
        avatar_file = user_profile.avatar.storage.open(expected_name, 'rb')
    except FileNotFoundError as exc:
        raise Http404('Profile picture not found.') from exc
    except OSError as exc:
        message = 'Avatar storage read failed.'
        sanitized = RuntimeError(message)
        logger.exception(
            message,
            exc_info=(RuntimeError, sanitized, exc.__traceback__),
        )
        raise Http404('Profile picture not found.') from exc
    response = FileResponse(avatar_file, content_type=content_type)
    response['Cache-Control'] = 'private, max-age=3600'
    response['X-Content-Type-Options'] = 'nosniff'
    return response

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
    """Retire the legacy signup path so every account uses allauth policy."""
    return redirect('account_signup')


@login_required
def pay_plan_setup(request):
    if not _is_internal_pay_plan_user(request.user):
        messages.info(request, 'Use My Pay Plan to upload and review your pay plan.')
        return redirect('my_pay_plan')
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
    if not _is_internal_pay_plan_user(request.user):
        messages.info(request, 'Use My Pay Plan to review your pay-plan draft.')
        return redirect('my_pay_plan')
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
def my_pay_plan(request):
    if not uses_new_engine(request.user):
        messages.info(
            request,
            'Your current commission settings are available on View Commission.',
        )
        return redirect('view_commission')
    summary = CommissionEngineService.active_plan_summary(request.user)
    active_version = None
    active_version_id = summary.get('pay_plan_version_id')
    if active_version_id:
        active_version = get_object_or_404(
            PayPlanVersion.objects.prefetch_related('rules__conditions'),
            id=active_version_id,
            pay_plan__owner_user=request.user,
            is_sandbox=False,
        )
    draft = (
        PayPlanVersion.objects.filter(
            pay_plan__owner_user=request.user,
            is_sandbox=False,
            status__in=(PayPlanVersion.DRAFT, PayPlanVersion.REVIEW_REQUIRED),
        )
        .select_related('pay_plan', 'previous_version')
        .prefetch_related('rules__conditions')
        .order_by('-updated_at', '-id')
        .first()
    )
    return render(request, 'my_pay_plan.html', {
        'active_plan_summary': summary,
        'active_version': active_version,
        'active_rules': active_version.rules.all() if active_version else (),
        'draft': draft,
        'draft_changes': _pay_plan_changes(draft) if draft else None,
        'draft_messages': _customer_draft_messages(draft) if draft else None,
        'is_internal_user': _is_internal_pay_plan_user(request.user),
    })


@login_required
def replace_pay_plan(request):
    if not uses_new_engine(request.user):
        messages.error(request, 'Replacement plans are available only for the new pay-plan engine.')
        return redirect('view_commission')
    summary = CommissionEngineService.active_plan_summary(request.user)
    initial_name = summary['plan'].name if summary.get('plan') else 'Automotive Pay Plan'
    is_internal_user = _is_internal_pay_plan_user(request.user)
    form_class = PayPlanReplacementForm if is_internal_user else BasicPayPlanReplacementForm
    if request.method == 'POST':
        form = form_class(request.POST, request.FILES)
        if form.is_valid():
            try:
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
            except ValidationError as exc:
                form.add_error(None, '; '.join(exc.messages))
            except Exception as exc:
                logger.error(
                    'Unexpected pay-plan upload failure for user_id=%s error_type=%s',
                    request.user.pk,
                    type(exc).__name__,
                )
                form.add_error(
                    None,
                    'We could not process that document. Check the file and try '
                    'again. Your active pay plan was not changed.',
                )
            else:
                messages.success(
                    request,
                    'Your document was saved as a draft. Your current pay plan '
                    'remains active until you review and activate the draft.',
                )
                return redirect('replacement_pay_plan_review', version_id=version.id)
    else:
        form = form_class(initial={
            'plan_name': initial_name,
            'apply_from': form_class.FUTURE_ONLY,
        })
    return render(request, 'pay_plan_replace.html', {
        'form': form,
        'active_plan_summary': summary,
        'is_internal_user': is_internal_user,
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
        return redirect('my_pay_plan')
    except Exception as exc:
        logger.error(
            'Unexpected manual pay-plan draft failure for user_id=%s error_type=%s',
            request.user.pk,
            type(exc).__name__,
        )
        messages.error(
            request,
            'We could not create the edit draft. Your active pay plan was not changed.',
        )
        return redirect('my_pay_plan')
    messages.success(
        request,
        'A manual-edit draft was created. The active plan remains unchanged.',
    )
    return redirect('replacement_pay_plan_review', version_id=version.id)


@ask_stew_ai_required
@require_http_methods(['GET', 'POST'])
def ask_stew_ai(request):
    token_salt = f'ask-stew-submission:{request.user.pk}'

    def new_submission_token():
        return signing.dumps(uuid4().hex, salt=token_salt, compress=True)

    form = (
        AskStewQuestionForm(request.POST)
        if request.method == 'POST'
        else AskStewQuestionForm(initial={
            'submission_token': new_submission_token(),
        })
    )
    answer = None
    submitted_question = ''
    if request.method == 'POST' and form.is_valid():
        token = form.cleaned_data['submission_token']
        try:
            signing.loads(token, salt=token_salt, max_age=3600)
        except (BadSignature, SignatureExpired):
            form.add_error(
                None,
                'This question form expired. Refresh the page and try again.',
            )
        else:
            if request.session.get('ask_stew_last_submission_token') == token:
                submitted_question = form.cleaned_data['question']
                answer = AskStewAnswer(
                    intent='clarification',
                    answer=(
                        'That question was already submitted. No duplicate request '
                        'was sent. Start a new conversation or ask another question.'
                    ),
                    provider_status='not_requested',
                )
            else:
                request.session['ask_stew_last_submission_token'] = token
                submitted_question = form.cleaned_data['question']
                try:
                    answer = AskStewService.answer(
                        request.user,
                        submitted_question,
                        submission_token=token,
                    )
                except Exception as exc:
                    logger.error(
                        'Unexpected Ask Stew explanation failure for user_id=%s error_type=%s',
                        request.user.pk,
                        type(exc).__name__,
                    )
                    answer = AskStewAnswer(
                        intent='clarification',
                        answer=(
                            'I could not prepare that explanation safely. No account '
                            'data was changed. Try a more specific question or try again.'
                        ),
                        provider_status='provider_unavailable',
                    )
            form = AskStewQuestionForm(initial={
                'submission_token': new_submission_token(),
            })
    return render(request, 'ask_stew_ai.html', {
        'form': form,
        'answer': answer,
        'submitted_question': submitted_question,
        'provider_availability': ask_stew_provider_availability(request.user),
        'starter_questions': (
            'Explain my active pay plan.',
            'What are my current-month commission totals?',
            'How many credited units do I need for my next bonus?',
            'Which eligibility information is still missing?',
        ),
    })


@internal_pay_plan_tool_required
def pay_plan_assistant(request):
    if not uses_new_engine(request.user):
        return redirect('view_commission')
    active_plan = ActivePayPlanService.get_for_user(request.user)
    if active_plan.status != 'active':
        messages.error(
            request,
            'Finish pay-plan setup before using the assistant. No changes '
            'were made.',
        )
        return redirect('pay_plan_setup')
    conversation = None
    resolution = None
    follow_up_form = PayPlanAssistantFollowUpForm(initial={
        'submission_token': uuid4().hex,
    })
    initial_date = timezone.localdate() + timedelta(days=1)
    form = PayPlanAssistantForm(initial={
        'effective_date': initial_date,
        'submission_token': uuid4().hex,
    })
    draft_submission_token = uuid4().hex

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
                        submission_token=follow_up_form.cleaned_data[
                            'submission_token'
                        ],
                    )
                except ObjectDoesNotExist as exc:
                    raise Http404('Conversation not found.') from exc
                except ValidationError as exc:
                    follow_up_form.add_error(None, '; '.join(exc.messages))
                else:
                    conversation = outcome.conversation
                    resolution = outcome.resolution
                    follow_up_form = PayPlanAssistantFollowUpForm(initial={
                        'submission_token': uuid4().hex,
                    })
            if conversation is None and conversation_key:
                try:
                    outcome = PayPlanConversationService.resume(
                        request.user, conversation_key,
                    )
                except ObjectDoesNotExist as exc:
                    raise Http404('Conversation not found.') from exc
                except ValidationError as exc:
                    follow_up_form.add_error(None, '; '.join(exc.messages))
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
            draft_submission_token = request.POST.get(
                'draft_submission_token',
                '',
            )
            try:
                change_request = PayPlanConversationService.create_draft(
                    request.user,
                    conversation_key,
                    submission_token=draft_submission_token,
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
                except ValidationError as resume_exc:
                    messages.error(request, '; '.join(resume_exc.messages))
                else:
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
                            submission_token=form.cleaned_data[
                                'submission_token'
                            ],
                        )
                    else:
                        outcome = PayPlanConversationService.start(
                            request.user,
                            form.cleaned_data['request_text'],
                            form.cleaned_data['effective_date'],
                            submission_token=form.cleaned_data[
                                'submission_token'
                            ],
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
                                submission_token=(
                                    request.POST.get('draft_submission_token')
                                    or uuid4().hex
                                ),
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
        except ValidationError as exc:
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
        'draft_submission_token': draft_submission_token,
        'assistant_availability': provider_availability_for_user(request.user),
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

    is_internal_user = _is_internal_pay_plan_user(request.user)
    form_class = ManualPayPlanRuleForm if is_internal_user else BasicPayPlanRuleForm
    manual_form = form_class()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_rule':
            manual_form = form_class(request.POST)
            if manual_form.is_valid():
                try:
                    with transaction.atomic():
                        locked_version = get_object_or_404(
                            PayPlanVersion.objects.select_for_update(of=('self',)),
                            pk=version.pk,
                            pay_plan__owner_user=request.user,
                            is_sandbox=False,
                            status__in=(
                                PayPlanVersion.DRAFT,
                                PayPlanVersion.REVIEW_REQUIRED,
                            ),
                        )
                        if is_internal_user:
                            values = {
                                'name': manual_form.cleaned_data['name'],
                                'rule_type': manual_form.cleaned_data['rule_type'],
                                'calculation_scope': manual_form.cleaned_data['calculation_scope'],
                                'configuration': manual_form.cleaned_data['configuration'],
                                'conditions': manual_form.cleaned_data.get('conditions') or [],
                            }
                        else:
                            values = manual_form.rule_values()
                        conditions = values.pop('conditions')
                        rule = PayPlanRule(
                            pay_plan_version=locked_version,
                            sort_order=locked_version.rules.count() + 1,
                            **values,
                        )
                        rule.full_clean()
                        rule.save()
                        _create_guided_conditions(rule, conditions)
                        locked_version.processing_errors = _resolved_manual_rule_errors(
                            locked_version.processing_errors,
                        )
                        locked_version.processing_status = 'needs_review'
                        locked_version.save(update_fields=[
                            'processing_errors', 'processing_status', 'updated_at',
                        ])
                except (ValidationError, TypeError, KeyError) as exc:
                    manual_form.add_error(None, str(exc))
                except Exception as exc:
                    logger.error(
                        'Unexpected pay-plan rule creation failure for user_id=%s version_id=%s error_type=%s',
                        request.user.pk,
                        version.pk,
                        type(exc).__name__,
                    )
                    manual_form.add_error(
                        None,
                        'We could not save that rule. Check the values and try again.',
                    )
                else:
                    messages.success(request, 'Rule added to the draft.')
                    return redirect('replacement_pay_plan_review', version_id=version.id)
        elif action == 'activate':
            if not is_internal_user:
                return redirect('confirm_pay_plan_activation', version_id=version.id)
            warnings_approved = request.POST.get('approve_warnings') == 'on'
            try:
                report = PayPlanActivationService.activate(
                    request.user, version, warnings_approved=warnings_approved,
                    reason='User confirmed replacement after review',
                )
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
            except Exception as exc:
                logger.error(
                    'Unexpected pay-plan activation failure for user_id=%s version_id=%s error_type=%s',
                    request.user.pk,
                    version.pk,
                    type(exc).__name__,
                )
                messages.error(
                    request,
                    'We could not activate that pay plan. Your current plan remains active.',
                )
            else:
                messages.success(
                    request,
                    f"Pay plan {version.version_name} activated. "
                    f"{report['sales_tested']} sales reviewed; "
                    f"{report['calculated_count']} calculated; "
                    f"{report['excluded_count']} need attention.",
                )
                return redirect('view_commission')

    preview = preview_version(request.user, version) if is_internal_user else None
    plain_text_change = None
    if is_internal_user:
        try:
            plain_text_change = version.plain_text_change_request
        except PayPlanChangeRequest.DoesNotExist:
            pass
    return render(request, 'pay_plan_replacement_review.html', {
        'version': version,
        'rules': version.rules.prefetch_related('conditions').all(),
        'documents': version.documents.filter(user=request.user),
        'preview': preview,
        'manual_rule_form': manual_form,
        'plain_text_change': plain_text_change,
        'changes': _pay_plan_changes(version),
        'is_internal_user': is_internal_user,
        'draft_messages': _customer_draft_messages(version),
    })


@login_required
def confirm_pay_plan_activation(request, version_id):
    version = get_object_or_404(
        PayPlanVersion.objects.select_related('pay_plan', 'previous_version'),
        id=version_id,
        pay_plan__owner_user=request.user,
        is_sandbox=False,
        status__in=(PayPlanVersion.DRAFT, PayPlanVersion.REVIEW_REQUIRED),
    )
    form = BasicPayPlanActivationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        if version.processing_warnings and not form.cleaned_data['approve_warnings']:
            form.add_error(
                'approve_warnings',
                'Confirm that you reviewed the items above before activation.',
            )
        else:
            try:
                PayPlanActivationService.activate(
                    request.user,
                    version,
                    warnings_approved=form.cleaned_data['approve_warnings'],
                    reason='User confirmed pay-plan activation',
                )
            except ValidationError as exc:
                form.add_error(None, '; '.join(exc.messages))
            except Exception as exc:
                logger.error(
                    'Unexpected pay-plan activation failure for user_id=%s version_id=%s error_type=%s',
                    request.user.pk,
                    version.pk,
                    type(exc).__name__,
                )
                form.add_error(
                    None,
                    'We could not activate this pay plan. Your current plan remains active. '
                    'Try again or review the draft for missing information.',
                )
            else:
                messages.success(
                    request,
                    f'{version.pay_plan.name} is now your active pay plan.',
                )
                return redirect('my_pay_plan')
    return render(request, 'pay_plan_activation_confirm.html', {
        'version': version,
        'rules': version.rules.prefetch_related('conditions').all(),
        'changes': _pay_plan_changes(version),
        'draft_messages': _customer_draft_messages(version),
        'form': form,
    })


@login_required
def pay_plan_history(request):
    versions = PayPlanVersion.objects.filter(
        pay_plan__owner_user=request.user,
        is_sandbox=False,
    ).select_related('pay_plan', 'previous_version').prefetch_related('documents')
    return render(request, 'pay_plan_history.html', {
        'versions': versions,
        'is_internal_user': _is_internal_pay_plan_user(request.user),
    })


@login_required
def pay_plan_rules(request, version_id):
    version = get_object_or_404(
        PayPlanVersion,
        id=version_id,
        pay_plan__owner_user=request.user,
        is_sandbox=False,
    )
    is_internal_user = _is_internal_pay_plan_user(request.user)
    return render(request, 'pay_plan_rules.html', {
        'version': version,
        'rules': version.rules.prefetch_related('conditions').all(),
        'can_edit_rules': (
            is_internal_user
            or version.status in {PayPlanVersion.DRAFT, PayPlanVersion.REVIEW_REQUIRED}
        ),
        'is_internal_user': is_internal_user,
    })


@login_required
def edit_pay_plan_rule(request, version_id, rule_id):
    is_internal_user = _is_internal_pay_plan_user(request.user)
    can_edit_any = (
        is_internal_user
        and request.user.has_perm('SalesLogApp.change_payplanrule')
    )
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
    version = rule.pay_plan_version
    if (
        not is_internal_user
        and version.status not in {PayPlanVersion.DRAFT, PayPlanVersion.REVIEW_REQUIRED}
    ):
        messages.info(
            request,
            'Active pay-plan rules cannot be changed directly. Create an edit draft first.',
        )
        return redirect('my_pay_plan')
    form_class = PayPlanRuleConditionEditForm if is_internal_user else BasicPayPlanRuleForm
    if request.method == 'POST':
        form = form_class(request.POST, rule=rule)
        if form.is_valid():
            try:
                with transaction.atomic():
                    locked_queryset = PayPlanRule.objects.select_for_update(
                        of=('self',),
                    ).select_related('pay_plan_version__pay_plan').filter(
                        pay_plan_version__is_sandbox=False,
                    )
                    if not can_edit_any:
                        locked_queryset = locked_queryset.filter(
                            pay_plan_version__pay_plan__owner_user=request.user,
                        )
                    if not is_internal_user:
                        locked_queryset = locked_queryset.filter(
                            pay_plan_version__status__in=(
                                PayPlanVersion.DRAFT,
                                PayPlanVersion.REVIEW_REQUIRED,
                            ),
                        )
                    locked_rule = get_object_or_404(
                        locked_queryset,
                        pk=rule.pk,
                        pay_plan_version_id=version_id,
                    )
                    if is_internal_user:
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
                            condition = PayPlanRuleCondition(
                                rule=locked_rule,
                                sort_order=(
                                    existing_order
                                    if existing_order is not None
                                    else locked_rule.conditions.count() + 1
                                ),
                                field_name='vehicle_condition',
                                operator='equals',
                                value=selected,
                            )
                            condition.full_clean()
                            condition.save()
                    else:
                        values = form.rule_values()
                        conditions = values.pop('conditions')
                        for field, value in values.items():
                            setattr(locked_rule, field, value)
                        locked_rule.full_clean()
                        locked_rule.save(update_fields=[
                            'name', 'rule_type', 'calculation_scope',
                            'configuration', 'updated_at',
                        ])
                        locked_rule.conditions.all().delete()
                        _create_guided_conditions(locked_rule, conditions)
                        locked_version = locked_rule.pay_plan_version
                        locked_version.processing_errors = _resolved_manual_rule_errors(
                            locked_version.processing_errors,
                        )
                        locked_version.save(update_fields=[
                            'processing_errors', 'updated_at',
                        ])
            except (ValidationError, TypeError, KeyError) as exc:
                form.add_error(None, str(exc))
            except Exception as exc:
                logger.error(
                    'Unexpected pay-plan rule edit failure for user_id=%s version_id=%s rule_id=%s error_type=%s',
                    request.user.pk,
                    version_id,
                    rule_id,
                    type(exc).__name__,
                )
                form.add_error(
                    None,
                    'We could not save that rule. Check the values and try again.',
                )
            else:
                if is_internal_user:
                    messages.success(
                        request,
                        f'{rule.name} applicability was updated.',
                    )
                    return redirect(
                        'edit_pay_plan_rule',
                        version_id=version_id,
                        rule_id=rule_id,
                    )
                messages.success(request, f'{rule.name} was updated in the draft.')
                return redirect(
                    'replacement_pay_plan_review',
                    version_id=version_id,
                )
    else:
        form = form_class(rule=rule)
    return render(request, 'pay_plan_rule_edit.html', {
        'rule': rule,
        'version': version,
        'form': form,
        'is_internal_user': is_internal_user,
        'unsupported_rule': getattr(form, 'unsupported_rule', False),
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
            sandbox=sandbox,
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
def commission_sandbox_hypothetical(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    if sandbox.status != CommissionSandbox.DRAFT:
        messages.error(request, 'Only a draft sandbox can be edited.')
        return redirect(
            'commission_sandbox_detail', sandbox_id=sandbox.public_id,
        )
    form = SandboxHypotheticalDealForm(request.POST, sandbox=sandbox)
    if form.is_valid():
        try:
            with transaction.atomic():
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
        except ValidationError as exc:
            _add_validation_error(form, exc)
        except IntegrityError as exc:
            if not _is_hypothetical_deal_number_conflict(exc):
                raise
            form.add_error(
                'dealNumber',
                SandboxHypotheticalDealForm.DUPLICATE_DEAL_NUMBER_MESSAGE,
            )
        else:
            messages.success(
                request,
                'Hypothetical deal added without creating a sale.',
            )
    if form.errors:
        messages.error(
            request,
            _form_error_message(form, 'Hypothetical deal was not added: '),
        )
    return redirect('commission_sandbox_detail', sandbox_id=sandbox.public_id)


@login_required
@require_POST
@internal_pay_plan_tool_required
def commission_sandbox_project(request, sandbox_id):
    sandbox = _sandbox_or_404(request, sandbox_id)
    from .scenario_services import ScenarioCalculationService
    try:
        run = ScenarioCalculationService.recalculate(
            request.user, sandbox, mode=SandboxRun.PROJECTION,
        )
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    except Exception:
        logger.exception(
            'Unexpected sandbox projection failure.',
            extra={
                'sandbox_id': str(sandbox.public_id),
                'sandbox_owner_id': sandbox.owner_id,
            },
        )
        messages.error(
            request,
            'The projection could not be completed. Please try again.',
        )
    else:
        messages.success(
            request,
            f'Projected {run.statistics["hypothetical_deals"]} hypothetical deals.',
        )
    return redirect('commission_sandbox_detail', sandbox_id=sandbox.public_id)


@login_required
@require_POST
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
@internal_pay_plan_tool_required
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
            request.POST, sandbox=sandbox, instance=deal,
        )
        if form.is_valid():
            try:
                with transaction.atomic():
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
            except ValidationError as exc:
                _add_validation_error(form, exc)
            except IntegrityError as exc:
                if not _is_hypothetical_deal_number_conflict(exc):
                    raise
                form.add_error(
                    'dealNumber',
                    SandboxHypotheticalDealForm.DUPLICATE_DEAL_NUMBER_MESSAGE,
                )
            else:
                messages.success(request, 'Hypothetical deal updated.')
                return redirect(
                    'commission_sandbox_detail',
                    sandbox_id=sandbox.public_id,
                )
    else:
        form = SandboxHypotheticalDealForm(
            sandbox=sandbox, instance=deal,
        )
    return render(request, 'commission_scenario_hypothetical_form.html', {
        'sandbox': sandbox,
        'deal': deal,
        'form': form,
    })


@login_required
@require_POST
@internal_pay_plan_tool_required
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
