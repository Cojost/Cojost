from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
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

    def test_half_deal_pays_half_commission_and_counts_half_a_unit(self):
        self.commission.total_calculated_front_end = Decimal('0.25')
        self.commission.total_calculated_back_end = Decimal('0.05')
        self.commission.save(update_fields=['total_calculated_front_end', 'total_calculated_back_end'])
        sale = Sale.objects.create(
            user=self.user,
            customer='Half Buyer',
            dealNumber=100,
            count=Decimal('0.5'),
            frontEnd=Decimal('4000.00'),
            backend=Decimal('3000.00'),
            date=timezone.localdate(),
        )

        self.assertEqual(sale.calculate_frontEnd, Decimal('500.00'))
        self.assertEqual(sale.calculate_backend, Decimal('75.00'))
        self.assertEqual(sale.commission_total, Decimal('575.00'))
        self.assertEqual(sale.count, Decimal('0.5'))

    def test_double_counted_deal_counts_as_two_units_but_pays_full_commission(self):
        self.commission.total_calculated_front_end = Decimal('0.25')
        self.commission.total_calculated_back_end = Decimal('0.05')
        self.commission.save(update_fields=['total_calculated_front_end', 'total_calculated_back_end'])
        sale = Sale.objects.create(
            user=self.user,
            customer='Double Buyer',
            dealNumber=101,
            count=Decimal('2.0'),
            frontEnd=Decimal('4000.00'),
            backend=Decimal('3000.00'),
            date=timezone.localdate(),
        )

        self.assertEqual(sale.calculate_frontEnd, Decimal('1000.00'))
        self.assertEqual(sale.calculate_backend, Decimal('150.00'))
        self.assertEqual(sale.commission_total, Decimal('1150.00'))
        self.assertEqual(sale.count, Decimal('2.0'))

    def test_minimum_commission_applies_before_half_deal_multiplier(self):
        self.commission.total_calculated_front_end = Decimal('0.25')
        self.commission.frontend_minimum = Decimal('250.00')
        self.commission.total_calculated_back_end = Decimal('0.00')
        self.commission.save(update_fields=['total_calculated_front_end', 'frontend_minimum', 'total_calculated_back_end'])

        half_sale = Sale.objects.create(
            user=self.user,
            customer='Min Half',
            dealNumber=102,
            count=Decimal('0.5'),
            frontEnd=Decimal('400.00'),
            backend=Decimal('0.00'),
            date=timezone.localdate(),
        )
        double_sale = Sale.objects.create(
            user=self.user,
            customer='Min Double',
            dealNumber=103,
            count=Decimal('2.0'),
            frontEnd=Decimal('400.00'),
            backend=Decimal('0.00'),
            date=timezone.localdate(),
        )

        self.assertEqual(half_sale.calculate_frontEnd, Decimal('125.00'))
        self.assertEqual(double_sale.calculate_frontEnd, Decimal('250.00'))
        self.assertEqual(double_sale.count, Decimal('2.0'))

    def test_monthly_totals_include_half_and_double_units_without_double_commission(self):
        self.commission.total_calculated_front_end = Decimal('0.25')
        self.commission.total_calculated_back_end = Decimal('0.00')
        self.commission.save(update_fields=['total_calculated_front_end', 'total_calculated_back_end'])

        sale1 = Sale.objects.create(
            user=self.user,
            customer='Regular',
            dealNumber=104,
            count=Decimal('1.0'),
            frontEnd=Decimal('2000.00'),
            backend=Decimal('0.00'),
            date=timezone.localdate(),
        )
        sale2 = Sale.objects.create(
            user=self.user,
            customer='Half',
            dealNumber=105,
            count=Decimal('0.5'),
            frontEnd=Decimal('2000.00'),
            backend=Decimal('0.00'),
            date=timezone.localdate(),
        )
        sale3 = Sale.objects.create(
            user=self.user,
            customer='Double',
            dealNumber=106,
            count=Decimal('2.0'),
            frontEnd=Decimal('2000.00'),
            backend=Decimal('0.00'),
            date=timezone.localdate(),
        )

        sales = [sale1, sale2, sale3]
        self.assertEqual(sum(s.count for s in sales), Decimal('3.5'))
        self.assertEqual(sum(s.calculate_frontEnd for s in sales), Decimal('1250.00'))

    def test_double_counted_deal_helps_bonus_tier_without_double_payout(self):
        self.commission.total_calculated_front_end = Decimal('0.25')
        self.commission.total_calculated_back_end = Decimal('0.00')
        self.commission.save(update_fields=['total_calculated_front_end', 'total_calculated_back_end'])
        for idx in range(8):
            Sale.objects.create(
                user=self.user,
                customer=f'Regular {idx}',
                dealNumber=200 + idx,
                count=Decimal('1.0'),
                frontEnd=Decimal('2000.00'),
                backend=Decimal('0.00'),
                date=timezone.localdate(),
            )
        double_sale = Sale.objects.create(
            user=self.user,
            customer='Double Bonus',
            dealNumber=999,
            count=Decimal('2.0'),
            frontEnd=Decimal('2000.00'),
            backend=Decimal('0.00'),
            date=timezone.localdate(),
        )
        self.make_tier(10, '500.00')
        sales = list(Sale.objects.filter(user=self.user))

        self.assertEqual(sum(s.count for s in sales), Decimal('10.0'))
        self.assertEqual(calculate_bonus(sales, self.commission.bonus_levels_set.all()), Decimal('500.00'))
        self.assertEqual(double_sale.calculate_frontEnd, Decimal('500.00'))
        self.assertEqual(double_sale.calculate_backend, Decimal('0.00'))
        self.assertEqual(double_sale.commission_total, Decimal('500.00'))

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

    def test_legacy_user_without_commission_can_access_sales_without_forced_migration(self):
        new_user = User.objects.create_user(username='new-user', password='password')
        self.client.force_login(new_user)

        response = self.client.get(reverse('view_sales'))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Commission.objects.filter(user=new_user).exists())

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

    def test_backend_percentage_displays_two_places_and_preserves_rate(self):
        self.commission.total_calculated_back_end = Decimal('0.000')
        self.commission.save(update_fields=['total_calculated_back_end'])
        zero_form = CommissionAdjustmentForm(instance=self.commission)
        self.assertEqual(
            zero_form['total_calculated_back_end'].value(),
            Decimal('0.00'),
        )
        self.assertEqual(
            zero_form.fields['total_calculated_back_end'].widget.attrs['step'],
            '0.01',
        )

        self.commission.total_calculated_back_end = Decimal('0.003')
        self.commission.save(update_fields=['total_calculated_back_end'])
        rate_form = CommissionAdjustmentForm(instance=self.commission)
        self.assertEqual(
            rate_form['total_calculated_back_end'].value(),
            Decimal('0.30'),
        )
        self.assertEqual(
            rate_form['total_calculated_back_end'].as_widget().count('step="0.01"'),
            1,
        )

    def test_backend_percentage_round_trip_and_calculation_are_unchanged(self):
        form = CommissionAdjustmentForm(
            data={
                'total_calculated_front_end': '10.00',
                'frontend_minimum': '',
                'frontend_maximum': '',
                'total_calculated_back_end': '0.30',
                'backend_minimum': '',
                'backend_maximum': '',
            },
            instance=self.commission,
        )
        self.assertTrue(form.is_valid(), form.errors)
        commission = form.save()
        self.assertEqual(
            commission.total_calculated_back_end,
            Decimal('0.003'),
        )
        self.assertEqual(
            commission.calculate_backend(Decimal('1000')),
            Decimal('3.000'),
        )

        unchanged = CommissionAdjustmentForm(
            data={
                'total_calculated_front_end': '10.00',
                'frontend_minimum': '',
                'frontend_maximum': '',
                'total_calculated_back_end': '0.30',
                'backend_minimum': '',
                'backend_maximum': '',
            },
            instance=commission,
        )
        self.assertTrue(unchanged.is_valid(), unchanged.errors)
        unchanged.save()
        commission.refresh_from_db()
        self.assertEqual(
            commission.total_calculated_back_end,
            Decimal('0.003'),
        )

    def test_sale_gross_fields_accept_zero_and_cents(self):
        valid_form = SaleForm(
            data={
                'customer': 'Customer',
                'date': timezone.localdate(),
                'frontEnd': '0.00',
                'backend': '0.00',
                'dealNumber': '9876',
                'count': '1',
            }
        )
        decimal_form = SaleForm(
            data={
                'customer': 'Customer',
                'date': timezone.localdate(),
                'frontEnd': '2706.02',
                'backend': '50.25',
                'dealNumber': '9877',
                'count': '1',
            }
        )

        self.assertTrue(valid_form.is_valid(), valid_form.errors)
        self.assertTrue(decimal_form.is_valid(), decimal_form.errors)
        self.assertEqual(valid_form.cleaned_data['frontEnd'], Decimal('0.00'))
        self.assertEqual(valid_form.cleaned_data['backend'], Decimal('0.00'))
        self.assertEqual(decimal_form.cleaned_data['frontEnd'], Decimal('2706.02'))
        self.assertEqual(decimal_form.cleaned_data['backend'], Decimal('50.25'))

    def test_sale_gross_fields_accept_negative_values(self):
        form = SaleForm(data={
            'customer': 'Customer',
            'date': timezone.localdate(),
            'frontEnd': '-0.01',
            'backend': '-1.00',
            'dealNumber': '9878',
            'count': '1',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['frontEnd'], Decimal('-0.01'))
        self.assertEqual(form.cleaned_data['backend'], Decimal('-1.00'))

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

    def test_adjust_commission_with_invalid_id_redirects_to_user_commission(self):
        self.client.force_login(self.user)
        other_commission = Commission.objects.create(user=self.other_user)

        response = self.client.get(
            reverse('adjust_commission_by_id', args=[other_commission.id])
        )

        self.assertRedirects(
            response,
            reverse('adjust_commission_by_id', args=[self.commission.id]),
        )

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
