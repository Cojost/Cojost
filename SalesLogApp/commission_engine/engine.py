from __future__ import annotations

from datetime import date, timedelta
from dataclasses import replace
from decimal import Decimal
from typing import Any

from django.db.models import Q
from django.core.exceptions import ObjectDoesNotExist

from .constants import RULE_SCOPE_PERIOD, RULE_SCOPE_PER_SALE
from .exceptions import (
    CalculationError,
    CommissionEngineError,
    PayPlanResolutionError,
    UnsupportedRuleTypeError,
)
from .registry import RULE_TYPE_REGISTRY
from .results import CalculationLineItem, CalculationResult, PeriodCalculationResult
from .conditions import evaluate_conditions
from .evaluators import round_money
from .vehicle_conditions import normalize_vehicle_condition
from .unit_position import condition_unit_position

from django.apps import apps


def resolve_pay_plan_version(user: Any, as_of_date: date | None = None) -> Any:
    PayPlanAssignment = apps.get_model('SalesLogApp', 'PayPlanAssignment')
    PayPlanVersion = apps.get_model('SalesLogApp', 'PayPlanVersion')
    as_of_date = as_of_date or date.today()
    assignment = (
        PayPlanAssignment.objects.select_related(
            'pay_plan_version',
            'pay_plan_version__pay_plan',
        )
        .filter(user=user, is_active=True, effective_start_date__lte=as_of_date)
        .filter(Q(effective_end_date__isnull=True) | Q(effective_end_date__gte=as_of_date))
        .order_by('-effective_start_date', '-id')
        .first()
    )
    if assignment is None:
        raise PayPlanResolutionError(
            f'No active pay plan assignment found for user {user} on {as_of_date}.'
        )
    version = assignment.pay_plan_version
    if version.pay_plan.owner_user_id != user.id:
        raise PayPlanResolutionError(
            'The active pay plan assignment references another user’s plan.'
        )
    if version.status not in {PayPlanVersion.ACTIVE, PayPlanVersion.INACTIVE}:
        raise PayPlanResolutionError(
            f'Pay plan version {version} is not active for user {user} on {as_of_date}.'
        )
    return version


def resolve_pay_plan_version_for_period(
    user: Any,
    period_start: date | None = None,
    period_end: date | None = None,
) -> Any:
    try:
        return resolve_pay_plan_version(user, period_start)
    except PayPlanResolutionError:
        if period_start is None and period_end is None:
            raise

    PayPlanAssignment = apps.get_model('SalesLogApp', 'PayPlanAssignment')
    PayPlanVersion = apps.get_model('SalesLogApp', 'PayPlanVersion')
    period_end = period_end or period_start or date.today()
    assignment = (
        PayPlanAssignment.objects.select_related(
            'pay_plan_version',
            'pay_plan_version__pay_plan',
        )
        .filter(user=user, is_active=True, effective_start_date__lte=period_end)
        .filter(Q(effective_end_date__isnull=True) | Q(effective_end_date__gte=period_start or period_end))
        .order_by('effective_start_date', 'id')
        .first()
    )
    if assignment is None:
        raise PayPlanResolutionError(
            f'No active pay plan assignment found for user {user} between '
            f'{period_start or period_end} and {period_end}.'
        )
    version = assignment.pay_plan_version
    if version.status not in {PayPlanVersion.ACTIVE, PayPlanVersion.INACTIVE}:
        raise PayPlanResolutionError(
            f'Pay plan version {version} is not active for user {user} during the requested period.'
        )
    return version


