from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class IntentType(str, Enum):
    CREATE_RULE = 'create_rule'
    UPDATE_RULE = 'update_rule'
    DELETE_RULE = 'delete_rule'
    COMPARE_RULES = 'compare_rules'
    EXPLAIN_RULE = 'explain_rule'
    SIMULATE_COMMISSION = 'simulate_commission'
    VALIDATE_PAY_PLAN = 'validate_pay_plan'
    COMPILE_PAY_PLAN = 'compile_pay_plan'


@dataclass(frozen=True)
class PayPlanIntent:
    intent_type: IntentType
    plan_version_id: int | None = None
    rule_key: str = ''
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationState:
    user_id: int
    conversation_key: str
    active_plan_version_id: int | None = None
    pending_intent: PayPlanIntent | None = None
    selected_rule_key: str = ''
    context: dict[str, Any] = field(default_factory=dict)


class PayPlanInterpreter(Protocol):
    def interpret(self, source: Any, *, context: dict | None = None): ...


class IntentProcessor(Protocol):
    def process(self, intent: PayPlanIntent, *, user: Any): ...


class RuleGenerator(Protocol):
    def generate(self, interpreted_input: Any): ...


class QuestionAnswerService(Protocol):
    def answer(self, question: str, *, plan: Any, context: dict | None = None): ...


class SimulationService(Protocol):
    def simulate(self, *, user: Any, plan: Any, sales: list[Any]): ...


class NaturalLanguageEditor(Protocol):
    def propose(self, text: str, *, plan: Any, state: ConversationState): ...


class RuleImporter(Protocol):
    def import_source(self, source: Any, *, source_type: str): ...
