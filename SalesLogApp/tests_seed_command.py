from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from SalesLogApp.models import Sale, Vehicle, VehicleMake, VehicleModel


def parse_month(value):
    return datetime.strptime(value, '%Y-%m').date()


@override_settings(DEBUG=True)
class SeedTestSalesCommandTests(TestCase):
    def setUp(self):
        self.username = 'demo_salesperson'
        self.password = 'DemoTest123!'
        self.email = 'demo.salesperson@example.com'
        self.month_arg = '2026-07'
        self.month_date = parse_month(self.month_arg)

    def test_command_creates_demo_user_and_sales(self):
        call_command('seed_test_sales', '--month', self.month_arg, '--count', '5', '--random-seed', '42')

        User = get_user_model()
        user = User.objects.get(username=self.username)
        self.assertEqual(user.email, self.email)
        self.assertIsNotNone(user.first_name)
        self.assertIsNotNone(user.last_name)
        self.assertTrue(user.check_password(self.password))

        sales = Sale.objects.filter(user=user, dealNumber__gte=900001, dealNumber__lte=900099)
        self.assertEqual(sales.count(), 5)
        self.assertTrue(all(sale.date.year == self.month_date.year and sale.date.month == self.month_date.month for sale in sales))
        self.assertTrue(all(isinstance(sale.frontEnd, Decimal) for sale in sales))
        self.assertTrue(all(isinstance(sale.backend, Decimal) for sale in sales))

    def test_sales_unique_deal_stock_and_vin(self):
        call_command('seed_test_sales', '--month', self.month_arg, '--count', '8', '--random-seed', '42')
        User = get_user_model()
        user = User.objects.get(username=self.username)

        sales = Sale.objects.filter(user=user, dealNumber__gte=900001, dealNumber__lte=900099)
        self.assertEqual(sales.count(), 8)
        self.assertEqual(sales.values_list('dealNumber', flat=True).distinct().count(), 8)

        vehicles = Vehicle.objects.filter(sale__in=sales)
        self.assertEqual(vehicles.count(), 8)
        self.assertEqual(vehicles.values_list('stock_number', flat=True).distinct().count(), 8)
        self.assertEqual(vehicles.values_list('vin', flat=True).distinct().count(), 8)

    def test_command_is_idempotent_without_reset(self):
        call_command('seed_test_sales', '--month', self.month_arg, '--count', '6', '--random-seed', '42')
        call_command('seed_test_sales', '--month', self.month_arg, '--count', '6', '--random-seed', '42')

        User = get_user_model()
        user = User.objects.get(username=self.username)
        sales = Sale.objects.filter(user=user, dealNumber__gte=900001, dealNumber__lte=900099)
        self.assertEqual(sales.count(), 6)

    def test_reset_only_deletes_demo_user_sales(self):
        call_command('seed_test_sales', '--month', self.month_arg, '--count', '3', '--random-seed', '42')
        User = get_user_model()
        user = User.objects.get(username=self.username)

        other_user = User.objects.create_user(username='other-user', password='password')
        Sale.objects.create(
            user=other_user,
            customer='Other Buyer',
            dealNumber=900050,
            count=Decimal('1.0'),
            frontEnd=Decimal('100.00'),
            backend=Decimal('50.00'),
            date=timezone.localdate(),
        )

        sales_before = Sale.objects.filter(user=user).count()
        self.assertEqual(sales_before, 3)

        call_command('seed_test_sales', '--month', self.month_arg, '--count', '4', '--random-seed', '43', '--reset')

        sales_after = Sale.objects.filter(user=user, dealNumber__gte=900001, dealNumber__lte=900099).count()
        self.assertEqual(sales_after, 4)
        self.assertTrue(Sale.objects.filter(user=other_user, dealNumber=900050).exists())

    def test_command_refuses_to_run_when_debug_false(self):
        with override_settings(DEBUG=False):
            with self.assertRaises(CommandError) as cm:
                call_command('seed_test_sales', '--month', self.month_arg, '--count', '1')
        self.assertIn('DEBUG=True', str(cm.exception))

    def test_vehicle_catalog_entries_are_created(self):
        call_command('seed_test_sales', '--month', self.month_arg, '--count', '4', '--random-seed', '42')

        self.assertTrue(VehicleMake.objects.filter(name='Subaru').exists())
        self.assertTrue(VehicleModel.objects.filter(name='Outback').exists())

    def test_commission_totals_calculate_for_seeded_sales(self):
        call_command('seed_test_sales', '--month', self.month_arg, '--count', '5', '--random-seed', '42')
        User = get_user_model()
        user = User.objects.get(username=self.username)
        sales = Sale.objects.filter(user=user, dealNumber__gte=900001, dealNumber__lte=900099)
        for sale in sales:
            _ = sale.commission_total
            self.assertIsInstance(sale.commission_total, Decimal)

    def test_dates_are_distributed_throughout_month(self):
        call_command('seed_test_sales', '--month', self.month_arg, '--count', '10', '--random-seed', '42')
        User = get_user_model()
        user = User.objects.get(username=self.username)
        sales = Sale.objects.filter(user=user, dealNumber__gte=900001, dealNumber__lte=900099)
        days = {sale.date.day for sale in sales}
        self.assertGreater(len(days), 1)
