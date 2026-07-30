import json
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError

from .constants import SUPPORTED_RULE_SCOPES
from .exceptions import ConditionValidationError, RuleConfigurationError

SUPPORTED_OPERATOR_CONFIG = {
    'equals': ['string', 'decimal', 'date', 'boolean'],
    'not_equals': ['string', 'decimal', 'date', 'boolean'],
    'greater_than': ['decimal', 'date'],
    'greater_than_or_equal': ['decimal', 'date'],
    'less_than': ['decimal', 'date'],
    'less_than_or_equal': ['decimal', 'date'],
    'in': ['string', 'decimal', 'date'],
    'not_in': ['string', 'decimal', 'date'],
    'between': ['decimal', 'date'],
    'is_true': ['boolean'],
    'is_false': ['boolean'],
}

SUPPORTED_RULE_TYPES = set([
    'front_gross_percentage',
    'progressive_unit_position_percentage',
    'tiered_minimum_commission',
    'back_gross_percentage',
    'flat_per_deal',
    'flat_backend_commission',
    'minimum_commission',
    'maximum_commission',
    'volume_bonus',
    'per_unit_bonus',
    'vehicle_spiff',
    'manual_adjustment',
    'deduction',
    'period_qualification_bonus',
    'draw',
    'survey_count_bonus',
    'acquisition_bonus',
])

SUPPORTED_CONDITION_FIELDS = {
    'vehicle_condition': 'string',
    'acquisition_source': 'string',
    'make': 'string',
    'model': 'string',
    'year': 'decimal',
    'mileage': 'decimal',
    'is_cpo': 'boolean',
    'deal_type': 'string',
    'front_end_gross': 'decimal',
    'back_end_gross': 'decimal',
    'total_gross': 'decimal',
    'deal_credit': 'decimal',
    'sale_date': 'date',
    'count': 'decimal',
    'unit_credit': 'decimal',
    'commission_credit_multiplier': 'decimal',
    'monthly_units': 'decimal',
    'monthly_front_gross': 'decimal',
    'monthly_back_gross': 'decimal',
    'monthly_total_gross': 'decimal',
    'monthly_new_units': 'decimal',
    'monthly_used_units': 'decimal',
    'fast_start_volume_units': 'decimal',
    'units_by_day_10': 'decimal',
    'green_pea': 'boolean',
    'nps_finance_eligible': 'boolean',
    'ar_requirement_met': 'boolean',
    'training_requirements_met': 'boolean',
    'call_requirement_met': 'boolean',
    'video_requirement_met': 'boolean',
    'nps_bonus_eligible': 'boolean',
    'nps_qualifying_surveys': 'decimal',
    'nps_low_score_surveys': 'decimal',
    'holiday_bonus_eligible': 'boolean',
    'holiday_bonus_forfeited': 'boolean',
}

REQUIRED_RULE_FIELDS = {
    'front_gross_percentage': ['rate', 'gross_field'],
    'progressive_unit_position_percentage': [
        'gross_field', 'pack_amount', 'unit_filter', 'tiers', 'non_retroactive',
    ],
    'tiered_minimum_commission': ['tiers', 'unit_metric', 'applies_to_categories'],
    'back_gross_percentage': ['rate', 'gross_field'],
    'flat_per_deal': ['amount'],
    'flat_backend_commission': ['amount'],
    'minimum_commission': ['minimum_amount', 'applies_to_categories'],
    'maximum_commission': ['maximum_amount', 'applies_to_categories'],
    'volume_bonus': ['tiers', 'tier_mode'],
    'per_unit_bonus': ['amount_per_unit', 'starting_after_units', 'include_threshold_unit'],
    'vehicle_spiff': ['amount'],
    'manual_adjustment': ['amount', 'adjustment_type'],
    'deduction': ['amount'],
    'period_qualification_bonus': ['amount', 'requirements', 'requirement_mode'],
    'draw': ['amount', 'frequency', 'recoverable'],
    'survey_count_bonus': [
        'grid', 'qualifying_count_field', 'low_score_count_field',
    ],
    'acquisition_bonus': ['amount'],
}

CATEGORY_FIELDS = {
    'front_gross_percentage': 'front_end',
    'progressive_unit_position_percentage': 'front_end',
    'tiered_minimum_commission': 'minimum_adjustment',
    'back_gross_percentage': 'back_end',
    'flat_per_deal': 'flat',
    'flat_backend_commission': 'back_end',
    'minimum_commission': 'minimum_adjustment',
    'maximum_commission': 'cap_adjustment',
    'volume_bonus': 'bonus',
    'per_unit_bonus': 'bonus',
    'vehicle_spiff': 'spiff',
    'manual_adjustment': 'manual_adjustment',
    'deduction': 'deduction',
    'period_qualification_bonus': 'bonus',
    'draw': 'draw',
    'survey_count_bonus': 'bonus',
    'acquisition_bonus': 'bonus',
}


def validate_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise RuleConfigurationError(f'Invalid decimal for {field_name}: {value}')


