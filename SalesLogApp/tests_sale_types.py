from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    ArchivedSale, Commission, Sale, SaleType, Vehicle, VehicleMake, VehicleModel,
)
from .services import archive_sale, sales_month_context


class AutomotiveSaleTypeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user('type-owner', password='pass')
        self.other = User.objects.create_user('type-other', password='pass')
        Commission.objects.create(
            user=self.user, total_calculated_front_end=Decimal('0.25'),
            total_calculated_back_end=Decimal('0.05'),
        )
        Commission.objects.create(user=self.other)
        self.make = VehicleMake.objects.create(name='Subaru', verified=True)
        self.model = VehicleModel.objects.create(
            make=self.make, name='Outback', verified=True
        )

    def make_sale(self, deal=91001, user=None):
        return Sale.objects.create(
            user=user or self.user, customer='Buyer', dealNumber=deal,
            count=Decimal('0.5'), split_with_name='Partner',
            frontEnd=Decimal('1000'), backend=Decimal('500'),
            date=timezone.localdate(),
        )

    def add_vehicle(self, sale):
        return Vehicle.objects.create(
            sale=sale, year=timezone.localdate().year, make=self.make,
            model=self.model, mileage=12000, stock_number='ST-1',
            vin='1HGCM82633A004352',
        )

    def test_existing_and_new_records_default_to_automotive(self):
        sale = self.make_sale()
        archived = ArchivedSale.objects.create(
            user=self.user, customer='Old Buyer', dealNumber=91002,
            count=1, frontEnd=100, backend=50, date=timezone.localdate(),
        )
        self.assertEqual(sale.sale_type, SaleType.AUTOMOTIVE)
        self.assertEqual(archived.sale_type, SaleType.AUTOMOTIVE)
        self.assertEqual(SaleType.choices, [('automotive', 'Automotive')])

    def test_database_rejects_unsupported_sale_type(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.create(
                user=self.user, customer='Invalid', dealNumber=91003, count=1,
                frontEnd=100, backend=50, date=timezone.localdate(),
                sale_type='real_estate',
            )

    def test_sale_form_has_no_type_selector_or_other_industries(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('add_sale'))
        self.assertNotContains(response, 'name="sale_type"')
        for unsupported in ('Real estate', 'Insurance', 'Retail', 'SaaS'):
            self.assertNotContains(response, unsupported)

    def test_forged_type_is_ignored_and_automotive_details_remain_separate(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('add_sale'), {
            'customer': 'New Buyer', 'date': timezone.localdate(),
            'frontEnd': '1000', 'backend': '500', 'dealNumber': '91004',
            'count': '1', 'split_with_name': '', 'sale_type': 'insurance',
            'year': timezone.localdate().year, 'make': self.make.name,
            'make_id': self.make.pk, 'model': self.model.name,
            'model_id': self.model.pk, 'mileage': '1000',
            'stock_number': 'NEW-1', 'vin': '1HGCM82633A004352',
        })
        self.assertRedirects(response, reverse('view_sales'))
        sale = Sale.objects.get(dealNumber=91004)
        self.assertEqual(sale.sale_type, SaleType.AUTOMOTIVE)
        self.assertFalse(any(field.name == 'vin' for field in Sale._meta.fields))
        self.assertEqual(sale.vehicle.model, self.model)

    def test_commission_math_is_unchanged(self):
        sale = self.make_sale(deal=91005)
        self.assertEqual(sale.calculate_frontEnd, Decimal('250.00'))
        self.assertEqual(sale.calculate_backend, Decimal('25.00'))
        self.assertEqual(sale.commission_total, Decimal('275.00'))

    def test_archive_keeps_type_vehicle_split_and_owner(self):
        sale = self.make_sale(deal=91006)
        self.add_vehicle(sale)
        archived = archive_sale(sale)
        self.assertEqual(archived.sale_type, SaleType.AUTOMOTIVE)
        self.assertEqual(archived.split_with_name, 'Partner')
        self.assertEqual(archived.user, self.user)
        self.assertEqual(archived.vehicle.model_name, 'Outback')
        self.assertEqual(archived.vehicle.vin, '1HGCM82633A004352')

    def test_vehicle_select_related_avoids_detail_n_plus_one(self):
        first = self.make_sale(deal=91007)
        second = self.make_sale(deal=91008)
        self.add_vehicle(first)
        self.add_vehicle(second)
        context = sales_month_context(
            self.user, timezone.localdate().replace(day=1)
        )
        sales = list(context['sales'])
        with self.assertNumQueries(0):
            labels = [
                f'{sale.vehicle.year} {sale.vehicle.make.name} {sale.vehicle.model.name}'
                for sale in sales
            ]
        self.assertEqual(len(labels), 2)

    def test_views_do_not_expose_another_users_automotive_details(self):
        private = self.make_sale(deal=91009, user=self.other)
        self.add_vehicle(private)
        private.vehicle.stock_number = 'PRIVATE-ONLY'
        private.vehicle.save()
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse('view_sales')), 'PRIVATE-ONLY')
