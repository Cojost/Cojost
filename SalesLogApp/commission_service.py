from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.utils import timezone

from .access import get_commission_system
from .plan_requirements import ActivePayPlanService, PlanRequirementService
from .commission_engine.engine import (
    build_eligibility_context,
    build_period_context,
    calculate_period_commission,
    calculate_sale_commission as calculate_sale_commission_v2,
    calculate_sale_commission_for_version,
    resolve_pay_plan_version,
)
from .commission_engine.conditions import evaluate_conditions
from .commission_engine.exceptions import PayPlanResolutionError
from .commission_engine.exceptions import CommissionEngineError

STATUS_CALCULATED = 'calculated'
STATUS_MISSING_PLAN = 'missing_plan'
STATUS_INACTIVE_PLAN = 'inactive_plan'
STATUS_MISSING_SALE_DATA = 'missing_sale_data'
STATUS_NO_MATCHING_RULE = 'no_matching_rule'
STATUS_CONFIGURATION_ERROR = 'configuration_error'
STATUS_LEGACY_SETTINGS_MISSING = 'legacy_settings_missing'
STATUS_CALCULATION_ERROR = 'calculation_error'
STATUS_PARTIAL = 'partial'
COMPONENT_MATCHED_RULE = 'matched_rule'
COMPONENT_DEFAULT_FALLBACK = 'default_fallback'
COMPONENT_NOT_APPLICABLE = 'not_applicable'
COMPONENT_MISSING_CONFIGURATION = 'missing_configuration'
COMPONENT_INVALID_CONFIGURATION = 'invalid_configuration'


@dataclass
class SaleCommissionDiagnostic:
    status: str
    engine: str
    sale_id: int | None
    plan_id: int | None = None
    plan_name: str = ''
    plan_version: str = ''
    frontend_gross: Decimal = Decimal('0.00')
    backend_gross: Decimal = Decimal('0.00')
    frontend_commission: Decimal = Decimal('0.00')
    backend_commission: Decimal = Decimal('0.00')
    bonus_commission: Decimal = Decimal('0.00')
    acquisition_bonus: Decimal = Decimal('0.00')
    total_commission: Decimal = Decimal('0.00')
    unit_credit: Decimal = Decimal('0.0')
    matched_rules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    frontend_status: str = COMPONENT_NOT_APPLICABLE
    backend_status: str = COMPONENT_NOT_APPLICABLE
    frontend_rule: str = ''
    backend_rule: str = ''
    frontend_explanation: list[str] = field(default_factory=list)
    backend_explanation: list[str] = field(default_factory=list)
    component_errors: dict[str, list[str]] = field(default_factory=dict)
    line_items: list[dict[str, Any]] = field(default_factory=list)
    half_deal_multiplier: Decimal = Decimal('1.0')
    running_period_units: Decimal = Decimal('0.0')
    current_unit_tier: dict[str, Any] | None = None
    next_unit_tier: dict[str, Any] | None = None
    units_needed_for_next_tier: Decimal | None = None

    @property
    def calculated(self) -> bool:
        return self.status in {STATUS_CALCULATED, STATUS_PARTIAL}

    @property
    def total_deal_commission(self) -> Decimal:
        return self.total_commission


