from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .access import get_commission_system
from .commission_engine.engine import calculate_sale_commission
from .forms import SandboxRuleForm
from .models import (
    CommissionSandbox, PayPlanActivationEvent, PayPlanRule, PayPlanVersion,
    Sale, SandboxHypotheticalDeal, SandboxResult, SandboxRun, UserProfile,
)
from .sandbox_services import (
    SandboxActivationService, SandboxManager, SandboxRuleEditor,
    ComparisonEngine, ScenarioRunner,
)


class CommissionSandboxTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='sandbox-owner', password='test-password',
        )
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system'])
        self.assignment = self.user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan'
        ).get()
        self.source = self.assignment.pay_plan_version
        self.source.rules.all().delete()
        self.source_rule = PayPlanRule.objects.create(
            pay_plan_version=self.source,
            name='Front 10%',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.10', 'gross_field': 'front_end_gross'},
            sort_order=1,
        )
        self.sale = Sale.objects.create(
            user=self.user, customer='Historical', dealNumber=771001,
            count=Decimal('1'), frontEnd=Decimal('1000'), backend=Decimal('0'),
            date=self.assignment.effective_start_date,
            vehicle_condition='new',
        )
        self.sandbox = SandboxManager.create(
            self.user, self.source, 'Promotion Offer', 'Test 20 percent.',
        )

    def set_sandbox_rate(self, rate):
        rule = self.sandbox.draft_version.rules.get(name='Front 10%')
        SandboxRuleEditor.save(self.sandbox, rule=rule, data={
            'name': rule.name,
            'rule_type': rule.rule_type,
            'calculation_scope': rule.calculation_scope,
            'configuration': {
                'rate': str(rate), 'gross_field': 'front_end_gross',
            },
            'conditions': [],
            'is_active': True,
            'sort_order': 1,
        })
        self.sandbox.refresh_from_db()
        return rule

    def test_rule_edits_and_sessions_are_isolated(self):
        source_snapshot = dict(self.source_rule.configuration)
        self.set_sandbox_rate('0.20')
        self.source_rule.refresh_from_db()
        self.assertEqual(self.source_rule.configuration, source_snapshot)
        self.assertEqual(self.assignment.pay_plan_version, self.source)
        second = SandboxManager.create(
            self.user, self.source, 'Second experiment',
        )
        self.assertEqual(
            second.draft_version.rules.get().configuration['rate'], '0.10',
        )
        self.assertNotEqual(
            second.draft_version.rules.get().pk,
            self.sandbox.draft_version.rules.get().pk,
        )

    def test_duplicate_toggle_move_and_delete_are_sandbox_only(self):
        original = self.sandbox.draft_version.rules.get()
        duplicate = SandboxRuleEditor.duplicate(self.sandbox, original.pk)
        self.assertNotEqual(duplicate.pk, original.pk)
        SandboxRuleEditor.toggle(self.sandbox, duplicate.pk)
        duplicate.refresh_from_db()
        self.assertFalse(duplicate.is_active)
        old_priority = duplicate.sort_order
        SandboxRuleEditor.move(self.sandbox, duplicate.pk, 'up')
        duplicate.refresh_from_db()
        self.assertLessEqual(duplicate.sort_order, old_priority)
        SandboxRuleEditor.delete(self.sandbox, duplicate.pk)
        self.assertFalse(PayPlanRule.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(PayPlanRule.objects.filter(pk=self.source_rule.pk).exists())

    def test_disabled_condition_is_preserved_but_not_executed(self):
        rule = self.sandbox.draft_version.rules.get()
        SandboxRuleEditor.save(self.sandbox, rule=rule, data={
            'name': rule.name,
            'rule_type': rule.rule_type,
            'calculation_scope': rule.calculation_scope,
            'configuration': {
                'rate': '0.20', 'gross_field': 'front_end_gross',
            },
            'conditions': [{
                'field_name': 'vehicle_condition',
                'operator': 'equals',
                'value': 'used',
                'enabled': False,
            }],
            'is_active': True,
            'sort_order': 1,
        })
        rule.refresh_from_db()
        self.assertFalse(rule.conditions.exists())
        self.assertEqual(
            rule.configuration['_sandbox_disabled_conditions'],
            [{
                'field_name': 'vehicle_condition',
                'operator': 'equals',
                'value': 'used',
            }],
        )
        form = SandboxRuleForm(rule=rule)
        self.assertFalse(form.initial['conditions'][0]['enabled'])

        duplicate = SandboxRuleEditor.duplicate(self.sandbox, rule.pk)
        self.assertFalse(duplicate.conditions.exists())
        self.assertIn(
            '_sandbox_disabled_conditions', duplicate.configuration,
        )
        SandboxRuleEditor.delete(self.sandbox, duplicate.pk)
        run = ScenarioRunner.run(
            self.user, self.sandbox, mode=SandboxRun.REPLAY,
            period_start=self.sale.date, period_end=self.sale.date,
        )
        self.assertEqual(run.results.get().sandbox_commission, Decimal('200.00'))

    def test_historical_replay_uses_sandbox_rules_and_caches(self):
        self.set_sandbox_rate('0.20')
        run = ScenarioRunner.run(
            self.user, self.sandbox, mode=SandboxRun.REPLAY,
            period_start=self.sale.date, period_end=self.sale.date,
        )
        self.assertEqual(run.actual_total, Decimal('100.00'))
        self.assertEqual(run.sandbox_total, Decimal('200.00'))
        self.assertEqual(run.difference, Decimal('100.00'))
        result = run.results.get()
        self.assertEqual(result.percent_change, Decimal('100.0000'))
        self.assertEqual(result.comparison, SandboxResult.HIGHER)
        cached = ScenarioRunner.run(
            self.user, self.sandbox, mode=SandboxRun.REPLAY,
            period_start=self.sale.date, period_end=self.sale.date,
        )
        self.assertEqual(cached.pk, run.pk)

    def test_hypothetical_deal_never_creates_or_changes_sale(self):
        production_count = Sale.objects.count()
        hypothetical = SandboxHypotheticalDeal.objects.create(
            sandbox=self.sandbox, label='Future Outback', customer='Scenario',
            dealNumber=self.sale.dealNumber, count=Decimal('1'),
            split_with_name='', frontEnd=Decimal('2400'),
            backend=Decimal('1100'), date=self.sale.date,
            vehicle_condition='new',
        )
        self.set_sandbox_rate('0.20')
        run = ScenarioRunner.run(
            self.user, self.sandbox, mode=SandboxRun.PROJECTION,
        )
        self.assertEqual(Sale.objects.count(), production_count)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.frontEnd, Decimal('1000'))
        result = run.results.get(hypothetical_deal=hypothetical)
        self.assertEqual(result.actual_commission, Decimal('0'))
        self.assertEqual(result.sandbox_commission, Decimal('480.00'))
        second = SandboxManager.create(self.user, self.source, 'Second')
        SandboxHypotheticalDeal.objects.create(
            sandbox=second, label='Same number', customer='Scenario',
            dealNumber=self.sale.dealNumber, count=Decimal('1'),
            split_with_name='', frontEnd=Decimal('1'), backend=Decimal('0'),
            date=self.sale.date, vehicle_condition='used',
        )

    def test_hypothetical_half_deal_uses_half_commission(self):
        SandboxHypotheticalDeal.objects.create(
            sandbox=self.sandbox, label='Half scenario', customer='Scenario',
            dealNumber=771099, count=Decimal('0.5'),
            split_with_name='Scenario Partner',
            frontEnd=Decimal('1000'), backend=Decimal('0'),
            date=self.sale.date, vehicle_condition='new',
        )
        self.set_sandbox_rate('0.20')
        run = ScenarioRunner.run(
            self.user, self.sandbox, mode=SandboxRun.PROJECTION,
        )
        result = run.results.get()
        self.assertEqual(result.sandbox_commission, Decimal('100.00'))
        rules = result.explanation['rules']
        self.assertEqual(rules[0]['metadata']['pre_split_amount'], '200.00')
        self.assertEqual(
            rules[0]['metadata']['commission_credit_multiplier'], '0.5',
        )

    def test_monthly_replay_does_not_combine_period_bonus_thresholds(self):
        PayPlanRule.objects.create(
            pay_plan_version=self.sandbox.draft_version,
            name='Ten Unit Bonus', rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [{'minimum_units': '10', 'amount': '500'}],
                'tier_mode': 'highest_only',
            },
            sort_order=2,
        )
        SandboxRuleEditor.toggle(
            self.sandbox,
            self.sandbox.draft_version.rules.get(name='Ten Unit Bonus').pk,
        )
        SandboxRuleEditor.toggle(
            self.sandbox,
            self.sandbox.draft_version.rules.get(name='Ten Unit Bonus').pk,
        )
        january = date(2026, 1, 10)
        february = date(2026, 2, 10)
        self.assignment.effective_start_date = january
        self.assignment.save(update_fields=['effective_start_date'])
        self.source.effective_start_date = january
        self.source.save(update_fields=['effective_start_date'])
        self.sandbox.draft_version.effective_start_date = january
        self.sandbox.draft_version.save(update_fields=['effective_start_date'])
        self.sale.date = january
        self.sale.count = Decimal('8')
        self.sale.save(update_fields=['date', 'count'])
        Sale.objects.create(
            user=self.user, customer='February', dealNumber=771002,
            count=Decimal('8'), frontEnd=Decimal('1000'), backend=Decimal('0'),
            date=february, vehicle_condition='new',
        )
        run = ScenarioRunner.run(
            self.user, self.sandbox, mode=SandboxRun.REPLAY,
            period_start=january, period_end=february,
        )
        self.assertEqual(run.statistics['sandbox_period_bonus'], '0')
        self.assertEqual(run.sandbox_total, Decimal('200.00'))

    def test_other_user_cannot_access_or_edit_sandbox(self):
        other = get_user_model().objects.create_user(
            username='sandbox-intruder', password='test-password',
        )
        with self.assertRaises(PermissionDenied):
            SandboxManager.get_for_user(other, self.sandbox.public_id)
        self.client.login(username='sandbox-intruder', password='test-password')
        response = self.client.get(reverse(
            'commission_sandbox_detail', args=[self.sandbox.public_id],
        ))
        self.assertEqual(response.status_code, 404)

    def test_comparison_handles_lower_unchanged_and_zero_baseline(self):
        self.assertEqual(
            ComparisonEngine.compare('100', '50')['comparison'],
            SandboxResult.LOWER,
        )
        self.assertEqual(
            ComparisonEngine.compare('100', '100')['comparison'],
            SandboxResult.UNCHANGED,
        )
        zero = ComparisonEngine.compare('0', '50')
        self.assertIsNone(zero['percent_change'])

    def test_owner_can_open_sandbox_pages(self):
        self.client.login(username='sandbox-owner', password='test-password')
        index = self.client.get(reverse('commission_sandbox_index'))
        detail = self.client.get(reverse(
            'commission_sandbox_detail', args=[self.sandbox.public_id],
        ))
        self.assertEqual(index.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Historical replay')
        self.assertContains(detail, 'Hypothetical deals')
        self.assertContains(detail, 'Create Pay Plan Draft')
        self.assertNotContains(detail, 'Activate Sandbox')

    def test_owner_can_compare_multiple_sandboxes_over_same_range(self):
        second = SandboxManager.create(
            self.user, self.source, 'Manager Proposal',
        )
        self.set_sandbox_rate('0.20')
        second_rule = second.draft_version.rules.get()
        SandboxRuleEditor.save(second, rule=second_rule, data={
            'name': second_rule.name,
            'rule_type': second_rule.rule_type,
            'calculation_scope': second_rule.calculation_scope,
            'configuration': {
                'rate': '0.30', 'gross_field': 'front_end_gross',
            },
            'conditions': [],
            'is_active': True,
            'sort_order': 1,
        })
        self.client.login(username='sandbox-owner', password='test-password')
        response = self.client.post(reverse('commission_sandbox_compare'), {
            'sandboxes': [self.sandbox.pk, second.pk],
            'preset': 'custom',
            'start_date': self.sale.date,
            'end_date': self.sale.date,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Promotion Offer')
        self.assertContains(response, 'Manager Proposal')
        self.assertContains(response, '$200.00')
        self.assertContains(response, '$300.00')

    def test_archived_sandbox_cannot_be_mutated_by_direct_post(self):
        rule = self.sandbox.draft_version.rules.get()
        SandboxManager.archive(self.user, self.sandbox)
        with self.assertRaises(ValidationError):
            SandboxRuleEditor.toggle(self.sandbox, rule.pk)
        self.client.login(username='sandbox-owner', password='test-password')
        before = self.sandbox.hypothetical_deals.count()
        response = self.client.post(reverse(
            'commission_sandbox_hypothetical',
            args=[self.sandbox.public_id],
        ), {
            'label': 'Blocked', 'customer': 'Blocked', 'dealNumber': 99101,
            'date': self.sale.date, 'frontEnd': '1000', 'backend': '0',
            'count': '1', 'vehicle_condition': 'new',
            'acquisition_source': '',
        })
        self.assertRedirects(response, reverse(
            'commission_sandbox_detail', args=[self.sandbox.public_id],
        ))
        self.assertEqual(self.sandbox.hypothetical_deals.count(), before)

    def test_deleting_sandbox_preserves_production(self):
        source_id = self.source.pk
        rule_id = self.source_rule.pk
        sale_id = self.sale.pk
        SandboxManager.delete(self.user, self.sandbox)
        self.assertTrue(PayPlanVersion.objects.filter(pk=source_id).exists())
        self.assertTrue(PayPlanRule.objects.filter(pk=rule_id).exists())
        self.assertTrue(Sale.objects.filter(pk=sale_id).exists())
        self.assertFalse(
            CommissionSandbox.objects.filter(pk=self.sandbox.pk).exists()
        )

    def test_activation_creates_fresh_version_and_preserves_history(self):
        self.set_sandbox_rate('0.20')
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.sale.date = timezone.localdate()
        self.sale.save(update_fields=['date'])
        self.assignment.effective_start_date = timezone.localdate()
        self.assignment.save(update_fields=['effective_start_date'])
        self.source.effective_start_date = timezone.localdate()
        self.source.save(update_fields=['effective_start_date'])
        version_count = PayPlanVersion.objects.filter(is_sandbox=False).count()
        result = SandboxActivationService.activate(
            self.user, self.sandbox,
            effective_start_date=tomorrow, confirmed=True,
        )
        activated = result['version']
        self.assertFalse(activated.is_sandbox)
        self.assertEqual(
            PayPlanVersion.objects.filter(is_sandbox=False).count(),
            version_count + 1,
        )
        self.assertEqual(activated.rules.get().configuration['rate'], '0.20')
        self.assertEqual(
            calculate_sale_commission(self.user, self.sale).total,
            Decimal('100.00'),
        )
        future = Sale.objects.create(
            user=self.user, customer='Future', dealNumber=771003,
            count=Decimal('1'), frontEnd=Decimal('1000'), backend=Decimal('0'),
            date=tomorrow, vehicle_condition='new',
        )
        self.assertEqual(
            calculate_sale_commission(self.user, future).total,
            Decimal('200.00'),
        )
        self.sandbox.refresh_from_db()
        self.assertEqual(self.sandbox.status, CommissionSandbox.ARCHIVED)
        self.assertTrue(PayPlanActivationEvent.objects.filter(
            version=activated,
        ).exists())

    def test_conflicting_sandbox_cannot_activate(self):
        PayPlanRule.objects.create(
            pay_plan_version=self.sandbox.draft_version,
            name='Conflicting Front 30%',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.30', 'gross_field': 'front_end_gross'},
            sort_order=1,
        )
        production_count = PayPlanVersion.objects.filter(is_sandbox=False).count()
        with self.assertRaises(ValidationError):
            SandboxActivationService.activate(
                self.user, self.sandbox,
                effective_start_date=timezone.localdate() + timedelta(days=1),
                confirmed=True,
            )
        self.assertEqual(
            PayPlanVersion.objects.filter(is_sandbox=False).count(),
            production_count,
        )
        self.assertEqual(
            self.user.pay_plan_assignments.filter(is_active=True).count(), 1,
        )

    def test_thousand_sale_replay_is_deterministic_and_cached(self):
        base = self.sale.date
        Sale.objects.bulk_create([
            Sale(
                user=self.user, customer=f'Bulk {index}',
                dealNumber=772000 + index, count=Decimal('1'),
                frontEnd=Decimal('100'), backend=Decimal('0'), date=base,
                vehicle_condition='new',
            )
            for index in range(1000)
        ])
        self.set_sandbox_rate('0.20')
        run = ScenarioRunner.run(
            self.user, self.sandbox, mode=SandboxRun.REPLAY,
            period_start=base, period_end=base,
        )
        self.assertEqual(run.statistics['sales_tested'], 1001)
        self.assertEqual(run.sandbox_total, Decimal('20200.00'))
        self.assertEqual(
            ScenarioRunner.run(
                self.user, self.sandbox, mode=SandboxRun.REPLAY,
                period_start=base, period_end=base,
            ).pk,
            run.pk,
        )