def build_eligibility_context(user: Any | None, as_of_date: date | None) -> dict[str, Any]:
    context = {
        'green_pea': None,
        'nps_finance_eligible': None,
        'ar_requirement_met': None,
        'training_requirements_met': None,
        'call_requirement_met': None,
        'video_requirement_met': None,
        'nps_bonus_eligible': None,
        'nps_qualifying_surveys': Decimal('0'),
        'nps_low_score_surveys': Decimal('0'),
        'holiday_bonus_eligible': None,
        'holiday_bonus_forfeited': False,
    }
    if user is not None and as_of_date is not None:
        PayPlanEligibility = apps.get_model('SalesLogApp', 'PayPlanEligibility')
        eligibility = PayPlanEligibility.objects.filter(
            user=user, month_start=as_of_date.replace(day=1),
        ).first()
        if eligibility is not None:
            context = {
                'green_pea': eligibility.green_pea,
                'nps_finance_eligible': eligibility.nps_finance_eligible,
                'ar_requirement_met': eligibility.ar_requirement_met,
                'training_requirements_met': eligibility.training_requirements_met,
                'call_requirement_met': eligibility.call_requirement_met,
                'video_requirement_met': eligibility.video_requirement_met,
                'nps_bonus_eligible': eligibility.nps_status == eligibility.NPS_ELIGIBLE,
                'nps_qualifying_surveys': Decimal(str(eligibility.nps_qualifying_surveys)),
                'nps_low_score_surveys': Decimal(str(eligibility.nps_low_score_surveys)),
                'holiday_bonus_eligible': eligibility.holiday_bonus_eligible,
                'holiday_bonus_forfeited': eligibility.holiday_bonus_forfeited,
            }
    return context


def build_sale_context(
    sale: Any,
    monthly_metrics: dict[str, Any] | None = None,
    user: Any | None = None,
) -> dict[str, Any]:
    monthly_metrics = monthly_metrics or {}
    front_end_gross = Decimal(str(getattr(sale, 'frontEnd', 0) or 0))
    back_end_gross = Decimal(str(getattr(sale, 'backend', 0) or 0))
    total_gross = front_end_gross + back_end_gross
    eligibility_context = build_eligibility_context(
        user, getattr(sale, 'date', None),
    )

    try:
        mileage = sale.vehicle.mileage
    except (AttributeError, ObjectDoesNotExist):
        mileage = (getattr(sale, 'custom_pay_plan_fields', {}) or {}).get('mileage')
    condition = normalize_vehicle_condition(getattr(sale, 'vehicle_condition', None))
    positions = monthly_metrics.get('_condition_positions', {})
    if id(sale) in positions:
        before, after = positions[id(sale)]
    else:
        before, after = condition_unit_position(
            sale, condition, monthly_metrics.get('_sales'),
        )
    unit_credit = Decimal(str(
        getattr(sale, 'unit_credit', getattr(sale, 'count', 0)) or 0
    ))
    commission_multiplier = getattr(
        sale, 'commission_credit_multiplier', None,
    )
    if commission_multiplier is None:
        commission_multiplier = (
            Decimal('0.5')
            if unit_credit == Decimal('0.5')
            else Decimal('1.0')
        )
    return {
        'vehicle_condition': condition,
        'acquisition_source': getattr(sale, 'acquisition_source', None),
        'make': getattr(sale, 'make', None),
        'model': getattr(sale, 'model', None),
        'year': getattr(sale, 'year', None),
        'mileage': mileage,
        'is_cpo': getattr(sale, 'is_cpo', None),
        'deal_type': getattr(sale, 'sale_type', None),
        'front_end_gross': front_end_gross,
        'back_end_gross': back_end_gross,
        'total_gross': total_gross,
        'deal_credit': getattr(sale, 'deal_credit', None),
        'sale_date': getattr(sale, 'date', None),
        'count': Decimal(str(getattr(sale, 'count', 0) or 0)),
        'unit_credit': unit_credit,
        'commission_credit_multiplier': Decimal(str(
            commission_multiplier or Decimal('1.0')
        )),
        'monthly_units': Decimal(str(monthly_metrics.get('monthly_units', 0) or 0)),
        'monthly_front_gross': Decimal(str(monthly_metrics.get('monthly_front_gross', 0) or 0)),
        'monthly_back_gross': Decimal(str(monthly_metrics.get('monthly_back_gross', 0) or 0)),
        'monthly_total_gross': Decimal(str(monthly_metrics.get('monthly_total_gross', 0) or 0)),
        'monthly_new_units': Decimal(str(monthly_metrics.get('monthly_new_units', 0) or 0)),
        'monthly_used_units': Decimal(str(monthly_metrics.get('monthly_used_units', 0) or 0)),
        'condition_units_before_sale': before,
        'condition_units_after_sale': after,
        **eligibility_context,
    }


