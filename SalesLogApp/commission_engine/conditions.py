from datetime import date
from decimal import Decimal
from typing import Any

from .exceptions import ConditionValidationError
from .validators import SUPPORTED_CONDITION_FIELDS
from .vehicle_conditions import normalize_vehicle_condition


def resolve_condition_value(context: dict[str, Any], field_name: str) -> Any:
    if field_name not in SUPPORTED_CONDITION_FIELDS:
        raise ConditionValidationError(f'Unsupported condition field: {field_name}')
    return context.get(field_name)


def evaluate_operator(value: Any, operator: str, target: Any) -> bool:
    if operator == 'equals':
        return value == target
    if operator == 'not_equals':
        return value != target
    if operator == 'greater_than':
        return value is not None and target is not None and value > target
    if operator == 'greater_than_or_equal':
        return value is not None and target is not None and value >= target
    if operator == 'less_than':
        return value is not None and target is not None and value < target
    if operator == 'less_than_or_equal':
        return value is not None and target is not None and value <= target
    if operator == 'in':
        return value in target
    if operator == 'not_in':
        return value not in target
    if operator == 'between':
        return value is not None and target[0] <= value <= target[1]
    if operator == 'is_true':
        return value is True
    if operator == 'is_false':
        return value is False
    raise ConditionValidationError(f'Unsupported operator: {operator}')


def normalize_condition_value(field_name: str, raw_value: Any) -> Any:
    field_type = SUPPORTED_CONDITION_FIELDS[field_name]
    if field_type == 'decimal':
        return Decimal(str(raw_value))
    if field_type == 'boolean':
        return bool(raw_value)
    if field_type == 'date':
        return raw_value
    if field_type == 'string':
        value = str(raw_value).strip()
        return normalize_vehicle_condition(value) if field_name == 'vehicle_condition' else value
    return raw_value


def evaluate_condition(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    field_name = condition['field_name']
    operator = condition['operator']
    raw_value = condition.get('value')
    value = resolve_condition_value(context, field_name)
    if operator in ('in', 'not_in', 'between'):
        target = [normalize_condition_value(field_name, item) for item in raw_value]
    else:
        target = normalize_condition_value(field_name, raw_value) if raw_value is not None else None
    if value is not None and field_name in (
        'year', 'front_end_gross', 'back_end_gross', 'total_gross',
        'deal_credit', 'monthly_units', 'monthly_front_gross',
        'monthly_back_gross', 'monthly_total_gross', 'monthly_new_units',
        'monthly_used_units', 'fast_start_volume_units', 'units_by_day_10',
        'nps_qualifying_surveys', 'nps_low_score_surveys',
    ):
        value = Decimal(str(value))
    if field_name == 'sale_date' and isinstance(value, str):
        from datetime import datetime
        value = datetime.strptime(value, '%Y-%m-%d').date()
    if field_name == 'vehicle_condition' and value is not None:
        value = normalize_vehicle_condition(value)
    return evaluate_operator(value, operator, target)


def evaluate_conditions(conditions: list[dict[str, Any]], context: dict[str, Any], group_operator: str = 'all') -> bool:
    if group_operator not in ('all', 'any'):
        raise ConditionValidationError(f'Unsupported condition group operator: {group_operator}')
    results = [evaluate_condition(condition, context) for condition in conditions]
    return all(results) if group_operator == 'all' else any(results)
