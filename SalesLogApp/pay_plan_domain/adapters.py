from __future__ import annotations

from .canonical import (
    CanonicalCondition, CanonicalPayPlan, CanonicalRule, PayPlanGeneral,
    RuleAction,
)
from ..commission_engine.validators import CATEGORY_FIELDS


class ImportDraftAdapter:
    """Convert every existing importer output into the canonical model."""

    @staticmethod
    def to_canonical(draft, *, version=None):
        general = PayPlanGeneral(
            name=draft.get('plan_name') or (
                version.pay_plan.name if version is not None else 'Untitled Pay Plan'
            ),
            version=version.version_name if version is not None else '',
            effective_date=version.effective_start_date if version is not None else None,
            expiration_date=version.effective_end_date if version is not None else None,
            dealership=version.pay_plan.dealership_name if version is not None else '',
            industry=version.pay_plan.industry.slug if version is not None else '',
            currency=draft.get('currency', 'USD'),
        )
        rules = []
        for index, candidate in enumerate(draft.get('rules') or [], 1):
            rule_type = candidate.get('rule_type', '')
            conditions = tuple(
                CanonicalCondition(
                    field=item['field_name'],
                    operator=item['operator'],
                    value=(
                        None if item['operator'] in {'is_true', 'is_false'}
                        else item.get('value')
                    ),
                )
                for item in candidate.get('conditions') or []
            )
            rules.append(CanonicalRule(
                key=candidate.get('key') or f'imported-{index:04d}',
                name=candidate.get('name') or f'Imported Rule {index}',
                category=candidate.get('category') or CATEGORY_FIELDS.get(
                    rule_type, 'adjustment',
                ),
                scope=candidate.get('calculation_scope', 'per_sale'),
                action=RuleAction(
                    action_type=rule_type,
                    parameters=dict(candidate.get('configuration') or {}),
                ),
                conditions=conditions,
                condition_mode=candidate.get('condition_group_operator', 'all'),
                priority=int(candidate.get('sort_order', index)),
                active=bool(candidate.get('is_active', True)),
                source_reference=dict(candidate.get('source_reference') or {}),
            ))
        return CanonicalPayPlan(
            general=general,
            rules=tuple(rules),
            source_type=draft.get('source', 'manual'),
            source_metadata={
                'parser_profile': draft.get('parser_profile', ''),
                'parser_version': draft.get('parser_version', ''),
                'confidence': draft.get('confidence'),
            },
            unsupported_clauses=tuple(draft.get('unrecognized_sections') or ()),
        )


class VersionAdapter:
    """Read a stored version back into the same canonical representation."""

    @staticmethod
    def to_canonical(version):
        draft = {
            'plan_name': version.pay_plan.name,
            'source': version.source_type,
            'rules': [{
                'key': str(rule.semantic_key),
                'name': rule.name,
                'rule_type': rule.rule_type,
                'calculation_scope': rule.calculation_scope,
                'condition_group_operator': rule.condition_group_operator,
                'configuration': rule.configuration,
                'conditions': [
                    {
                        'field_name': condition.field_name,
                        'operator': condition.operator,
                        'value': condition.value,
                    }
                    for condition in rule.conditions.all().order_by('sort_order', 'id')
                ],
                'sort_order': rule.sort_order,
                'is_active': rule.is_active,
            } for rule in version.rules.prefetch_related('conditions').order_by(
                'sort_order', 'id',
            )],
        }
        return ImportDraftAdapter.to_canonical(draft, version=version)
