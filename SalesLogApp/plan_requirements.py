from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.db.models import Q
from django.utils import timezone


@dataclass(frozen=True)
class ActivePayPlanResult:
    status: str
    plan: Any = None
    version: Any = None
    assignment: Any = None
    error: str = ''


class ActivePayPlanService:
    """Resolve an active plan without any global or cross-user fallback."""

    @staticmethod
    def get_for_user(user, as_of_date=None) -> ActivePayPlanResult:
        PayPlanAssignment = apps.get_model('SalesLogApp', 'PayPlanAssignment')
        as_of_date = as_of_date or timezone.localdate()
        assignments = list(
            PayPlanAssignment.objects.select_related(
                'pay_plan_version__pay_plan',
            )
            .filter(
                user=user,
                is_active=True,
                effective_start_date__lte=as_of_date,
            )
            .filter(
                Q(effective_end_date__isnull=True)
                | Q(effective_end_date__gte=as_of_date)
            )
            .order_by('-effective_start_date', '-id')[:2]
        )
        if not assignments:
            return ActivePayPlanResult(status='no_active_plan')
        if len(assignments) > 1:
            return ActivePayPlanResult(
                status='multiple_active_plans',
                error='Multiple active pay-plan assignments cover this date.',
            )
        assignment = assignments[0]
        version = assignment.pay_plan_version
        plan = version.pay_plan
        if plan.owner_user_id != user.id:
            return ActivePayPlanResult(
                status='ownership_error',
                error='The active assignment references a plan owned by another user.',
            )
        if version.status != 'active':
            return ActivePayPlanResult(
                status='inactive_plan',
                plan=plan,
                version=version,
                assignment=assignment,
                error='The assigned pay-plan version is not active.',
            )
        return ActivePayPlanResult(
            status='active',
            plan=plan,
            version=version,
            assignment=assignment,
        )


class PlanRequirementService:
    CONDITION_REQUIREMENTS = {
        'nps_finance_eligible': 'nps',
        'green_pea': 'green_pea',
        'ar_requirement_met': 'ar',
        'appointment_ratio_met': 'ar',
        'training_requirements_met': 'training',
        'call_requirement_met': 'calls',
        'video_requirement_met': 'video',
        'nps_bonus_eligible': 'nps_bonus',
        'nps_qualifying_surveys': 'nps_bonus',
        'nps_low_score_surveys': 'nps_bonus',
        'holiday_bonus_eligible': 'holiday',
        'holiday_bonus_forfeited': 'holiday',
    }
    EMPTY = {
        'nps': None,
        'ar': None,
        'green_pea': None,
        'training': None,
        'calls': None,
        'video': None,
        'nps_bonus': None,
        'holiday': None,
        'other_requirements': [],
    }

    @classmethod
    def get_for_user(cls, user, plan_result=None, as_of_date=None):
        plan_result = plan_result or ActivePayPlanService.get_for_user(
            user, as_of_date,
        )
        output = {**cls.EMPTY, 'other_requirements': []}
        if plan_result.status != 'active':
            return {
                **output,
                'status': plan_result.status,
                'plan_id': None,
                'version_id': None,
                'has_monthly_requirements': False,
            }
        version = plan_result.version
        if version.pay_plan.owner_user_id != user.id:
            return {
                **output,
                'status': 'ownership_error',
                'plan_id': None,
                'version_id': None,
                'has_monthly_requirements': False,
            }
        rules = version.rules.filter(is_active=True).prefetch_related('conditions')
        for rule in rules:
            discovered = set()
            for condition in rule.conditions.all():
                key = cls.CONDITION_REQUIREMENTS.get(condition.field_name)
                if key:
                    discovered.add(key)
            configured_requirements = (
                (rule.configuration or {}).get('requirements', [])
                + (rule.configuration or {}).get('data_fields', [])
            )
            for requirement in configured_requirements:
                field_name = (
                    requirement.get('field')
                    or requirement.get('field_name')
                    or requirement.get('metric')
                )
                key = cls.CONDITION_REQUIREMENTS.get(field_name)
                if key:
                    discovered.add(key)
            for key in discovered:
                detail = {
                    'rule_id': rule.id,
                    'rule_name': rule.name,
                    'rule_type': rule.rule_type,
                    'plan_id': version.pay_plan_id,
                    'version_id': version.id,
                }
                if output[key] is None:
                    output[key] = detail
                else:
                    output['other_requirements'].append(detail)
        return {
            **output,
            'status': 'active',
            'plan_id': version.pay_plan_id,
            'version_id': version.id,
            'has_monthly_requirements': any(
                output[key] is not None
                for key in (
                    'nps', 'nps_bonus', 'ar', 'green_pea', 'training',
                    'calls', 'video', 'holiday',
                )
            ),
        }