def build_period_context(sales: list[Any]) -> dict[str, Any]:
    monthly_units = Decimal('0')
    monthly_front_gross = Decimal('0')
    monthly_back_gross = Decimal('0')
    monthly_new_units = Decimal('0')
    monthly_used_units = Decimal('0')
    fast_start_bonus_units = Decimal('0')
    units_by_day_10 = Decimal('0')
    period_month = None
    sale_dates = [
        getattr(sale, 'date', None)
        for sale in sales
        if getattr(sale, 'date', None) is not None
    ]
    if sale_dates:
        period_month = min(sale_dates).replace(day=1)
    fast_start_cutoff = None
    if period_month is not None:
        working_days = []
        candidate = period_month
        while len(working_days) < 7:
            if candidate.weekday() != 6:
                working_days.append(candidate)
            candidate += timedelta(days=1)
        fast_start_cutoff = working_days[-1]
    for sale in sales:
        unit_credit = Decimal(str(getattr(sale, 'unit_credit', getattr(sale, 'count', 0)) or 0))
        monthly_units += unit_credit
        sale_date = getattr(sale, 'date', None)
        if sale_date is not None and sale_date.day <= 10:
            units_by_day_10 += unit_credit
        if fast_start_cutoff is not None and sale_date is not None and sale_date <= fast_start_cutoff:
            fast_start_bonus_units += unit_credit
        monthly_front_gross += Decimal(str(getattr(sale, 'frontEnd', 0) or 0))
        monthly_back_gross += Decimal(str(getattr(sale, 'backend', 0) or 0))
        condition = normalize_vehicle_condition(getattr(sale, 'vehicle_condition', None))
        if condition == 'new':
            monthly_new_units += Decimal(str(getattr(sale, 'unit_credit', getattr(sale, 'count', 0)) or 0))
        elif condition in {'used', 'retired_sslp'}:
            monthly_used_units += Decimal(str(getattr(sale, 'unit_credit', getattr(sale, 'count', 0)) or 0))
    monthly_total_gross = monthly_front_gross + monthly_back_gross
    return {
        'monthly_units': monthly_units,
        'monthly_front_gross': monthly_front_gross,
        'monthly_back_gross': monthly_back_gross,
        'monthly_total_gross': monthly_total_gross,
        'monthly_new_units': monthly_new_units,
        'monthly_used_units': monthly_used_units,
        'fast_start_volume_units': monthly_units + fast_start_bonus_units,
        'units_by_day_10': units_by_day_10,
    }


def evaluate_rules(result: CalculationResult, rules: list[Any], context: dict[str, Any]) -> None:
    for rule in rules:
        evaluator_cls = RULE_TYPE_REGISTRY.get(rule.rule_type)
        if evaluator_cls is None:
            result.add_skipped_rule(
                rule.id,
                rule.name,
                rule.rule_type,
                f'Unsupported rule type: {rule.rule_type}',
            )
            continue

        conditions = [
            condition.as_dict()
            for condition in getattr(rule, 'conditions', []).all().order_by('sort_order', 'id')
        ]
        configuration = rule.configuration or {}
        evaluator = evaluator_cls(
            rule=rule,
            configuration=configuration,
            conditions=conditions,
            condition_group_operator=getattr(rule, 'condition_group_operator', 'all'),
        )
        try:
            line_item = evaluator.evaluate(context)
        except CommissionEngineError as exc:
            result.add_skipped_rule(
                rule.id,
                rule.name,
                rule.rule_type,
                str(exc),
            )
            continue
        result.add_line_item(line_item)
        if line_item.applied:
            subtotal_key = f'{line_item.category}_subtotal'
            context[subtotal_key] = (
                Decimal(str(context.get(subtotal_key, Decimal('0.00'))))
                + line_item.amount
            )


