from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class PayPlanGeneral:
    name: str
    version: str = ''
    effective_date: date | None = None
    expiration_date: date | None = None
    dealership: str = ''
    industry: str = ''
    currency: str = 'USD'
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanonicalCondition:
    field: str
    operator: str
    value: Any = None

    def to_dict(self):
        result = {'field': self.field, 'operator': self.operator}
        if self.operator not in {'is_true', 'is_false'}:
            result['value'] = _json_value(self.value)
        return result


@dataclass(frozen=True)
class RuleAction:
    action_type: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            'action_type': self.action_type,
            'parameters': _json_value(self.parameters),
        }


@dataclass(frozen=True)
class CanonicalRule:
    key: str
    name: str
    category: str
    scope: str
    action: RuleAction
    conditions: tuple[CanonicalCondition, ...] = ()
    condition_mode: str = 'all'
    priority: int = 0
    active: bool = True
    source_reference: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self):
        return {
            'key': self.key,
            'name': self.name,
            'category': self.category,
            'scope': self.scope,
            'action': self.action.to_dict(),
            'conditions': [condition.to_dict() for condition in self.conditions],
            'condition_mode': self.condition_mode,
            'priority': self.priority,
            'active': self.active,
            'source_reference': _json_value(self.source_reference),
            'notes': list(self.notes),
        }


@dataclass(frozen=True)
class CanonicalPayPlan:
    general: PayPlanGeneral
    rules: tuple[CanonicalRule, ...] = ()
    source_type: str = 'manual'
    source_metadata: dict[str, Any] = field(default_factory=dict)
    unsupported_clauses: tuple[str, ...] = ()
    schema_version: str = '1.0'

    def to_dict(self):
        return {
            'schema_version': self.schema_version,
            'general': _json_value(asdict(self.general)),
            'source_type': self.source_type,
            'source_metadata': _json_value(self.source_metadata),
            'rules': [rule.to_dict() for rule in self.rules],
            'unsupported_clauses': list(self.unsupported_clauses),
        }

    def canonical_json(self):
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':'), ensure_ascii=True,
        )

    @property
    def fingerprint(self):
        return hashlib.sha256(self.canonical_json().encode('utf-8')).hexdigest()
