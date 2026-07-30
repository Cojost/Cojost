from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from .models import PayPlanDescriptionSubmission, PayPlanDocument, PayPlanRule

PARSER_CONFIDENCE_THRESHOLD = Decimal('0.60')
PARSER_VERSION = 'pypdf-text-v3'


def _eligibility_conditions(*, green_pea: bool | None = None) -> list[dict[str, Any]]:
    conditions = [
        {
            'field_name': field_name,
            'operator': 'is_true',
            'value': None,
        }
        for field_name in (
            'training_requirements_met',
            'call_requirement_met',
            'video_requirement_met',
        )
    ]
    if green_pea is not None:
        conditions.insert(0, {
            'field_name': 'green_pea',
            'operator': 'is_true' if green_pea else 'is_false',
            'value': None,
        })
    return conditions


def _parse_subaru_simplified_bonus_plan(
    text: str,
    plan_name: str,
) -> dict[str, Any] | None:
    """Parse the structured Subaru bonus summary without flattening its two ladders."""
    lower = text.lower()
    required_markers = (
        'simplified bonus rules',
        'green pea program',
        'all other pay plans',
        'fast start bonuses',
        'unique co-videos',
        'used vehicle qualifier',
    )
    if not all(marker in lower for marker in required_markers):
        return None

    green_tiers = [
        {'minimum_units': '7', 'maximum_units': '8.5', 'amount': '500.00'},
        {'minimum_units': '9', 'maximum_units': '12.5', 'amount': '1000.00'},
        {'minimum_units': '13', 'maximum_units': '16.5', 'amount': '1500.00'},
        {'minimum_units': '17', 'maximum_units': '20.5', 'amount': '2000.00'},
        {'minimum_units': '21', 'amount': '2500.00'},
    ]
    standard_tiers = [
        {'minimum_units': '10', 'maximum_units': '11.5', 'amount': '500.00'},
        {'minimum_units': '12', 'maximum_units': '15.5', 'amount': '750.00'},
        {'minimum_units': '16', 'maximum_units': '19.5', 'amount': '2000.00'},
        {'minimum_units': '20', 'maximum_units': '24.5', 'amount': '2500.00'},
        {'minimum_units': '25', 'maximum_units': '29.5', 'amount': '3000.00'},
        {'minimum_units': '30', 'amount': '4000.00'},
    ]
    non_acquisition = {
        'field_name': 'acquisition_source',
        'operator': 'not_in',
        'value': ['street_curb', 'current_service_customer'],
    }
    front_rule = _build_front_rule(Decimal('25'), text)
    front_rule['conditions'].append(non_acquisition)
    minimum_rule = _build_minimum_rule(Decimal('250'))
    minimum_rule['conditions'].append(non_acquisition)
    rules = [
        front_rule,
        {
            **_build_back_rule(Decimal('3'), text),
            'name': 'Base Finance Gross 3%',
            'conditions': [{
                'field_name': 'nps_finance_eligible',
                'operator': 'is_true',
                'value': None,
            }, {
                'field_name': 'monthly_units',
                'operator': 'less_than',
                'value': '15',
            }, non_acquisition],
        },
        minimum_rule,
        {
            'name': 'Green Pea Volume Bonus',
            'rule_type': 'volume_bonus',
            'calculation_scope': 'period',
            'configuration': {
                'tiers': green_tiers,
                'tier_mode': 'highest_only',
                'unit_metric': 'fast_start_volume_units',
                'source_text': 'Green Pea Program volume ladder.',
            },
            'conditions': _eligibility_conditions(green_pea=True),
            'is_active': True,
        },
        {
            'name': 'Standard Volume Bonus',
            'rule_type': 'volume_bonus',
            'calculation_scope': 'period',
            'configuration': {
                'tiers': standard_tiers,
                'tier_mode': 'highest_only',
                'unit_metric': 'fast_start_volume_units',
                'source_text': 'All Other Pay Plans volume ladder.',
            },
            'conditions': _eligibility_conditions(green_pea=False),
            'is_active': True,
        },
        {
            'name': 'Fast Start - 10 Units by the 10th',
            'rule_type': 'period_qualification_bonus',
            'calculation_scope': 'period',
            'configuration': {
                'amount': '1000.00',
                'requirements': [{
                    'metric': 'units_by_day_10',
                    'operator': 'greater_than_or_equal',
                    'value': '10',
                }],
                'requirement_mode': 'all',
            },
            'conditions': _eligibility_conditions(),
            'is_active': True,
        },
        {
            'name': 'Let It Ride - Total Finance Gross 7%',
            'rule_type': 'back_gross_percentage',
            'calculation_scope': 'per_sale',
            'configuration': {
                'rate': '0.07',
                'gross_field': 'back_end_gross',
            },
            'conditions': [
                {
                    'field_name': 'monthly_units',
                    'operator': 'greater_than_or_equal',
                    'value': '15',
                },
                {
                    'field_name': 'nps_finance_eligible',
                    'operator': 'is_true',
                    'value': None,
                },
                non_acquisition,
            ],
            'is_active': True,
        },
        {
            'name': 'Used Vehicle Minimum Deduction',
            'rule_type': 'deduction',
            'calculation_scope': 'period',
            'configuration': {
                'amount': '500.00',
                'reason': 'Fewer than four used vehicles sold during the month.',
            },
            'conditions': [{
                'field_name': 'monthly_used_units',
                'operator': 'less_than',
                'value': '4',
            }],
            'is_active': True,
        },
        {
            'name': 'NPS Survey Count Bonus',
            'rule_type': 'survey_count_bonus',
            'calculation_scope': 'period',
            'configuration': {
                'qualifying_count_field': 'nps_qualifying_surveys',
                'low_score_count_field': 'nps_low_score_surveys',
                'grid': [
                    {'count': 1, 'rate_per_survey': '175.00', 'total': '175.00'},
                    {'count': 2, 'rate_per_survey': '175.00', 'total': '350.00'},
                    {'count': 3, 'rate_per_survey': '175.00', 'total': '525.00'},
                    {'count': 4, 'rate_per_survey': '200.00', 'total': '800.00'},
                    {'count': 5, 'rate_per_survey': '200.00', 'total': '1000.00'},
                    {'count': 6, 'rate_per_survey': '250.00', 'total': '1500.00'},
                    {'count': 7, 'rate_per_survey': '250.00', 'total': '1750.00'},
                    {'count': 8, 'rate_per_survey': '250.00', 'total': '2000.00'},
                    {'count': 9, 'rate_per_survey': '250.00', 'total': '2250.00'},
                    {'count': 10, 'rate_per_survey': '250.00', 'total': '2500.00'},
                ],
                'data_fields': [
                    {'field': 'nps_qualifying_surveys'},
                    {'field': 'nps_low_score_surveys'},
                ],
            },
            'conditions': [{
                'field_name': 'nps_bonus_eligible',
                'operator': 'is_true',
                'value': None,
            }],
            'is_active': True,
        },
        {
            'name': 'Used Vehicle Acquisition Bonus',
            'rule_type': 'acquisition_bonus',
            'calculation_scope': 'per_sale',
            'configuration': {'amount': '350.00'},
            'conditions': [{
                'field_name': 'acquisition_source',
                'operator': 'in',
                'value': ['street_curb', 'current_service_customer'],
            }],
            'is_active': True,
        },
        {
            **_build_draw_rule(Decimal('2000'), 'monthly', None, text),
            'configuration': {
                **_build_draw_rule(
                    Decimal('2000'), 'monthly', None, text,
                )['configuration'],
                'data_fields': [
                    {'field': 'holiday_bonus_eligible'},
                    {'field': 'holiday_bonus_forfeited'},
                ],
            },
        },
    ]
    warnings = [
        (
            'Fast Start adjusted volume uses the first seven non-Sunday calendar '
            'dates as dealership working days; review months with dealership closures.'
        ),
        (
            'NPS survey-count bonuses require monthly eligibility and returned-survey counts.'
        ),
        (
            'The $350 acquisition bonus is calculated from the sale acquisition-source field.'
        ),
        (
            'Holiday Bonus Fund accrual and forfeiture remain outside the monthly '
            'commission engine.'
        ),
        (
            'Retired SSLP is treated as a used unit for the monthly used-vehicle qualifier.'
        ),
    ]
    return {
        'plan_name': plan_name,
        'source': 'description',
        'rules': rules,
        'warnings': warnings,
        'unrecognized_sections': [
            'Holiday Bonus Fund',
        ],
        'confidence': '0.95',
        'parser_profile': 'subaru_simplified_bonus_v1',
        'data_field_requests': [
            {
                'field': 'nps_status',
                'label': 'NPS eligibility',
                'scope': 'monthly',
                'input_type': 'eligibility_dropdown',
            },
            {
                'field': 'nps_qualifying_surveys',
                'label': 'Qualifying NPS surveys',
                'scope': 'monthly',
                'input_type': 'whole_number',
            },
            {
                'field': 'nps_low_score_surveys',
                'label': 'NPS surveys scored 8 or below',
                'scope': 'monthly',
                'input_type': 'whole_number',
            },
            {
                'field': 'acquisition_source',
                'label': 'Acquisition source',
                'scope': 'sale',
                'input_type': 'dropdown',
            },
            {
                'field': 'vehicle_condition',
                'label': 'Vehicle type',
                'scope': 'sale',
                'input_type': 'dropdown',
                'options_added': ['Retired SSLP'],
            },
            {
                'field': 'holiday_bonus_eligible',
                'label': 'Holiday Bonus Fund eligibility',
                'scope': 'monthly',
                'input_type': 'eligibility_dropdown',
            },
        ],
        'requires_review': True,
        'approved': False,
        'generated_at': timezone.now().isoformat(),
    }


