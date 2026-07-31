from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any


class IntentAction(StrEnum):
    ADD = 'add'
    CHANGE = 'change'
    REMOVE = 'remove'
    REPLACE = 'replace'
    INCREASE = 'increase'
    DECREASE = 'decrease'
    ENABLE = 'enable'
    DISABLE = 'disable'
    RENAME = 'rename'
    DUPLICATE = 'duplicate'


class TargetType(StrEnum):
    FRONT_END_MINIMUM = 'front_end_minimum'
    FRONT_END_MAXIMUM = 'front_end_maximum'
    FRONT_END_PERCENTAGE = 'front_end_percentage'
    BACK_END_MINIMUM = 'back_end_minimum'
    BACK_END_MAXIMUM = 'back_end_maximum'
    BACK_END_PERCENTAGE = 'back_end_percentage'
    FRONT_END_PACK = 'front_end_pack'
    BACK_END_PACK = 'back_end_pack'
    VOLUME_BONUS_TIER = 'volume_bonus_tier'
    FLAT_BONUS = 'flat_bonus'
    MODEL_BONUS = 'model_bonus'
    NEW_VEHICLE_BONUS = 'new_vehicle_bonus'
    USED_VEHICLE_BONUS = 'used_vehicle_bonus'
    DRAW = 'draw'
    MANUFACTURER_INCENTIVE = 'manufacturer_incentive'
    CONDITION_REQUIREMENT = 'condition_requirement'


ACTIONS = frozenset(item.value for item in IntentAction)
TARGET_TYPES = frozenset(item.value for item in TargetType)


@dataclass(frozen=True)
class CandidateTarget:
    selector: str
    label: str
    rule_name: str
    applies_to: str


@dataclass(frozen=True)
class PayPlanIntent:
    source_text: str
    action: str | None = None
    target_type: str | None = None
    target_scope: str | None = None
    rule_selector: str | None = None
    amount: Decimal | None = None
    percentage: Decimal | None = None
    unit_threshold: Decimal | None = None
    current_value: Decimal | None = None
    new_value: Decimal | None = None
    conditions: tuple[dict[str, Any], ...] = ()
    effective_date: date | None = None
    confidence: Decimal = Decimal('0')
    missing_information: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()
    clarification_question: str = ''
    candidate_targets: tuple[CandidateTarget, ...] = ()
    normalized_text: str = ''

    @property
    def is_complete(self) -> bool:
        return bool(
            self.action
            and self.target_type
            and not self.missing_information
            and not self.ambiguities
        )

    def with_resolution(
        self,
        *,
        rule_selector: str | None = None,
        missing_information: tuple[str, ...] | None = None,
        ambiguities: tuple[str, ...] | None = None,
        clarification_question: str | None = None,
        candidate_targets: tuple[CandidateTarget, ...] | None = None,
    ) -> 'PayPlanIntent':
        values = {
            **self.__dict__,
            'rule_selector': (
                self.rule_selector if rule_selector is None else rule_selector
            ),
            'missing_information': (
                self.missing_information
                if missing_information is None else missing_information
            ),
            'ambiguities': (
                self.ambiguities if ambiguities is None else ambiguities
            ),
            'clarification_question': (
                self.clarification_question
                if clarification_question is None else clarification_question
            ),
            'candidate_targets': (
                self.candidate_targets
                if candidate_targets is None else candidate_targets
            ),
        }
        return type(self)(**values)

    def as_dict(self) -> dict[str, Any]:
        def serialize(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, tuple):
                return [serialize(item) for item in value]
            if isinstance(value, dict):
                return {key: serialize(item) for key, item in value.items()}
            return value

        return serialize(asdict(self))


@dataclass(frozen=True)
class ProposedChange:
    action_type: str
    target_type: str
    target_label: str
    rule_selector: str | None
    rule_name: str
    current_value: Decimal | None
    new_value: Decimal | None
    applies_to: str
    source_version_id: int
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def current_display(self) -> str:
        return _display_value(self.current_value, self.target_type)

    @property
    def new_display(self) -> str:
        return _display_value(self.new_value, self.target_type)

    def as_action(self) -> dict[str, Any]:
        action = {
            'action_type': self.action_type,
            'target_key': (
                f'{self.target_type}:{self.rule_selector or self.rule_name}'
            ),
            'target_type': self.target_type,
            'rule_name': self.rule_name,
            'old_value': self._stored_value(self.current_value),
            'new_value': self._stored_value(self.new_value),
            'applies_to': self.applies_to,
        }
        action.update({
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in self.details.items()
        })
        return action

    def _stored_value(self, value):
        if value is None:
            return None
        if self.target_type in {
            TargetType.FRONT_END_PERCENTAGE,
            TargetType.BACK_END_PERCENTAGE,
        }:
            return str(value)
        return str(value.quantize(Decimal('0.01')))


def _display_value(value: Decimal | None, target_type: str) -> str:
    if value is None:
        return 'Not currently set'
    if target_type in {
        TargetType.FRONT_END_PERCENTAGE,
        TargetType.BACK_END_PERCENTAGE,
    }:
        number = format(value * Decimal('100'), 'f').rstrip('0').rstrip('.')
        return f'{number}%'
    return f'${value.quantize(Decimal("0.01")):,.2f}'


@dataclass(frozen=True)
class IntentResolution:
    status: str
    intent: PayPlanIntent
    proposal: ProposedChange | None = None
    message: str = ''

    @property
    def may_create_draft(self) -> bool:
        return self.status == 'proposed' and self.proposal is not None
