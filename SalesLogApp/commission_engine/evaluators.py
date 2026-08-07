from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .conditions import evaluate_conditions
from .constants import MONETARY_QUANTIZE, MONETARY_ROUNDING
from .exceptions import CalculationError
from .results import CalculationLineItem
from .validators import normalize_percentage_rate, validate_decimal


def round_money(value: Decimal) -> Decimal:
    return value.quantize(MONETARY_QUANTIZE, rounding=MONETARY_ROUNDING)


@dataclass
class BaseEvaluator:
    rule: Any
    configuration: dict[str, Any]
    conditions: list[dict[str, Any]]
    condition_group_operator: str

    def applies(self, context: dict[str, Any]) -> bool:
        effective_start = self.configuration.get('effective_start_date')
        if effective_start:
            effective_start = date.fromisoformat(str(effective_start))
            sale_date = context.get('sale_date')
            period_end = context.get('period_end')
            if sale_date is not None and sale_date < effective_start:
                return False
            if sale_date is None and period_end is not None and period_end < effective_start:
                return False
        if not self.conditions:
            return True
        return evaluate_conditions(self.conditions, context, self.condition_group_operator)

    def build_line_item(self, amount: Decimal, category: str, applied: bool, explanation: str, warnings: list[str] | None = None) -> CalculationLineItem:
        return CalculationLineItem(
            rule_id=self.rule.id,
            rule_name=self.rule.name,
            rule_type=self.rule.rule_type,
            category=category,
            scope=self.rule.calculation_scope,
            amount=round_money(amount),
            explanation=explanation,
            applied=applied,
            warnings=warnings or [],
            metadata={
                'configuration': self.configuration,
            },
        )


class FrontGrossPercentageEvaluator(BaseEvaluator):
    category = 'front_end'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        rate = normalize_percentage_rate(self.configuration['rate'])
        gross_field = self.configuration['gross_field']
        gross = context.get(gross_field)
        if gross is None:
            return self.build_line_item(Decimal('0.00'), self.category, False, f'{gross_field} is missing.')
        gross = Decimal(str(gross))
        pack = validate_decimal(self.configuration.get('pack_amount', 0), 'pack_amount')
        commissionable = gross - pack
        amount = commissionable * rate
        item = self.build_line_item(
            amount, self.category, True,
            f'${gross:.2f} gross - ${pack:.2f} pack = ${commissionable:.2f}; '
            f'{rate * 100}% = ${amount:.2f}.',
        )
        item.metadata.update({
            'raw_gross': str(gross), 'pack': str(pack),
            'commissionable_gross': str(commissionable), 'rate': str(rate),
        })
        return item


class BackGrossPercentageEvaluator(BaseEvaluator):
    category = 'back_end'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        rate = normalize_percentage_rate(self.configuration['rate'])
        gross_field = self.configuration['gross_field']
        gross = context.get(gross_field)
        if gross is None:
            return self.build_line_item(Decimal('0.00'), self.category, False, f'{gross_field} is missing.')
        gross = Decimal(str(gross))
        pack = validate_decimal(self.configuration.get('pack_amount', 0), 'pack_amount')
        commissionable = max(gross - pack, Decimal('0'))
        amount = commissionable * rate
        item = self.build_line_item(
            amount, self.category, True,
            f'${gross:.2f} gross - ${pack:.2f} pack = ${commissionable:.2f}; '
            f'{rate * 100}% = ${amount:.2f}.',
        )
        item.metadata.update({
            'raw_gross': str(gross), 'pack': str(pack),
            'commissionable_gross': str(commissionable), 'rate': str(rate),
        })
        return item


class ProgressiveUnitPositionPercentageEvaluator(BaseEvaluator):
    category = 'front_end'

    def evaluate(self, context):
        if not self.applies(context):
            return self.build_line_item(Decimal('0'), self.category, False, 'Conditions not met.')
        gross = Decimal(str(context.get(self.configuration['gross_field'], 0) or 0))
        pack = validate_decimal(self.configuration['pack_amount'], 'pack_amount')
        before = Decimal(str(context.get('condition_units_before_sale', 0)))
        after = Decimal(str(context.get('condition_units_after_sale', 0)))
        selected = None
        for tier in self.configuration['tiers']:
            start = Decimal(str(tier['start']))
            end = tier.get('end')
            if after >= start and (end in (None, '') or after <= Decimal(str(end))):
                selected = tier
                break
        if selected is None:
            raise CalculationError(f'No progressive tier contains unit position {after}.')
        rate = normalize_percentage_rate(selected['rate'])
        commissionable = gross - pack
        amount = commissionable * rate
        item = self.build_line_item(
            amount, self.category, True,
            f'Used units before/after: {before}/{after}. ${gross:.2f} gross - '
            f'${pack:.2f} pack = ${commissionable:.2f}; {rate * 100}% = ${amount:.2f}.',
        )
        item.metadata.update({
            'raw_gross': str(gross), 'pack': str(pack),
            'commissionable_gross': str(commissionable), 'rate': str(rate),
            'units_before_sale': str(before), 'unit_position': str(after),
            'non_retroactive': True,
        })
        return item


