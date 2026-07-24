from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import DailyActivityForm, MonthlyGoalForm
from .models.sales import ArchivedSale, Commission, DailyActivity, MonthlyGoal, Sale
from .services import forecast, month_metrics, round_up_half


class ActivityGoalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('owner', password='pw')
        self.other = User.objects.create_user('other', password='pw')
        self.commission = Commission.objects.create(
            user=self.user, total_calculated_front_end=Decimal('.10'),
            total_calculated_back_end=Decimal('.10'),
        )
        self.today = timezone.localdate()
        self.month = self.today.replace(day=1)

    def test_activity_unique_and_validation(self):
        DailyActivity.objects.create(user=self.user, date=self.today, leads_taken=1)
        with self.assertRaises(ValidationError):
            DailyActivity.objects.create(user=self.user, date=self.today, leads_taken=2)
        with self.assertRaises(ValidationError):
            DailyActivity.objects.create(user=self.user, date=self.today + timedelta(days=1))
        self.assertFalse(DailyActivityForm(data={
            'date': self.today, 'leads_taken': -1, 'phone_calls_made': 0
        }).is_valid())

    def test_post_updates_same_date_and_uses_prg(self):
        self.client.force_login(self.user)
        url = reverse('activity_goals')
        for leads in (2, 7):
            response = self.client.post(url, {
                'form_type': 'activity', 'month': self.month.strftime('%Y-%m'),
                'date': self.today, 'leads_taken': leads, 'phone_calls_made': 3,
            })
            self.assertEqual(response.status_code, 302)
        self.assertEqual(DailyActivity.objects.filter(user=self.user).count(), 1)
        self.assertEqual(DailyActivity.objects.get(user=self.user).leads_taken, 7)

    def test_goal_normalizes_month_and_is_unique(self):
        goal = MonthlyGoal.objects.create(
            user=self.user, month_start=self.month.replace(day=12),
            target_units=Decimal('2.5'), target_commission=100,
        )
        self.assertEqual(goal.month_start.day, 1)
        form = MonthlyGoalForm(data={
            'month': self.month.strftime('%Y-%m'),
            'target_units': '-.5', 'target_commission': '1',
        })
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValidationError):
            MonthlyGoal.objects.create(user=self.user, month_start=self.month)

    def test_half_units_and_commission_progress(self):
        Sale.objects.create(
            user=self.user, customer='A', dealNumber=9001, count=Decimal('.5'),
            frontEnd=100, backend=50, date=self.today,
        )
        metrics = month_metrics(self.user, self.month)
        self.assertEqual(metrics['units'], Decimal('.5'))
        self.assertEqual(metrics['commission'], Decimal('15.0'))

    def test_forecast_and_half_rounding(self):
        previous = (self.month - timedelta(days=1)).replace(day=1)
        DailyActivity.objects.create(user=self.user, date=previous, leads_taken=10, phone_calls_made=20)
        Sale.objects.create(
            user=self.user, customer='Past', dealNumber=9002, count=Decimal('2'),
            frontEnd=1000, backend=0, date=previous,
        )
        current = month_metrics(self.user, self.month)
        result = forecast(self.user, self.month, current, Decimal('3'), Decimal('260'))
        self.assertTrue(result['available'])
        self.assertEqual(result['leads_for_unit_goal'], 15)
        self.assertEqual(round_up_half(Decimal('2.01')), Decimal('2.5'))
        self.assertGreaterEqual(result['recommended_remaining_leads'], result['leads_for_unit_goal'])

    def test_insufficient_and_zero_history(self):
        result = forecast(self.user, self.month, month_metrics(self.user, self.month), Decimal('1'), Decimal('1'))
        self.assertFalse(result['available'])

    def test_login_and_idor_isolation(self):
        activity = DailyActivity.objects.create(user=self.other, date=self.today, leads_taken=99)
        self.assertEqual(self.client.get(reverse('activity_goals')).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('edit_activity', args=[activity.pk])).status_code, 404)
        response = self.client.get(reverse('activity_goals'))
        self.assertNotContains(response, '>99<')

    def test_unowned_archives_are_excluded(self):
        ArchivedSale.objects.create(
            customer='Legacy', dealNumber=9003, count=2, frontEnd=9999,
            backend=9999, date=self.today,
        )
        ArchivedSale.objects.create(
            user=self.other, customer='Other', dealNumber=9004, count=2,
            frontEnd=9999, backend=9999, date=self.today,
        )
        self.assertEqual(month_metrics(self.user, self.month)['units'], Decimal('0'))
