from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .commission_service import CommissionEngineService
from .models import (
    PayPlanChangeRequest, PayPlanRule, PayPlanRuleCondition, PayPlanVersion,
    Sale, UserProfile,
)
from .templatetags.pay_plan_display import _percent


class Phase1AUserInterfaceTests(TestCase):
    password = 'phase1a-password'

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='phase1a-owner', password=self.password,
        )
        self.other = get_user_model().objects.create_user(
            username='phase1a-other', password=self.password,
        )
        for user in (self.user, self.other):
            profile = user.sales_profile
            profile.commission_system = UserProfile.PAY_PLAN_V2
            profile.save(update_fields=['commission_system', 'updated_at'])
            assignment = user.pay_plan_assignments.select_related(
                'pay_plan_version__pay_plan',
            ).get()
            version = assignment.pay_plan_version
            version.status = PayPlanVersion.ACTIVE
            version.rules.all().delete()
            version.save(update_fields=['status', 'updated_at'])
            onboarding = user.pay_plan_onboarding
            onboarding.current_pay_plan = version.pay_plan
            onboarding.current_version = version
            onboarding.status = onboarding.ACTIVE
            onboarding.save(update_fields=[
                'current_pay_plan', 'current_version', 'status', 'updated_at',
            ])
        self.version = self.user.pay_plan_assignments.get().pay_plan_version
        self.rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Standard Volume Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [{
                    'minimum_units': '10',
                    'maximum_units': None,
                    'amount': '500.00',
                }],
                'tier_mode': 'highest_only',
            },
        )
        self.client.force_login(self.user)

    def make_sale(self, user=None):
        return Sale.objects.create(
            user=user or self.user,
            customer='Visible Customer',
            dealNumber=910001,
            count=Decimal('1.0'),
            frontEnd=Decimal('1000.00'),
            backend=Decimal('200.00'),
            date=timezone.localdate(),
            vehicle_condition='used',
        )

    def test_navigation_has_one_of_each_primary_destination(self):
        response = self.client.get(reverse('view_sales'))
        content = response.content.decode()
        navigation = content[
            content.index('<nav class="menu"'):
            content.index('</nav>', content.index('<nav class="menu"'))
        ]
        for name in ('view_sales', 'view_commission', 'activity_goals'):
            self.assertEqual(navigation.count(f'href="{reverse(name)}"'), 1)
        self.assertIn('>Dashboard</a>', navigation)
        self.assertNotIn('>View Sales</a>', navigation)

    def test_dashboard_name_does_not_change_sales_route(self):
        response = self.client.get(reverse('view_sales'))
        self.assertEqual(reverse('view_sales'), '/SalesLogApp/view_sales/')
        self.assertEqual(response.resolver_match.url_name, 'view_sales')
        self.assertContains(response, '<title>Dashboard</title>', html=True)
        self.assertContains(response, '<h2>Dashboard</h2>', html=True)

    def test_primary_sales_actions_remain_reachable(self):
        self.make_sale()
        response = self.client.get(reverse('view_sales'))
        for name in ('add_sale', 'view_commission'):
            self.assertContains(response, reverse(name))
        self.assertContains(response, 'Edit')
        self.assertContains(response, 'Delete')
        self.assertContains(response, 'View calculation')

    def test_sales_shows_per_deal_commission_without_full_breakdown(self):
        self.make_sale()
        response = self.client.get(reverse('view_sales'))
        self.assertContains(response, 'Per-deal commission')
        self.assertContains(response, 'Estimated total commission')
        self.assertContains(response, 'Front-end commission')
        self.assertContains(response, 'Back-end commission')
        self.assertContains(response, 'Bonuses')
        self.assertNotContains(response, 'How this was calculated')

    def test_dashboard_restores_authoritative_reconciling_totals_box(self):
        sale = self.make_sale()
        expected = CommissionEngineService.calculate_sales(self.user, [sale])
        response = self.client.get(reverse('view_sales'))
        self.assertContains(response, 'class="summary dashboard-summary"')
        self.assertEqual(response.context['total_count'], sale.unit_credit)
        self.assertEqual(response.context['total_front_end'], expected['total_front'])
        self.assertEqual(response.context['total_back_end'], expected['total_back'])
        self.assertEqual(response.context['total_bonus'], expected['total_bonus'])
        self.assertEqual(response.context['total_adjustments'], Decimal('0'))
        components = (
            response.context['total_front_end']
            + response.context['total_back_end']
            + response.context['total_bonus']
            + response.context['total_adjustments']
        )
        self.assertEqual(components, response.context['total_commission'])
        self.assertEqual(response.context['total_commission'], expected['total_commission'])

    def test_dashboard_graph_has_no_visible_calculation_action_but_popup_remains(self):
        self.make_sale()
        response = self.client.get(reverse('view_sales'))
        content = response.content.decode()
        self.assertNotIn('class="graph-action"', content)
        self.assertNotIn('class="chart-action"', content)
        self.assertContains(response, '— View calculation')
        self.assertContains(response, 'commission-details-trigger')
        self.assertContains(response, 'commission-dialog-')
        self.assertContains(response, 'dialog.showModal()')

    def test_commission_totals_match_authoritative_engine(self):
        sale = self.make_sale()
        expected = CommissionEngineService.calculate_sales(
            self.user, [sale],
        )
        response = self.client.get(reverse('view_commission'))
        self.assertEqual(response.context['total_commission'], expected['total_commission'])
        self.assertContains(response, 'Front-end commission')
        self.assertContains(response, 'Back-end commission')
        self.assertContains(response, 'Adjustments')

    def test_rule_page_leads_with_human_readable_summary(self):
        response = self.client.get(
            reverse('pay_plan_rules', args=[self.version.id]),
        )
        self.assertContains(response, '$500.00 bonus at 10 units')
        self.assertContains(response, 'Active plan')
        self.assertContains(
            response,
            'See how your commission is calculated and when each rule applies.',
        )
        self.assertNotContains(response, 'Human-readable')

    def test_percent_formatter_preserves_decimal_precision_without_exponents(self):
        examples = {
            Decimal('0.25'): '25%',
            '0.275': '27.5%',
            Decimal('0.003'): '0.3%',
            0: '0%',
            None: 'Not available',
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                formatted = _percent(value)
                self.assertEqual(formatted, expected)
                if value is not None:
                    self.assertNotIn('E', formatted)
                    self.assertNotIn('e', formatted)

    def test_front_rule_and_acquisition_exclusion_use_natural_language(self):
        front_rule = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='standard_front_end_percentage_rule',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={'rate': '0.25'},
        )
        PayPlanRuleCondition.objects.create(
            rule=front_rule,
            field_name='acquisition_source',
            operator='not_in',
            value=['street_curb', 'current_service_customer'],
        )
        response = self.client.get(
            reverse('pay_plan_rules', args=[self.version.id]),
        )
        self.assertContains(response, 'Front-end commission')
        self.assertContains(
            response,
            'You earn 25% of the front-end gross on qualifying sales.',
        )
        self.assertContains(
            response,
            'Does not apply when the acquisition source is Street Curb or '
            'Current Service Customer.',
        )
        content = response.content.decode()
        advanced = content.index('<summary>Advanced details</summary>')
        raw = content.index(
            'acquisition_source not_in', advanced,
        )
        self.assertLess(advanced, raw)

    def test_raw_rule_configuration_is_only_in_advanced_details(self):
        response = self.client.get(
            reverse('pay_plan_rules', args=[self.version.id]),
        )
        content = response.content.decode()
        summary = content.index('<summary>Advanced details</summary>')
        raw = content.index('tier_mode')
        self.assertLess(summary, raw)
        self.assertNotIn('Priority:', content[:summary])

    def test_active_draft_and_sandbox_language_is_distinct(self):
        rules = self.client.get(
            reverse('pay_plan_rules', args=[self.version.id]),
        )
        sandbox = self.client.get(reverse('commission_sandbox_index'))
        assistant = self.client.get(reverse('pay_plan_assistant'))
        self.assertContains(rules, 'Active plan')
        self.assertContains(sandbox, 'Sandbox scenario')
        self.assertContains(sandbox, 'private simulation')
        self.assertContains(assistant, 'draft change')
        self.assertContains(assistant, 'active plan stays unchanged')

    def test_assistant_exact_37_unit_request_reaches_review(self):
        response = self.client.post(
            reverse('pay_plan_assistant'),
            {
                'request_text': 'Pay $251 at 37 units',
                'effective_date': (
                    timezone.localdate() + timedelta(days=1)
                ).isoformat(),
                'confirm_retroactive': 'on',
            },
            follow=True,
        )
        self.assertEqual(
            response.resolver_match.url_name, 'replacement_pay_plan_review',
        )
        self.assertContains(response, '37+ units pays $251.00')
        self.assertEqual(
            PayPlanChangeRequest.objects.get(user=self.user).draft_version.status,
            PayPlanVersion.REVIEW_REQUIRED,
        )

    def test_other_users_sales_and_plan_are_not_visible(self):
        self.make_sale(user=self.other)
        sales = self.client.get(reverse('view_sales'))
        self.assertNotContains(sales, 'Visible Customer')
        other_version = self.other.pay_plan_assignments.get().pay_plan_version
        response = self.client.get(
            reverse('pay_plan_rules', args=[other_version.id]),
        )
        self.assertEqual(response.status_code, 404)

    def test_bound_assistant_error_preserves_entered_request(self):
        response = self.client.post(
            reverse('pay_plan_assistant'),
            {
                'request_text': 'Pay $251 at 37 units',
                'effective_date': (
                    timezone.localdate() - timedelta(days=1)
                ).isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Confirm retroactive recalculation')
        self.assertContains(response, 'Pay $251 at 37 units')

    def test_empty_pages_do_not_present_zero_as_a_calculation(self):
        sales = self.client.get(reverse('view_sales'))
        commission = self.client.get(reverse('view_commission'))
        self.assertContains(sales, 'No sales recorded')
        self.assertNotContains(sales, '$0.00')
        self.assertContains(commission, 'Commission is not available yet')
        self.assertNotContains(commission, 'Total commission</dt><dd>$0.00')

    def test_theme_selection_remains_in_base_page(self):
        profile = self.user.sales_profile
        profile.theme_mode = 'dark'
        profile.header_color = 'purple'
        profile.save(update_fields=['theme_mode', 'header_color', 'updated_at'])
        response = self.client.get(reverse('view_sales'))
        self.assertContains(response, 'data-theme="dark"')
        self.assertContains(response, 'header-theme-purple')