class TieredMinimumCommissionEvaluator(BaseEvaluator):
    category = 'minimum_adjustment'

    def evaluate(self, context):
        if not self.applies(context):
            return self.build_line_item(Decimal('0'), self.category, False, 'Conditions not met.')
        units = Decimal(str(context.get(self.configuration['unit_metric'], 0) or 0))
        selected = None
        for tier in self.configuration['tiers']:
            low = Decimal(str(tier['minimum_units']))
            high = tier.get('maximum_units')
            if units >= low and (high in (None, '') or units <= Decimal(str(high))):
                selected = tier
                break
        if selected is None:
            return self.build_line_item(Decimal('0'), self.category, False, 'No minimum tier reached.')
        minimum = Decimal(str(selected['amount']))
        applies_to = self.configuration['applies_to_categories']
        subtotal = sum(
            (Decimal(str(context.get(f'{category}_subtotal', 0))) for category in applies_to),
            Decimal('0'),
        )
        adjustment = max(minimum - subtotal, Decimal('0'))
        item = self.build_line_item(
            adjustment, 'front_end', adjustment > 0,
            f'{units} New units selects ${minimum:.2f} minimum; '
            f'adjustment ${adjustment:.2f}.',
        )
        item.metadata.update({'monthly_new_units': str(units), 'minimum': str(minimum)})
        return item


class FlatPerDealEvaluator(BaseEvaluator):
    category = 'flat'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        amount = validate_decimal(self.configuration['amount'], 'amount')
        if amount == 0:
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Flat amount is zero.')
        return self.build_line_item(amount, self.category, True, f'Flat per-deal commission = ${amount:.2f}.')


class FlatBackendCommissionEvaluator(FlatPerDealEvaluator):
    category = 'back_end'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(
                Decimal('0.00'), self.category, False, 'Conditions not met.'
            )
        amount = validate_decimal(self.configuration['amount'], 'amount')
        if amount == 0:
            return self.build_line_item(
                Decimal('0.00'), self.category, False,
                'Flat backend amount is zero.',
            )
        return self.build_line_item(
            amount, self.category, True,
            f'Flat backend commission = ${amount:.2f}.',
        )


class MinimumCommissionEvaluator(BaseEvaluator):
    category = 'minimum_adjustment'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        minimum_amount = validate_decimal(self.configuration['minimum_amount'], 'minimum_amount')
        applies_to = self.configuration['applies_to_categories']
        applicable_total = sum(
            context.get(f'{category}_subtotal', Decimal('0.00'))
            for category in applies_to
        )
        adjustment = max(Decimal('0.00'), minimum_amount - applicable_total)
        output_category = (
            applies_to[0]
            if len(applies_to) == 1 and applies_to[0] in {'front_end', 'back_end'}
            else self.category
        )
        if adjustment == 0:
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Minimum already met.')
        return self.build_line_item(adjustment, output_category, True, f'Calculated commission was ${applicable_total:.2f}. Minimum adjustment added ${adjustment:.2f}.')


class MaximumCommissionEvaluator(BaseEvaluator):
    category = 'cap_adjustment'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        maximum_amount = validate_decimal(self.configuration['maximum_amount'], 'maximum_amount')
        applies_to = self.configuration['applies_to_categories']
        applicable_total = sum(
            context.get(f'{category}_subtotal', Decimal('0.00'))
            for category in applies_to
        )
        output_category = (
            applies_to[0]
            if len(applies_to) == 1 and applies_to[0] in {'front_end', 'back_end'}
            else self.category
        )
        if applicable_total <= maximum_amount:
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Maximum not exceeded.')
        adjustment = maximum_amount - applicable_total
        return self.build_line_item(adjustment, output_category, True, f'Calculated commission was ${applicable_total:.2f}. Cap adjustment applied ${adjustment:.2f}.')


