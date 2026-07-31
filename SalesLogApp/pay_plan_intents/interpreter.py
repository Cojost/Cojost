from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from .contract import IntentAction, PayPlanIntent, TargetType
from .normalization import detect_action, normalize_text, numeric_mentions


TARGET_LABELS = {
    TargetType.FRONT_END_MINIMUM: 'Front-end commission minimum',
    TargetType.FRONT_END_MAXIMUM: 'Front-end commission maximum',
    TargetType.FRONT_END_PERCENTAGE: 'Front-end commission percentage',
    TargetType.BACK_END_MINIMUM: 'Back-end commission minimum',
    TargetType.BACK_END_MAXIMUM: 'Back-end commission maximum',
    TargetType.BACK_END_PERCENTAGE: 'Back-end commission percentage',
    TargetType.FRONT_END_PACK: 'Front-end pack',
    TargetType.BACK_END_PACK: 'Back-end pack',
    TargetType.VOLUME_BONUS_TIER: 'Volume bonus tier',
    TargetType.FLAT_BONUS: 'Flat bonus',
    TargetType.MODEL_BONUS: 'Model bonus',
    TargetType.NEW_VEHICLE_BONUS: 'New-vehicle bonus',
    TargetType.USED_VEHICLE_BONUS: 'Used-vehicle bonus',
    TargetType.DRAW: 'Draw',
    TargetType.MANUFACTURER_INCENTIVE: 'Manufacturer incentive',
    TargetType.CONDITION_REQUIREMENT: 'Condition requirement',
}


