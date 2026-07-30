from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


def _money(value):
    try:
        return f'${Decimal(str(value)):,.2f}'
    except (InvalidOperation, TypeError):
        return 'Not available'


def _percent(value):
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return 'Not available'
    if rate <= 1:
        rate *= 100
    return f'{rate.normalize()}%'


def _units(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return str(value)
    return str(number.quantize(Decimal('0.1'))).rstrip('0').rstrip('.')


@register.filter
def rule_summary(rule):
    config = rule.configuration or {}
    rule_type = rule.rule_type
    if rule_type == 'front_gross_percentage':
        return f'{_percent(config.get("rate"))} of front-end gross'
    if rule_type == 'back_gross_percentage':
        return f'{_percent(config.get("rate"))} of back-end gross'
    if rule_type == 'minimum_commission':
        return f'{_money(config.get("minimum_amount"))} minimum commission'
    if rule_type == 'maximum_commission':
        return f'Commission capped at {_money(config.get("maximum_amount"))}'
    if rule_type == 'flat_per_deal':
        return f'{_money(config.get("amount"))} per eligible deal'
    if rule_type == 'vehicle_spiff':
        return f'{_money(config.get("amount"))} vehicle bonus'
    if rule_type == 'per_unit_bonus':
        return (
            f'{_money(config.get("amount_per_unit"))} per unit after '
            f'{_units(config.get("starting_after_units"))} units'
        )
    if rule_type == 'volume_bonus':
        tiers = config.get('tiers') or []
        if not tiers:
            return 'Volume bonus with no configured tiers'
        return '; '.join(
            f'{_money(tier.get("amount"))} bonus at '
            f'{_units(tier.get("minimum_units"))} units'
            for tier in tiers
        )
    if rule_type == 'progressive_unit_position_percentage':
        return 'Front-end percentage increases by monthly unit position'
    if rule_type == 'tiered_minimum_commission':
        return 'Minimum commission increases by monthly unit total'
    if rule_type == 'draw':
        return f'{_money(config.get("amount"))} {config.get("frequency", "")} draw'.strip()
    return rule.name


@register.filter
def condition_summary(condition):
    labels = {
        'vehicle_condition': 'vehicle type',
        'green_pea': 'new-hire status',
        'monthly_units': 'monthly units',
        'monthly_new_units': 'monthly new units',
        'monthly_used_units': 'monthly used units',
        'make': 'vehicle make',
        'model': 'vehicle model',
        'mileage': 'vehicle mileage',
    }
    operators = {
        'equals': 'is',
        'not_equals': 'is not',
        'greater_than': 'is greater than',
        'greater_than_or_equal': 'is at least',
        'less_than': 'is less than',
        'less_than_or_equal': 'is no more than',
        'is_true': 'is required',
        'is_false': 'is not required',
    }
    field = labels.get(condition.field_name, condition.field_name.replace('_', ' '))
    operator = operators.get(condition.operator, condition.operator.replace('_', ' '))
    if condition.operator in {'is_true', 'is_false'}:
        return f'{field.title()} {operator}'
    return f'{field.title()} {operator} {condition.value}'