class VolumeBonusEvaluator(BaseEvaluator):
    category = 'bonus'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        tiers = self.configuration['tiers']
        tier_mode = self.configuration['tier_mode']
        unit_metric = self.configuration.get('unit_metric', 'monthly_units')
        effective_start = self.configuration.get('effective_start_date')
        if effective_start and context.get('_sales') is not None:
            start = date.fromisoformat(str(effective_start))
            eligible_sales = [
                sale for sale in context['_sales']
                if getattr(sale, 'date', None) is not None
                and sale.date >= start
            ]
            if unit_metric in {'monthly_new_units', 'monthly_used_units'}:
                from .vehicle_conditions import normalize_vehicle_condition
                required_condition = (
                    'new' if unit_metric == 'monthly_new_units' else 'used'
                )
                eligible_sales = [
                    sale for sale in eligible_sales
                    if normalize_vehicle_condition(
                        getattr(sale, 'vehicle_condition', None)
                    ) == required_condition
                ]
            units = sum(
                (
                    Decimal(str(getattr(
                        sale, 'unit_credit', getattr(sale, 'count', 0),
                    ) or 0))
                    for sale in eligible_sales
                ),
                Decimal('0'),
            )
            if unit_metric == 'fast_start_volume_units' and eligible_sales:
                month_start = min(sale.date for sale in eligible_sales).replace(day=1)
                working_days = []
                candidate = month_start
                while len(working_days) < 7:
                    if candidate.weekday() != 6:
                        working_days.append(candidate)
                    candidate += timedelta(days=1)
                cutoff = working_days[-1]
                units += sum(
                    (
                        Decimal(str(getattr(
                            sale, 'unit_credit', getattr(sale, 'count', 0),
                        ) or 0))
                        for sale in eligible_sales
                        if sale.date <= cutoff
                    ),
                    Decimal('0'),
                )
        else:
            units = Decimal(str(context.get(unit_metric, Decimal('0.00'))))
        qualified = [
            (Decimal(str(t['minimum_units'])), Decimal(str(t['amount'])))
            for t in tiers
            if units >= Decimal(str(t['minimum_units']))
            and (
                t.get('maximum_units') in (None, '')
                or units <= Decimal(str(t['maximum_units']))
            )
        ]
        if not qualified:
            return self.build_line_item(Decimal('0.00'), self.category, False, 'No eligible volume bonus tiers.')
        if tier_mode == 'highest_only':
            amount = max(item[1] for item in qualified)
        elif tier_mode == 'cumulative':
            amount = sum(item[1] for item in qualified)
        else:
            raise CalculationError(f'Unsupported tier mode: {tier_mode}')
        return self.build_line_item(amount, self.category, True, f'Volume bonus applied for {units} units = ${amount:.2f}.')


class PerUnitBonusEvaluator(BaseEvaluator):
    category = 'bonus'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        amount_per_unit = validate_decimal(self.configuration['amount_per_unit'], 'amount_per_unit')
        threshold = Decimal(str(self.configuration['starting_after_units']))
        include_threshold = bool(self.configuration['include_threshold_unit'])
        units = Decimal(str(context.get('monthly_units', Decimal('0.00'))))
        if include_threshold:
            eligible_units = max(Decimal('0.00'), units - threshold + Decimal('1.00'))
        else:
            eligible_units = max(Decimal('0.00'), units - threshold)
        if eligible_units <= 0:
            return self.build_line_item(Decimal('0.00'), self.category, False, 'No eligible units for per-unit bonus.')
        amount = amount_per_unit * eligible_units
        return self.build_line_item(amount, self.category, True, f'Per-unit bonus on {eligible_units} units = ${amount:.2f}.')


class VehicleSpiffEvaluator(BaseEvaluator):
    category = 'spiff'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        amount = validate_decimal(self.configuration['amount'], 'amount')
        if amount == 0:
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Spiff amount is zero.')
        return self.build_line_item(amount, self.category, True, f'Vehicle spiff applied = ${amount:.2f}.')


class AcquisitionBonusEvaluator(VehicleSpiffEvaluator):
    category = 'bonus'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(
                Decimal('0.00'), self.category, False,
                'Acquisition source is not eligible.',
            )
        amount = validate_decimal(self.configuration['amount'], 'amount')
        return self.build_line_item(
            amount, self.category, True,
            f'Eligible acquisition-source bonus applied = ${amount:.2f}.',
        )


class ManualAdjustmentEvaluator(BaseEvaluator):
    category = 'manual_adjustment'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        amount = validate_decimal(self.configuration['amount'], 'amount')
        adjustment_type = self.configuration['adjustment_type']
        if adjustment_type not in ('bonus', 'deduction'):
            raise CalculationError('Invalid adjustment_type')
        explanation = self.configuration.get('reason', 'Manual adjustment applied.')
        return self.build_line_item(amount, self.category, True, explanation)