def apply_sale_commission_credit(
    result: CalculationResult,
    multiplier: Decimal,
    rule_configurations: dict[int, dict[str, Any]],
) -> None:
    """Apply a deal split once, after all per-sale rules and limits resolve."""
    multiplier = Decimal(str(multiplier or Decimal('1.0')))
    if multiplier == Decimal('1.0'):
        return

    original_items = list(result.line_items)
    result.line_items = []
    result.base_commission = Decimal('0.00')
    result.bonuses = Decimal('0.00')
    result.spiffs = Decimal('0.00')
    result.adjustments = Decimal('0.00')
    result.deductions = Decimal('0.00')
    result.total = Decimal('0.00')

    for line_item in original_items:
        configuration = rule_configurations.get(line_item.rule_id, {})
        should_split = (
            line_item.applied
            and line_item.scope == RULE_SCOPE_PER_SALE
            and configuration.get(
                'apply_commission_credit_multiplier', True,
            ) is not False
        )
        if should_split:
            pre_split = line_item.amount
            final_amount = round_money(pre_split * multiplier)
            line_item = replace(
                line_item,
                amount=final_amount,
                explanation=(
                    f'{line_item.explanation} Pre-split ${pre_split:.2f}; '
                    f'deal-share multiplier {multiplier} = '
                    f'${final_amount:.2f}.'
                ),
                metadata={
                    **line_item.metadata,
                    'pre_split_amount': str(pre_split),
                    'commission_credit_multiplier': str(multiplier),
                },
            )
        result.add_line_item(line_item)


def calculate_sale_commission(user: Any, sale: Any, monthly_metrics: dict[str, Any] | None = None) -> CalculationResult:
    pay_plan_version = resolve_pay_plan_version(user, getattr(sale, 'date', None))
    return calculate_sale_commission_for_version(
        user, sale, pay_plan_version, monthly_metrics,
    )


def calculate_sale_commission_for_version(
    user: Any,
    sale: Any,
    pay_plan_version: Any,
    monthly_metrics: dict[str, Any] | None = None,
    rules: list[Any] | None = None,
) -> CalculationResult:
    """Evaluate a specified version for previews without changing assignments."""
    from ..pay_plan_scope import OwnedPayPlanRuleService

    OwnedPayPlanRuleService.validate_version_owner(user, pay_plan_version)
    if getattr(sale, 'user_id', None) != user.id:
        raise PayPlanResolutionError(
            'The sale does not belong to the user requesting this calculation.'
        )
    result = CalculationResult(
        user=user,
        pay_plan=pay_plan_version.pay_plan,
        pay_plan_version=pay_plan_version,
        sale=sale,
    )
    rules = (
        list(rules)
        if rules is not None
        else list(OwnedPayPlanRuleService.active_rules_for_user(
            user, pay_plan_version, scope=RULE_SCOPE_PER_SALE,
        ))
    )
    context = build_sale_context(sale, monthly_metrics, user=user)
    backend_types = {'back_gross_percentage', 'flat_backend_commission'}
    front_types = {'front_gross_percentage', 'progressive_unit_position_percentage'}
    backend_rules = [rule for rule in rules if rule.rule_type in backend_types]
    front_rules = [rule for rule in rules if rule.rule_type in front_types]
    other_rules = [
        rule for rule in rules
        if rule.rule_type not in backend_types | front_types
    ]
    def specificity(rule):
        return rule.conditions.count()

    def select_matching(candidates):
        matching = []
        for candidate in candidates:
            conditions = [
                condition.as_dict()
                for condition in candidate.conditions.all().order_by('sort_order', 'id')
            ]
            try:
                matches = not conditions or evaluate_conditions(
                    conditions, context, candidate.condition_group_operator,
                )
            except CommissionEngineError as exc:
                result.add_skipped_rule(
                    candidate.id, candidate.name, candidate.rule_type,
                    f'Invalid condition configuration: {exc}',
                )
                matches = False
            if matches:
                matching.append(candidate)
        matching.sort(key=lambda rule: (-specificity(rule), rule.sort_order, rule.id))
        return matching[0] if matching else None

    selected_front = select_matching(front_rules)
    if selected_front is not None:
        evaluate_rules(result, [selected_front], context)
    elif front_rules and not context['vehicle_condition']:
        result.warnings.append(
            'missing_vehicle_condition: vehicle condition is required to select '
            'the New or Used front-end commission rule.'
        )
    specialized = [rule for rule in backend_rules if rule.conditions.exists()]
    default_rules = [rule for rule in backend_rules if not rule.conditions.exists()]
    selected_backend = None
    selected_backend = select_matching(specialized)
    if selected_backend is None and default_rules:
        selected_backend = default_rules[0]

    if selected_backend is not None:
        line_start = len(result.line_items)
        evaluate_rules(result, [selected_backend], context)
        if selected_backend in default_rules:
            for index in range(line_start, len(result.line_items)):
                item = result.line_items[index]
                if item.category != 'back_end' or not item.applied:
                    continue
                result.line_items[index] = replace(
                    item,
                    explanation=(
                        'No specialized backend rule matched. '
                        'Default backend rule applied. '
                        + item.explanation
                    ),
                    metadata={
                        **item.metadata,
                        'calculation_source': 'default_fallback',
                    },
                )
    elif (
        context['back_end_gross'] > 0
        and pay_plan_version.default_backend_percentage is not None
    ):
        gross = context['back_end_gross']
        rate = Decimal(str(pay_plan_version.default_backend_percentage))
        pre_split = gross * rate
        minimum = pay_plan_version.default_backend_minimum
        maximum = pay_plan_version.default_backend_maximum
        amount = pre_split
        limits = []
        if minimum is not None and amount < minimum:
            amount = Decimal(str(minimum))
            limits.append(f'minimum ${minimum:.2f} applied')
        if maximum is not None and amount > maximum:
            amount = Decimal(str(maximum))
            limits.append(f'maximum ${maximum:.2f} applied')
        final_amount = round_money(amount)
        explanation = (
            'No specialized backend rule matched. Default backend calculation '
            f'applied: ${gross:.2f} × {rate * 100}% = ${pre_split:.2f}'
        )
        if limits:
            explanation += '; ' + '; '.join(limits)
        explanation += '.'
        fallback = CalculationLineItem(
            rule_id=0,
            rule_name=f'Default backend percentage ({rate * 100}%)',
            rule_type='default_backend_percentage',
            category='back_end',
            scope=RULE_SCOPE_PER_SALE,
            amount=final_amount,
            explanation=explanation,
            applied=True,
            metadata={
                'calculation_source': 'default_fallback',
                'gross': str(gross),
                'rate': str(rate),
                'pre_split_amount': str(amount),
            },
        )
        result.add_line_item(fallback)
        context['back_end_subtotal'] = final_amount

    evaluate_rules(result, other_rules, context)
    apply_sale_commission_credit(
        result,
        context['commission_credit_multiplier'],
        {
            rule.id: dict(rule.configuration or {})
            for rule in rules
        },
    )
    return result


