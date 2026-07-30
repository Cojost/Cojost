from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, List, Optional


@dataclass(frozen=True)
class CalculationLineItem:
    rule_id: int
    rule_name: str
    rule_type: str
    category: str
    scope: str
    amount: Decimal
    explanation: str
    applied: bool
    warnings: List[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CalculationResult:
    user: Any
    pay_plan: Any
    pay_plan_version: Any
    period_start: Any = None
    period_end: Any = None
    sale: Any = None
    base_commission: Decimal = Decimal('0.00')
    bonuses: Decimal = Decimal('0.00')
    spiffs: Decimal = Decimal('0.00')
    adjustments: Decimal = Decimal('0.00')
    deductions: Decimal = Decimal('0.00')
    total: Decimal = Decimal('0.00')
    line_items: List[CalculationLineItem] = field(default_factory=list)
    skipped_rules: List[dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_line_item(self, item: CalculationLineItem) -> None:
        self.line_items.append(item)
        if not item.applied:
            return
        self.total += item.amount
        if item.category == 'front_end' or item.category == 'back_end' or item.category == 'flat':
            self.base_commission += item.amount
        elif item.category == 'bonus':
            self.bonuses += item.amount
        elif item.category == 'spiff':
            self.spiffs += item.amount
        elif item.category == 'deduction':
            self.deductions += item.amount
        elif item.category in ('minimum_adjustment', 'cap_adjustment', 'manual_adjustment'):
            self.adjustments += item.amount
        else:
            self.adjustments += item.amount

    def add_skipped_rule(self, rule_id: int, rule_name: str, rule_type: str, reason: str) -> None:
        self.skipped_rules.append({
            'rule_id': rule_id,
            'rule_name': rule_name,
            'rule_type': rule_type,
            'reason': reason,
        })


@dataclass
class LegacyComparisonResult:
    user: Any
    sale: Any | None = None
    period_start: Any = None
    period_end: Any = None
    legacy_totals: dict[str, Decimal] = field(default_factory=dict)
    engine_result: Any = None
    sale_comparisons: list[dict[str, Any]] = field(default_factory=list)
    mismatches: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PeriodCalculationResult(CalculationResult):
    sale_results: List[CalculationResult] = field(default_factory=list)

    def add_warning(self, warning: str) -> None:
        self.warnings.append(warning)
