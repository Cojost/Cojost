from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import VehicleForm
from .models import (
    ArchivedVehicle, Commission, Sale, Vehicle, VehicleMake, VehicleModel,
)
from .services import archive_sale


class VehicleFeatureTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user('vehicle-owner', password='pass')
        self.other = User.objects.create_user('vehicle-other', password='pass')
        Commission.objects.create(user=self.user)
        Commission.objects.create(user=self.other)
        self.make = VehicleMake.objects.create(name='Subaru', verified=True)
        self.model = VehicleModel.objects.create(
            make=self.make, name='Outback', verified=True
        )
        self.sale_data = {
            'customer': 'Customer', 'date': timezone.localdate(),
            'frontEnd': '1000.00', 'backend': '500', 'dealNumber': '81001',
            'count': '1', 'split_with_name': '',
        }
        self.vehicle_data = {
            'year': str(timezone.localdate().year), 'make': 'Subaru',
            'make_id': str(self.make.pk), 'model': 'Outback',
            'model_id': str(self.model.pk), 'mileage': '12345',
            'stock_number': ' ab-12 ', 'vin': '1hgcm82633a004352',
        }

    def create_sale(self, user=None, deal=81002):
        return Sale.objects.create(
            user=user or self.user, customer='Existing', dealNumber=deal,
            count='1.0', frontEnd='100', backend='50', date=date.today(),
        )

    def add_vehicle(self, sale, data=None, user=None):
        form = VehicleForm(data or self.vehicle_data, user=user or sale.user)
        self.assertTrue(form.is_valid(), form.errors)
        return form.save(sale)

    def test_year_choices_and_invalid_year(self):
        form = VehicleForm(user=self.user)
        years = [value for value, _ in form.fields['year'].choices]
        self.assertEqual(years[0], timezone.localdate().year + 1)
        self.assertEqual(years[-1], 2000)
        invalid = VehicleForm(
            {**self.vehicle_data, 'year': '1999'}, user=self.user
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn('year', invalid.errors)

    def test_normalization_and_vehicle_validation(self):
        form = VehicleForm(self.vehicle_data, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['stock_number'], 'AB-12')
        self.assertEqual(form.cleaned_data['vin'], '1HGCM82633A004352')
        for changes in (
            {'vin': '1HGCM82633A00435I'},
            {'mileage': '10000001'},
        ):
            invalid = VehicleForm({**self.vehicle_data, **changes}, user=self.user)
            self.assertFalse(invalid.is_valid())

    def test_model_must_belong_to_make(self):
        other_make = VehicleMake.objects.create(name='Honda')
        other_model = VehicleModel.objects.create(make=other_make, name='Civic')
        form = VehicleForm({
            **self.vehicle_data, 'model': other_model.name,
            'model_id': other_model.pk,
        }, user=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn('model', form.errors)

    def test_catalog_names_are_case_insensitively_unique(self):
        with self.assertRaises(ValidationError):
            VehicleMake.objects.create(name=' SUBARU ')
        with self.assertRaises(ValidationError):
            VehicleModel.objects.create(make=self.make, name=' OUTBACK ')

    def test_explicit_custom_catalog_creation(self):
        sale = self.create_sale()
        form = VehicleForm({
            **self.vehicle_data, 'make': ' rivian ', 'make_id': '',
            'add_make': 'on', 'model': ' r1s ', 'model_id': '',
            'add_model': 'on',
        }, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        vehicle = form.save(sale)
        self.assertEqual(vehicle.make.name, 'Rivian')
        self.assertEqual(vehicle.model.name, 'R1S')
        self.assertFalse(vehicle.make.verified)
        self.assertEqual(vehicle.make.created_by, self.user)

    def test_new_sale_requires_vehicle_and_saves_atomically(self):
        self.client.force_login(self.user)
        missing = self.client.post(reverse('add_sale'), self.sale_data)
        self.assertEqual(missing.status_code, 200)
        self.assertFalse(Sale.objects.filter(dealNumber=81001).exists())
        response = self.client.post(
            reverse('add_sale'), {**self.sale_data, **self.vehicle_data}
        )
        self.assertRedirects(response, reverse('view_sales'))
        self.assertTrue(Vehicle.objects.filter(sale__dealNumber=81001).exists())

    def test_existing_sale_without_vehicle_remains_editable(self):
        sale = self.create_sale(deal=81003)
        self.client.force_login(self.user)
        response = self.client.post(reverse('edit_sale', args=[sale.pk]), {
            **self.sale_data, 'dealNumber': sale.dealNumber,
            'customer': 'Updated', 'year': '', 'make': '', 'model': '',
            'mileage': '', 'stock_number': '', 'vin': '',
        })
        self.assertRedirects(response, reverse('view_sales'))
        sale.refresh_from_db()
        self.assertEqual(sale.customer, 'Updated')
        self.assertFalse(Vehicle.objects.filter(sale=sale).exists())

    def test_edit_updates_vehicle_instead_of_duplicating(self):
        sale = self.create_sale(deal=81004)
        self.add_vehicle(sale)
        self.client.force_login(self.user)
        response = self.client.post(reverse('edit_sale', args=[sale.pk]), {
            **self.sale_data, **self.vehicle_data, 'dealNumber': sale.dealNumber,
            'mileage': '54321',
        })
        self.assertRedirects(response, reverse('view_sales'))
        self.assertEqual(Vehicle.objects.filter(sale=sale).count(), 1)
        self.assertEqual(sale.vehicle.mileage, 54321)

    def test_view_dialog_and_print_are_owner_isolated(self):
        sale = self.create_sale(deal=81005)
        self.add_vehicle(sale)
        private = self.create_sale(user=self.other, deal=81006)
        self.add_vehicle(private, {
            **self.vehicle_data, 'stock_number': 'PRIVATE-STOCK',
            'vin': '1M8GDM9AXKP042788',
        }, user=self.other)
        self.client.force_login(self.user)
        page = self.client.get(reverse('view_sales'))
        self.assertContains(page, f'{sale.vehicle.year} Outback')
        self.assertContains(page, 'AB-12')
        self.assertNotContains(page, 'PRIVATE-STOCK')
        printed = self.client.get(reverse('print_sales'))
        self.assertContains(printed, f'{sale.vehicle.year} Outback')
        self.assertNotContains(printed, sale.vehicle.vin)

    def test_autocomplete_is_login_required_scoped_and_limited(self):
        url = reverse('vehicle_make_search')
        self.assertEqual(self.client.get(url).status_code, 302)
        for number in range(25):
            VehicleMake.objects.create(name=f'Test Make {number}')
        self.client.force_login(self.user)
        payload = self.client.get(url, {'q': 'test'}).json()
        self.assertEqual(len(payload['results']), 20)
        models = self.client.get(
            reverse('vehicle_model_search'),
            {'make_id': self.make.pk, 'q': 'out'},
        ).json()
        self.assertEqual(models['results'][0]['name'], 'Outback')

    def test_vehicle_survives_atomic_archive(self):
        sale = self.create_sale(deal=81007)
        self.add_vehicle(sale)
        archived = archive_sale(sale)
        snapshot = ArchivedVehicle.objects.get(archived_sale=archived)
        self.assertEqual(snapshot.make_name, 'Subaru')
        self.assertEqual(snapshot.vin, '1HGCM82633A004352')
        self.assertEqual(archived.user, self.user)


class VehicleCatalogCommandTests(TestCase):
    @patch('SalesLogApp.management.commands.sync_vehicle_catalog.fetch_results')
    def test_sync_is_repeatable(self, fetch):
        fetch.side_effect = [
            [{'MakeId': 1, 'MakeName': 'Subaru'}],
            [{'Model_Name': 'Outback'}],
            [{'MakeId': 1, 'MakeName': 'Subaru'}],
            [{'Model_Name': 'Outback'}],
        ]
        call_command('sync_vehicle_catalog')
        call_command('sync_vehicle_catalog')
        self.assertEqual(VehicleMake.objects.count(), 1)
        self.assertEqual(VehicleModel.objects.count(), 1)

    @patch('SalesLogApp.management.commands.sync_vehicle_catalog.fetch_results')
    def test_sync_external_failure_is_clear_and_keeps_local_catalog(self, fetch):
        VehicleMake.objects.create(name='Local Make')
        fetch.side_effect = CommandError('NHTSA vPIC catalog request failed')
        with self.assertRaises(CommandError):
            call_command('sync_vehicle_catalog')
        self.assertTrue(VehicleMake.objects.filter(name='Local Make').exists())

    @patch('SalesLogApp.management.commands.sync_vehicle_catalog.fetch_results')
    def test_sync_dry_run_rolls_back(self, fetch):
        fetch.side_effect = [
            [{'MakeId': 2, 'MakeName': 'Honda'}],
            [{'Model_Name': 'Civic'}],
        ]
        call_command('sync_vehicle_catalog', dry_run=True)
        self.assertFalse(VehicleMake.objects.exists())
