from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .commission_service import (
    COMPONENT_DEFAULT_FALLBACK,
    COMPONENT_MATCHED_RULE,
    CommissionEngineService,
)
from .models import (
    PayPlanRule,
    PayPlanRuleCondition,
    Sale,
    UserProfile,
)


class PayPlanRuleConditionEditTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='micheals-rule-owner', password='test-password', is_staff=True,
        )
        self.other = User.objects.create_user(
            username='other-rule-owner', password='test-password',
        )
        for user in (self.user, self.other):
            profile = user.sales_profile
            profile.commission_system = UserProfile.PAY_PLAN_V2
            profile.save(update_fields=['commission_system', 'updated_at'])
        self.version = self.user.pay_plan_assignments.get().pay_plan_version
        self.version.default_backend_percentage = Decimal('0.02')
        self.version.save(update_fields=['default_backend_percentage'])
        self.rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Back Gross 5%',
            rule_type='back_gross_percentage',
            calculation_scope='per_sale',
            configuration={
                'rate': '0.0500',
                'gross_field': 'back_end_gross',
            },
            sort_order=1,
        )
        self.vehicle_condition = PayPlanRuleCondition.objects.create(
            rule=self.rule,
            field_name='vehicle_condition',
            operator='equals',
            value='new',
            sort_order=1,
        )
        self.other_version = (
            self.other.pay_plan_assignments.get().pay_plan_version
        )
        self.other_rule = PayPlanRule.objects.create(
            pay_plan_version=self.other_version,
            name='Back Gross 5%',
            rule_type='back_gross_percentage',
            calculation_scope='per_sale',
            configuration={
                'rate': '0.0900',
                'gross_field': 'back_end_gross',
            },
            sort_order=1,
        )
        self.url = reverse(
            'edit_pay_plan_rule', args=[self.version.id, self.rule.id],
        )
        self.client.login(
            username=self.user.username, password='test-password',
        )

    def post_condition(self, value):
        return self.client.post(self.url, {'vehicle_condition': value})

    def current_vehicle_condition(self):
        condition = self.rule.conditions.filter(
            field_name='vehicle_condition',
        ).first()
        return condition.value if condition else None

    def test_edit_new_to_used_and_reopen_prepopulates_value(self):
        response = self.post_condition('used')
        self.assertRedirects(response, self.url)
        self.assertEqual(self.current_vehicle_condition(), 'used')
        reopened = self.client.get(self.url)
        self.assertContains(reopened, 'value="used" selected')

    def test_edit_used_to_new(self):
        self.vehicle_condition.value = 'used'
        self.vehicle_condition.save(update_fields=['value'])
        self.post_condition('new')
        self.assertEqual(self.current_vehicle_condition(), 'new')

    def test_remove_condition_applies_to_all_vehicles(self):
        self.post_condition('')
        self.assertIsNone(self.current_vehicle_condition())
        self.assertFalse(
            self.rule.conditions.filter(field_name='vehicle_condition').exists()
        )

    def test_all_alias_removes_condition(self):
        self.post_condition('all')
        self.assertIsNone(self.current_vehicle_condition())

    def test_add_condition_when_none_exists(self):
        self.vehicle_condition.delete()
        self.post_condition('used')
        self.assertEqual(self.current_vehicle_condition(), 'used')

    def test_configuration_is_preserved_exactly(self):
        original = dict(self.rule.configuration)
        self.post_condition('used')
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.configuration, original)
        self.assertEqual(self.rule.configuration['rate'], '0.0500')
        self.assertEqual(
            self.rule.configuration['gross_field'], 'back_end_gross',
        )

    def test_unrelated_conditions_are_preserved(self):
        unrelated = PayPlanRuleCondition.objects.create(
            rule=self.rule,
            field_name='green_pea',
            operator='is_true',
            value=True,
            sort_order=2,
        )
        self.post_condition('used')
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.field_name, 'green_pea')
        self.assertTrue(unrelated.value)

    def test_invalid_vehicle_condition_returns_form_error(self):
        response = self.post_condition('certified')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select a valid choice')
        self.assertEqual(self.current_vehicle_condition(), 'new')

    def test_user_cannot_edit_another_users_rule(self):
        url = reverse(
            'edit_pay_plan_rule',
            args=[self.other_version.id, self.other_rule.id],
        )
        response = self.client.post(url, {'vehicle_condition': 'used'})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            self.other_rule.conditions.filter(
                field_name='vehicle_condition',
            ).exists()
        )

    def test_same_rule_names_do_not_conflict(self):
        self.post_condition('used')
        self.assertEqual(self.current_vehicle_condition(), 'used')
        self.other_rule.refresh_from_db()
        self.assertEqual(self.other_rule.configuration['rate'], '0.0900')
        self.assertFalse(self.other_rule.conditions.exists())

    def test_engine_uses_selected_condition_and_default_fallback(self):
        new_sale = Sale.objects.create(
            user=self.user, customer='New Buyer', dealNumber=773001,
            count=Decimal('1.0'), frontEnd=Decimal('0.00'),
            backend=Decimal('1000.00'),
            date=self.version.effective_start_date,
            vehicle_condition=' NEW ',
        )
        used_sale = Sale.objects.create(
            user=self.user, customer='Used Buyer', dealNumber=773002,
            count=Decimal('1.0'), frontEnd=Decimal('0.00'),
            backend=Decimal('1000.00'),
            date=self.version.effective_start_date,
            vehicle_condition='used',
        )
        new_result = CommissionEngineService.calculate_sale(
            self.user, new_sale,
        )
        used_result = CommissionEngineService.calculate_sale(
            self.user, used_sale,
        )
        self.assertEqual(new_result.backend_commission, Decimal('50.00'))
        self.assertEqual(new_result.backend_status, COMPONENT_MATCHED_RULE)
        self.assertEqual(used_result.backend_commission, Decimal('20.00'))
        self.assertEqual(
            used_result.backend_status, COMPONENT_DEFAULT_FALLBACK,
        )

        self.post_condition('used')
        used_result = CommissionEngineService.calculate_sale(
            self.user, used_sale,
        )
        self.assertEqual(used_result.backend_commission, Decimal('50.00'))

    def test_rule_without_vehicle_condition_applies_to_new_and_used(self):
        self.post_condition('')
        results = []
        for index, condition in enumerate(('new', 'used'), start=1):
            sale = Sale.objects.create(
                user=self.user, customer=condition, dealNumber=774000 + index,
                count=Decimal('1.0'), frontEnd=Decimal('0.00'),
                backend=Decimal('1000.00'),
                date=self.version.effective_start_date,
                vehicle_condition=condition,
            )
            results.append(
                CommissionEngineService.calculate_sale(self.user, sale)
            )
        self.assertEqual(
            [result.backend_commission for result in results],
            [Decimal('50.00'), Decimal('50.00')],
        )
        self.assertTrue(all(
            result.backend_status == COMPONENT_DEFAULT_FALLBACK
            for result in results
        ))

    def test_period_rule_rejects_per_sale_vehicle_condition(self):
        period_rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='New Vehicle Volume Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [{'minimum_units': '5', 'amount': '500.00'}],
                'tier_mode': 'highest_only',
                'unit_metric': 'monthly_new_units',
            },
            sort_order=3,
        )

        response = self.client.post(
            reverse(
                'edit_pay_plan_rule',
                kwargs={
                    'version_id': self.version.id,
                    'rule_id': period_rule.id,
                },
            ),
            {'vehicle_condition': 'new'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Vehicle conditions apply to individual-sale rules only.',
        )
        self.assertFalse(period_rule.conditions.exists())
