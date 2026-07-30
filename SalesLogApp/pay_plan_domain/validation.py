from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..commission_engine.validators import (
    SUPPORTED_RULE_TYPES, validate_condition, validate_configuration,
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str
    rule_key: str = ''


@dataclass
class ValidationReport:
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self):
        return not self.errors

    def add(self, issue):
        (self.errors if issue.severity == 'error' else self.warnings).append(issue)


class PayPlanValidationService:
    """Industry-neutral structural validation before executable compilation."""

    @classmethod
    def validate(cls, plan):
        report = ValidationReport()
        if (
            plan.general.effective_date and plan.general.expiration_date
            and plan.general.expiration_date < plan.general.effective_date
        ):
            report.add(ValidationIssue(
                'invalid_date_range', 'Expiration precedes the effective date.', 'error',
            ))
        signatures = {}
        active_keys = {rule.key for rule in plan.rules if rule.active}
        dependencies = {}
        for rule in plan.rules:
            signature = (
                rule.category, rule.scope, rule.action.action_type,
                tuple(
                    (item.field, item.operator, repr(item.value))
                    for item in rule.conditions
                ),
                rule.priority,
            )
            if signature in signatures:
                report.add(ValidationIssue(
                    'duplicate_rule',
                    f'Duplicates rule {signatures[signature]}.',
                    'warning', rule.key,
                ))
            signatures[signature] = rule.key
            if rule.action.action_type not in SUPPORTED_RULE_TYPES:
                report.add(ValidationIssue(
                    'unsupported_action',
                    f'Unsupported action {rule.action.action_type}.',
                    'error', rule.key,
                ))
                continue
            try:
                validate_configuration(
                    rule.action.action_type, rule.action.parameters,
                )
                for condition in rule.conditions:
                    validate_condition({
                        'field_name': condition.field,
                        'operator': condition.operator,
                        'value': condition.value,
                    })
            except Exception as exc:
                report.add(ValidationIssue(
                    'invalid_configuration', str(exc), 'error', rule.key,
                ))
            dependencies[rule.key] = set(
                rule.action.parameters.get('depends_on') or ()
            )
            for dependency in dependencies[rule.key] - active_keys:
                report.add(ValidationIssue(
                    'inactive_reference',
                    f'References missing or inactive rule {dependency}.',
                    'error', rule.key,
                ))
            cls._validate_tiers(rule, report)
        cls._detect_conflicts(plan, report)
        cls._detect_cycles(dependencies, report)
        return report

    @staticmethod
    def _validate_tiers(rule, report):
        tiers = rule.action.parameters.get('tiers')
        if not isinstance(tiers, list):
            return
        intervals = []
        for tier in tiers:
            low = tier.get('start', tier.get('minimum_units'))
            high = tier.get('end', tier.get('maximum_units'))
            try:
                low = Decimal(str(low))
                high = None if high in (None, '') else Decimal(str(high))
            except (InvalidOperation, TypeError):
                continue
            if high is not None and high < low:
                report.add(ValidationIssue(
                    'invalid_threshold_range', 'Tier maximum is below its minimum.',
                    'error', rule.key,
                ))
            intervals.append((low, high))
        if intervals and all(high is None for _, high in intervals):
            return
        intervals.sort(key=lambda item: item[0])
        for previous, current in zip(intervals, intervals[1:]):
            if previous[1] is None or current[0] <= previous[1]:
                report.add(ValidationIssue(
                    'overlapping_thresholds', 'Tier thresholds overlap.',
                    'error', rule.key,
                ))

    @staticmethod
    def _detect_conflicts(plan, report):
        groups = {}
        for rule in plan.rules:
            if not rule.active:
                continue
            key = (
                rule.category, rule.scope, rule.priority,
                tuple((c.field, c.operator, repr(c.value)) for c in rule.conditions),
            )
            groups.setdefault(key, []).append(rule)
        for rules in groups.values():
            actions = {rule.action.to_dict().__repr__() for rule in rules}
            if len(rules) > 1 and len(actions) > 1:
                report.add(ValidationIssue(
                    'conflicting_rules',
                    'Rules with the same priority and conditions perform different actions.',
                    'error', rules[-1].key,
                ))

    @staticmethod
    def _detect_cycles(graph, report):
        visiting, visited = set(), set()

        def visit(node):
            if node in visiting:
                report.add(ValidationIssue(
                    'circular_dependency', f'Circular dependency includes {node}.',
                    'error', node,
                ))
                return
            if node in visited:
                return
            visiting.add(node)
            for child in graph.get(node, ()):
                visit(child)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)