class DeductionEvaluator(BaseEvaluator):
    category = 'deduction'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        amount = validate_decimal(self.configuration['amount'], 'amount')
        if amount == 0:
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Deduction amount is zero.')
        amount = -abs(amount)
        reason = self.configuration.get('reason', 'Deduction applied.')
        return self.build_line_item(amount, self.category, True, reason)


class PeriodQualificationBonusEvaluator(BaseEvaluator):
    category = 'bonus'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Conditions not met.')
        amount = validate_decimal(self.configuration['amount'], 'amount')
        requirements = self.configuration['requirements']
        requirement_mode = self.configuration['requirement_mode']
        metrics = {
            'monthly_units': Decimal(str(context.get('monthly_units', Decimal('0.00')))),
            'monthly_total_gross': Decimal(str(context.get('monthly_total_gross', Decimal('0.00')))),
            'units_by_day_10': Decimal(str(context.get('units_by_day_10', Decimal('0.00')))),
        }
        effective_start = self.configuration.get('effective_start_date')
        if effective_start and context.get('_sales') is not None:
            start = date.fromisoformat(str(effective_start))
            eligible_sales = [
                sale for sale in context['_sales']
                if getattr(sale, 'date', None) is not None
                and sale.date >= start
            ]
            metrics['monthly_units'] = sum(
                (
                    Decimal(str(getattr(
                        sale, 'unit_credit', getattr(sale, 'count', 0),
                    ) or 0))
                    for sale in eligible_sales
                ),
                Decimal('0'),
            )
            metrics['units_by_day_10'] = sum(
                (
                    Decimal(str(getattr(
                        sale, 'unit_credit', getattr(sale, 'count', 0),
                    ) or 0))
                    for sale in eligible_sales
                    if sale.date.day <= 10
                ),
                Decimal('0'),
            )
        satisfied = []
        for requirement in requirements:
            metric = requirement['metric']
            operator = requirement['operator']
            value = Decimal(str(requirement['value']))
            if metric not in metrics:
                raise CalculationError(f'Unsupported requirement metric: {metric}')
            if operator == 'greater_than_or_equal':
                satisfied.append(metrics[metric] >= value)
            elif operator == 'less_than_or_equal':
                satisfied.append(metrics[metric] <= value)
            elif operator == 'greater_than':
                satisfied.append(metrics[metric] > value)
            elif operator == 'less_than':
                satisfied.append(metrics[metric] < value)
            elif operator == 'equals':
                satisfied.append(metrics[metric] == value)
            elif operator == 'not_equals':
                satisfied.append(metrics[metric] != value)
            else:
                raise CalculationError(f'Unsupported requirement operator: {operator}')
        if requirement_mode == 'all':
            applies = all(satisfied)
        elif requirement_mode == 'any':
            applies = any(satisfied)
        else:
            raise CalculationError(f'Unsupported requirement mode: {requirement_mode}')
        if not applies:
            return self.build_line_item(Decimal('0.00'), self.category, False, 'Period qualification requirements not met.')
        return self.build_line_item(amount, self.category, True, f'Period qualification bonus applied = ${amount:.2f}.')


class SurveyCountBonusEvaluator(BaseEvaluator):
    category = 'bonus'

    def evaluate(self, context: dict[str, Any]) -> CalculationLineItem:
        if not self.applies(context):
            return self.build_line_item(
                Decimal('0.00'), self.category, False,
                'Survey bonus eligibility conditions not met.',
            )
        qualifying = int(Decimal(str(context.get(
            self.configuration['qualifying_count_field'], 0,
        ))))
        low_score = int(Decimal(str(context.get(
            self.configuration['low_score_count_field'], 0,
        ))))
        grid = sorted(
            self.configuration['grid'],
            key=lambda row: int(row['count']),
        )
        if qualifying <= 0 and low_score <= 0:
            return self.build_line_item(
                Decimal('0.00'), self.category, False,
                'No returned NPS surveys recorded.',
            )
        capped_count = min(qualifying, int(grid[-1]['count']))
        row = next(
            (item for item in grid if int(item['count']) == capped_count),
            grid[0],
        )
        earned = Decimal(str(row['total'])) if qualifying > 0 else Decimal('0')
        rate = Decimal(str(row['rate_per_survey']))
        deduction = rate * Decimal(low_score)
        amount = earned - deduction
        return self.build_line_item(
            amount,
            self.category,
            True,
            (
                f'NPS survey bonus: {qualifying} qualifying survey(s) earned '
                f'${earned:.2f}; {low_score} low-score survey(s) deducted '
                f'${deduction:.2f}; net ${amount:.2f}.'
            ),
        )