class DeterministicIntentInterpreter:
    """Pure interpretation. This class never queries or writes the database."""

    def interpret(
        self,
        source_text: str,
        *,
        effective_date: date | None = None,
    ) -> PayPlanIntent:
        text = normalize_text(source_text)
        mentions = numeric_mentions(text)
        action = detect_action(text)
        target = self._target(text, mentions)
        ambiguities = []
        missing = []
        question = ''

        if self._has_multiple_changes(text):
            ambiguities.append('multiple_requested_changes')
            question = (
                'I found more than one requested change. Please confirm whether '
                'you want to update them together or handle them one at a time.'
            )

        if target is None:
            missing.append('target_type')
            if 'minimum' in text and mentions:
                question = (
                    f'Should the ${mentions[-1].value.quantize(Decimal("0.01"))} '
                    'minimum apply to front-end commission, back-end commission, '
                    'or a bonus?'
                )
            else:
                question = (
                    'Which part of the pay plan would you like to change?'
                )

        if action is None:
            if target == TargetType.VOLUME_BONUS_TIER and mentions:
                action = IntentAction.ADD
            elif target is not None:
                missing.append('action')
                question = (
                    f'What would you like to do with '
                    f'{TARGET_LABELS[target].lower()}?'
                )

        amount, percentage, threshold, current_value, new_value = (
            self._values(text, target, mentions)
        )
        if target in {
            TargetType.FRONT_END_MINIMUM,
            TargetType.FRONT_END_MAXIMUM,
            TargetType.BACK_END_MINIMUM,
            TargetType.BACK_END_MAXIMUM,
            TargetType.FRONT_END_PACK,
            TargetType.BACK_END_PACK,
            TargetType.FLAT_BONUS,
            TargetType.MODEL_BONUS,
            TargetType.NEW_VEHICLE_BONUS,
            TargetType.USED_VEHICLE_BONUS,
            TargetType.DRAW,
            TargetType.MANUFACTURER_INCENTIVE,
        } and new_value is None and action not in {
            IntentAction.REMOVE, IntentAction.DISABLE,
        }:
            missing.append('new_value')
            if target == TargetType.FRONT_END_MINIMUM:
                question = 'What should the new front-end minimum be?'
            elif target:
                question = (
                    f'What should the new '
                    f'{TARGET_LABELS[target].lower()} be?'
                )
        if target in {
            TargetType.FRONT_END_PERCENTAGE,
            TargetType.BACK_END_PERCENTAGE,
        } and percentage is None:
            missing.append('percentage')
            question = (
                f'What should the new {TARGET_LABELS[target].lower()} be?'
            )
        if target == TargetType.VOLUME_BONUS_TIER:
            if amount is None:
                missing.append('amount')
            if threshold is None:
                missing.append('unit_threshold')
            if missing:
                question = (
                    'What bonus amount and unit threshold should apply? '
                    'For example: Pay $500 at 10 units.'
                )
        if target == TargetType.CONDITION_REQUIREMENT:
            conditions = self._conditions(text)
            if not conditions:
                missing.append('conditions')
                question = 'Which requirement would you like to change?'
        else:
            conditions = ()

        confidence = Decimal('0.95')
        if missing or ambiguities:
            confidence = Decimal('0.60') if target else Decimal('0.35')
        return PayPlanIntent(
            source_text=(source_text or '').strip(),
            action=str(action) if action else None,
            target_type=str(target) if target else None,
            target_scope=self._scope(text),
            amount=amount,
            percentage=percentage,
            unit_threshold=threshold,
            current_value=current_value,
            new_value=new_value,
            conditions=conditions,
            effective_date=effective_date,
            confidence=confidence,
            missing_information=tuple(dict.fromkeys(missing)),
            ambiguities=tuple(ambiguities),
            clarification_question=question,
            normalized_text=text,
        )

    @staticmethod
    def _target(text, mentions):
        front = 'frontend' in text
        back = 'backend' in text
        if 'minimum' in text or 'pay at least' in text:
            if front:
                return TargetType.FRONT_END_MINIMUM
            if back:
                return TargetType.BACK_END_MINIMUM
            return None
        if 'maximum' in text:
            if front:
                return TargetType.FRONT_END_MAXIMUM
            if back:
                return TargetType.BACK_END_MAXIMUM
            return None
        if 'pack' in text:
            if front:
                return TargetType.FRONT_END_PACK
            if back:
                return TargetType.BACK_END_PACK
        if ('percentage' in text or 'rate' in text or any(
            item.is_percentage for item in mentions
        )):
            if front:
                return TargetType.FRONT_END_PERCENTAGE
            if back:
                return TargetType.BACK_END_PERCENTAGE
        if 'draw' in text:
            return TargetType.DRAW
        if 'manufacturer' in text or 'factory incentive' in text:
            return TargetType.MANUFACTURER_INCENTIVE
        if any(word in text for word in (
            'requirement', 'require ', 'video', 'training', 'nps', 'phone',
            'call ',
        )):
            return TargetType.CONDITION_REQUIREMENT
        if 'bonus' in text:
            if 'model' in text:
                return TargetType.MODEL_BONUS
            if re.search(r'\bnew\b', text):
                return TargetType.NEW_VEHICLE_BONUS
            if re.search(r'\bused\b', text):
                return TargetType.USED_VEHICLE_BONUS
            if 'flat' in text:
                return TargetType.FLAT_BONUS
            if any(item.is_unit_threshold for item in mentions) or 'volume' in text:
                return TargetType.VOLUME_BONUS_TIER
        if (
            re.search(r'\b(?:pay|pays|paying|give|receive|worth)\b', text)
            and any(item.is_currency for item in mentions)
            and any(item.is_unit_threshold for item in mentions)
        ):
            return TargetType.VOLUME_BONUS_TIER
        return None

    @staticmethod
    def _values(text, target, mentions):
        percentage_mention = next(
            (item for item in mentions if item.is_percentage), None,
        )
        threshold_mention = next(
            (item for item in mentions if item.is_unit_threshold), None,
        )
        value_mentions = [
            item for item in mentions
            if not item.is_percentage and not item.is_unit_threshold
        ]
        current = None
        new = None
        from_to = re.search(
            r'\bfrom\s+\$?(\d+(?:\.\d+)?)\s+to\s+\$?(\d+(?:\.\d+)?)',
            text,
        )
        if from_to:
            current = Decimal(from_to.group(1))
            new = Decimal(from_to.group(2))
        elif value_mentions:
            new = value_mentions[-1].value
        percentage = (
            percentage_mention.value / Decimal('100')
            if percentage_mention else None
        )
        if target in {
            TargetType.FRONT_END_PERCENTAGE,
            TargetType.BACK_END_PERCENTAGE,
        } and percentage is not None:
            new = percentage
        amount = None
        if target == TargetType.VOLUME_BONUS_TIER and value_mentions:
            amount = value_mentions[-1].value
            new = amount
        elif target not in {
            TargetType.FRONT_END_PERCENTAGE,
            TargetType.BACK_END_PERCENTAGE,
        }:
            amount = new
        return (
            amount,
            percentage,
            threshold_mention.value if threshold_mention else None,
            current,
            new,
        )

    @staticmethod
    def _scope(text):
        if re.search(r'\bnew\b', text):
            return 'new'
        if re.search(r'\bused\b', text):
            return 'used'
        if 'green pea' in text:
            return 'green_pea'
        if 'standard' in text or 'all qualifying' in text:
            return 'standard'
        return None

    @staticmethod
    def _conditions(text):
        mapping = {
            'video': 'video_requirement_met',
            'training': 'training_requirements_met',
            'phone': 'call_requirement_met',
            'call': 'call_requirement_met',
            'nps': 'nps_bonus_eligible',
        }
        for label, field in mapping.items():
            if re.search(rf'\b{label}\b', text):
                return ({'field_name': field},)
        return ()

    @staticmethod
    def _has_multiple_changes(text):
        segments = re.split(r'\s+(?:and|also|plus)\s+|[;]', text)
        if len(segments) < 2:
            return False
        concept_count = sum(
            bool(re.search(
                r'\b(?:minimum|maximum|percentage|rate|pack|bonus|draw|requirement)\b',
                segment,
            ))
            for segment in segments
        )
        return concept_count > 1