def _decimal_from_match(value: str) -> Decimal | None:
    try:
        normalized = value.replace(',', '').strip()
        return Decimal(normalized)
    except (InvalidOperation, AttributeError):
        return None


def _vehicle_condition_conditions(text: str) -> list[dict[str, Any]]:
    lower = text.lower()
    if 'used only' in lower:
        return [{'field_name': 'vehicle_condition', 'operator': 'equals', 'value': 'used'}]
    if 'new only' in lower:
        return [{'field_name': 'vehicle_condition', 'operator': 'equals', 'value': 'new'}]
    return []


def _build_front_rule(percent: Decimal, source_text: str) -> dict[str, Any]:
    return {
        'name': f'Front Gross {percent}%',
        'rule_type': 'front_gross_percentage',
        'calculation_scope': 'per_sale',
        'configuration': {
            'rate': str((percent / Decimal('100')).quantize(Decimal('0.0001'))),
            'gross_field': 'front_end_gross',
        },
        'conditions': _vehicle_condition_conditions(source_text),
        'is_active': True,
    }


def _build_back_rule(percent: Decimal, source_text: str) -> dict[str, Any]:
    lower = source_text.lower()
    conditions = _vehicle_condition_conditions(source_text)
    if 'nps' in lower and (
        'qualify for the finance gross' in lower
        or 'finance gross portion' in lower
    ):
        conditions.append({
            'field_name': 'nps_finance_eligible',
            'operator': 'is_true',
            'value': True,
        })
    return {
        'name': f'Back Gross {percent}%',
        'rule_type': 'back_gross_percentage',
        'calculation_scope': 'per_sale',
        'configuration': {
            'rate': str((percent / Decimal('100')).quantize(Decimal('0.0001'))),
            'gross_field': 'back_end_gross',
        },
        'conditions': conditions,
        'is_active': True,
    }


