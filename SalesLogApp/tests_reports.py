from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models.sales import Commission, DailyActivity, MonthlyGoal, Sale


class PrintReportTests(TestCase):
    def setUp(self):
        self.entitlement_patch = patch(
            'SalesLogApp.billing_entitlements.get_billing_entitlement',
            return_value=SimpleNamespace(has_pro_access=True),
        )
        self.entitlement_patch.start()
        self.addCleanup(self.entitlement_patch.stop)
        self.user = User.objects.create_user('report-owner', password='pw')
        self.other = User.objects.create_user('report-other', password='pw')
        self.commission = Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.10'),
            total_calculated_back_end=Decimal('0.10'),
        )
        Commission.objects.create(
            user=self.other,
            total_calculated_front_end=Decimal('0.99'),
            total_calculated_back_end=Decimal('0.99'),
        )
        self.month = timezone.localdate().replace(day=1)
        self.previous = (self.month - timedelta(days=1)).replace(day=1)
        self.sale = self.make_sale(
            self.user, 81001, self.month, 'Owned Buyer', 'Owned Partner'
        )
        self.make_sale(
            self.other, 81002, self.month, 'Private Buyer', 'Private Partner'
        )
        self.make_sale(
            self.user, 81003, self.previous, 'Previous Buyer', 'Previous Partner'
        )
        DailyActivity.objects.create(
            user=self.user, date=self.month, leads_taken=8, phone_calls_made=16
        )
        DailyActivity.objects.create(
            user=self.other, date=self.month, leads_taken=99, phone_calls_made=99
        )
        MonthlyGoal.objects.create(
            user=self.user, month_start=self.month,
            target_units=Decimal('2.0'), target_commission=Decimal('500.00'),
        )

    def make_sale(self, user, deal, date, customer, split):
        return Sale.objects.create(
            user=user, customer=customer, dealNumber=deal, date=date,
            count=Decimal('0.5'), split_with_name=split,
            frontEnd=Decimal('100.00'), backend=Decimal('50.00'),
        )

    def test_all_print_routes_require_login(self):
        for name in ('print_sales', 'print_activity_goals', 'print_activity_history'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)

    def test_sales_report_is_isolated_and_shows_split_name(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse('print_sales'), {'month': self.month.strftime('%Y-%m')}
        )
        self.assertContains(response, 'Owned Buyer')
        self.assertContains(response, 'Owned Partner')
        self.assertNotContains(response, 'Private Buyer')
        self.assertNotContains(response, 'Private Partner')
        self.assertNotContains(response, 'Edit')
        self.assertNotContains(response, 'Delete')

    def test_sales_month_filter_and_totals_match_normal_page(self):
        self.client.force_login(self.user)
        params = {'month': self.month.strftime('%Y-%m')}
        normal = self.client.get(reverse('view_sales'), params)
        printed = self.client.get(reverse('print_sales'), params)
        self.assertEqual(normal.context['total_count'], printed.context['total_count'])
        self.assertEqual(
            normal.context['total_commission'], printed.context['total_commission']
        )
        self.assertNotContains(printed, 'Previous Buyer')

    def test_goal_report_excludes_history_and_other_user_activity(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse('print_activity_goals'),
            {'month': self.month.strftime('%Y-%m')},
        )
        self.assertContains(response, 'Activity &amp; Goals Report')
        self.assertContains(response, '>8<', html=False)
        self.assertNotContains(response, 'Month History')
        self.assertNotContains(response, '>99<', html=False)
        self.assertNotContains(response, '<form', html=False)
        self.assertContains(response, 'Total gross')
        self.assertNotContains(response, 'Estimated forecast')

    def test_history_report_contains_only_history_content(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('print_activity_history'), {
            'month': self.month.strftime('%Y-%m'),
            'history_start': self.previous.strftime('%Y-%m'),
            'history_end': self.month.strftime('%Y-%m'),
        })
        self.assertContains(response, 'Month History')
        self.assertContains(response, self.previous.strftime('%B %Y'))
        self.assertEqual(response.context['history_start'], self.previous)
        self.assertEqual(response.context['history_end'], self.month)
        self.assertNotContains(response, '>99<', html=False)
        self.assertNotContains(response, 'Daily activity')
        self.assertNotContains(response, 'Goal progress')
        self.assertNotContains(response, '<form', html=False)

    def test_invalid_parameters_fall_back_safely(self):
        self.client.force_login(self.user)
        sales = self.client.get(reverse('print_sales'), {'month': 'not-a-month'})
        history = self.client.get(reverse('print_activity_history'), {
            'month': 'bad', 'history_start': 'later', 'history_end': 'invalid',
        })
        self.assertEqual(sales.status_code, 200)
        self.assertEqual(sales.context['selected_month'], self.month)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.context['history_end'], self.month)
