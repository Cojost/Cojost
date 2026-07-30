from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from .commission_engine.engine import build_period_context
from .commission_engine.evaluators import SurveyCountBonusEvaluator, VolumeBonusEvaluator
from .commission_engine.conditions import evaluate_conditions
from .commission_engine.validators import validate_conditions, validate_configuration
from .pay_plan_imports import parse_description_to_import_draft


SIMPLIFIED_TEXT = """
Simplified bonus rules
Green Pea Program
7-8.5 $500 9-12.5 $1,000 13-16.5 $1,500 17-20.5 $2,000 21+ $2,500
All Other Pay Plans
10-11.5 $500 12-15.5 $750 16-19.5 $2,000 20-24.5 $2,500
25-29.5 $3,000 30+ $4,000
Fast Start Bonuses
Unique Co-Videos
Used vehicle qualifier
Let It Ride
"""


class SubaruSimplifiedImportTests(SimpleTestCase):
    def test_specialized_import_preserves_bonus_ladders_and_requirements(self):
        draft = parse_description_to_import_draft(SIMPLIFIED_TEXT, 'Subaru 2026')
        rules = {rule['name']: rule for rule in draft['rules']}

        self.assertEqual(draft['parser_profile'], 'subaru_simplified_bonus_v1')
        self.assertEqual(
            rules['Green Pea Volume Bonus']['configuration']['tiers'][-1]['amount'],
            '2500.00',
        )
        self.assertEqual(
            rules['Standard Volume Bonus']['configuration']['tiers'][-1]['amount'],
            '4000.00',
        )
        green_fields = {
            condition['field_name']
            for condition in rules['Green Pea Volume Bonus']['conditions']
        }
        self.assertEqual(
            green_fields,
            {
                'green_pea',
                'training_requirements_met',
                'call_requirement_met',
                'video_requirement_met',
            },
        )
        self.assertIn('Holiday Bonus Fund', draft['unrecognized_sections'])
        self.assertNotIn('NPS survey-count bonus', draft['unrecognized_sections'])
        self.assertIn('NPS Survey Count Bonus', rules)
        self.assertIn('Used Vehicle Acquisition Bonus', rules)

        for rule in draft['rules']:
            validate_configuration(rule['rule_type'], rule['configuration'])
            validate_conditions(rule['conditions'])

    def test_fast_start_metrics_double_early_units_and_count_day_ten_units(self):
        sales = [
            SimpleNamespace(
                date=date(2026, 7, 2), unit_credit=Decimal('1'),
                frontEnd=0, backend=0, vehicle_condition='new',
            ),
            SimpleNamespace(
                date=date(2026, 7, 8), unit_credit=Decimal('1'),
                frontEnd=0, backend=0, vehicle_condition='used',
            ),
            SimpleNamespace(
                date=date(2026, 7, 11), unit_credit=Decimal('1'),
                frontEnd=0, backend=0, vehicle_condition='used',
            ),
        ]

        metrics = build_period_context(sales)

        self.assertEqual(metrics['monthly_units'], Decimal('3'))
        self.assertEqual(metrics['fast_start_volume_units'], Decimal('5'))
        self.assertEqual(metrics['units_by_day_10'], Decimal('2'))
        self.assertEqual(metrics['monthly_used_units'], Decimal('2'))

    def test_nps_grid_uses_monthly_counts_and_low_score_deduction(self):
        rule = SimpleNamespace(
            id=1, name='NPS Survey Count Bonus',
            rule_type='survey_count_bonus', calculation_scope='period',
        )
        evaluator = SurveyCountBonusEvaluator(
            rule=rule,
            configuration={
                'qualifying_count_field': 'nps_qualifying_surveys',
                'low_score_count_field': 'nps_low_score_surveys',
                'grid': [
                    {'count': 1, 'rate_per_survey': '175', 'total': '175'},
                    {'count': 4, 'rate_per_survey': '200', 'total': '800'},
                ],
            },
            conditions=[],
            condition_group_operator='all',
        )

        result = evaluator.evaluate({
            'nps_qualifying_surveys': 4,
            'nps_low_score_surveys': 1,
        })

        self.assertEqual(result.amount, Decimal('600.00'))

    def test_acquisition_source_and_retired_sslp_are_engine_facts(self):
        acquisition_conditions = [{
            'field_name': 'acquisition_source',
            'operator': 'in',
            'value': ['street_curb', 'current_service_customer'],
        }]
        self.assertTrue(evaluate_conditions(
            acquisition_conditions,
            {'acquisition_source': 'street_curb'},
        ))
        sslp = SimpleNamespace(
            date=date(2026, 7, 15), unit_credit=Decimal('1'),
            frontEnd=0, backend=0, vehicle_condition='retired_sslp',
        )
        self.assertEqual(
            build_period_context([sslp])['monthly_used_units'],
            Decimal('1'),
        )

    def test_volume_bonus_effective_date_excludes_earlier_month_sales(self):
        rule = SimpleNamespace(
            id=2, name='Volume Bonus', rule_type='volume_bonus',
            calculation_scope='period',
        )
        evaluator = VolumeBonusEvaluator(
            rule=rule,
            configuration={
                'tiers': [{'minimum_units': '2', 'amount': '500'}],
                'tier_mode': 'highest_only',
                'effective_start_date': '2026-07-26',
            },
            conditions=[],
            condition_group_operator='all',
        )
        sales = [
            SimpleNamespace(date=date(2026, 7, 10), unit_credit=Decimal('2')),
            SimpleNamespace(date=date(2026, 7, 27), unit_credit=Decimal('1')),
        ]

        result = evaluator.evaluate({
            '_sales': sales,
            'period_end': date(2026, 7, 31),
            'monthly_units': Decimal('3'),
        })

        self.assertFalse(result.applied)
        self.assertEqual(result.amount, Decimal('0.00'))