class CommissionEngineService:
    @staticmethod
    def _sum_line_item_category(result: Any, category: str) -> Decimal:
        return sum(
            (
                item.amount
                for item in result.line_items
                if item.category == category and item.applied
            ),
            Decimal('0.00'),
        )

    @staticmethod
    def _missing_sale_data_errors(sale: Any) -> list[str]:
        errors = []
        for field_name in ('date', 'frontEnd', 'backend', 'count'):
            if getattr(sale, field_name, None) is None:
                errors.append(f'{field_name} is required.')
        return errors

    @classmethod
    def calculate_sale(
        cls, user: Any, sale: Any,
        monthly_metrics: dict[str, Any] | None = None,
        version: Any | None = None,
        allow_historical_version: bool = False,
    ) -> SaleCommissionDiagnostic:
        from .models import Commission, UserProfile

        engine = get_commission_system(user)
        base = {
            'engine': engine,
            'sale_id': getattr(sale, 'id', None),
            'unit_credit': Decimal(str(getattr(sale, 'unit_credit', 0) or 0)),
            'frontend_gross': Decimal(str(getattr(sale, 'frontEnd', 0) or 0)),
            'backend_gross': Decimal(str(getattr(sale, 'backend', 0) or 0)),
            'half_deal_multiplier': Decimal(str(
                getattr(sale, 'commission_credit_multiplier', 1) or 1
            )),
        }
        missing_errors = cls._missing_sale_data_errors(sale)
        if missing_errors:
            return SaleCommissionDiagnostic(
                status=STATUS_MISSING_SALE_DATA,
                errors=missing_errors,
                explanation=['Commission could not be calculated because required sale fields are missing.'],
                **base,
            )

        if engine == UserProfile.LEGACY:
            settings = Commission.objects.filter(user=user).first()
            if settings is None:
                return SaleCommissionDiagnostic(
                    status=STATUS_LEGACY_SETTINGS_MISSING,
                    errors=['Legacy commission settings are missing.'],
                    explanation=['Create legacy commission settings or switch to pay-plan v2.'],
                    **base,
                )
            front = Decimal(str(sale.calculate_frontEnd))
            back = Decimal(str(sale.calculate_backend))
            total = front + back
            warnings = []
            if settings.opt_out_front:
                warnings.append('Legacy front-end opt-out is enabled.')
            if settings.opt_out_back:
                warnings.append('Legacy back-end opt-out is enabled.')
            return SaleCommissionDiagnostic(
                status=STATUS_CALCULATED,
                frontend_commission=front,
                backend_commission=back,
                total_commission=total,
                matched_rules=['Legacy front-end settings', 'Legacy back-end settings'],
                warnings=warnings,
                explanation=[
                    f'Legacy front-end commission: ${front:.2f}.',
                    f'Legacy back-end commission: ${back:.2f}.',
                ],
                **base,
            )

        if version is None:
            try:
                version = resolve_pay_plan_version(user, getattr(sale, 'date', None))
            except PayPlanResolutionError as exc:
                return SaleCommissionDiagnostic(
                    status=STATUS_MISSING_PLAN,
                    errors=[str(exc)],
                    explanation=['No active pay plan assignment covers this sale date.'],
                    **base,
                )

        previewing = version.status in {version.DRAFT, version.REVIEW_REQUIRED}
        historical = (
            allow_historical_version
            and version.status in {version.ACTIVE, version.INACTIVE}
        )
        if version.status != version.ACTIVE and not previewing and not historical:
            return SaleCommissionDiagnostic(
                status=STATUS_INACTIVE_PLAN,
                plan_id=version.pay_plan_id,
                plan_name=version.pay_plan.name,
                plan_version=version.version_name,
                errors=['Resolved pay plan version is not active.'],
                **base,
            )

        active_rules = version.rules.filter(is_active=True)
        try:
            result = (
                calculate_sale_commission_for_version(
                    user, sale, version, monthly_metrics,
                )
                if previewing or historical
                else calculate_sale_commission_v2(user, sale, monthly_metrics)
            )
        except CommissionEngineError as exc:
            return SaleCommissionDiagnostic(
                status=STATUS_CALCULATION_ERROR,
                plan_id=version.pay_plan_id,
                plan_name=version.pay_plan.name,
                plan_version=version.version_name,
                errors=[str(exc)],
                explanation=['Unexpected calculation error.'],
                **base,
            )

        matched_line_items = [item for item in result.line_items if item.applied]
        matched_rules = [item.rule_name for item in matched_line_items]
        explanations = [item.explanation for item in matched_line_items]
        front_items = [
            item for item in result.line_items
            if item.category == 'front_end' and item.applied
        ]
        back_items = [
            item for item in result.line_items
            if item.category == 'back_end' and item.applied
        ]
        acquisition_items = [
            item for item in result.line_items
            if item.rule_type == 'acquisition_bonus' and item.applied
        ]
        front_gross = base['frontend_gross']
        back_gross = base['backend_gross']
        component_errors: dict[str, list[str]] = {}
        skipped_front = [
            item for item in result.skipped_rules
            if item['rule_type'] == 'front_gross_percentage'
        ]
        skipped_back = [
            item for item in result.skipped_rules
            if item['rule_type'] in {
                'back_gross_percentage', 'flat_backend_commission',
                'default_backend_percentage',
            }
        ]
        if acquisition_items:
            frontend_status = COMPONENT_NOT_APPLICABLE
        elif not front_gross:
            frontend_status = COMPONENT_NOT_APPLICABLE
        elif front_items:
            frontend_status = COMPONENT_MATCHED_RULE
        elif skipped_front:
            frontend_status = COMPONENT_INVALID_CONFIGURATION
            component_errors['frontend'] = [
                'The active front-end calculation is invalid: '
                + '; '.join(item['reason'] for item in skipped_front)
            ]
        else:
            frontend_status = COMPONENT_MISSING_CONFIGURATION
            component_errors['frontend'] = [
                'Front-end gross exists, but no valid front-end calculation is configured.'
            ]
        if acquisition_items:
            backend_status = COMPONENT_NOT_APPLICABLE
        elif not back_gross:
            backend_status = COMPONENT_NOT_APPLICABLE
        elif back_items:
            backend_status = (
                COMPONENT_DEFAULT_FALLBACK
                if any(
                    item.metadata.get('calculation_source') == 'default_fallback'
                    for item in back_items
                )
                else COMPONENT_MATCHED_RULE
            )
        elif skipped_back:
            backend_status = COMPONENT_INVALID_CONFIGURATION
            component_errors['backend'] = [
                'The active backend calculation is invalid: '
                + '; '.join(item['reason'] for item in skipped_back)
            ]
        else:
            backend_status = COMPONENT_MISSING_CONFIGURATION
            component_errors['backend'] = [
                'Backend gross exists, but no default backend calculation is configured.'
            ]
        status = (
            STATUS_PARTIAL
            if component_errors and (front_items or back_items or matched_line_items)
            else STATUS_CONFIGURATION_ERROR if component_errors else STATUS_CALCULATED
        )
        skipped_by_type = {
            'frontend': [
                item for item in result.line_items
                if item.category == 'front_end' and not item.applied
            ],
            'backend': [
                item for item in result.line_items
                if item.category == 'back_end' and not item.applied
            ],
        }
        line_items = [
            {
                'rule_name': item.rule_name,
                'rule_type': item.rule_type,
                'category': item.category,
                'amount': item.amount,
                'applied': item.applied,
                'explanation': item.explanation,
                'metadata': item.metadata,
            }
            for item in result.line_items
        ]
        errors = [error for values in component_errors.values() for error in values]
        return SaleCommissionDiagnostic(
            status=status,
            plan_id=version.pay_plan_id,
            plan_name=version.pay_plan.name,
            plan_version=version.version_name,
            frontend_commission=cls._sum_line_item_category(result, 'front_end'),
            backend_commission=cls._sum_line_item_category(result, 'back_end'),
            bonus_commission=cls._sum_line_item_category(result, 'bonus'),
            acquisition_bonus=sum(
                (item.amount for item in acquisition_items),
                Decimal('0.00'),
            ),
            total_commission=result.total,
            matched_rules=matched_rules,
            warnings=result.warnings,
            errors=errors,
            explanation=explanations,
            frontend_status=frontend_status,
            backend_status=backend_status,
            frontend_rule=front_items[0].rule_name if front_items else '',
            backend_rule=back_items[0].rule_name if back_items else '',
            frontend_explanation=[item.explanation for item in front_items] + [
                item.explanation for item in skipped_by_type['frontend']
            ],
            backend_explanation=[item.explanation for item in back_items] + [
                item.explanation for item in skipped_by_type['backend']
            ],
            component_errors=component_errors,
            line_items=line_items,
            **base,
        )

    @classmethod
    def calculate_sales(
        cls, user: Any, sales: list[Any], *,
        allow_historical_versions: bool = False,
    ) -> dict[str, Any]:
        sales = list(sales)
        monthly_metrics = build_period_context(sales) if sales else {}
        results = [
            cls.calculate_sale(
                user,
                sale,
                monthly_metrics,
                allow_historical_version=allow_historical_versions,
            )
            for sale in sales
        ]
        calculated = [item for item in results if item.calculated]
        complete = [item for item in results if item.status == STATUS_CALCULATED]
        partial = [item for item in results if item.status == STATUS_PARTIAL]
        excluded = [item for item in results if item.status != STATUS_CALCULATED]
        total_front = sum((item.frontend_commission for item in calculated), Decimal('0.00'))
        total_back = sum((item.backend_commission for item in calculated), Decimal('0.00'))
        deal_bonus = sum((item.bonus_commission for item in calculated), Decimal('0.00'))
        unit_bonus = UnitBonusService.calculate(user, sales)
        period_unit_bonus = unit_bonus['amount']
        total_bonus = deal_bonus + period_unit_bonus
        total_deal_commission = sum(
            (item.total_commission for item in calculated), Decimal('0.00')
        )
        total_commission = total_deal_commission + period_unit_bonus
        draw_progress = DrawProgressService.calculate(
            user=user,
            sales=sales,
            frontend_commission=total_front,
            backend_commission=total_back,
            unit_bonus=period_unit_bonus,
            other_bonus=deal_bonus,
        )
        running_units = Decimal('0.0')
        by_sale_id = {item.sale_id: item for item in results}
        for sale in sorted(sales, key=lambda value: (value.date, value.id or 0)):
            running_units += Decimal(str(getattr(sale, 'unit_credit', 0) or 0))
            item = by_sale_id.get(sale.id)
            if item:
                item.running_period_units = running_units
                item.current_unit_tier = unit_bonus['current_tier']
                item.next_unit_tier = unit_bonus['next_tier']
                item.units_needed_for_next_tier = unit_bonus['units_needed']
        return {
            'results': results,
            'calculated_count': len(complete),
            'partial_count': len(partial),
            'excluded_count': len(excluded),
            'excluded': excluded,
            'total_front': total_front,
            'total_back': total_back,
            'total_bonus': total_bonus,
            'total_deal_bonus': deal_bonus,
            'period_unit_bonus': period_unit_bonus,
            'unit_bonus': unit_bonus,
            'total_deal_commission': total_deal_commission,
            'total_commission': total_commission,
            'total_units': sum((Decimal(str(getattr(sale, 'unit_credit', 0) or 0)) for sale in sales), Decimal('0.0')),
            'draw_progress': draw_progress,
        }


