from decimal import Decimal

from .vehicle_conditions import normalize_vehicle_condition


def condition_unit_position(sale, condition, sales=None):
    """Return deterministic condition-specific units before and after a sale."""
    normalized = normalize_vehicle_condition(condition)
    if not normalized:
        return Decimal('0'), Decimal('0')
    if sales is None:
        Sale = type(sale)
        sales = list(Sale.objects.filter(
            user_id=sale.user_id,
            date__year=sale.date.year,
            date__month=sale.date.month,
        ))
    eligible = [
        item for item in sales
        if item.user_id == sale.user_id
        and item.date.year == sale.date.year
        and item.date.month == sale.date.month
        and normalize_vehicle_condition(item.vehicle_condition) == normalized
    ]
    eligible.sort(key=lambda item: (item.date, item.pk or 0))
    before = Decimal('0')
    for item in eligible:
        if item.pk == sale.pk:
            credit = Decimal(str(item.count or 0))
            return before, before + credit
        before += Decimal(str(item.count or 0))
    credit = Decimal(str(getattr(sale, 'count', 0) or 0))
    return before, before + credit