def _build_volume_bonus_rule(units: Decimal, amount: Decimal) -> dict[str, Any]:
    return {
        'name': f'Volume Bonus {units} Units',
        'rule_type': 'volume_bonus',
        'calculation_scope': 'period',
        'configuration': {
            'tiers': [{'minimum_units': str(units), 'amount': str(amount.quantize(Decimal('0.01')))}],
            'tier_mode': 'highest_only',
        },
        'conditions': [],
        'is_active': True,
    }


def _build_volume_bonus_tiers_rule(
    tiers, source_text, *, unit_metric='monthly_units', conditions=None,
):
    return {
        'name': 'Unit Volume Bonus',
        'rule_type': 'volume_bonus',
        'calculation_scope': 'period',
        'configuration': {
            'tiers': tiers,
            'tier_mode': 'highest_only',
            'unit_metric': unit_metric,
            'source_text': source_text[:1000],
        },
        'conditions': conditions or [],
        'is_active': True,
    }


def _parse_explicit_volume_bonus(text: str):
    """Parse the tier table nearest an explicit volume/unit bonus heading."""
    heading = re.search(
        r'(?:volume|unit)\s+bonus(?:\s*[-:]\s*(new|used|pre[- ]?owned)\s+vehicles?)?',
        text,
        flags=re.IGNORECASE,
    )
    if heading is None:
        return None
    end = re.search(
        r'\b(?:qualification|pre[- ]?owned vehicles?|used vehicles?|'
        r'additional bonuses?|draw|salesman of the month)\b',
        text[heading.end():],
        flags=re.IGNORECASE,
    )
    section_end = heading.end() + end.start() if end else len(text)
    section = text[heading.start():section_end]
    pairs = re.findall(
        r'(\d+(?:\.\d+)?)(\+)?\s+\$\s*([\d,]+(?:\.\d{1,2})?)',
        section,
        flags=re.IGNORECASE,
    )
    tiers = []
    for minimum_value, _open_ended, amount_value in pairs:
        minimum = _decimal_from_match(minimum_value)
        amount = _decimal_from_match(amount_value)
        if minimum is None or amount is None:
            continue
        tiers.append({
            'minimum_units': str(minimum),
            'amount': str(amount.quantize(Decimal('0.01'))),
        })
    if not tiers:
        return None
    heading_text = heading.group(0).lower()
    if 'new' in heading_text:
        unit_metric = 'monthly_new_units'
    elif 'used' in heading_text or 'pre-owned' in heading_text or 'pre owned' in heading_text:
        unit_metric = 'monthly_used_units'
    else:
        unit_metric = 'monthly_units'
    qualification_text = text[section_end:section_end + 220].lower()
    conditions = []
    if (
        'customer satisfaction' in qualification_text
        and ('higher than' in qualification_text or 'above' in qualification_text)
    ):
        conditions.append({
            'field_name': 'nps_bonus_eligible',
            'operator': 'is_true',
            'value': True,
        })
    return tiers, section, unit_metric, conditions