class _UnitBonusCalculator:
    """Period-level unit bonus adapter; never allocates the full bonus to a sale."""

    @staticmethod
    def calculate(user: Any, sales: list[Any]) -> dict[str, Any]:
        sales = list(sales)
        units = sum(
            (Decimal(str(getattr(s, 'unit_credit', 0) or 0)) for s in sales),
            Decimal('0.0'),
        )
        empty = {
            'amount': Decimal('0.00'),
            'units': units,
            'new_units': Decimal('0.0'),
            'used_units': Decimal('0.0'),
            'current_tier': None,
            'next_tier': None,
            'units_needed': None,
            'qualification_pending': False,
            'explanation': [],
        }
        if not sales:
            return empty
        try:
            period = calculate_period_commission(
                user, sales,
                min(s.date for s in sales),
                max(s.date for s in sales),
            )
        except CommissionEngineError:
            return empty
        bonus_items = [
            item for item in period.line_items
            if item.scope == 'period'
            and item.category == 'bonus'
            and item.applied
            and item.rule_type in {
                'volume_bonus',
                'per_unit_bonus',
                'period_qualification_bonus',
                'survey_count_bonus',
            }
        ]
        version = period.pay_plan_version
        metrics = build_period_context(sales)
        tiers: list[dict[str, Any]] = []
        period_date = min(s.date for s in sales)
        period_context = {
            **metrics,
            **build_eligibility_context(user, period_date),
        }
        active_volume_rules = version.rules.filter(
            is_active=True,
            rule_type='volume_bonus',
            calculation_scope='period',
        ).prefetch_related('conditions').order_by('sort_order', 'id')
        tier_units = units
        qualification_pending = False
        for rule in active_volume_rules:
            conditions = [
                {
                    'field_name': condition.field_name,
                    'operator': condition.operator,
                    'value': condition.value,
                }
                for condition in rule.conditions.all()
            ]
            if conditions and not evaluate_conditions(
                conditions, period_context, rule.condition_group_operator,
            ):
                qualification_pending = True
                continue
            tiers.extend(rule.configuration.get('tiers') or [])
            metric_name = rule.configuration.get('unit_metric', 'monthly_units')
            tier_units = Decimal(str(metrics.get(metric_name, units)))
        ordered = sorted(
            tiers, key=lambda tier: Decimal(str(tier.get('minimum_units', 0)))
        )
        current = None
        next_tier = None
        for tier in ordered:
            threshold = Decimal(str(tier.get('minimum_units', 0)))
            maximum = tier.get('maximum_units')
            if tier_units >= threshold and (
                maximum in (None, '') or tier_units <= Decimal(str(maximum))
            ):
                current = tier
            elif threshold > tier_units and next_tier is None:
                next_tier = tier
        return {
            'amount': sum((item.amount for item in bonus_items), Decimal('0.00')),
            'units': units,
            'bonus_units': tier_units,
            'new_units': metrics.get('monthly_new_units', Decimal('0.0')),
            'used_units': metrics.get('monthly_used_units', Decimal('0.0')),
            'current_tier': current,
            'next_tier': next_tier,
            'units_needed': (
                max(
                    Decimal('0'),
                    Decimal(str(next_tier['minimum_units'])) - tier_units,
                )
                if next_tier else None
            ),
            'qualification_pending': qualification_pending and not tiers,
            'explanation': [item.explanation for item in bonus_items],
        }

