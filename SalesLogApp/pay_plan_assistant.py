from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import PayPlanChangeRequest
from .pay_plan_management import create_manual_draft, preview_version


REQUIREMENT_FIELDS = {
    'call': 'call_requirement_met',
    'calls': 'call_requirement_met',
    'phone': 'call_requirement_met',
    'video': 'video_requirement_met',
    'videos': 'video_requirement_met',
    'training': 'training_requirements_met',
    'green pea': 'green_pea',
    'nps': 'nps_bonus_eligible',
}


def _money(value: str) -> str:
    return str(Decimal(value.replace(',', '')).quantize(Decimal('0.01')))


def _selected_volume_rules(version, text):
    rules = version.rules.filter(rule_type='volume_bonus', is_active=True)
    lower = text.lower()
    if 'green pea' in lower:
        return rules.filter(name__icontains='green pea')
    if 'standard' in lower or 'all other' in lower:
        return rules.exclude(name__icontains='green pea')
    return rules


@transaction.atomic
def create_plain_text_change_draft(user, request_text, effective_date):
    source = user.pay_plan_assignments.filter(
        is_active=True,
    ).select_related('pay_plan_version').order_by(
        '-effective_start_date', '-id',
    ).first()
    if source is None:
        raise ValidationError('An active pay plan is required.')

    draft = create_manual_draft(user, effective_date)
    text = request_text.strip()
    lower = text.lower()
    actions = []
    warnings = []

    tier_match = re.search(
        r'(?:at|for)\s+(\d+(?:\.\d+)?)\s*units?.*?'
        r'(?:to|=)\s*\$?\s*([\d,]+(?:\.\d{1,2})?)',
        text,
        flags=re.IGNORECASE,
    )
    if tier_match:
        unit_point = Decimal(tier_match.group(1))
        new_amount = _money(tier_match.group(2))
        matched = []
        for rule in _selected_volume_rules(draft, text):
            configuration = deepcopy(rule.configuration)
            for tier in configuration.get('tiers', []):
                minimum = Decimal(str(tier['minimum_units']))
                maximum = (
                    Decimal(str(tier['maximum_units']))
                    if tier.get('maximum_units') not in (None, '') else None
                )
                if unit_point >= minimum and (
                    maximum is None or unit_point <= maximum
                ):
                    old_amount = str(tier['amount'])
                    tier['amount'] = new_amount
                    rule.configuration = configuration
                    rule.save(update_fields=['configuration', 'updated_at'])
                    matched.append(rule.name)
                    actions.append({
                        'action_type': 'change_volume_tier_amount',
                        'target_key': f'{rule.name}:{minimum}',
                        'rule_name': rule.name,
                        'minimum_units': str(minimum),
                        'maximum_units': str(maximum) if maximum is not None else None,
                        'old_value': old_amount,
                        'new_value': new_amount,
                    })
                    break
        if not matched:
            warnings.append(
                f'No volume tier containing {unit_point} units was found.'
            )

    rate_patterns = [
        (
            'front_gross_percentage',
            r'(?:front|front[- ]end).*?(?:to|=)\s*(\d+(?:\.\d+)?)\s*%',
        ),
        (
            'back_gross_percentage',
            r'(?:finance|back[- ]end).*?(?:to|=)\s*(\d+(?:\.\d+)?)\s*%',
        ),
    ]
    for rule_type, pattern in rate_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        new_rate = Decimal(match.group(1)) / Decimal('100')
        candidates = draft.rules.filter(rule_type=rule_type, is_active=True)
        if rule_type == 'back_gross_percentage' and 'let it ride' not in lower:
            candidates = candidates.exclude(name__icontains='let it ride')
        for rule in candidates:
            configuration = deepcopy(rule.configuration)
            old_rate = configuration.get('rate')
            configuration['rate'] = str(new_rate)
            rule.configuration = configuration
            rule.save(update_fields=['configuration', 'updated_at'])
            actions.append({
                'action_type': 'change_commission_rate',
                'target_key': rule.name,
                'rule_name': rule.name,
                'old_value': old_rate,
                'new_value': str(new_rate),
            })

    remove_requirement = any(
        phrase in lower
        for phrase in ('remove ', 'drop ', 'no longer require', 'without ')
    )
    add_requirement = any(
        phrase in lower
        for phrase in ('add ', 'require ', 'must meet')
    ) and not remove_requirement
    if remove_requirement or add_requirement:
        for label, field_name in REQUIREMENT_FIELDS.items():
            if label not in lower:
                continue
            for rule in _selected_volume_rules(draft, text):
                existing = rule.conditions.filter(field_name=field_name)
                if remove_requirement and existing.exists():
                    existing.delete()
                    actions.append({
                        'action_type': 'remove_requirement',
                        'target_key': f'{rule.name}:{field_name}',
                        'rule_name': rule.name,
                        'field_name': field_name,
                    })
                elif add_requirement and not existing.exists():
                    rule.conditions.create(
                        field_name=field_name,
                        operator='is_true',
                        value=True,
                        sort_order=rule.conditions.count() + 1,
                    )
                    actions.append({
                        'action_type': 'add_requirement',
                        'target_key': f'{rule.name}:{field_name}',
                        'rule_name': rule.name,
                        'field_name': field_name,
                    })
            break

    if not actions:
        raise ValidationError(
            'I could not safely identify that change. Include the rule, '
            'threshold or percentage, and the new value.'
        )

    draft.processing_warnings = warnings
    draft.processing_errors = []
    draft.processing_status = 'needs_review'
    draft.activation_reason = f'Plain-language request: {text}'
    draft.save(update_fields=[
        'processing_warnings', 'processing_errors', 'processing_status',
        'activation_reason', 'updated_at',
    ])
    preview = preview_version(user, draft)
    request = PayPlanChangeRequest.objects.create(
        user=user,
        source_version=source.pay_plan_version,
        draft_version=draft,
        request_text=text,
        parsed_actions=actions,
        warnings=warnings,
        preview={
            'sales_tested': preview['sales_tested'],
            'calculated_count': preview['calculated_count'],
            'estimated_total': str(preview['estimated_total']),
        },
    )
    return request