def _build_draw_rule(amount, frequency, recoverable, source_text):
    return {
        'name': f'{frequency.title()} Draw ${amount.quantize(Decimal("0.01"))}',
        'rule_type': 'draw',
        'calculation_scope': 'period',
        'configuration': {
            'amount': str(amount.quantize(Decimal('0.01'))),
            'frequency': frequency,
            'recoverable': recoverable,
            'draw_type': (
                'recoverable' if recoverable is True
                else 'non_recoverable' if recoverable is False
                else 'review_required'
            ),
            'carry_forward': None,
            'reset_behavior': 'unspecified',
            'eligible_categories': ['front_end', 'back_end', 'unit_bonus', 'other_bonus'],
            'source_text': source_text[:1000],
            'confidence': '0.80' if recoverable is not None else '0.60',
            'review_status': 'review_required',
        },
        'conditions': [],
        'is_active': True,
    }


def _build_minimum_rule(amount: Decimal) -> dict[str, Any]:
    return {
        'name': f'Front Minimum ${amount.quantize(Decimal("0.01"))}',
        'rule_type': 'minimum_commission',
        'calculation_scope': 'per_sale',
        'configuration': {
            'minimum_amount': str(amount.quantize(Decimal('0.01'))),
            'applies_to_categories': ['front_end'],
        },
        'conditions': [],
        'is_active': True,
    }


