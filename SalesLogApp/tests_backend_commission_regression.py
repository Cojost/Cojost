from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .commission_engine.validators import normalize_percentage_rate
from .commission_service import (
    COMPONENT_DEFAULT_FALLBACK,
    COMPONENT_MATCHED_RULE,
    COMPONENT_MISSING_CONFIGURATION,
    COMPONENT_NOT_APPLICABLE,
    CommissionEngineService,
    STATUS_PARTIAL,
)
from .models import (
    Commission,
    PayPlanRule,
    PayPlanRuleCondition,
    Sale,
    UserProfile,
)
from .pay_plan_imports import parse_description_to_import_draft


class BackendCommissionRegressionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='backend-owner', password='test-password',
        )
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        self.assignment = self.user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan',
        ).get()
        self.version = self.assignment.pay_plan_version
        self.sale_date = self.assignment.effective_start_date
        onboarding = self.user.pay_plan_onboarding
        onboarding.current_pay_plan = self.version.pay_plan
        onboarding.current_version = self.version
        onboarding.status = onboarding.ACTIVE
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        self.front_rule = self.rule(
            '25% Front', 'front_gross_percentage',
            {'rate': '0.25', 'gross_field': 'front_end_gross'}, 1,
        )
        self.back_rule = self.rule(
            '5% Backend', 'back_gross_percentage',
            {'rate': '0.05', 'gross_field': 'back_end_gross'}, 2,
        )
        self.sale = Sale.objects.create(
            user=self.user, customer='Backend Buyer', dealNumber=881001,
            count=Decimal('1.0'), frontEnd=Decimal('2500.00'),
            backend=Decimal('1200.00'), date=self.sale_date,
            vehicle_condition='new',
        )
        self.client.login(username='backend-owner', password='test-password')

    def rule(self, name, rule_type, configuration, order, active=True, scope='per_sale'):
        return PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name=name,
            rule_type=rule_type,
            calculation_scope=scope,
            configuration=configuration,
            is_active=active,
            sort_order=order,
        )

    def test_recorded_backend_gross_calculates_and_contributes_everywhere(self):
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.frontend_gross, Decimal('2500.00'))
        self.assertEqual(result.backend_gross, Decimal('1200.00'))
        self.assertEqual(result.frontend_commission, Decimal('625.00'))
        self.assertEqual(result.backend_commission, Decimal('60.00'))
        self.assertEqual(result.total_deal_commission, Decimal('685.00'))

        response = self.client.get(reverse('view_sales'))
        self.assertContains(response, '$685.00')
        self.assertContains(response, 'Recorded finance gross: $1200.00')
        self.assertContains(response, 'Finance commission: $60.00')
        self.assertNotContains(response, '<dt>Engine</dt>')
        self.assertContains(response, '5% Backend')

        summary = CommissionEngineService.calculate_sales(self.user, [self.sale])
        self.assertEqual(summary['total_back'], Decimal('60.00'))
        self.assertEqual(summary['total_commission'], Decimal('685.00'))

    def test_backend_only_and_flat_backend_plans(self):
        self.front_rule.delete()
        percentage = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(percentage.backend_commission, Decimal('60.00'))
        self.back_rule.delete()
        self.rule(
            'Funded Deal Backend', 'flat_backend_commission',
            {'amount': '100.00'}, 1,
        )
        flat = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(flat.backend_commission, Decimal('100.00'))

    def test_backend_minimum_and_maximum_apply_to_backend_component(self):
        self.rule(
            'Backend Minimum', 'minimum_commission',
            {'minimum_amount': '75', 'applies_to_categories': ['back_end']}, 3,
        )
        minimum = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(minimum.backend_commission, Decimal('75.00'))
        self.version.rules.filter(name='Backend Minimum').delete()
        self.rule(
            'Backend Maximum', 'maximum_commission',
            {'maximum_amount': '50', 'applies_to_categories': ['back_end']}, 3,
        )
        maximum = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(maximum.backend_commission, Decimal('50.00'))

    def test_new_and_used_backend_conditions(self):
        PayPlanRuleCondition.objects.create(
            rule=self.back_rule, field_name='vehicle_condition',
            operator='equals', value='new',
        )
        new_result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(new_result.backend_commission, Decimal('60.00'))
        self.sale.vehicle_condition = 'used'
        self.sale.save(update_fields=['vehicle_condition'])
        used_result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(used_result.status, STATUS_PARTIAL)
        self.assertEqual(
            used_result.backend_status, COMPONENT_MISSING_CONFIGURATION,
        )

    def test_percentage_normalization(self):
        self.assertEqual(normalize_percentage_rate('5'), Decimal('0.05'))
        self.assertEqual(normalize_percentage_rate('5%'), Decimal('0.05'))
        self.assertEqual(normalize_percentage_rate('0.05'), Decimal('0.05'))
        for value in ('5', '5%', '0.05'):
            self.back_rule.configuration['rate'] = value
            self.back_rule.save(update_fields=['configuration'])
            result = CommissionEngineService.calculate_sale(self.user, self.sale)
            self.assertEqual(result.backend_commission, Decimal('60.00'))

    def test_half_deal_halves_front_backend_and_total_commission(self):
        self.sale.count = Decimal('0.5')
        self.sale.save(update_fields=['count'])
        half = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(half.frontend_commission, Decimal('312.50'))
        self.assertEqual(half.backend_commission, Decimal('30.00'))
        self.assertEqual(half.total_deal_commission, Decimal('342.50'))
        self.assertIn('multiplier 0.5', ' '.join(half.backend_explanation))
        summary = CommissionEngineService.calculate_sales(
            self.user, [self.sale],
        )
        self.assertEqual(summary['total_front'], Decimal('312.50'))
        self.assertEqual(summary['total_back'], Decimal('30.00'))
        self.assertEqual(summary['total_commission'], Decimal('342.50'))
        response = self.client.get(reverse('view_sales'))
        self.assertContains(response, '$342.50')
        self.assertContains(response, '0.5 share')
        self.assertContains(response, 'deal-share multiplier 0.5')
        self.sale.count = Decimal('2.0')
        self.sale.save(update_fields=['count'])
        double_units = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(double_units.backend_commission, Decimal('60.00'))

    def test_half_deal_applies_minimum_before_split(self):
        self.sale.frontEnd = Decimal('100.00')
        self.sale.backend = Decimal('0.00')
        self.sale.count = Decimal('0.5')
        self.sale.save(update_fields=['frontEnd', 'backend', 'count'])
        self.rule(
            'Front Minimum', 'minimum_commission',
            {
                'minimum_amount': '100.00',
                'applies_to_categories': ['front_end'],
            },
            3,
        )
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.frontend_commission, Decimal('50.00'))
        self.assertEqual(result.total_deal_commission, Decimal('50.00'))

    def test_rule_can_explicitly_exclude_amount_from_deal_split(self):
        self.sale.count = Decimal('0.5')
        self.sale.save(update_fields=['count'])
        configuration = dict(self.back_rule.configuration)
        configuration['apply_commission_credit_multiplier'] = False
        self.back_rule.configuration = configuration
        self.back_rule.save(update_fields=['configuration'])
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.frontend_commission, Decimal('312.50'))
        self.assertEqual(result.backend_commission, Decimal('60.00'))

    def test_backend_failure_is_partial_and_does_not_erase_frontend(self):
        self.back_rule.is_active = False
        self.back_rule.save(update_fields=['is_active'])
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.status, STATUS_PARTIAL)
        self.assertEqual(result.frontend_commission, Decimal('625.00'))
        self.assertEqual(result.backend_commission, Decimal('0.00'))
        self.assertIn('backend', result.component_errors)

    def test_legacy_backend_opt_out_is_ignored_for_pay_plan_user(self):
        settings, _ = Commission.objects.get_or_create(user=self.user)
        settings.opt_out_back = True
        settings.save(update_fields=['opt_out_back'])
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.backend_commission, Decimal('60.00'))

    def test_zero_backend_is_a_valid_calculated_component(self):
        self.sale.backend = Decimal('0.00')
        self.sale.save(update_fields=['backend'])
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.backend_commission, Decimal('0.00'))
        self.assertEqual(result.backend_status, COMPONENT_NOT_APPLICABLE)

    def test_default_backend_percentage_without_backend_rules(self):
        self.back_rule.delete()
        self.sale.backend = Decimal('1000.00')
        self.sale.save(update_fields=['backend'])
        self.version.default_backend_percentage = Decimal('0.05')
        self.version.save(update_fields=['default_backend_percentage'])
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.backend_commission, Decimal('50.00'))
        self.assertEqual(result.backend_status, COMPONENT_DEFAULT_FALLBACK)
        self.assertFalse(result.component_errors)
        self.assertIn(
            'No specialized backend rule matched',
            ' '.join(result.backend_explanation),
        )

    def test_half_deal_halves_default_backend_fallback(self):
        self.back_rule.delete()
        self.sale.frontEnd = Decimal('0.00')
        self.sale.backend = Decimal('1000.00')
        self.sale.count = Decimal('0.5')
        self.sale.save(update_fields=['frontEnd', 'backend', 'count'])
        self.version.default_backend_percentage = Decimal('0.05')
        self.version.save(update_fields=['default_backend_percentage'])
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.backend_commission, Decimal('25.00'))
        self.assertEqual(result.total_deal_commission, Decimal('25.00'))
        self.assertIn('multiplier 0.5', ' '.join(result.backend_explanation))

    def test_matching_specialized_backend_rule_overrides_default(self):
        self.back_rule.delete()
        self.sale.backend = Decimal('1000.00')
        self.sale.save(update_fields=['backend'])
        self.version.default_backend_percentage = Decimal('0.05')
        self.version.save(update_fields=['default_backend_percentage'])
        specialized = self.rule(
            'New Vehicle Backend 8%', 'back_gross_percentage',
            {'rate': '0.08', 'gross_field': 'back_end_gross'}, 1,
        )
        PayPlanRuleCondition.objects.create(
            rule=specialized, field_name='vehicle_condition',
            operator='equals', value='new',
        )
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.backend_commission, Decimal('80.00'))
        self.assertEqual(result.backend_status, COMPONENT_MATCHED_RULE)

    def test_nonmatching_specialized_backend_rule_uses_default(self):
        self.back_rule.delete()
        self.sale.backend = Decimal('1000.00')
        self.sale.save(update_fields=['backend'])
        self.version.default_backend_percentage = Decimal('0.05')
        self.version.save(update_fields=['default_backend_percentage'])
        specialized = self.rule(
            'Used Vehicle Backend 8%', 'back_gross_percentage',
            {'rate': '0.08', 'gross_field': 'back_end_gross'}, 1,
        )
        PayPlanRuleCondition.objects.create(
            rule=specialized, field_name='vehicle_condition',
            operator='equals', value='used',
        )
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.backend_commission, Decimal('50.00'))
        self.assertEqual(result.backend_status, COMPONENT_DEFAULT_FALLBACK)
        self.assertEqual(result.errors, [])

    def test_backend_gross_without_rule_or_default_is_missing_configuration(self):
        self.back_rule.delete()
        self.sale.backend = Decimal('1000.00')
        self.sale.save(update_fields=['backend'])
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.backend_commission, Decimal('0.00'))
        self.assertEqual(
            result.backend_status, COMPONENT_MISSING_CONFIGURATION,
        )
        self.assertIn('backend', result.component_errors)

    def test_front_minimum_is_valid_front_calculation_without_percentage_rule(self):
        self.front_rule.delete()
        self.rule(
            'Front Minimum $100', 'minimum_commission',
            {
                'minimum_amount': '100.00',
                'applies_to_categories': ['front_end'],
            }, 3,
        )
        result = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(result.frontend_commission, Decimal('100.00'))
        self.assertEqual(result.frontend_status, COMPONENT_MATCHED_RULE)
        self.assertNotIn('frontend', result.component_errors)

    def test_default_backend_configuration_is_isolated_between_users(self):
        self.back_rule.delete()
        self.version.default_backend_percentage = Decimal('0.05')
        self.version.save(update_fields=['default_backend_percentage'])
        self.sale.backend = Decimal('1000.00')
        self.sale.save(update_fields=['backend'])

        other = get_user_model().objects.create_user(
            username='other-backend-owner', password='test-password',
        )
        profile = other.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        other_version = other.pay_plan_assignments.get().pay_plan_version
        other_version.default_backend_percentage = Decimal('0.10')
        other_version.save(update_fields=['default_backend_percentage'])
        other_sale = Sale.objects.create(
            user=other, customer='Other Buyer', dealNumber=881002,
            count=Decimal('1.0'), frontEnd=Decimal('0.00'),
            backend=Decimal('1000.00'), date=other_version.effective_start_date,
        )

        mine = CommissionEngineService.calculate_sale(self.user, self.sale)
        theirs = CommissionEngineService.calculate_sale(other, other_sale)
        self.assertEqual(mine.backend_commission, Decimal('50.00'))
        self.assertEqual(theirs.backend_commission, Decimal('100.00'))

    def test_default_backend_minimum_and_maximum(self):
        self.back_rule.delete()
        self.version.default_backend_percentage = Decimal('0.05')
        self.version.default_backend_minimum = Decimal('75.00')
        self.version.save(update_fields=[
            'default_backend_percentage', 'default_backend_minimum',
        ])
        minimum = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(minimum.backend_commission, Decimal('75.00'))
        self.version.default_backend_minimum = None
        self.version.default_backend_maximum = Decimal('50.00')
        self.version.save(update_fields=[
            'default_backend_minimum', 'default_backend_maximum',
        ])
        maximum = CommissionEngineService.calculate_sale(self.user, self.sale)
        self.assertEqual(maximum.backend_commission, Decimal('50.00'))

    def test_unit_bonus_is_once_per_period_and_draw_includes_backend(self):
        self.rule(
            'Volume Ladder', 'volume_bonus',
            {
                'tiers': [{'minimum_units': '1', 'amount': '500'}],
                'tier_mode': 'highest_only',
            }, 3, scope='period',
        )
        self.rule(
            'Monthly Recoverable Draw', 'draw',
            {
                'amount': '2000', 'frequency': 'monthly',
                'recoverable': True, 'draw_type': 'recoverable',
                'eligible_categories': [
                    'front_end', 'back_end', 'unit_bonus', 'other_bonus',
                ],
            }, 4, scope='period',
        )
        totals = CommissionEngineService.calculate_sales(self.user, [self.sale])
        self.assertEqual(totals['period_unit_bonus'], Decimal('500.00'))
        self.assertEqual(totals['results'][0].total_commission, Decimal('685.00'))
        self.assertEqual(totals['total_commission'], Decimal('1185.00'))
        self.assertEqual(
            totals['draw_progress']['eligible_earnings'], Decimal('1185.00'),
        )
        self.assertEqual(
            totals['draw_progress']['backend_commission'], Decimal('60.00'),
        )

    def test_new_vehicle_volume_bonus_uses_period_new_unit_metric(self):
        self.sale.count = Decimal('2.0')
        self.sale.vehicle_condition = 'new'
        self.sale.save(update_fields=['count', 'vehicle_condition'])
        self.rule(
            'New Vehicle Volume Ladder', 'volume_bonus',
            {
                'tiers': [
                    {'minimum_units': '5', 'amount': '500.00'},
                    {'minimum_units': '7', 'amount': '750.00'},
                ],
                'tier_mode': 'highest_only',
                'unit_metric': 'monthly_new_units',
            },
            3,
            scope='period',
        )

        below_tier = CommissionEngineService.calculate_sales(
            self.user, [self.sale],
        )
        self.assertEqual(below_tier['period_unit_bonus'], Decimal('0.00'))
        self.assertEqual(below_tier['unit_bonus']['units_needed'], Decimal('3.0'))

        self.sale.count = Decimal('5.0')
        self.sale.save(update_fields=['count'])
        totals = CommissionEngineService.calculate_sales(self.user, [self.sale])

        self.assertEqual(totals['unit_bonus']['new_units'], Decimal('5.0'))
        self.assertEqual(totals['period_unit_bonus'], Decimal('500.00'))

    def test_parser_recognizes_backend_synonyms_tiers_and_draw(self):
        draft = parse_description_to_import_draft(
            'Salesperson receives 5% of F&I gross. '
            '10–11.5 units: $500 12–15.5 units: $750 30+ units: $4,000. '
            'Recoverable monthly draw of $2,000.',
            'Parsed Plan',
        )
        types = [rule['rule_type'] for rule in draft['rules']]
        self.assertIn('back_gross_percentage', types)
        self.assertIn('volume_bonus', types)
        self.assertIn('draw', types)
