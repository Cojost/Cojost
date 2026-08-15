from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .archive_aggregation import ArchivedSaleAggregationAdapter
from .models import (
    Industry,
    PayPlan,
    PayPlanAssignment,
    PayPlanRule,
    PayPlanRuleCondition,
    PayPlanVersion,
    Team,
    TeamMembership,
    UserProfile,
)
from .models.sales import (
    ArchivedSale,
    BonusLevel,
    Commission,
    CommissionAdjustment,
    Sale,
)
from .services import commission_totals, month_metrics


class ArchivedSaleAggregationRegressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('archive-owner', password='pw')
        self.commission = Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.10'),
            total_calculated_back_end=Decimal('0.10'),
        )
        self.month = timezone.localdate().replace(day=1)

    def make_archive(self, *, deal, count='1.0', front='100.00', back='50.00',
                     owner=None, sale_date=None):
        return ArchivedSale.objects.create(
            user=owner or self.user,
            customer='Archived customer',
            dealNumber=deal,
            count=Decimal(count),
            frontEnd=Decimal(front),
            backend=Decimal(back),
            date=sale_date or self.month,
        )

    def test_owned_archive_is_counted_without_crashing_or_inventing_commission(self):
        self.make_archive(deal=980001)

        metrics = month_metrics(self.user, self.month)

        self.assertEqual(metrics['units'], Decimal('1.0'))
        self.assertEqual(metrics['total_gross'], Decimal('150.00'))
        self.assertIsNone(metrics['commission'])
        self.assertFalse(metrics['commission_complete'])
        self.assertIn('snapshot', metrics['commission_diagnostic'].lower())

    def test_owner_scope_mixed_units_and_recorded_gross(self):
        Sale.objects.create(
            user=self.user, customer='Live', dealNumber=980002,
            count=Decimal('0.5'), frontEnd=Decimal('10.00'),
            backend=Decimal('5.00'), date=self.month,
        )
        one = self.make_archive(deal=980003, count='1.0')
        double = self.make_archive(
            deal=980004, count='2.0', front='200.00', back='25.00',
        )
        other = User.objects.create_user('archive-other', password='pw')
        self.make_archive(
            deal=980005, count='2.0', front='9999.00', back='9999.00',
            owner=other,
        )

        metrics = month_metrics(self.user, self.month)

        self.assertEqual(metrics['units'], Decimal('3.5'))
        self.assertEqual(metrics['total_gross'], Decimal('390.00'))
        self.assertEqual(
            ArchivedSaleAggregationAdapter(one).unit_credit,
            Decimal('1.0'),
        )
        self.assertEqual(
            ArchivedSaleAggregationAdapter(double).unit_credit,
            Decimal('2.0'),
        )

    def test_double_count_does_not_multiply_recorded_gross(self):
        self.make_archive(
            deal=980006, count='2.0', front='100.00', back='50.00',
        )
        metrics = month_metrics(self.user, self.month)
        self.assertEqual(metrics['units'], Decimal('2.0'))
        self.assertEqual(metrics['total_gross'], Decimal('150.00'))

    def test_live_archive_overlap_uses_proven_deal_identity_once(self):
        Sale.objects.create(
            user=self.user, customer='Live', dealNumber=980007,
            count=Decimal('0.5'), frontEnd=Decimal('10.00'),
            backend=Decimal('5.00'), date=self.month,
        )
        self.make_archive(
            deal=980007, count='2.0', front='999.00', back='999.00',
        )

        metrics = month_metrics(self.user, self.month)

        self.assertEqual(metrics['units'], Decimal('0.5'))
        self.assertEqual(metrics['total_gross'], Decimal('15.00'))
        self.assertEqual(metrics['commission'], Decimal('0.750'))
        self.assertTrue(metrics['commission_complete'])
        self.assertEqual(metrics['duplicate_archive_count'], 1)

    def test_historical_archive_and_current_live_months_are_both_safe(self):
        historical = (self.month - timedelta(days=1)).replace(day=1)
        self.make_archive(deal=980008, sale_date=historical)
        Sale.objects.create(
            user=self.user, customer='Current', dealNumber=980009,
            count=Decimal('1.0'), frontEnd=Decimal('100.00'),
            backend=Decimal('50.00'), date=self.month,
        )

        past = month_metrics(self.user, historical)
        current = month_metrics(self.user, self.month)

        self.assertEqual(past['units'], Decimal('1.0'))
        self.assertIsNone(past['commission'])
        self.assertEqual(current['commission'], Decimal('15.000'))
        self.assertTrue(current['commission_complete'])

    def test_live_commission_bonus_and_adjustment_arithmetic_is_unchanged(self):
        sale = Sale.objects.create(
            user=self.user, customer='Live', dealNumber=980010,
            count=Decimal('1.0'), frontEnd=Decimal('100.00'),
            backend=Decimal('50.00'), date=self.month,
        )
        BonusLevel.objects.create(
            user=self.user, commission=self.commission,
            count_threshold=1, amount=Decimal('20.00'), active=True,
        )
        CommissionAdjustment.objects.create(
            user=self.user, commission=self.commission,
            description='Period adjustment',
            kind=CommissionAdjustment.BONUS,
            amount=Decimal('7.00'), active=True,
        )

        totals = commission_totals(self.user, [sale])
        metrics = month_metrics(self.user, self.month)

        self.assertEqual(totals['total'], Decimal('42.000'))
        self.assertEqual(metrics['commission'], totals['total'])
        self.assertEqual(metrics['commission'], Decimal('42.000'))

    def test_archive_calculation_performs_no_database_writes(self):
        self.make_archive(deal=980011)
        with CaptureQueriesContext(connection) as queries:
            month_metrics(self.user, self.month)
        writes = [
            query['sql'] for query in queries
            if query['sql'].lstrip().upper().startswith(
                ('INSERT', 'UPDATE', 'DELETE', 'REPLACE')
            )
        ]
        self.assertEqual(writes, [])

    @patch(
        'SalesLogApp.billing_entitlements.get_billing_entitlement',
        return_value=SimpleNamespace(has_pro_access=True),
    )
    def test_sc1_pages_and_team_privacy_remain_safe_with_archives(self, _entitlement):
        other = User.objects.create_user('team-other', password='pw')
        team = Team.objects.create(name='Private team', owner=other)
        TeamMembership.objects.create(
            team=team, user=self.user, role=TeamMembership.MEMBER,
            status=TeamMembership.ACTIVE,
        )
        self.make_archive(deal=980012)
        self.make_archive(
            deal=980013, owner=other, front='9999.00', back='9999.00',
        )
        self.client.force_login(self.user)

        for name in (
            'activity_goals', 'print_activity_goals', 'print_activity_history',
        ):
            with self.subTest(name=name):
                response = self.client.get(
                    reverse(name), {'month': self.month.strftime('%Y-%m')},
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Unavailable')
                self.assertNotContains(response, '9999')
                self.assertNotContains(response, 'Archived customer')


class ArchivedSaleHistoricalPayPlanTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('historical-owner', password='pw')
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system'])
        PayPlanAssignment.objects.filter(user=self.user).delete()
        self.month = timezone.localdate().replace(day=1)
        self.industry = Industry.objects.create(
            name='Archive Test Industry', slug='archive-test-industry',
        )
        self.plan = PayPlan.objects.create(
            industry=self.industry, owner_user=self.user,
            name='Historical plan', is_active=True,
        )

    def make_version(self, *, name, status, start, end=None,
                     front_rate='0.10', back_rate='0.10'):
        version = PayPlanVersion.objects.create(
            pay_plan=self.plan, version_name=name,
            effective_start_date=start, effective_end_date=end,
            status=status,
            activated_at=timezone.now(),
        )
        PayPlanRule.objects.create(
            pay_plan_version=version, name=f'{name} front',
            rule_type='front_gross_percentage',
            calculation_scope='per_sale',
            configuration={
                'rate': front_rate, 'gross_field': 'front_end_gross',
            },
            sort_order=1,
        )
        PayPlanRule.objects.create(
            pay_plan_version=version, name=f'{name} back',
            rule_type='back_gross_percentage',
            calculation_scope='per_sale',
            configuration={
                'rate': back_rate, 'gross_field': 'back_end_gross',
            },
            sort_order=2,
        )
        return version

    def assign(self, version, *, start, end=None):
        return PayPlanAssignment.objects.create(
            user=self.user, pay_plan_version=version,
            effective_start_date=start, effective_end_date=end,
            is_active=True,
        )

    def make_archive(self, *, deal, count='1.0', front='100.00',
                     back='50.00', sale_date=None, condition=''):
        return ArchivedSale.objects.create(
            user=self.user, customer='Historical archive', dealNumber=deal,
            count=Decimal(count), frontEnd=Decimal(front),
            backend=Decimal(back), date=sale_date or self.month,
            vehicle_condition=condition,
        )

    def test_half_deal_is_half_and_double_count_pays_once(self):
        version = self.make_version(
            name='Current', status=PayPlanVersion.ACTIVE, start=self.month,
        )
        self.assign(version, start=self.month)
        self.make_archive(deal=981001, count='0.5')
        self.make_archive(deal=981002, count='2.0')

        metrics = month_metrics(self.user, self.month)

        self.assertTrue(metrics['commission_complete'])
        self.assertEqual(metrics['commission_source'], 'historical_pay_plan')
        self.assertEqual(metrics['units'], Decimal('2.5'))
        self.assertEqual(metrics['commission'], Decimal('22.50'))

    def test_archive_uses_effective_dated_inactive_version_not_current_plan(self):
        historical = (self.month - timedelta(days=1)).replace(day=1)
        old_end = self.month - timedelta(days=1)
        old = self.make_version(
            name='Historical 10 percent', status=PayPlanVersion.INACTIVE,
            start=historical, end=old_end, front_rate='0.10',
        )
        current = self.make_version(
            name='Unrelated current 50 percent', status=PayPlanVersion.ACTIVE,
            start=self.month, front_rate='0.50',
        )
        self.assign(old, start=historical, end=old_end)
        self.assign(current, start=self.month)
        self.make_archive(
            deal=981003, front='100.00', back='0.00', sale_date=historical,
        )

        metrics = month_metrics(self.user, historical)

        self.assertTrue(metrics['commission_complete'])
        self.assertEqual(metrics['commission'], Decimal('10.00'))

    def test_missing_historical_condition_data_is_incomplete_not_zero(self):
        version = self.make_version(
            name='Conditioned', status=PayPlanVersion.ACTIVE,
            start=self.month,
        )
        front_rule = version.rules.get(rule_type='front_gross_percentage')
        PayPlanRuleCondition.objects.create(
            rule=front_rule, field_name='vehicle_condition',
            operator='equals', value='new', sort_order=1,
        )
        self.assign(version, start=self.month)
        self.make_archive(deal=981004, condition='')

        metrics = month_metrics(self.user, self.month)

        self.assertIsNone(metrics['commission'])
        self.assertFalse(metrics['commission_complete'])
        self.assertEqual(
            metrics['commission_source'], 'historical_pay_plan_incomplete',
        )

    def test_plan_activated_after_archive_is_not_used_retroactively(self):
        archive = self.make_archive(deal=981007)
        ArchivedSale.objects.filter(pk=archive.pk).update(
            archived_on=timezone.localdate() - timedelta(days=1),
        )
        version = self.make_version(
            name='Later unrelated plan', status=PayPlanVersion.ACTIVE,
            start=self.month,
        )
        self.assign(version, start=self.month)

        metrics = month_metrics(self.user, self.month)

        self.assertIsNone(metrics['commission'])
        self.assertFalse(metrics['commission_complete'])

    def test_combined_period_bonus_is_applied_once(self):
        version = self.make_version(
            name='Bonus', status=PayPlanVersion.ACTIVE, start=self.month,
        )
        PayPlanRule.objects.create(
            pay_plan_version=version, name='Two unit bonus',
            rule_type='volume_bonus', calculation_scope='period',
            configuration={
                'tier_mode': 'highest_only',
                'tiers': [
                    {'minimum_units': '2', 'maximum_units': None,
                     'amount': '100.00'},
                ],
            },
            sort_order=3,
        )
        self.assign(version, start=self.month)
        Sale.objects.create(
            user=self.user, customer='Live', dealNumber=981005,
            count=Decimal('1.0'), frontEnd=Decimal('100.00'),
            backend=Decimal('0.00'), date=self.month,
        )
        self.make_archive(
            deal=981006, count='1.0', front='100.00', back='0.00',
        )

        metrics = month_metrics(self.user, self.month)

        self.assertTrue(metrics['commission_complete'])
        self.assertEqual(metrics['units'], Decimal('2.0'))
        self.assertEqual(metrics['commission'], Decimal('120.00'))