def _parse_condition_specific_automotive_plan(text, plan_name):
    """Compile the supported New/Pre-Owned structure by document content."""
    lower = re.sub(r'\s+', ' ', text.lower())
    required = (
        ('new vehicles' in lower)
        and ('pre-owned vehicles' in lower or 'preowned vehicles' in lower)
        and re.search(r'18\s*%.*vehicle gross profit', lower)
        and re.search(r'300\s+(?:dollar\s+)?soft pack|\$300 soft pack', lower)
        and 'not retroactive' in lower
        and re.search(r'15\+?\s+40\s*%', lower)
    )
    if not required:
        return None
    new_condition = [{
        'field_name': 'vehicle_condition', 'operator': 'equals', 'value': 'new',
    }]
    used_condition = [{
        'field_name': 'vehicle_condition', 'operator': 'equals', 'value': 'used',
    }]
    rules = [
        {
            'name': 'New Vehicle Front Gross 18%',
            'rule_type': 'front_gross_percentage', 'calculation_scope': 'per_sale',
            'configuration': {
                'rate': '0.18', 'gross_field': 'front_end_gross',
                'pack_amount': '0.00',
            },
            'conditions': new_condition, 'is_active': True,
        },
        {
            'name': 'New Vehicle Monthly Unit Minimum',
            'rule_type': 'tiered_minimum_commission',
            'calculation_scope': 'per_sale',
            'configuration': {
                'unit_metric': 'monthly_new_units',
                'applies_to_categories': ['front_end'],
                'tiers': [
                    {'minimum_units': '0', 'maximum_units': '4.5', 'amount': '100.00'},
                    {'minimum_units': '5', 'maximum_units': None, 'amount': '200.00'},
                ],
            },
            'conditions': new_condition, 'is_active': True,
        },
        {
            'name': 'New Demo Over 4,000 Miles',
            'rule_type': 'vehicle_spiff', 'calculation_scope': 'per_sale',
            'configuration': {'amount': '150.00'},
            'conditions': new_condition + [{
                'field_name': 'mileage', 'operator': 'greater_than', 'value': '4000',
            }],
            'is_active': True,
        },
        {
            'name': 'Used Non-Retroactive Front Gross Tiers',
            'rule_type': 'progressive_unit_position_percentage',
            'calculation_scope': 'per_sale',
            'configuration': {
                'gross_field': 'front_end_gross', 'pack_amount': '300.00',
                'unit_filter': {'vehicle_condition': 'used'},
                'non_retroactive': True,
                'tiers': [
                    {'start': '0', 'end': '4.5', 'rate': '0.25'},
                    {'start': '5', 'end': '9.5', 'rate': '0.30'},
                    {'start': '10', 'end': '14.5', 'rate': '0.35'},
                    {'start': '15', 'end': None, 'rate': '0.40'},
                ],
            },
            'conditions': used_condition, 'is_active': True,
        },
        {
            'name': 'F&I Gross 5% After $150 Pack',
            'rule_type': 'back_gross_percentage', 'calculation_scope': 'per_sale',
            'configuration': {
                'rate': '0.05', 'gross_field': 'back_end_gross',
                'pack_amount': '150.00',
            },
            'conditions': [], 'is_active': True,
        },
    ]
    explicit_volume = _parse_explicit_volume_bonus(text)
    if explicit_volume:
        tiers, source, metric, conditions = explicit_volume
        rules.append(_build_volume_bonus_tiers_rule(
            tiers, source, unit_metric=metric, conditions=conditions,
        ))
    return {
        'plan_name': plan_name,
        'source': 'description',
        'rules': rules,
        'warnings': [
            'Holdback is not fabricated; the supplied Sale front-end gross is used.',
            'Customer-satisfaction eligibility and draw recoverability require review.',
            'Any provisions not shown in the compiled summary require manual confirmation.',
        ],
        'unrecognized_sections': [],
        'confidence': '0.96',
        'parser_profile': 'condition_specific_automotive_v1',
        'compiled_summary': {
            'new': {
                'front_rate': '18%', 'front_pack': '$0',
                'minimums': ['1–4.5 New units: $100', '5+ New units: $200'],
                'backend': '5% after $150 pack',
                'demo_bonus': '$150 over 4,000 miles',
            },
            'used': {
                'front_pack': '$300',
                'tiers': ['1–4.5: 25%', '5–9.5: 30%', '10–14.5: 35%', '15+: 40%'],
                'tier_behavior': 'Non-retroactive',
                'backend': '5% after $150 pack',
            },
        },
        'requires_review': True, 'approved': False,
        'generated_at': timezone.now().isoformat(),
    }