class CommissionEngineService(CommissionEngineService):
    @classmethod
    def preview_sales(
        cls, user: Any, sales: list[Any], version: Any,
    ) -> dict[str, Any]:
        sales = list(sales)
        monthly_metrics = build_period_context(sales) if sales else {}
        results = [
            cls.calculate_sale(user, sale, monthly_metrics, version=version)
            for sale in sales
        ]
        calculated = [item for item in results if item.calculated]
        complete = [item for item in results if item.status == STATUS_CALCULATED]
        partial = [item for item in results if item.status == STATUS_PARTIAL]
        excluded = [item for item in results if not item.calculated]
        return {
            'results': results,
            'sales_tested': len(results),
            'calculated_count': len(calculated),
            'complete_count': len(complete),
            'partial_count': len(partial),
            'excluded_count': len(excluded),
            'valid_zero_count': sum(
                1 for item in calculated if item.total_commission == Decimal('0.00')
            ),
            'missing_information_count': sum(
                1 for item in excluded if item.status == STATUS_MISSING_SALE_DATA
            ),
            'no_matching_rule_count': sum(
                1 for item in excluded if item.status == STATUS_NO_MATCHING_RULE
            ),
            'estimated_total': sum(
                (item.total_commission for item in calculated), Decimal('0.00')
            ),
        }

    @staticmethod
    def active_plan_summary(user: Any) -> dict[str, Any]:
        from .models import Commission, PayPlanAssignment, PayPlanOnboarding, PayPlanRule, PayPlanRuleCondition, UserProfile

        engine = get_commission_system(user)
        legacy_settings = Commission.objects.filter(user=user).first()
        summary = {
            'engine': engine,
            'legacy_settings_exists': legacy_settings is not None,
            'legacy_opt_out_front': bool(legacy_settings.opt_out_front) if legacy_settings else False,
            'legacy_opt_out_back': bool(legacy_settings.opt_out_back) if legacy_settings else False,
            'legacy_ignored': engine == UserProfile.PAY_PLAN_V2 and legacy_settings is not None,
            'warnings': [],
        }
        if engine == UserProfile.LEGACY:
            summary.update({
                'plan': None,
                'active_rule_count': 0,
                'inactive_rule_count': 0,
                'front_end_rule_count': 0,
                'back_end_rule_count': 0,
                'unit_bonus_rule_count': 0,
                'model_specific_rule_count': 0,
                'new_used_rule_count': 0,
                'imported_filename': '',
                'imported_at': None,
                'effective_start_date': None,
                'effective_end_date': None,
            })
            if legacy_settings is None:
                summary['warnings'].append('No legacy commission settings found.')
            return summary

        today = timezone.localdate()
        active_result = ActivePayPlanService.get_for_user(user, today)
        if active_result.status != 'active':
            summary['warnings'].append(
                active_result.error or 'No active plan assignment covers today.'
            )
            summary.update({
                'plan': None,
                'active_rule_count': 0,
                'inactive_rule_count': 0,
                'front_end_rule_count': 0,
                'back_end_rule_count': 0,
                'unit_bonus_rule_count': 0,
                'model_specific_rule_count': 0,
                'new_used_rule_count': 0,
                'imported_filename': '',
                'imported_at': None,
                'effective_start_date': None,
                'effective_end_date': None,
            })
            return summary

        assignment = active_result.assignment
        version = active_result.version
        rules = PayPlanRule.objects.filter(pay_plan_version=version)
        active_rules = rules.filter(is_active=True)
        inactive_rules = rules.filter(is_active=False)
        model_specific_rule_ids = set(
            PayPlanRuleCondition.objects.filter(
                rule__pay_plan_version=version,
                field_name__in=['make', 'model', 'year'],
            ).values_list('rule_id', flat=True)
        )
        new_used_rule_ids = set(
            PayPlanRuleCondition.objects.filter(
                rule__pay_plan_version=version,
                field_name__in=['vehicle_condition'],
            ).values_list('rule_id', flat=True)
        )
        onboarding = PayPlanOnboarding.objects.filter(user=user).first()
        latest_doc = version.documents.filter(user=user).order_by('-uploaded_at', '-id').first()
        if latest_doc is None and onboarding:
            latest_doc = onboarding.documents.filter(user=user).order_by('-uploaded_at', '-id').first()
        summary.update({
            'plan': version.pay_plan,
            'plan_version_name': version.version_name,
            'plan_status': version.status,
            'effective_start_date': assignment.effective_start_date,
            'effective_end_date': assignment.effective_end_date,
            'active_rule_count': active_rules.count(),
            'inactive_rule_count': inactive_rules.count(),
            'front_end_rule_count': active_rules.filter(rule_type__icontains='front').count(),
            'back_end_rule_count': active_rules.filter(rule_type__icontains='back').count(),
            'unit_bonus_rule_count': active_rules.filter(rule_type__in=['volume_bonus', 'per_unit_bonus', 'period_qualification_bonus']).count(),
            'model_specific_rule_count': len(model_specific_rule_ids),
            'new_used_rule_count': len(new_used_rule_ids),
            'imported_filename': latest_doc.original_filename if latest_doc else '',
            'imported_at': onboarding.submitted_at if onboarding else None,
            'pay_plan_version_id': version.id,
            'source_available': bool(latest_doc and latest_doc.is_available),
            'last_processed_at': latest_doc.last_processed_at if latest_doc else None,
            'processing_status': (
                version.processing_status
                or (latest_doc.status if latest_doc else '')
            ),
            'processing_warnings': (
                version.processing_warnings
                or (latest_doc.processing_warnings if latest_doc else [])
            ),
            'processing_errors': version.processing_errors,
            'default_backend_percentage': version.default_backend_percentage,
            'default_backend_minimum': version.default_backend_minimum,
            'default_backend_maximum': version.default_backend_maximum,
            'requirements': PlanRequirementService.get_for_user(
                user, active_result,
            ),
        })
        if summary['requirements'].get('holiday'):
            summary['holiday_fund'] = HolidayBonusFundService.calculate(user)
        if active_rules.count() == 0 and version.default_backend_percentage is None:
            summary['warnings'].append('Active plan has no active rules.')
        if rules.count() == 0 and version.default_backend_percentage is None:
            summary['warnings'].append('Active plan has no rules.')
        if summary['legacy_ignored']:
            summary['warnings'].append('Legacy settings exist but are ignored for this user.')
        return summary


