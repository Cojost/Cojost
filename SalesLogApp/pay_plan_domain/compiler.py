from __future__ import annotations

from dataclasses import dataclass, field

from .validation import PayPlanValidationService, ValidationIssue


@dataclass
class CompilationReport:
    fingerprint: str
    executable_rules: list[dict] = field(default_factory=list)
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    skipped_rules: list[dict] = field(default_factory=list)
    unsupported_clauses: list[str] = field(default_factory=list)

    @property
    def compiled_rule_count(self):
        return len(self.executable_rules)

    @property
    def statistics(self):
        return {
            'source_rule_count': (
                len(self.executable_rules) + len(self.skipped_rules)
            ),
            'compiled_rule_count': self.compiled_rule_count,
            'skipped_rule_count': len(self.skipped_rules),
            'error_count': len(self.errors),
            'warning_count': len(self.warnings),
        }


class PayPlanCompiler:
    """Deterministically compile canonical actions to the existing engine contract."""

    @classmethod
    def compile(cls, plan):
        validation = PayPlanValidationService.validate(plan)
        report = CompilationReport(
            fingerprint=plan.fingerprint,
            errors=list(validation.errors),
            warnings=list(validation.warnings),
            unsupported_clauses=list(plan.unsupported_clauses),
        )
        invalid_keys = {issue.rule_key for issue in validation.errors if issue.rule_key}
        ordered = sorted(
            plan.rules,
            key=lambda rule: (
                rule.priority, -len(rule.conditions), rule.key, rule.name,
            ),
        )
        for rule in ordered:
            if not rule.active:
                report.skipped_rules.append({
                    'key': rule.key, 'reason': 'inactive',
                })
                continue
            if rule.key in invalid_keys:
                report.skipped_rules.append({
                    'key': rule.key, 'reason': 'validation_error',
                })
                continue
            report.executable_rules.append({
                'key': rule.key,
                'name': rule.name,
                'category': rule.category,
                'rule_type': rule.action.action_type,
                'calculation_scope': rule.scope,
                'condition_group_operator': rule.condition_mode,
                'configuration': rule.action.parameters,
                'conditions': [{
                    'field_name': condition.field,
                    'operator': condition.operator,
                    'value': condition.value,
                } for condition in rule.conditions],
                'sort_order': rule.priority,
                'is_active': rule.active,
                'source_reference': rule.source_reference,
            })
        return report
