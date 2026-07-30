from decimal import Decimal, InvalidOperation

from django import template


register = template.Library()


def _decimal(value):
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


@register.filter
def scenario_currency(value):
    return f'${_decimal(value):,.2f}'


@register.filter
def signed_scenario_currency(value):
    amount = _decimal(value)
    sign = '+' if amount > 0 else '-' if amount < 0 else ''
    return f'{sign}${abs(amount):,.2f}'


@register.filter
def signed_scenario_percent(value):
    if value is None or value == '':
        return 'Not applicable'
    amount = _decimal(value)
    sign = '+' if amount > 0 else ''
    return f'{sign}{amount:,.2f}%'