UnitBonusService = _UnitBonusCalculator


class HolidayBonusFundService:
    """Track annual Holiday Fund accrual separately from monthly commission."""

    @staticmethod
    def calculate(user: Any, as_of_date=None) -> dict[str, Any]:
        from datetime import date
        from .models import ArchivedSale, PayPlanEligibility, Sale

        as_of_date = as_of_date or timezone.localdate()
        if (as_of_date.month, as_of_date.day) >= (12, 1):
            period_start = date(as_of_date.year, 12, 1)
            period_end = date(as_of_date.year + 1, 11, 30)
        else:
            period_start = date(as_of_date.year - 1, 12, 1)
            period_end = date(as_of_date.year, 11, 30)

        records = {}
        for model in (ArchivedSale, Sale):
            for sale in model.objects.filter(
                user=user, date__gte=period_start, date__lte=min(period_end, as_of_date),
            ):
                records[sale.dealNumber] = Decimal(str(sale.count or 0))
        units = sum(records.values(), Decimal('0'))
        rate = Decimal('15.00') if units >= Decimal('200') else Decimal('10.00')
        eligibility = PayPlanEligibility.objects.filter(
            user=user,
            month_start__gte=period_start.replace(day=1),
            month_start__lte=as_of_date.replace(day=1),
        ).order_by('-month_start', '-id').first()
        eligible = eligibility.holiday_bonus_eligible if eligibility else None
        forfeited = eligibility.holiday_bonus_forfeited if eligibility else False
        projected = units * rate
        accrued = projected if eligible is True and not forfeited else Decimal('0.00')
        return {
            'period_start': period_start,
            'period_end': period_end,
            'units': units,
            'rate_per_vehicle': rate,
            'projected_amount': projected,
            'accrued_amount': accrued,
            'eligible': eligible,
            'forfeited': forfeited,
            'payable_in_december': as_of_date >= period_end,
        }


