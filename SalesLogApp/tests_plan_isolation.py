from datetime import date
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .commission_engine.exceptions import PayPlanResolutionError
from .commission_engine.engine import resolve_pay_plan_version
from .commission_engine.engine import calculate_sale_commission_for_version
from .models import (
    PayPlanAssignment,
    PayPlanRule,
    PayPlanRuleCondition,
    Sale,
    UserProfile,
)
from .pay_plan_imports import parse_description_to_import_draft
from .pay_plan_management import create_manual_draft
from .plan_requirements import ActivePayPlanService, PlanRequirementService


class MultiUserPlanIsolationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.itsumi = User.objects.create_user(
            username='isolation-itsumi', password='test-password',
        )
        self.michaels = User.objects.create_user(
            username='isolation-michaels', password='test-password',
        )
        for user in (self.itsumi, self.michaels):
            profile = user.sales_profile
            profile.commission_system = UserProfile.PAY_PLAN_V2
            profile.save(update_fields=['commission_system', 'updated_at'])
            assignment = user.pay_plan_assignments.select_related(
                'pay_plan_version__pay_plan',
            ).get()
            period_start = date.today().replace(day=1)
            assignment.effective_start_date = period_start
            assignment.pay_plan_version.effective_start_date = period_start
            assignment.pay_plan_version.save(
                update_fields=['effective_start_date', 'updated_at'],
            )
            assignment.save(
                update_fields=['effective_start_date', 'updated_at'],
            )
            onboarding = user.pay_plan_onboarding
            onboarding.current_pay_plan = assignment.pay_plan_version.pay_plan
            onboarding.current_version = assignment.pay_plan_version
            onboarding.status = onboarding.ACTIVE
            onboarding.save(update_fields=[
                'current_pay_plan', 'current_version', 'status', 'updated_at',
            ])
        self.itsumi_version = (
            self.itsumi.pay_plan_assignments.get().pay_plan_version
        )
        self.michaels_version = (
            self.michaels.pay_plan_assignments.get().pay_plan_version
        )
        self._add_requirement_rule(
            self.itsumi_version, 'NPS Finance Requirement',
            'nps_finance_eligible', 1,
        )
        self._add_requirement_rule(
            self.itsumi_version, 'Green Pea Requirement',
            'green_pea', 2,
        )
        self._add_requirement_rule(
            self.itsumi_version, 'AR Requirement',
            'ar_requirement_met', 3,
        )
        PayPlanRule.objects.create(
            pay_plan_version=self.michaels_version,
            name='Michaels Front Commission',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.25', 'gross_field': 'front_end_gross'},
            sort_order=1,
        )

    @staticmethod
    def _add_requirement_rule(version, name, field_name, order):
        rule = PayPlanRule.objects.create(
            pay_plan_version=version,
            name=name,
            rule_type='flat_per_deal',
            calculation_scope='per_sale',
            configuration={'amount': '10.00'},
            sort_order=order,
        )
        PayPlanRuleCondition.objects.create(
            rule=rule, field_name=field_name,
            operator='is_true', value=True,
        )

    def test_requirement_service_is_scoped_to_user_active_version(self):
        itsumi = PlanRequirementService.get_for_user(self.itsumi)
        michaels = PlanRequirementService.get_for_user(self.michaels)
        self.assertIsNotNone(itsumi['nps'])
        self.assertIsNotNone(itsumi['ar'])
        self.assertIsNotNone(itsumi['green_pea'])
        self.assertIsNone(michaels['nps'])
        self.assertIsNone(michaels['ar'])
        self.assertIsNone(michaels['green_pea'])
        self.assertEqual(michaels['version_id'], self.michaels_version.id)

    def test_michaels_commission_and_sales_pages_hide_itsumi_requirements(self):
        self.client.login(
            username=self.michaels.username, password='test-password',
        )
        commission = self.client.get(reverse('view_commission'))
        self.assertEqual(commission.status_code, 200)
        self.assertNotContains(commission, 'NPS')
        sales = self.client.get(reverse('view_sales'))
        self.assertEqual(sales.status_code, 200)
        self.assertNotContains(sales, 'NPS Survey Projection')
        for response in (commission, sales):
            self.assertNotContains(response, 'Green Pea')
            self.assertNotContains(response, 'AR Requirement')
        response = self.client.get(reverse('pay_plan_eligibility'))
        self.assertRedirects(response, reverse('view_commission'))

    def test_itsumi_still_sees_only_her_plan_backed_requirements(self):
        self.client.login(
            username=self.itsumi.username, password='test-password',
        )
        commission = self.client.get(reverse('view_commission'))
        self.assertContains(commission, 'Update monthly eligibility')
        eligibility = self.client.get(reverse('pay_plan_eligibility'))
        self.assertContains(eligibility, 'NPS')
        self.assertContains(eligibility, 'Green Pea')

    def test_switching_authenticated_users_does_not_preserve_requirements(self):
        self.client.login(
            username=self.itsumi.username, password='test-password',
        )
        self.assertContains(
            self.client.get(reverse('pay_plan_eligibility')), 'NPS',
        )
        self.client.logout()
        self.client.login(
            username=self.michaels.username, password='test-password',
        )
        response = self.client.get(reverse('view_commission'))
        self.assertNotContains(response, 'NPS')
        self.assertNotContains(response, 'Green Pea')

    def test_new_user_shell_contains_no_assumed_requirements(self):
        user = get_user_model().objects.create_user(
            username='blank-plan-user', password='test-password',
        )
        version = user.pay_plan_assignments.get().pay_plan_version
        self.assertFalse(version.rules.exists())
        requirements = PlanRequirementService.get_for_user(user)
        self.assertFalse(requirements['has_monthly_requirements'])

    def test_supported_fields_do_not_become_active_requirements(self):
        requirements = PlanRequirementService.get_for_user(self.michaels)
        self.assertFalse(requirements['has_monthly_requirements'])

    def test_parser_uses_fresh_state_between_users(self):
        itsumi_draft = parse_description_to_import_draft(
            '5% of F&I gross. NPS finance gross portion must qualify.',
            'Itsumi',
        )
        michaels_draft = parse_description_to_import_draft(
            '25% of front-end gross.',
            'Michaels',
        )
        self.assertTrue(any(
            condition['field_name'] == 'nps_finance_eligible'
            for rule in itsumi_draft['rules']
            for condition in rule['conditions']
        ))
        self.assertFalse(any(
            condition['field_name'] == 'nps_finance_eligible'
            for rule in michaels_draft['rules']
            for condition in rule['conditions']
        ))

    def test_manual_clone_uses_only_same_users_active_version(self):
        draft = create_manual_draft(
            self.michaels, date.today().replace(day=1),
        )
        self.assertEqual(draft.pay_plan.owner_user, self.michaels)
        self.assertEqual(
            list(draft.rules.values_list('name', flat=True)),
            ['Michaels Front Commission'],
        )

    def test_cross_owner_assignment_is_rejected_by_active_lookup_and_engine(self):
        self.michaels.pay_plan_assignments.all().delete()
        PayPlanAssignment.objects.create(
            user=self.michaels,
            pay_plan_version=self.itsumi_version,
            effective_start_date=date.today(),
        )
        active = ActivePayPlanService.get_for_user(self.michaels)
        self.assertEqual(active.status, 'ownership_error')
        with self.assertRaises(PayPlanResolutionError):
            resolve_pay_plan_version(self.michaels, date.today())

    def test_isolation_command_reports_no_cross_owner_records(self):
        output = StringIO()
        call_command(
            'inspect_plan_isolation', self.michaels.username, stdout=output,
        )
        report = output.getvalue()
        self.assertIn('nps: none', report)
        self.assertIn('green_pea: none', report)
        self.assertIn('Cross-owner assignments: none', report)
        self.assertIn('Cached requirement keys: none', report)

    def test_calculation_rejects_another_users_explicit_version(self):
        sale = Sale.objects.create(
            user=self.michaels,
            customer='Michaels Buyer',
            dealNumber=99001,
            count='1.0',
            frontEnd='1000.00',
            backend='100.00',
            date=self.michaels_version.effective_start_date,
        )

        with self.assertRaises(PayPlanResolutionError):
            calculate_sale_commission_for_version(
                self.michaels, sale, self.itsumi_version,
            )

    def test_period_rule_rejects_per_sale_condition_at_model_boundary(self):
        rule = PayPlanRule.objects.create(
            pay_plan_version=self.michaels_version,
            name='Monthly New Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [{'minimum_units': '5', 'amount': '500'}],
                'tier_mode': 'highest_only',
                'unit_metric': 'monthly_new_units',
            },
        )
        condition = PayPlanRuleCondition(
            rule=rule,
            field_name='vehicle_condition',
            operator='equals',
            value='new',
        )

        with self.assertRaises(ValidationError):
            condition.full_clean()

    def test_deployment_isolation_audit_passes_for_clean_users(self):
        output = StringIO()

        call_command(
            'audit_plan_isolation', self.michaels.username, stdout=output,
        )

        self.assertIn('Pay-plan isolation audit passed.', output.getvalue())

    def test_deployment_isolation_audit_fails_for_cross_owner_assignment(self):
        assignment = self.michaels.pay_plan_assignments.get()
        assignment.pay_plan_version = self.itsumi_version
        assignment.save(update_fields=['pay_plan_version', 'updated_at'])

        with self.assertRaises(CommandError):
            call_command('audit_plan_isolation', self.michaels.username)
