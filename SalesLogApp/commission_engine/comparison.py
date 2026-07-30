from decimal import Decimal
from typing import Any

from .engine import calculate_period_commission, calculate_sale_commission
from .results import LegacyComparisonResult
from ..services import commission_totals


def _to_decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _sum_category(result: Any, category: str) -> Decimal:
    return sum(
        (item.amount for item in getattr(result, 'line_items', [])
         if item.category == category and item.applied),
        Decimal('0.00'),
    )


def compare_sale_commission(user: Any, sale: Any) -> LegacyComparisonResult:
    engine_result = calculate_sale_commission(user, sale)
    legacy_front = _to_decimal(sale.calculate_frontEnd)
    legacy_back = _to_decimal(sale.calculate_backend)
    legacy_total = legacy_front + legacy_back
    engine_front = _sum_category(engine_result, 'front_end')
    engine_back = _sum_category(engine_result, 'back_end')
    sale_comparison = {
        'sale_id': getattr(sale, 'id', None),
        'legacy_front_end': legacy_front,
        'legacy_back_end': legacy_back,
        'legacy_total': legacy_total,
        'engine_front_end': engine_front,
        'engine_back_end': engine_back,
        'engine_total': engine_result.total,
        'mismatches': {
            'front_end': legacy_front != engine_front,
            'back_end': legacy_back != engine_back,
            'total': legacy_total != engine_result.total,
        },
    }
    return LegacyComparisonResult(
        user=user,
        sale=sale,
        legacy_totals={
            'front_end': legacy_front,
            'back_end': legacy_back,
            'total': legacy_total,
        },
        engine_result=engine_result,
        sale_comparisons=[sale_comparison],
        mismatches=sale_comparison['mismatches'],
    )


def compare_period_commission(user: Any, sales: list[Any], period_start: Any = None, period_end: Any = None) -> LegacyComparisonResult:
    engine_result = calculate_period_commission(user, sales, period_start, period_end)
    legacy_totals = commission_totals(user, sales)
    comparisons = [compare_sale_commission(user, sale).sale_comparisons[0] for sale in sales]
    mismatches = {
        'base_commission': (
            _to_decimal(legacy_totals.get('front', 0))
            + _to_decimal(legacy_totals.get('back', 0))
        ) != engine_result.base_commission,
        'bonuses': _to_decimal(legacy_totals.get('bonus', 0)) != engine_result.bonuses,
        'adjustments': _to_decimal(legacy_totals.get('adjustments', 0)) != engine_result.adjustments,
        'total': _to_decimal(legacy_totals.get('total', 0)) != engine_result.total,
    }
    return LegacyComparisonResult(
        user=user,
        period_start=period_start,
        period_end=period_end,
        legacy_totals={
            'units': _to_decimal(legacy_totals.get('units', 0)),
            'front': _to_decimal(legacy_totals.get('front', 0)),
            'back': _to_decimal(legacy_totals.get('back', 0)),
            'bonus': _to_decimal(legacy_totals.get('bonus', 0)),
            'adjustments': _to_decimal(legacy_totals.get('adjustments', 0)),
            'total': _to_decimal(legacy_totals.get('total', 0)),
        },
        engine_result=engine_result,
        sale_comparisons=comparisons,
        mismatches=mismatches,
    )