class DrawProgressService:
    """Explain draw coverage without treating the draw as earned commission."""

    @staticmethod
    def calculate(
        user: Any, sales: list[Any], frontend_commission: Decimal,
        backend_commission: Decimal, unit_bonus: Decimal,
        other_bonus: Decimal,
    ) -> dict[str, Any] | None:
        sales = list(sales)
        if not sales:
            return None
        try:
            version = resolve_pay_plan_version(user, min(s.date for s in sales))
        except CommissionEngineError:
            return None
        rule = version.rules.filter(
            is_active=True, rule_type='draw', calculation_scope='period',
        ).order_by('sort_order', 'id').first()
        if rule is None:
            return None
        config = rule.configuration or {}
        eligible = set(config.get('eligible_categories') or [])
        components = {
            'front_end': frontend_commission,
            'back_end': backend_commission,
            'unit_bonus': unit_bonus,
            'other_bonus': other_bonus,
        }
        eligible_earnings = sum(
            (amount for category, amount in components.items() if category in eligible),
            Decimal('0.00'),
        )
        draw_amount = Decimal(str(config.get('amount') or 0))
        prior_balance = Decimal(str(config.get('prior_carried_balance') or 0))
        recoverable = config.get('recoverable')
        needed = max(Decimal('0.00'), draw_amount - eligible_earnings)
        above = max(Decimal('0.00'), eligible_earnings - draw_amount)
        projected = (
            prior_balance + needed - above if recoverable is True else prior_balance
        )
        return {
            'rule_name': rule.name,
            'draw_type': config.get('draw_type', 'review_required'),
            'amount': draw_amount,
            'frequency': config.get('frequency', 'monthly'),
            'recoverable': recoverable,
            'frontend_commission': frontend_commission,
            'backend_commission': backend_commission,
            'unit_bonus': unit_bonus,
            'other_bonus': other_bonus,
            'eligible_earnings': eligible_earnings,
            'amount_needed': needed,
            'amount_above': above,
            'prior_balance': prior_balance,
            'projected_balance': projected,
            'period_start': min(s.date for s in sales),
            'period_end': max(s.date for s in sales),
            'review_status': config.get('review_status', 'review_required'),
        }


class CommissionHelpContext:
    @classmethod
    def build(cls, user: Any, sale: Any | None = None) -> dict[str, Any]:
        today = timezone.localdate()
        month_start = today.replace(day=1)
        month_sales = list(
            user.sale_set.filter(date__gte=month_start).order_by('date', 'dealNumber')
        )
        sales_diagnostics = CommissionEngineService.calculate_sales(user, month_sales)
        sale_detail = (
            CommissionEngineService.calculate_sale(user, sale)
            if sale is not None else None
        )
        return {
            'active_plan': CommissionEngineService.active_plan_summary(user),
            'sales_diagnostics': sales_diagnostics,
            'sale_detail': sale_detail,
            'suggested_actions': [
                'Why is this sale showing $0.00?',
                'Show my active commission plan',
                'Which sales need attention?',
                'Which information is missing?',
                'Are legacy settings affecting my account?',
                'What rules were imported from my pay plan?',
            ],
        }
