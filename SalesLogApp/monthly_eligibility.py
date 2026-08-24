from django.utils import timezone

from .models import PayPlanEligibility
from .plan_requirements import PlanRequirementService


MONTHLY_REQUIREMENT_KEYS = (
    'nps',
    'nps_bonus',
    'ar',
    'green_pea',
    'training',
    'calls',
    'video',
    'holiday',
)


def _eligible_defaults(requirements, user):
    enabled = {
        key for key in MONTHLY_REQUIREMENT_KEYS if requirements.get(key)
    }
    defaults = {'updated_by': user}
    if enabled & {'nps', 'nps_bonus'}:
        defaults['nps_status'] = PayPlanEligibility.NPS_ELIGIBLE
    boolean_defaults = {
        'ar': 'ar_requirement_met',
        'green_pea': 'green_pea',
        'training': 'training_requirements_met',
        'calls': 'call_requirement_met',
        'video': 'video_requirement_met',
        'holiday': 'holiday_bonus_eligible',
    }
    for requirement, field_name in boolean_defaults.items():
        if requirement in enabled:
            defaults[field_name] = True
    return enabled, defaults


def ensure_current_month_eligibility(
    user,
    month_start=None,
    *,
    today=None,
    plan_requirements=None,
):
    """Create current-month eligible defaults without changing saved history."""
    current_month = (today or timezone.localdate()).replace(day=1)
    selected_month = (month_start or current_month).replace(day=1)
    existing = PayPlanEligibility.objects.filter(
        user=user,
        month_start=selected_month,
    ).first()
    if existing is not None or selected_month != current_month:
        return existing, False

    requirements = plan_requirements or PlanRequirementService.get_for_user(
        user,
        as_of_date=selected_month,
    )
    enabled, defaults = _eligible_defaults(requirements, user)
    if not enabled:
        return None, False
    return PayPlanEligibility.objects.get_or_create(
        user=user,
        month_start=selected_month,
        defaults=defaults,
    )
