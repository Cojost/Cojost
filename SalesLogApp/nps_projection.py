from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from .commission_engine.evaluators import SurveyCountBonusEvaluator
from .commission_engine.exceptions import CommissionEngineError
from .plan_requirements import ActivePayPlanService


class NPSSurveyProjectionService:
    """Estimate survey compensation without changing payroll engine inputs."""

    PASSING_FIELDS = {'nps_bonus_eligible', 'nps_finance_eligible'}

    @classmethod
    def rules_for_user(cls, user, month_start):
        plan_result = ActivePayPlanService.get_for_user(user, month_start)
        if plan_result.status != 'active':
            return []
        candidates = plan_result.version.rules.filter(
            is_active=True,
            rule_type='survey_count_bonus',
        ).prefetch_related('conditions').order_by('sort_order', 'id')
        return [rule for rule in candidates if cls._is_nps_rule(rule)]

    @staticmethod
    def _is_nps_rule(rule):
        configuration = rule.configuration or {}
        fields = {
            configuration.get('qualifying_count_field'),
            configuration.get('low_score_count_field'),
        }
        fields.update(condition.field_name for condition in rule.conditions.all())
        return any(str(field or '').startswith('nps_') for field in fields)

    @staticmethod
    def _month_end(month_start):
        if month_start.month == 12:
            next_month = month_start.replace(
                year=month_start.year + 1, month=1, day=1,
            )
        else:
            next_month = month_start.replace(month=month_start.month + 1, day=1)
        return next_month - timedelta(days=1)

    @staticmethod
    def _tier_rate(configuration, good_surveys):
        if good_surveys <= 0:
            return None
        grid = sorted(
            configuration.get('grid') or [],
            key=lambda row: int(row['count']),
        )
        if not grid:
            return None
        capped_count = min(good_surveys, int(grid[-1]['count']))
        row = next(
            (item for item in grid if int(item['count']) == capped_count),
            grid[0],
        )
        return Decimal(str(row['rate_per_survey']))

    @classmethod
    def calculate(
        cls, rules, month_start, passing=None, good_surveys=0, bad_surveys=0,
    ):
        good_surveys = int(good_surveys or 0)
        bad_surveys = int(bad_surveys or 0)
        payout = Decimal('0.00')
        rates = []
        passing_required = False
        calculation_warning = False

        for rule in rules:
            configuration = rule.configuration or {}
            conditions = [condition.as_dict() for condition in rule.conditions.all()]
            passing_required = passing_required or any(
                condition['field_name'] in cls.PASSING_FIELDS
                and condition['operator'] == 'is_true'
                for condition in conditions
            )
            context = {
                configuration.get(
                    'qualifying_count_field', 'nps_qualifying_surveys'
                ): good_surveys,
                configuration.get(
                    'low_score_count_field', 'nps_low_score_surveys'
                ): bad_surveys,
                'nps_qualifying_surveys': good_surveys,
                'nps_low_score_surveys': bad_surveys,
                'nps_bonus_eligible': passing,
                'nps_finance_eligible': passing,
                'period_start': month_start,
                'period_end': cls._month_end(month_start),
            }
            evaluator = SurveyCountBonusEvaluator(
                rule=rule,
                configuration=configuration,
                conditions=conditions,
                condition_group_operator=rule.condition_group_operator,
            )
            try:
                item = evaluator.evaluate(context)
            except CommissionEngineError:
                calculation_warning = True
                continue
            if item.applied:
                payout += item.amount
            rate = cls._tier_rate(configuration, good_surveys)
            if rate is not None and rate not in rates:
                rates.append(rate)

        if not rates:
            tier_label = 'No tier yet'
        elif len(rates) == 1:
            tier_label = f'${rates[0]:,.2f} per good survey'
        else:
            tier_label = ' + '.join(
                f'${rate:,.2f} per good survey' for rate in rates
            )

        return {
            'passing': passing,
            'good_surveys': good_surveys,
            'bad_surveys': bad_surveys,
            'net_survey_impact': good_surveys - bad_surveys,
            'payout': payout,
            'tier_label': tier_label,
            'passing_required': passing_required,
            'calculation_warning': calculation_warning,
        }