def normalize_percentage_rate(value: Any, field_name: str = 'rate') -> Decimal:
    """Normalize 5, 5%, and 0.05 to the canonical multiplier 0.05."""
    raw = str(value).strip()
    explicit_percent = raw.endswith('%')
    if explicit_percent:
        raw = raw[:-1].strip()
    rate = validate_decimal(raw, field_name)
    if explicit_percent or rate > Decimal('1'):
        rate /= Decimal('100')
    if rate <= 0 or rate > Decimal('1'):
        raise RuleConfigurationError(
            f'{field_name} must be greater than 0% and no more than 100%.'
        )
    return rate


def validate_boolean(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    raise RuleConfigurationError(f'Invalid boolean for {field_name}: {value}')


def validate_required_fields(rule_type: str, configuration: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_RULE_FIELDS.get(rule_type, []) if key not in configuration]
    if missing:
        raise RuleConfigurationError(f'Missing required fields for {rule_type}: {missing}')


def validate_rule_type(rule_type: str) -> None:
    if rule_type not in SUPPORTED_RULE_TYPES:
        raise RuleConfigurationError(f'Unsupported rule type: {rule_type}')


def validate_rule_scope(scope: str, rule_type: str) -> None:
    if scope not in SUPPORTED_RULE_SCOPES:
        raise RuleConfigurationError(f'Unsupported rule scope: {scope}')
    if rule_type == 'period_qualification_bonus' and scope != 'period':
        raise RuleConfigurationError('period_qualification_bonus must use period scope')


def validate_condition_field(field_name: str) -> str:
    if field_name not in SUPPORTED_CONDITION_FIELDS:
        raise ConditionValidationError(f'Unsupported condition field: {field_name}')
    return SUPPORTED_CONDITION_FIELDS[field_name]


def validate_condition_operator(operator: str, field_type: str) -> None:
    if operator not in SUPPORTED_OPERATOR_CONFIG:
        raise ConditionValidationError(f'Unsupported operator: {operator}')
    if field_type not in SUPPORTED_OPERATOR_CONFIG[operator]:
        raise ConditionValidationError(
            f'Operator {operator} is not valid for field type {field_type}'
        )


def validate_condition_value(value: Any, field_type: str) -> Any:
    if field_type == 'decimal':
        if isinstance(value, list):
            return [validate_decimal(item, 'condition value') for item in value]
        return validate_decimal(value, 'condition value')
    if field_type == 'boolean':
        return validate_boolean(value, 'condition value')
    if field_type == 'date':
        if isinstance(value, str):
            try:
                from datetime import datetime
                return datetime.strptime(value, '%Y-%m-%d').date()
            except ValueError:
                raise ConditionValidationError(f'Invalid date format: {value}')
        return value
    if field_type == 'string':
        if isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise ConditionValidationError(
                    f'Invalid string list value: {value}'
                )
            return value
        if not isinstance(value, str):
            raise ConditionValidationError(f'Invalid string value: {value}')
        return value
    return value


def validate_condition(condition: dict[str, Any]) -> None:
    field_type = validate_condition_field(condition.get('field_name', ''))
    operator = condition.get('operator')
    validate_condition_operator(operator, field_type)
    value = condition.get('value')
    if operator in ('is_true', 'is_false'):
        if value is not None:
            raise ConditionValidationError(
                f'Operator {operator} must not include a value'
            )
        return
    if value is None:
        raise ConditionValidationError(f'Condition value is required for operator {operator}')
    validated = validate_condition_value(value, field_type)
    if operator == 'between':
        if not isinstance(validated, list) or len(validated) != 2:
            raise ConditionValidationError('Between operator requires a two-element list')
        if validated[0] > validated[1]:
            raise ConditionValidationError('Between operator requires low <= high')


def validate_conditions(conditions: list[dict[str, Any]]) -> None:
    for condition in conditions:
        validate_condition(condition)


def validate_configuration(rule_type: str, configuration: Any) -> None:
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except json.JSONDecodeError:
            raise RuleConfigurationError('Configuration must be valid JSON')
    if not isinstance(configuration, dict):
        raise RuleConfigurationError('Configuration must be an object')
    validate_rule_type(configuration.get('rule_type', rule_type))
    validate_required_fields(rule_type, configuration)
    if rule_type in {'front_gross_percentage', 'back_gross_percentage'}:
        normalize_percentage_rate(configuration.get('rate'))
    if rule_type == 'progressive_unit_position_percentage':
        if configuration.get('non_retroactive') is not True:
            raise RuleConfigurationError(
                'progressive_unit_position_percentage must be explicitly non-retroactive.'
            )
        validate_decimal(configuration.get('pack_amount'), 'pack_amount')
        for tier in configuration.get('tiers') or []:
            validate_decimal(tier.get('start'), 'tier start')
            if tier.get('end') not in (None, ''):
                validate_decimal(tier.get('end'), 'tier end')
            normalize_percentage_rate(tier.get('rate'))
    if 'conditions' in configuration:
        if not isinstance(configuration['conditions'], list):
            raise RuleConfigurationError('conditions must be a list')
        validate_conditions(configuration['conditions'])