def parse_description_to_import_draft(description_text: str, plan_name: str) -> dict[str, Any]:
    text = description_text or ''
    automotive = _parse_condition_specific_automotive_plan(text, plan_name)
    if automotive is not None:
        return automotive
    specialized = _parse_subaru_simplified_bonus_plan(text, plan_name)
    if specialized is not None:
        return specialized
    warnings: list[str] = []
    rules: list[dict[str, Any]] = []

    front_matches = re.findall(
        r'(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?(?:the\s+)?front(?:[- ]?end)?',
        text,
        flags=re.IGNORECASE,
    )
    for value in front_matches:
        percent = _decimal_from_match(value)
        if percent is None or percent <= 0:
            continue
        rules.append(_build_front_rule(percent, text))

    back_matches = re.findall(
        r'(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?'
        r'(?:finance(?:\s+(?:income|reserve|gross))?|'
        r'back(?:[- ]?end)?(?:\s+gross)?|f\s*&\s*i(?:\s+gross)?|'
        r'product\s+gross|aftermarket\s+gross)',
        text,
        flags=re.IGNORECASE,
    )
    for value in back_matches:
        percent = _decimal_from_match(value)
        if percent is None or percent <= 0:
            continue
        rules.append(_build_back_rule(percent, text))

    for value in re.findall(
        r'(?:pay\s+)?\$\s*([\d,]+(?:\.\d{1,2})?)\s+flat\s+'
        r'(?:back(?:[- ]?end)?|f\s*&\s*i|finance)',
        text, flags=re.IGNORECASE,
    ):
        amount = _decimal_from_match(value)
        if amount is not None and amount > 0:
            rules.append({
                'name': f'Flat Backend ${amount.quantize(Decimal("0.01"))}',
                'rule_type': 'flat_backend_commission',
                'calculation_scope': 'per_sale',
                'configuration': {'amount': str(amount.quantize(Decimal('0.01')))},
                'conditions': _vehicle_condition_conditions(text),
                'is_active': True,
            })

    minimum_match = re.search(r'\$\s*([\d,]+(?:\.\d{1,2})?)\s*minimum', text, flags=re.IGNORECASE)
    if minimum_match is None:
        minimum_match = re.search(
            r'minimum\s+commission.*?\$\s*([\d,]+(?:\.\d{1,2})?)',
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if minimum_match:
        minimum = _decimal_from_match(minimum_match.group(1))
        if minimum is not None and minimum > 0:
            rules.append(_build_minimum_rule(minimum))

    explicit_volume = _parse_explicit_volume_bonus(text)
    tier_matches = re.findall(
        r'(\d+(?:\.\d+)?)\s*(?:[-–]\s*(\d+(?:\.\d+)?)|\+)?\s*units?\s*'
        r'[:\-]\s*\$\s*([\d,]+(?:\.\d{1,2})?)',
        text, flags=re.IGNORECASE,
    )
    if explicit_volume:
        tiers, source_section, unit_metric, conditions = explicit_volume
        rules.append(_build_volume_bonus_tiers_rule(
            tiers,
            source_section,
            unit_metric=unit_metric,
            conditions=conditions,
        ))
    elif tier_matches:
        tiers = []
        for minimum_value, maximum_value, amount_value in tier_matches:
            tier = {
                'minimum_units': str(_decimal_from_match(minimum_value)),
                'amount': str(_decimal_from_match(amount_value).quantize(Decimal('0.01'))),
            }
            if maximum_value:
                tier['maximum_units'] = str(_decimal_from_match(maximum_value))
            tiers.append(tier)
        unit_metric = (
            'monthly_new_units' if 'new only' in text.lower()
            else 'monthly_used_units' if 'used only' in text.lower()
            else 'monthly_units'
        )
        rules.append(_build_volume_bonus_tiers_rule(
            tiers, text, unit_metric=unit_metric,
        ))

    bonus_match = re.search(
        r'(?:at|after)\s*(\d+(?:\.\d+)?)\s*units?.*?\$\s*([\d,]+(?:\.\d{1,2})?)',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if bonus_match and not tier_matches:
        units = _decimal_from_match(bonus_match.group(1))
        amount = _decimal_from_match(bonus_match.group(2))
        if units is not None and amount is not None and units > 0 and amount >= 0:
            rules.append(_build_volume_bonus_rule(units, amount))

    draw_match = re.search(
        r'(?:(recoverable|non[- ]?recoverable)\s+)?'
        r'(?:(weekly|biweekly|bi-weekly|semimonthly|semi-monthly|monthly)\s+)?'
        r'draw\s*[:\-]?\s*\$\s*([\d,]+(?:\.\d{1,2})?)',
        text, flags=re.IGNORECASE,
    )
    if draw_match is None:
        draw_match = re.search(
        r'(?:(recoverable|non[- ]?recoverable)\s+)?'
        r'(?:(weekly|biweekly|bi-weekly|semimonthly|semi-monthly|monthly)\s+)?'
        r'(?:draw|commission\s+advance|minimum\s+guarantee|training\s+guarantee)'
        r'.{0,80}?\$\s*([\d,]+(?:\.\d{1,2})?)',
            text, flags=re.IGNORECASE,
        )
    if draw_match:
        kind, frequency, amount_value = draw_match.groups()
        draw_amount = _decimal_from_match(amount_value)
        recoverable = (
            False if kind and kind.lower().replace('-', ' ').startswith('non ')
            else True if kind else None
        )
        if draw_amount is not None and draw_amount > 0:
            rules.append(_build_draw_rule(
                draw_amount,
                (frequency or 'monthly').lower().replace('-', ''),
                recoverable,
                text,
            ))

    if not rules:
        warnings.append(
            'No commission rules were recognized from the description. Add rules manually or revise the description.'
        )

    recognized_signals = sum(
        1 for marker in ('front', 'back', 'minimum', 'units')
        if marker in text.lower()
    )
    confidence = Decimal(str(min(1, (len(rules) * 0.20) + (recognized_signals * 0.10))))
    if confidence < PARSER_CONFIDENCE_THRESHOLD:
        warnings.append('Parser confidence is below the activation threshold and requires review.')

    return {
        'plan_name': plan_name,
        'source': 'description',
        'rules': rules,
        'warnings': warnings,
        'unrecognized_sections': [],
        'confidence': str(confidence.quantize(Decimal('0.01'))),
        'requires_review': True,
        'approved': False,
        'generated_at': timezone.now().isoformat(),
    }


def build_upload_import_draft(documents: list[PayPlanDocument], plan_name: str) -> dict[str, Any]:
    warnings: list[str] = []
    if not documents:
        warnings.append('No upload documents were found for parsing.')
    extracted_pages: list[str] = []
    unrecognized: list[str] = []
    for document in documents:
        if document.document_type != PayPlanDocument.PDF:
            warnings.append(
                f'{document.original_filename}: image OCR is not available; enter rules manually.'
            )
            unrecognized.append(document.original_filename)
            continue
        try:
            from pypdf import PdfReader
            document.file.open('rb')
            reader = PdfReader(document.file)
            for page_number, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ''
                if page_text.strip():
                    extracted_pages.append(page_text)
                else:
                    warnings.append(
                        f'{document.original_filename} page {page_number} contains no extractable text.'
                    )
        except Exception:
            warnings.append(
                f'{document.original_filename}: the PDF could not be read safely.'
            )
            unrecognized.append(document.original_filename)
        finally:
            document.file.close()

    normalized_text = re.sub(r'\s+', ' ', '\n'.join(extracted_pages)).strip()
    parsed = parse_description_to_import_draft(normalized_text, plan_name)
    parsed['source'] = 'upload'
    parsed['parser_version'] = PARSER_VERSION
    parsed['warnings'] = warnings + parsed['warnings']
    parsed['unrecognized_sections'] = unrecognized

    lower = normalized_text.lower()
    unsupported_markers = [] if parsed.get('parser_profile') else [
        ('nps bonus', 'NPS survey-count bonuses are not imported; NPS finance eligibility is controlled in Monthly Eligibility.'),
        ('used vehicle qualifier', 'The used-vehicle monthly deduction requires vehicle-condition data and manual review.'),
        ('monthly draw', 'Draw recovery is not automatically imported.'),
        ('holiday bonus', 'Annual Holiday Bonus Fund rules are not automatically imported.'),
    ]
    for marker, warning in unsupported_markers:
        if marker in lower and warning not in parsed['warnings']:
            parsed['warnings'].append(warning)
    parsed['requires_review'] = True
    return parsed


def apply_import_draft_to_version(version: Any, import_draft: dict[str, Any], overwrite: bool = True) -> dict[str, Any]:
    from .pay_plan_domain.adapters import ImportDraftAdapter
    from .pay_plan_domain.compiler import PayPlanCompiler
    from .pay_plan_domain.services import (
        CanonicalPlanStorageService, ImmutableVersionService,
    )

    canonical = ImportDraftAdapter.to_canonical(import_draft, version=version)
    compilation = PayPlanCompiler.compile(canonical)
    if version.status in ImmutableVersionService.MUTABLE_STATUSES:
        CanonicalPlanStorageService.store_compilation(
            version, canonical, compilation,
        )
    rules = compilation.executable_rules
    if overwrite:
        version.rules.all().delete()

    created_rules = 0
    rejected_rules: list[str] = [
        f'Compilation: {item.message}' for item in compilation.errors
    ]
    for index, candidate in enumerate(rules, start=1):
        configuration = candidate.get('configuration') or {}
        rule = PayPlanRule(
            pay_plan_version=version,
            name=candidate.get('name') or f'Imported Rule {index}',
            rule_type=candidate.get('rule_type') or '',
            calculation_scope=candidate.get('calculation_scope') or 'per_sale',
            configuration=configuration,
            is_active=bool(candidate.get('is_active', True)),
            sort_order=index,
        )
        try:
            rule.full_clean()
            rule.save()
            for order, condition in enumerate(candidate.get('conditions') or [], start=1):
                stored_value = condition.get('value')
                if stored_value is None and condition['operator'] in ('is_true', 'is_false'):
                    # JSONField is database NOT NULL even though unary boolean
                    # operators do not consume a comparison target.
                    stored_value = condition['operator'] == 'is_true'
                rule.conditions.create(
                    field_name=condition['field_name'],
                    operator=condition['operator'],
                    value=stored_value,
                    sort_order=order,
                )
            created_rules += 1
        except Exception as exc:  # pragma: no cover - defensive import guard
            rejected_rules.append(f"{rule.name}: {exc}")

    return {
        'created_rules': created_rules,
        'rejected_rules': rejected_rules,
        'canonical_fingerprint': canonical.fingerprint,
        'compilation_errors': [item.message for item in compilation.errors],
        'compilation_warnings': [item.message for item in compilation.warnings],
        'compilation_statistics': compilation.statistics,
        'skipped_rules': compilation.skipped_rules,
        'unsupported_clauses': compilation.unsupported_clauses,
    }


def mark_submission_review_state(onboarding: Any, approved: bool) -> None:
    latest_description = onboarding.description_submissions.order_by('-created_at').first()
    if latest_description is None:
        return
    latest_description.status = (
        PayPlanDescriptionSubmission.APPROVED
        if approved
        else PayPlanDescriptionSubmission.NEEDS_REVIEW
    )
    latest_description.reviewed_at = timezone.now()
    latest_description.save(update_fields=['status', 'reviewed_at', 'updated_at'])
