from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from django.core.exceptions import ValidationError

from ..commission_engine.conditions import evaluate_condition


@dataclass(frozen=True)
class ConditionTrace:
    field: str
    operator: str
    target: Any
    actual: Any
    satisfied: bool


class ConditionEvaluator:
    @staticmethod
    def evaluate(condition, context):
        payload = {
            'field_name': condition.field,
            'operator': condition.operator,
            'value': condition.value,
        }
        satisfied = evaluate_condition(payload, context)
        return ConditionTrace(
            field=condition.field,
            operator=condition.operator,
            target=condition.value,
            actual=context.get(condition.field),
            satisfied=satisfied,
        )


class RuleMatcher:
    """Pure deterministic matcher usable by calculation and simulation callers."""

    @classmethod
    def match(cls, rules, context):
        candidates = []
        rejected = []
        for rule in rules:
            if not rule.active:
                rejected.append({'rule_key': rule.key, 'reason': 'inactive'})
                continue
            traces = [
                ConditionEvaluator.evaluate(condition, context)
                for condition in rule.conditions
            ]
            matches = (
                all(item.satisfied for item in traces)
                if rule.condition_mode == 'all'
                else any(item.satisfied for item in traces)
            ) if traces else True
            record = {
                'rule_key': rule.key,
                'priority': rule.priority,
                'specificity': len(rule.conditions),
                'conditions': [asdict(item) for item in traces],
            }
            (candidates if matches else rejected).append(record)
        candidates.sort(key=lambda item: (
            item['priority'], -item['specificity'], item['rule_key'],
        ))
        return {'selected': candidates[0] if candidates else None,
                'matched': candidates, 'rejected': rejected}


class ExplanationBuilder:
    """Convert engine output to reusable data; formatting belongs to callers."""

    @staticmethod
    def from_calculation(result):
        return {
            'plan': {
                'id': getattr(result.pay_plan, 'id', None),
                'version_id': getattr(result.pay_plan_version, 'id', None),
                'version': getattr(result.pay_plan_version, 'version_name', ''),
            },
            'total': str(result.total),
            'rules': [{
                'rule_id': item.rule_id,
                'rule_name': item.rule_name,
                'rule_type': item.rule_type,
                'category': item.category,
                'applied': item.applied,
                'amount': str(item.amount),
                'explanation': item.explanation,
                'metadata': item.metadata,
                'warnings': item.warnings,
            } for item in result.line_items],
            'rejected_rules': list(result.skipped_rules),
            'warnings': list(result.warnings),
        }


class CommissionCalculator:
    @staticmethod
    def calculate(user, sale, monthly_metrics=None):
        from ..commission_engine.engine import calculate_sale_commission
        return calculate_sale_commission(user, sale, monthly_metrics)


class SimulationEngine:
    @staticmethod
    def simulate(user, version, sales):
        from ..commission_service import CommissionEngineService
        return CommissionEngineService.preview_sales(user, list(sales), version)


class ImmutableVersionService:
    MUTABLE_STATUSES = {'draft', 'review_required', 'failed'}

    @classmethod
    def assert_mutable(cls, version):
        if version.status not in cls.MUTABLE_STATUSES:
            raise ValidationError(
                'Compiled, active, historical, and archived versions are immutable. '
                'Create a new draft version instead.'
            )


class CanonicalPlanStorageService:
    @staticmethod
    def store_compilation(version, canonical, report):
        ImmutableVersionService.assert_mutable(version)
        version.canonical_schema_version = canonical.schema_version
        version.canonical_payload = canonical.to_dict()
        version.canonical_fingerprint = canonical.fingerprint
        version.compilation_report = {
            'fingerprint': report.fingerprint,
            'statistics': report.statistics,
            'errors': [asdict(item) for item in report.errors],
            'warnings': [asdict(item) for item in report.warnings],
            'skipped_rules': report.skipped_rules,
            'unsupported_clauses': report.unsupported_clauses,
        }
        version.save(update_fields=[
            'canonical_schema_version', 'canonical_payload',
            'canonical_fingerprint', 'compilation_report', 'updated_at',
        ])
