from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import PayPlanChangeRequest, PayPlanRule
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


def _standard_volume_rules(version, text):
    if 'green pea' in text.lower():
        return _selected_volume_rules(version, text)
    return version.rules.filter(
        rule_type='volume_bonus',
        is_active=True,
        name__icontains='standard',
    )


UNIT_WORD = r'(?:units?|cars?|vehicles?|deals?|sales?)'
NUMBER = r'(\d+(?:\.\d+)?)'
AMOUNT = r'\$?\s*([\d,]+(?:\.\d{1,2})?)(?:\s*dollars?)?'


def _new_volume_bonus_values(text):
    """Return (threshold, amount) for an instruction to add a volume tier."""
    patterns = (
        # Amount first: "Pay $250 at 8 units", "Give me $250 when I reach 8 units".
        rf'(?:pay(?:s|ing)?|give\s+me|receive|receives|worth)'
        rf'\s+{AMOUNT}.*?(?:at|when|once).*?{NUMBER}\s*{UNIT_WORD}',
        # Threshold first: "At 8 units I receive a $250 bonus", "8 units pays $250".
        rf'{NUMBER}\s*{UNIT_WORD}.*?'
        rf'(?:pay(?:s|ing)?|bonus(?:\s+worth)?|receive|receives|for)'
        rf'\s+(?:an?\s+)?{AMOUNT}',
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        if index == 0:
            amount, threshold = match.group(1), match.group(2)
        else:
            threshold, amount = match.group(1), match.group(2)
        threshold = Decimal(threshold)
        amount = Decimal(amount.replace(',', ''))
        if threshold <= 0 or amount <= 0:
            return None
        if threshold % Decimal('0.5'):
            raise ValidationError(
                'Volume-bonus thresholds must use whole or half units.'
            )
        return threshold, _money(str(amount))
    return None


def _add_volume_tier(rule, threshold, amount, actions, warnings):
    configuration = deepcopy(rule.configuration)
    tiers = configuration.get('tiers', [])
    ordered = sorted(
        tiers, key=lambda tier: Decimal(str(tier['minimum_units'])),
    )
    for tier in ordered:
        if Decimal(str(tier['minimum_units'])) == threshold:
            raise ValidationError(
                f'{rule.name} already has a tier beginning at {threshold} '
                f'units that pays ${_money(str(tier["amount"]))}. Do you '
                'want to change that tier instead?'
            )

    previous = next(
        (
            tier for tier in reversed(ordered)
            if Decimal(str(tier['minimum_units'])) < threshold
        ),
        None,
    )
    following = next(
        (
            tier for tier in ordered
            if Decimal(str(tier['minimum_units'])) > threshold
        ),
        None,
    )
    if previous is not None:
        old_maximum = previous.get('maximum_units')
        old_maximum_decimal = (
            Decimal(str(old_maximum))
            if old_maximum not in (None, '') else None
        )
        adjusted_maximum = threshold - Decimal('0.5')
        if old_maximum_decimal is None or old_maximum_decimal >= threshold:
            previous['maximum_units'] = str(adjusted_maximum)
            actions.append({
                'action_type': 'adjust_volume_tier_range',
                'target_key': (
                    f'{rule.name}:{previous["minimum_units"]}:maximum'
                ),
                'rule_name': rule.name,
                'minimum_units': str(previous['minimum_units']),
                'maximum_units': str(adjusted_maximum),
                'old_value': (
                    str(old_maximum) if old_maximum not in (None, '') else None
                ),
                'new_value': str(adjusted_maximum),
            })
            warnings.append(
                f'{rule.name}: adjusted the preceding tier maximum from '
                f'{old_maximum if old_maximum not in (None, "") else "open-ended"} '
                f'to {adjusted_maximum} units to prevent overlap.'
            )

    maximum = (
        Decimal(str(following['minimum_units'])) - Decimal('0.5')
        if following is not None else None
    )
    new_tier = {
        'minimum_units': str(threshold),
        'amount': amount,
    }
    if maximum is not None:
        new_tier['maximum_units'] = str(maximum)
    ordered.append(new_tier)
    ordered.sort(key=lambda tier: Decimal(str(tier['minimum_units'])))
    configuration['tiers'] = ordered
    rule.configuration = configuration
    rule.full_clean()
    rule.save(update_fields=['configuration', 'updated_at'])
    actions.append({
        'action_type': 'add_volume_tier',
        'target_key': f'{rule.name}:{threshold}',
        'rule_name': rule.name,
        'minimum_units': str(threshold),
        'maximum_units': str(maximum) if maximum is not None else None,
        'old_value': None,
        'new_value': amount,
    })


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

    new_volume_bonus = None if tier_match else _new_volume_bonus_values(text)
    if new_volume_bonus:
        threshold, amount = new_volume_bonus
        candidates = list(_standard_volume_rules(draft, text))
        if candidates:
            for rule in candidates:
                _add_volume_tier(
                    rule, threshold, amount, actions, warnings,
                )
        else:
            rule = PayPlanRule(
                pay_plan_version=draft,
                name='Standard Volume Bonus',
                rule_type='volume_bonus',
                calculation_scope='period',
                configuration={
                    'tiers': [{
                        'minimum_units': str(threshold),
                        'amount': amount,
                    }],
                    'tier_mode': 'highest_only',
                },
                sort_order=draft.rules.count() + 1,
            )
            rule.full_clean()
            rule.save()
            actions.append({
                'action_type': 'add_volume_tier',
                'target_key': f'{rule.name}:{threshold}',
                'rule_name': rule.name,
                'minimum_units': str(threshold),
                'maximum_units': None,
                'old_value': None,
                'new_value': amount,
            })

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
