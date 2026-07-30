from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import SaleForm
from .models.sales import ArchivedSale, Commission, Sale
from .models import VehicleMake, VehicleModel


class SplitDealTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('split-owner', password='pw')
        self.other = User.objects.create_user('split-other', password='pw')
        Commission.objects.create(user=self.user, opt_out_front=True, opt_out_back=True)
        Commission.objects.create(user=self.other, opt_out_front=True, opt_out_back=True)
        self.make = VehicleMake.objects.create(name='Subaru')
        self.model = VehicleModel.objects.create(make=self.make, name='Outback')
        self.base_data = {
            'customer': 'Buyer',
            'date': timezone.localdate(),
            'frontEnd': '100',
            'backend': '50',
            'dealNumber': '71001',
            'count': '0.5',
        }
        self.vehicle_data = {
            'year': str(timezone.localdate().year), 'make': self.make.name,
            'make_id': self.make.pk, 'model': self.model.name,
            'model_id': self.model.pk, 'mileage': '1000',
            'stock_number': 'SPLIT-1', 'vin': '1HGCM82633A004352',
        }

    def make_sale(self, **overrides):
        values = {
            'user': self.user, 'customer': 'Buyer', 'date': timezone.localdate(),
            'frontEnd': 100, 'backend': 50, 'dealNumber': 72001,
            'count': Decimal('0.5'), 'split_with_name': 'Alex Smith',
        }
        values.update(overrides)
        return Sale.objects.create(**values)

    def test_half_deal_accepts_trimmed_split_name(self):
        form = SaleForm(data={**self.base_data, 'split_with_name': '  Alex Smith  '})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['split_with_name'], 'Alex Smith')
        self.assertEqual(form.cleaned_data['count'], Decimal('0.5'))

    def test_add_view_saves_half_deal(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('add_sale'), {
            **self.base_data, **self.vehicle_data,
            'split_with_name': '  Alex Smith  ',
        })
        self.assertRedirects(response, reverse('view_sales'))
        sale = Sale.objects.get(user=self.user, dealNumber=71001)
        self.assertEqual(sale.count, Decimal('0.5'))
        self.assertEqual(sale.split_with_name, 'Alex Smith')

    def test_runtime_count_metadata_supports_one_decimal_place(self):
        field = Sale._meta.get_field('count')
        self.assertEqual(field.max_digits, 2)
        self.assertEqual(field.decimal_places, 1)

    def test_half_deal_rejects_missing_or_whitespace_name(self):
        for name in ('', '   '):
            form = SaleForm(data={**self.base_data, 'split_with_name': name})
            self.assertFalse(form.is_valid())
            self.assertIn('split_with_name', form.errors)

    def test_full_deal_does_not_require_and_clears_split_name(self):
        sale = self.make_sale()
        data = {
            **self.base_data, 'dealNumber': sale.dealNumber, 'count': '1',
            'split_with_name': 'Old Partner',
        }
        form = SaleForm(data=data, instance=sale)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        sale.refresh_from_db()
        self.assertEqual(sale.split_with_name, '')

    def test_full_deal_without_split_name_is_valid(self):
        form = SaleForm(data={
            **self.base_data, 'count': '1', 'split_with_name': '',
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_manual_double_count_is_not_an_add_sale_choice(self):
        form = SaleForm()
        values = [str(value) for value, _label in form.fields['count'].choices]

        self.assertEqual(values, ['1', '0.5'])

    def test_edit_half_deal_displays_saved_name(self):
        sale = self.make_sale()
        self.client.force_login(self.user)
        response = self.client.get(reverse('edit_sale', args=[sale.pk]))
        self.assertContains(response, 'Alex Smith')
        self.assertContains(response, 'Split Deal:')
        self.assertEqual(
            response.context['form']['count'].value(), Decimal('0.5')
        )

    def test_editing_half_deal_with_decimal_model_values_succeeds(self):
        sale = self.make_sale()
        self.client.force_login(self.user)
        response = self.client.post(reverse('edit_sale', args=[sale.pk]), {
            'customer': sale.customer,
            'date': sale.date,
            'frontEnd': '100.00',
            'backend': '50.00',
            'dealNumber': sale.dealNumber,
            'count': '0.5',
            'split_with_name': 'Alex Smith',
        })
        self.assertRedirects(response, reverse('view_sales'))
        sale.refresh_from_db()
        self.assertEqual(sale.count, Decimal('0.5'))
        self.assertEqual(sale.split_with_name, 'Alex Smith')

    def test_view_sales_displays_name_and_dash(self):
        self.make_sale()
        self.make_sale(
            dealNumber=72002, count=Decimal('1'), split_with_name='', customer='Full Buyer'
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse('view_sales'))
        self.assertContains(response, 'Split With')
        self.assertContains(response, 'Alex Smith')
        self.assertContains(response, '&mdash;', html=False)

    def test_other_user_cannot_view_or_edit_split_name(self):
        private_sale = self.make_sale(user=self.other, dealNumber=72003, split_with_name='Private Name')
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse('edit_sale', args=[private_sale.pk])).status_code, 404
        )
        self.assertNotContains(self.client.get(reverse('view_sales')), 'Private Name')

    def test_archive_model_preserves_split_name(self):
        archived = ArchivedSale.objects.create(
            user=self.user, customer='Archived Buyer', dealNumber=73001,
            count=Decimal('0.5'), split_with_name='Archive Partner',
            frontEnd=100, backend=50, date=timezone.localdate(),
        )
        archived.refresh_from_db()
        self.assertEqual(archived.count, Decimal('0.5'))
        self.assertEqual(archived.split_with_name, 'Archive Partner')
