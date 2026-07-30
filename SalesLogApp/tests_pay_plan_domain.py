from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from .commission_engine.engine import calculate_sale_commission
from .models import PayPlanConversation, PayPlanRule, PayPlanVersion
from .models.sales import Sale
from .pay_plan_domain.adapters import ImportDraftAdapter
from .pay_plan_domain.canonical import (
    CanonicalCondition, CanonicalPayPlan, CanonicalRule, PayPlanGeneral,
    RuleAction,
)
from .pay_plan_domain.compiler import PayPlanCompiler
from .pay_plan_domain.services import (
    ExplanationBuilder, ImmutableVersionService, RuleMatcher,
)
from .pay_plan_domain.validation import PayPlanValidationService
from .pay_plan_imports import apply_import_draft_to_version


def canonical_rule(key='front', rate='0.18', priority=1, condition='new'):
    conditions = () if condition is None else (
        CanonicalCondition('vehicle_condition', 'equals', condition),
    )
    return CanonicalRule(
        key=key, name=key.title(), category='front_end', scope='per_sale',
        action=RuleAction('front_gross_percentage', {
            'rate': rate, 'gross_field': 'front_end_gross',
        }),
        conditions=conditions, priority=priority,
    )


class CanonicalPayPlanFoundationTests(TestCase):
    def setUp(self):
        self.plan = CanonicalPayPlan(
            general=PayPlanGeneral(
                name='Canonical Plan', effective_date=date(2026, 7, 1),
                industry='automotive',
            ),
            rules=(
                canonical_rule('used', '0.25', 2, 'used'),
                canonical_rule('new', '0.18', 1, 'new'),
            ),
            source_type='api',
        )

    def test_compilation_is_deterministic(self):
        first = PayPlanCompiler.compile(self.plan)
        second = PayPlanCompiler.compile(self.plan)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.executable_rules, second.executable_rules)
        self.assertEqual(
            [rule['key'] for rule in first.executable_rules], ['new', 'used'],
        )

    def test_equivalent_imports_compile_identically(self):
        draft = {
            'plan_name': 'Plan', 'source': 'upload',
            'rules': [{
                'name': 'New', 'rule_type': 'front_gross_percentage',
                'calculation_scope': 'per_sale',
                'configuration': {
                    'gross_field': 'front_end_gross', 'rate': '0.18',
                },
                'conditions': [{
                    'field_name': 'vehicle_condition',
                    'operator': 'equals', 'value': 'new',
                }],
            }],
        }
        first = ImportDraftAdapter.to_canonical(draft)
        second = ImportDraftAdapter.to_canonical(draft)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(
            PayPlanCompiler.compile(first).executable_rules,
            PayPlanCompiler.compile(second).executable_rules,
        )

    def test_validation_detects_conflict_overlap_and_cycle(self):
        conflicting = CanonicalRule(
            **{
                **canonical_rule('conflict', '0.20', 1, 'new').__dict__,
                'key': 'conflict',
            }
        )
        tiered = CanonicalRule(
            key='tiers', name='Tiers', category='front_end', scope='per_sale',
            action=RuleAction('progressive_unit_position_percentage', {
                'gross_field': 'front_end_gross', 'pack_amount': '0',
                'unit_filter': {}, 'non_retroactive': True,
                'tiers': [
                    {'start': '0', 'end': '5', 'rate': '0.20'},
                    {'start': '5', 'end': '10', 'rate': '0.25'},
                ],
            }),
        )
        cycle_a = CanonicalRule(
            key='a', name='A', category='flat', scope='per_sale',
            action=RuleAction('flat_per_deal', {
                'amount': '1', 'depends_on': ['b'],
            }),
        )
        cycle_b = CanonicalRule(
            key='b', name='B', category='flat', scope='per_sale',
            action=RuleAction('flat_per_deal', {
                'amount': '1', 'depends_on': ['a'],
            }),
        )
        plan = CanonicalPayPlan(
            general=self.plan.general,
            rules=(canonical_rule('new'), conflicting, tiered, cycle_a, cycle_b),
        )
        codes = {
            issue.code for issue in PayPlanValidationService.validate(plan).errors
        }
        self.assertIn('conflicting_rules', codes)
        self.assertIn('overlapping_thresholds', codes)
        self.assertIn('circular_dependency', codes)

    def test_condition_matching_is_consistent_and_specific(self):
        result = RuleMatcher.match(
            (canonical_rule('fallback', priority=1, condition=None),
             canonical_rule('specific', priority=1, condition='new')),
            {'vehicle_condition': 'New'},
        )
        self.assertEqual(result['selected']['rule_key'], 'specific')
        self.assertTrue(result['matched'][0]['conditions'][0]['satisfied'])


class CanonicalStorageAndExplanationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='canonical-owner', password='test',
        )
        self.assignment = self.user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan'
        ).get()
        self.version = self.assignment.pay_plan_version
        self.version.status = PayPlanVersion.REVIEW_REQUIRED
        self.version.save(update_fields=['status'])

    def test_import_stores_canonical_snapshot_and_report(self):
        draft = {
            'plan_name': self.version.pay_plan.name, 'source': 'manual',
            'rules': [{
                'name': 'Front', 'rule_type': 'front_gross_percentage',
                'calculation_scope': 'per_sale',
                'configuration': {
                    'rate': '0.10', 'gross_field': 'front_end_gross',
                },
                'conditions': [], 'is_active': True,
            }],
        }
        report = apply_import_draft_to_version(self.version, draft)
        self.version.refresh_from_db()
        self.assertEqual(report['created_rules'], 1)
        self.assertEqual(
            self.version.canonical_fingerprint, report['canonical_fingerprint'],
        )
        self.assertEqual(
            self.version.compilation_report['statistics']['compiled_rule_count'], 1,
        )

    def test_historical_version_mutation_is_rejected_by_service(self):
        self.version.status = PayPlanVersion.ACTIVE
        with self.assertRaises(ValidationError):
            ImmutableVersionService.assert_mutable(self.version)

    def test_explanation_data_is_reproducible(self):
        PayPlanRule.objects.create(
            pay_plan_version=self.version, name='Front',
            rule_type='front_gross_percentage', calculation_scope='per_sale',
            configuration={'rate': '0.10', 'gross_field': 'front_end_gross'},
        )
        self.version.status = PayPlanVersion.ACTIVE
        self.version.save(update_fields=['status'])
        sale = Sale.objects.create(
            user=self.user, customer='Customer', dealNumber=99881,
            count=Decimal('1'), frontEnd=Decimal('1000'), backend=Decimal('0'),
            date=self.assignment.effective_start_date,
        )
        result = calculate_sale_commission(self.user, sale)
        self.assertEqual(
            ExplanationBuilder.from_calculation(result),
            ExplanationBuilder.from_calculation(result),
        )
        self.assertEqual(
            ExplanationBuilder.from_calculation(result)['rules'][0]['amount'],
            '100.00',
        )

    def test_conversation_memory_is_user_and_plan_scoped(self):
        conversation = PayPlanConversation(
            user=self.user, plan_version=self.version,
            conversation_key='used-rate-change',
            selected_rule_key='used-after-ten',
            pending_intent={'intent_type': 'update_rule'},
        )
        conversation.full_clean()
        conversation.save()
        other = get_user_model().objects.create_user(
            username='canonical-other', password='test',
        )
        conversation.user = other
        with self.assertRaises(ValidationError):
            conversation.full_clean()