def calculate_period_commission(
    user: Any,
    sales: list[Any],
    period_start: date | None = None,
    period_end: date | None = None,
) -> PeriodCalculationResult:
    from ..pay_plan_scope import OwnedPayPlanRuleService

    pay_plan_version = resolve_pay_plan_version_for_period(user, period_start, period_end)
    OwnedPayPlanRuleService.validate_version_owner(user, pay_plan_version)
    if any(getattr(sale, 'user_id', None) != user.id for sale in sales):
        raise PayPlanResolutionError(
            'Every sale in a period calculation must belong to the user.'
        )
    metrics = {**build_period_context(sales), '_sales': sales}
    period_result = PeriodCalculationResult(
        user=user,
        pay_plan=pay_plan_version.pay_plan,
        pay_plan_version=pay_plan_version,
        period_start=period_start,
        period_end=period_end,
    )

    sale_results: list[CalculationResult] = []
    for sale in sales:
        sale_result = calculate_sale_commission(user, sale, metrics)
        sale_results.append(sale_result)
        period_result.sale_results.append(sale_result)
        period_result.total += sale_result.total
        period_result.base_commission += sale_result.base_commission
        period_result.bonuses += sale_result.bonuses
        period_result.spiffs += sale_result.spiffs
        period_result.adjustments += sale_result.adjustments
        period_result.deductions += sale_result.deductions

    period_rules = list(OwnedPayPlanRuleService.active_rules_for_user(
        user, pay_plan_version, scope=RULE_SCOPE_PERIOD,
    ))
    period_context = {
        **metrics,
        **build_eligibility_context(user, period_start or period_end),
        '_sales': sales,
        'period_start': period_start,
        'period_end': period_end,
    }
    evaluate_rules(period_result, period_rules, period_context)
    return period_result
