from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import CommissionAdjustmentForm, SaleForm
from .models.sales import (
    BonusLevel,
    Commission,
    CommissionAdjustment,
    Sale,
    calculate_bonus,
)


class CommissionBonusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='password')
        self.other_user = User.objects.create_user(username='other', password='password')
        self.commission = Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.10'),
            total_calculated_back_end=Decimal('0.10'),
        )
        self.other_commission = Commission.objects.create(user=self.other_user)

    def make_sale(self, *, user=None, count='1.0', sale_date=None, deal_number=1):
        return Sale.objects.create(
            user=user or self.user,
            customer='Customer',
            dealNumber=deal_number,
            count=Decimal(count),
            frontEnd=Decimal('100.00'),
            backend=Decimal('50.00'),
            date=sale_date or timezone.localdate(),
        )

    def make_tier(self, threshold, amount, *, active=True, user=None, commission=None):
        return BonusLevel.objects.create(
            user=user or self.user,
            commission=commission or self.commission,
            count_threshold=threshold,
            amount=Decimal(amount),
            active=active,
            tied_to_units=True,
        )

    def test_bonus_uses_only_highest_qualifying_active_tier(self):
        sales = [self.make_sale(count='2.0')]
        self.make_tier(1, '100.00')
        self.make_tier(2, '250.00')
        self.make_tier(3, '500.00')

        self.assertEqual(
            calculate_bonus(sales, self.commission.bonus_levels_set.all()),
            Decimal('250.00'),
        )

    def test_inactive_tier_is_not_selected(self):
        sales = [self.make_sale(count='2.0')]
        self.make_tier(1, '100.00')
        self.make_tier(2, '250.00', active=False)

        self.assertEqual(
            calculate_bonus(sales, self.commission.bonus_levels_set.all()),
            Decimal('100.00'),
        )

    def test_commission_view_uses_current_month_and_owner_data_only(self):
        today = timezone.localdate()
        previous_month = today.replace(day=1) - timedelta(days=1)
        self.make_sale(count='2.0', deal_number=1)
        self.make_sale(count='2.0', sale_date=previous_month, deal_number=2)
        self.make_sale(user=self.other_user, count='2.0', deal_number=3)
        self.make_tier(2, '250.00')
        self.make_tier(
            1,
            '999.00',
            user=self.other_user,
            commission=self.other_commission,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('view_commission'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], Decimal('2.0'))
        self.assertEqual(response.context['total_bonus'], Decimal('250.00'))
        self.assertEqual(list(response.context['sales']), [Sale.objects.get(dealNumber=1)])

    def test_sale_commission_properties_use_the_sale_owner_settings(self):
        sale = self.make_sale()
        Commission.objects.filter(pk=self.other_commission.pk).update(
            total_calculated_front_end=Decimal('0.99'),
            total_calculated_back_end=Decimal('0.99'),
        )

        self.assertEqual(sale.calculate_frontEnd, Decimal('10.0000'))
        self.assertEqual(sale.calculate_backend, Decimal('5.0000'))
        self.assertEqual(sale.commission_total, Decimal('15.0000'))

    def test_view_sales_shows_combined_commission_column(self):
        self.make_sale()
        self.client.force_login(self.user)

        response = self.client.get(reverse('view_sales'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Commission Total')
        self.assertContains(response, '$15.00')
        self.assertNotContains(response, '<th>Front End</th>', html=True)
        self.assertNotContains(response, '<th>Back End</th>', html=True)

    def test_view_sales_summary_uses_current_month_commission_and_bonus(self):
        previous_month = timezone.localdate().replace(day=1) - timedelta(days=1)
        self.make_sale(count='2.0', deal_number=1)
        self.make_sale(count='2.0', sale_date=previous_month, deal_number=2)
        self.make_sale(user=self.other_user, count='2.0', deal_number=3)
        self.make_tier(2, '250.00')
        self.client.force_login(self.user)

        response = self.client.get(reverse('view_sales'))

        self.assertEqual(response.context['total_count'], Decimal('2.0'))
        self.assertEqual(response.context['total_commission'], Decimal('265.0000'))
        self.assertContains(response, 'Total Count')
        self.assertContains(response, 'Total Commission')
        self.assertNotContains(response, 'id="totalCount"')

    def test_user_without_commission_is_redirected_to_commission_setup(self):
        new_user = User.objects.create_user(username='new-user', password='password')
        self.client.force_login(new_user)

        response = self.client.get(reverse('view_sales'))

        self.assertRedirects(response, reverse('adjust_commission'))

        setup_response = self.client.get(response.url)
        self.assertEqual(setup_response.status_code, 200)
        self.assertTrue(Commission.objects.filter(user=new_user).exists())

    def test_commission_setup_urls_reverse_with_and_without_id(self):
        self.assertEqual(reverse('adjust_commission'), '/SalesLogApp/adjust_commission/')
        self.assertEqual(
            reverse('adjust_commission_by_id', args=[self.commission.id]),
            f'/SalesLogApp/adjust_commission/{self.commission.id}/',
        )

    def test_commission_form_accepts_human_readable_percentages(self):
        form = CommissionAdjustmentForm(
            data={
                'total_calculated_front_end': '25',
                'frontend_minimum': '',
                'frontend_maximum': '',
                'total_calculated_back_end': '5',
                'backend_minimum': '',
                'backend_maximum': '',
            },
            instance=self.commission,
        )

        self.assertTrue(form.is_valid(), form.errors)
        commission = form.save()
        self.assertEqual(commission.total_calculated_front_end, Decimal('0.25'))
        self.assertEqual(commission.total_calculated_back_end, Decimal('0.05'))

    def test_blank_backend_percentage_is_saved_as_zero(self):
        form = CommissionAdjustmentForm(
            data={
                'total_calculated_front_end': '25',
                'frontend_minimum': '',
                'frontend_maximum': '',
                'total_calculated_back_end': '',
                'backend_minimum': '',
                'backend_maximum': '',
            },
            instance=self.commission,
        )

        self.assertTrue(form.is_valid(), form.errors)
        commission = form.save()
        self.assertEqual(commission.total_calculated_back_end, Decimal('0'))

    def test_sale_backend_accepts_whole_dollars_only(self):
        valid_form = SaleForm(
            data={
                'customer': 'Customer',
                'date': timezone.localdate(),
                'frontEnd': '100.00',
                'backend': '50',
                'dealNumber': '9876',
                'count': '1',
            }
        )
        decimal_form = SaleForm(
            data={
                'customer': 'Customer',
                'date': timezone.localdate(),
                'frontEnd': '100.00',
                'backend': '50.25',
                'dealNumber': '9877',
                'count': '1',
            }
        )

        self.assertTrue(valid_form.is_valid(), valid_form.errors)
        self.assertFalse(decimal_form.is_valid())
        self.assertIn('backend', decimal_form.errors)

    def test_adjust_commission_page_renders_settings_and_bonus_formset(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse('adjust_commission_by_id', args=[self.commission.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Front-end commission')
        self.assertContains(response, 'Back-end commission')
        self.assertContains(response, 'Unit bonus tiers')
        self.assertContains(response, 'Enter 25 for 25%.')

    def test_other_adjustments_are_signed_without_changing_unit_bonus(self):
        sale = self.make_sale(count='2.0')
        self.make_tier(2, '250.00')
        CommissionAdjustment.objects.create(
            user=self.user,
            commission=self.commission,
            description='Customer satisfaction bonus',
            kind=CommissionAdjustment.BONUS,
            amount=Decimal('100.00'),
        )
        CommissionAdjustment.objects.create(
            user=self.user,
            commission=self.commission,
            description='Policy deduction',
            kind=CommissionAdjustment.DEDUCTION,
            amount=Decimal('25.00'),
        )
        CommissionAdjustment.objects.create(
            user=self.user,
            commission=self.commission,
            description='Inactive bonus',
            kind=CommissionAdjustment.BONUS,
            amount=Decimal('999.00'),
            active=False,
        )
        CommissionAdjustment.objects.create(
            user=self.other_user,
            commission=self.other_commission,
            description='Other user bonus',
            kind=CommissionAdjustment.BONUS,
            amount=Decimal('999.00'),
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('view_commission'))

        self.assertEqual(response.context['total_bonus'], Decimal('250.00'))
        self.assertEqual(response.context['total_adjustments'], Decimal('75.00'))
        self.assertEqual(response.context['total_commission'], Decimal('340.0000'))
        self.assertContains(response, 'Customer satisfaction bonus')
        self.assertContains(response, 'Policy deduction')
        self.assertNotContains(response, 'Inactive bonus')
        self.assertNotContains(response, 'Other user bonus')

    def test_adjustment_formset_is_scoped_to_the_signed_in_user(self):
        owned = CommissionAdjustment.objects.create(
            user=self.user,
            commission=self.commission,
            description='Owned adjustment',
            kind=CommissionAdjustment.BONUS,
            amount=Decimal('50.00'),
        )
        CommissionAdjustment.objects.create(
            user=self.other_user,
            commission=self.other_commission,
            description='Private adjustment',
            kind=CommissionAdjustment.BONUS,
            amount=Decimal('75.00'),
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse('adjust_commission_by_id', args=[self.commission.id])
        )

        adjustment_formset = response.context['adjustment_formset']
        self.assertEqual(list(adjustment_formset.queryset), [owned])
        self.assertContains(response, 'Other bonuses and deductions')
        self.assertNotContains(response, 'Private adjustment')
