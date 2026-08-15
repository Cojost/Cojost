from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .access import activity_goals_authorized
from .forms import DailyActivityForm, MonthlyGoalForm
from .models import BillingAccess, Team, TeamMembership, UserProfile
from .models.sales import ArchivedSale, Commission, DailyActivity, MonthlyGoal, Sale
from .services import forecast, month_metrics, round_up_half


class ActivityGoalTests(TestCase):
    def setUp(self):
        self.entitlement_patch = patch(
            'SalesLogApp.billing_entitlements.get_billing_entitlement',
            return_value=SimpleNamespace(has_pro_access=True),
        )
        self.entitlement_patch.start()
        self.addCleanup(self.entitlement_patch.stop)
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
                'user': self.other.pk,
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
            'target_units': '-.5', 'target_total_gross': '1',
            'target_commission': '1',
        })
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValidationError):
            MonthlyGoal.objects.create(user=self.user, month_start=self.month)

    def test_half_units_and_commission_progress(self):
        Sale.objects.create(
            user=self.user, customer='A', dealNumber=9001, count=Decimal('.5'),
            frontEnd=100, backend=50, date=self.today,
        )
        Sale.objects.create(
            user=self.user, customer='B', dealNumber=9005, count=Decimal('2.0'),
            frontEnd=100, backend=50, date=self.today,
        )
        metrics = month_metrics(self.user, self.month)
        self.assertEqual(metrics['units'], Decimal('2.5'))
        self.assertEqual(metrics['total_gross'], Decimal('300'))
        self.assertEqual(metrics['commission'], Decimal('22.5'))

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

    def test_entitled_user_creates_and_updates_exact_monthly_goals(self):
        url = reverse('activity_goals')
        self.client.force_login(self.user)
        first = {
            'form_type': 'goal',
            'month': self.month.strftime('%Y-%m'),
            'target_units': '12.5',
            'target_total_gross': '123456789.12',
            'target_commission': '9876543.21',
            'user': self.other.pk,
        }
        response = self.client.post(url, first)
        self.assertRedirects(
            response,
            f'{url}?month={self.month:%Y-%m}',
            fetch_redirect_response=False,
        )
        goal = MonthlyGoal.objects.get(user=self.user, month_start=self.month)
        self.assertEqual(goal.target_units, Decimal('12.5'))
        self.assertEqual(goal.target_total_gross, Decimal('123456789.12'))
        self.assertEqual(goal.target_commission, Decimal('9876543.21'))
        self.assertFalse(MonthlyGoal.objects.filter(user=self.other).exists())

        first.update({
            'target_units': '14.0',
            'target_total_gross': '200000.01',
            'target_commission': '20000.02',
        })
        self.assertEqual(self.client.post(url, first).status_code, 302)
        self.assertEqual(
            MonthlyGoal.objects.filter(user=self.user, month_start=self.month).count(),
            1,
        )
        goal.refresh_from_db()
        self.assertEqual(goal.target_units, Decimal('14.0'))
        self.assertEqual(goal.target_total_gross, Decimal('200000.01'))
        self.assertEqual(goal.target_commission, Decimal('20000.02'))

    def test_goal_rejects_negative_malformed_excessive_and_nonfinite_values(self):
        self.client.force_login(self.user)
        url = reverse('activity_goals')
        cases = (
            ('target_units', '-0.5'),
            ('target_total_gross', '-0.01'),
            ('target_commission', '-0.01'),
            ('target_units', 'not-a-number'),
            ('target_total_gross', 'not-money'),
            ('target_commission', 'not-money'),
            ('target_units', '999999999.0'),
            ('target_total_gross', '99999999999.99'),
            ('target_commission', '99999999999.99'),
            ('target_units', 'NaN'),
            ('target_total_gross', 'Infinity'),
            ('target_commission', '-Infinity'),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                payload = {
                    'form_type': 'goal',
                    'month': self.month.strftime('%Y-%m'),
                    'target_units': '10.5',
                    'target_total_gross': '50000.00',
                    'target_commission': '5000.00',
                }
                payload[field] = value
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['goal_form'].errors)
                self.assertEqual(response.context['goal_form'][field].value(), value)
                self.assertFalse(MonthlyGoal.objects.filter(user=self.user).exists())

    def test_activity_rejects_malformed_negative_and_excessive_values(self):
        self.client.force_login(self.user)
        url = reverse('activity_goals')
        for field, value in (
            ('leads_taken', '-1'),
            ('phone_calls_made', '-1'),
            ('leads_taken', 'one'),
            ('phone_calls_made', '99999999999999'),
        ):
            with self.subTest(field=field, value=value):
                payload = {
                    'form_type': 'activity',
                    'month': self.month.strftime('%Y-%m'),
                    'date': self.today,
                    'leads_taken': '1',
                    'phone_calls_made': '1',
                }
                payload[field] = value
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context['activity_form'].errors)
                self.assertFalse(DailyActivity.objects.filter(user=self.user).exists())

    def test_goal_and_activity_queries_remain_owner_scoped_for_team_members(self):
        team = Team.objects.create(name='Private team', owner=self.other)
        TeamMembership.objects.create(
            team=team,
            user=self.user,
            role=TeamMembership.ADMIN,
            status=TeamMembership.ACTIVE,
        )
        other_goal = MonthlyGoal.objects.create(
            user=self.other,
            month_start=self.month,
            target_units=Decimal('99.0'),
            target_total_gross=Decimal('99999.00'),
            target_commission=Decimal('9999.00'),
        )
        other_activity = DailyActivity.objects.create(
            user=self.other,
            date=self.today,
            leads_taken=99,
            phone_calls_made=99,
        )
        self.client.force_login(self.user)
        response = self.client.get(
            reverse('activity_goals'), {'month': self.month.strftime('%Y-%m')}
        )
        self.assertIsNone(response.context['goal'])
        self.assertNotContains(response, str(other_goal.target_total_gross))
        self.assertEqual(
            self.client.get(reverse('edit_activity', args=[other_activity.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse('edit_activity', args=[other_activity.pk]), {
                'form_type': 'activity',
                'month': self.month.strftime('%Y-%m'),
                'date': self.today,
                'leads_taken': '1',
                'phone_calls_made': '1',
            }).status_code,
            404,
        )

    def test_get_has_no_activity_goal_profile_or_billing_side_effects(self):
        UserProfile.objects.filter(user=self.user).delete()
        before = {
            'activity': DailyActivity.objects.count(),
            'goals': MonthlyGoal.objects.count(),
            'profiles': UserProfile.objects.count(),
            'billing': BillingAccess.objects.count(),
        }
        self.client.force_login(self.user)
        response = self.client.get(reverse('activity_goals'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(before, {
            'activity': DailyActivity.objects.count(),
            'goals': MonthlyGoal.objects.count(),
            'profiles': UserProfile.objects.count(),
            'billing': BillingAccess.objects.count(),
        })

    def test_mutations_require_post_and_csrf(self):
        self.client.force_login(self.user)
        url = reverse('activity_goals')
        self.assertEqual(self.client.put(url, data='form_type=goal').status_code, 405)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(url, {
            'form_type': 'goal',
            'month': self.month.strftime('%Y-%m'),
            'target_units': '1.0',
            'target_total_gross': '1.00',
            'target_commission': '1.00',
        }).status_code, 403)
        self.assertFalse(MonthlyGoal.objects.filter(user=self.user).exists())
        for report_name in ('print_activity_goals', 'print_activity_history'):
            self.assertEqual(self.client.post(reverse(report_name)).status_code, 405)

    def test_sc1_page_does_not_present_forecasts_or_coaching(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('activity_goals'))
        self.assertContains(response, 'Total gross goal')
        self.assertNotContains(response, 'Estimated forecast')
        self.assertNotContains(response, 'Daily lead pace')
        self.assertNotContains(response, 'Recommended leads')

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


@override_settings(BILLING_FEATURE_ENABLED=True)
class ActivityGoalEntitlementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('basic-owner', password='pw')
        self.other = User.objects.create_user('basic-other', password='pw')
        self.today = timezone.localdate()
        self.month = self.today.replace(day=1)

    @patch(
        'SalesLogApp.billing_entitlements.get_billing_entitlement',
        return_value=SimpleNamespace(has_pro_access=False),
    )
    def test_non_entitled_user_is_blocked_from_every_route_and_data_is_preserved(
        self, _entitlement,
    ):
        goal = MonthlyGoal.objects.create(
            user=self.user,
            month_start=self.month,
            target_units=Decimal('1.0'),
            target_total_gross=Decimal('100.00'),
            target_commission=Decimal('10.00'),
        )
        activity = DailyActivity.objects.create(
            user=self.user, date=self.today, leads_taken=1, phone_calls_made=2
        )
        self.client.force_login(self.user)
        routes = (
            reverse('activity_goals'),
            reverse('edit_activity', args=[activity.pk]),
            reverse('print_activity_goals'),
            reverse('print_activity_history'),
        )
        for url in routes:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(
                    response,
                    reverse('billing_overview'),
                    fetch_redirect_response=False,
                )
        response = self.client.post(reverse('activity_goals'), {
            'form_type': 'goal',
            'month': self.month.strftime('%Y-%m'),
            'target_units': '8.0',
            'target_total_gross': '800.00',
            'target_commission': '80.00',
        })
        self.assertEqual(response.status_code, 302)
        goal.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(goal.target_units, Decimal('1.0'))
        self.assertEqual(activity.leads_taken, 1)

    @patch(
        'SalesLogApp.billing_entitlements.get_billing_entitlement',
        return_value=SimpleNamespace(has_pro_access=False),
    )
    def test_basic_navigation_hides_activity_goals_link(self, _entitlement):
        self.client.force_login(self.user)
        response = self.client.get(reverse('profile'))
        self.assertNotContains(response, reverse('activity_goals'))

    @patch(
        'SalesLogApp.billing_entitlements.get_billing_entitlement',
        return_value=SimpleNamespace(has_pro_access=True),
    )
    def test_billing_entitled_regular_user_has_route_and_navigation_access(
        self, _entitlement,
    ):
        self.client.force_login(self.user)
        response = self.client.get(reverse('activity_goals'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('activity_goals'))
        self.assertContains(response, 'Pro')

    @patch('SalesLogApp.billing_entitlements.get_billing_entitlement')
    def test_staff_and_superuser_keep_internal_access_without_billing_lookup(
        self, entitlement,
    ):
        for suffix, attributes in (
            ('staff', {'is_staff': True}),
            ('super', {'is_superuser': True}),
        ):
            with self.subTest(kind=suffix):
                user = User.objects.create_user(
                    f'internal-{suffix}', password='pw', **attributes
                )
                self.assertTrue(activity_goals_authorized(user))
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse('activity_goals')).status_code, 200)
        entitlement.assert_not_called()

    def test_real_denied_gate_does_not_create_entitlement_or_profile_records(self):
        UserProfile.objects.filter(user=self.user).delete()
        self.client.force_login(self.user)
        response = self.client.get(reverse('activity_goals'))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(BillingAccess.objects.filter(user=self.user).exists())
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())
        self.assertFalse(MonthlyGoal.objects.filter(user=self.user).exists())
        self.assertFalse(DailyActivity.objects.filter(user=self.user).exists())

    @override_settings(
        BILLING_FEATURE_ENABLED=False,
        BILLING_ENFORCEMENT_ENABLED=False,
    )
    @patch(
        'SalesLogApp.billing_entitlements.get_billing_entitlement',
        return_value=SimpleNamespace(has_pro_access=False),
    )
    def test_denied_gate_has_friendly_fallback_when_billing_ui_is_disabled(
        self, _entitlement,
    ):
        self.client.force_login(self.user)
        response = self.client.get(reverse('activity_goals'), follow=True)
        self.assertRedirects(response, reverse('profile'))
        self.assertContains(response, 'Activity &amp; Goals is a Pro feature')
