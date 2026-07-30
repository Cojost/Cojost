from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .commission_service import CommissionEngineService
from .models import PayPlanChangeRequest, PayPlanRule, PayPlanVersion, Sale, UserProfile


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
        self.assertNotContains(response, 'Front-end commission</span>')
        self.assertNotContains(response, 'Back-end commission</span>')
        self.assertNotContains(response, 'Period Bonuses')

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
