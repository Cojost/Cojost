import calendar
import random
import string
from datetime import datetime, date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from SalesLogApp.models import BonusLevel, Commission, Sale, Vehicle, VehicleMake, VehicleModel
from SalesLogApp.models.vehicles import display_catalog_name, normalize_catalog_name

FIRST_NAMES = [
    'Avery', 'Blake', 'Charlie', 'Dana', 'Elliott', 'Jordan', 'Kai', 'Morgan',
    'Parker', 'Quinn', 'Riley', 'Taylor', 'Sydney', 'Logan', 'Hayden',
]
LAST_NAMES = [
    'Adams', 'Brooks', 'Carter', 'Diaz', 'Ellis', 'Foster', 'Griffin', 'Hayes',
    'Jordan', 'Kennedy', 'Lane', 'Morgan', 'Parker', 'Rivera', 'Turner',
]

VEHICLE_CATALOG = {
    'Subaru': [
        'Outback', 'Crosstrek', 'Forester', 'Ascent', 'Legacy', 'Impreza',
        'WRX', 'BRZ', 'Solterra',
    ],
    'Toyota': ['Camry', 'Corolla', 'RAV4', 'Highlander', 'Tacoma', 'Tundra'],
    'Honda': ['Civic', 'Accord', 'CR-V', 'Pilot', 'Ridgeline'],
    'Ford': ['F-150', 'Escape', 'Explorer', 'Bronco', 'Mustang'],
    'Chevrolet': ['Silverado 1500', 'Equinox', 'Traverse', 'Tahoe', 'Malibu'],
    'Nissan': ['Altima', 'Rogue', 'Pathfinder', 'Frontier'],
    'Hyundai': ['Elantra', 'Sonata', 'Tucson', 'Santa Fe'],
    'Kia': ['Forte', 'K5', 'Sportage', 'Sorento', 'Telluride'],
}

ALLOWED_VIN_CHARS = 'ABCDEFGHJKLMNPRSTUVWXYZ0123456789'

DEFAULT_COMMISSION_SETTINGS = {
    'total_calculated_front_end': Decimal('0.10'),
    'total_calculated_back_end': Decimal('0.03'),
    'frontend_minimum': Decimal('0.00'),
    'frontend_maximum': None,
    'backend_minimum': Decimal('0.00'),
    'backend_maximum': None,
    'opt_out_front': False,
    'opt_out_back': False,
}

DEFAULT_BONUS_LEVELS = [
    {'count_threshold': Decimal('2.0'), 'amount': Decimal('250.00')},
    {'count_threshold': Decimal('4.0'), 'amount': Decimal('600.00')},
    {'count_threshold': Decimal('8.0'), 'amount': Decimal('1200.00')},
]

CUSTOMER_FIRST_NAMES = [
    'Jordan', 'Taylor', 'Alex', 'Casey', 'Jamie', 'Morgan', 'Riley', 'Drew',
    'Avery', 'Parker', 'Cameron', 'Dakota', 'Hayden', 'Reese', 'Skyler',
]
CUSTOMER_LAST_NAMES = [
    'Anderson', 'Bennett', 'Cole', 'Davis', 'Ellison', 'Fitzgerald', 'Gray',
    'Harrison', 'Iverson', 'Jameson', 'Keller', 'Lane', 'Mitchell', 'Nolan',
    'Owens',
]


def generate_vin(existing_vins):
    for _ in range(1000):
        vin = ''.join(random.choice(ALLOWED_VIN_CHARS) for _ in range(17))
        if vin not in existing_vins:
            existing_vins.add(vin)
            return vin
    raise ValueError('Unable to generate a unique VIN.')


def display_money(amount):
    return f'${amount:,.2f}'


def make_decimal_cents(value):
    return Decimal(value).quantize(Decimal('0.01'))


def build_vehicle_catalog():
    catalog = []
    for make, models in VEHICLE_CATALOG.items():
        for model in models:
            catalog.append({'make': make, 'model': model})
    return catalog


def ensure_vehicle_catalog_entry(make_name, model_name):
    make_name = display_catalog_name(make_name)
    model_name = display_catalog_name(model_name)
    make, _ = VehicleMake.objects.get_or_create(
        normalized_name=normalize_catalog_name(make_name),
        defaults={'name': make_name, 'active': True, 'verified': True},
    )
    model, _ = VehicleModel.objects.get_or_create(
        make=make,
        normalized_name=normalize_catalog_name(model_name),
        defaults={'name': model_name, 'active': True, 'verified': True},
    )
    return make, model


def choose_vehicle_details():
    combo = random.choices(
        population=build_vehicle_catalog(),
        weights=[3 if item['make'] == 'Subaru' else 1 for item in build_vehicle_catalog()],
        k=1,
    )[0]
    if random.random() < 0.25:
        year = random.choice([2025, 2026])
        mileage = random.randint(5, 300)
    else:
        year = random.choice([2020, 2021, 2022, 2023, 2024, 2025])
        mileage = random.randint(5000, 95000)
    return combo['make'], combo['model'], year, mileage


def choose_gross_amounts():
    front_end = Decimal(random.randrange(0, 650001)) / Decimal('100')
    backend = Decimal(random.randrange(0, 500001)) / Decimal('100')
    if random.random() < 0.10:
        front_end = Decimal(random.randrange(0, 150001)) / Decimal('100')
        backend = Decimal(random.randrange(0, 100001)) / Decimal('100')
    return front_end.quantize(Decimal('0.01')), backend.quantize(Decimal('0.01'))


def choose_count():
    return Decimal(random.choices([1.0, 1.0, 1.0, 2.0, 0.5], weights=[40, 20, 20, 15, 5])[0]).quantize(Decimal('0.1'))


def name_pair():
    return f"{random.choice(CUSTOMER_FIRST_NAMES)} {random.choice(CUSTOMER_LAST_NAMES)}"


def select_seeded_deal_numbers(count):
    existing = set(Sale.objects.filter(dealNumber__gte=900001, dealNumber__lte=900099).values_list('dealNumber', flat=True))
    numbers = []
    for value in range(900001, 900100):
        if value not in existing:
            numbers.append(value)
        if len(numbers) >= count:
            break
    if len(numbers) < count:
        raise CommandError('Not enough available demo deal numbers in the 900001-900099 range.')
    return numbers


class Command(BaseCommand):
    help = 'Seed a demo salesperson and sample automotive sales data in development.'

    def add_arguments(self, parser):
        parser.add_argument('--month', help='Month to seed in YYYY-MM format.')
        parser.add_argument('--count', type=int, default=15, help='Number of sales to create.')
        parser.add_argument('--username', default='demo_salesperson', help='Demo username.')
        parser.add_argument('--reset', action='store_true', help='Remove prior demo sales for the selected user before seeding.')
        parser.add_argument('--random-seed', type=int, help='Optional random seed for reproducible demo data.')

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('seed_test_sales can only be run when DEBUG=True.')

        if options['random_seed'] is not None:
            random.seed(options['random_seed'])

        month = options['month']
        if month:
            try:
                month_date = datetime.strptime(month, '%Y-%m').date().replace(day=1)
            except ValueError:
                raise CommandError('Invalid --month value. Use YYYY-MM format.')
        else:
            month_date = timezone.localdate().replace(day=1)

        count = options['count']
        if count <= 0:
            raise CommandError('--count must be a positive integer.')

        username = options['username']
        password = 'DemoTest123!'
        email = 'demo.salesperson@example.com'

        User = get_user_model()
        user_defaults = {
            'email': email,
            'first_name': random.choice(FIRST_NAMES),
            'last_name': random.choice(LAST_NAMES),
        }

        with transaction.atomic():
            user, user_created = User.objects.get_or_create(
                username=username,
                defaults=user_defaults,
            )
            if user_created:
                user.set_password(password)
                user.save()

            if options['reset']:
                sales_to_delete = Sale.objects.filter(
                    user=user,
                    dealNumber__gte=900001,
                    dealNumber__lte=900099,
                    date__year=month_date.year,
                    date__month=month_date.month,
                )
                deleted_sales = sales_to_delete.count()
                if deleted_sales:
                    sales_to_delete.delete()
                else:
                    deleted_sales = 0
            else:
                deleted_sales = 0

            commission, commission_created = Commission.objects.get_or_create(
                user=user,
                defaults=DEFAULT_COMMISSION_SETTINGS,
            )
            if commission_created:
                commission.save()

            bonus_levels_created = 0
            for bonus in DEFAULT_BONUS_LEVELS:
                _, created = BonusLevel.objects.update_or_create(
                    user=user,
                    commission=commission,
                    count_threshold=bonus['count_threshold'],
                    defaults={
                        'amount': bonus['amount'],
                        'active': True,
                        'tied_to_units': True,
                    },
                )
                if created:
                    bonus_levels_created += 1

            catalog_created = set()
            for entry in build_vehicle_catalog():
                make, model = ensure_vehicle_catalog_entry(entry['make'], entry['model'])
                catalog_created.add((make.pk, model.pk))

            # Catalog entries created above

            existing_sales = Sale.objects.filter(
                user=user,
                dealNumber__gte=900001,
                dealNumber__lte=900099,
            )
            existing_count = existing_sales.count()
            missing = max(0, count - existing_count)
            created_sales = 0
            created_vehicle_combinations = set()
            total_units = Decimal('0.0')
            total_front_end = Decimal('0.00')
            total_back_end = Decimal('0.00')

            if missing > 0:
                deal_numbers = select_seeded_deal_numbers(missing)
                existing_vins = set(Vehicle.objects.filter(vin__isnull=False).values_list('vin', flat=True))
                for index, deal_number in enumerate(deal_numbers, start=1):
                    sale_date = date(
                        month_date.year,
                        month_date.month,
                        random.randint(1, calendar.monthrange(month_date.year, month_date.month)[1]),
                    )
                    make_name, model_name, year, mileage = choose_vehicle_details()
                    make, model = ensure_vehicle_catalog_entry(make_name, model_name)
                    front_end, backend = choose_gross_amounts()
                    count_value = choose_count()
                    customer_name = name_pair()
                    stock_number = f'TEST-{deal_number - 900000:04d}'
                    vin = generate_vin(existing_vins)

                    sale = Sale.objects.create(
                        user=user,
                        customer=customer_name,
                        dealNumber=deal_number,
                        count=count_value,
                        frontEnd=front_end,
                        backend=backend,
                        date=sale_date,
                    )
                    Vehicle.objects.create(
                        sale=sale,
                        year=year,
                        make=make,
                        model=model,
                        mileage=mileage,
                        stock_number=stock_number,
                        vin=vin,
                    )
                    created_sales += 1
                    created_vehicle_combinations.add((make_name, model_name, year))
                    total_units += count_value
                    total_front_end += front_end
                    total_back_end += backend

            seeded_sales = Sale.objects.filter(
                user=user,
                dealNumber__gte=900001,
                dealNumber__lte=900099,
                date__year=month_date.year,
                date__month=month_date.month,
            )
            sales_count = seeded_sales.count()
            total_units = sum((sale.unit_credit for sale in seeded_sales), Decimal('0.0'))
            total_front_end = sum((sale.frontEnd for sale in seeded_sales), Decimal('0.00'))
            total_back_end = sum((sale.backend for sale in seeded_sales), Decimal('0.00'))

            self.stdout.write(self.style.SUCCESS('Demo data created successfully.'))
            self.stdout.write(f'Username: {username}')
            self.stdout.write(f'Password: {password}')
            self.stdout.write(f'Month: {month_date:%B %Y}')
            self.stdout.write(f'Sales created: {created_sales}')
            self.stdout.write(f'Vehicle options created: {len(created_vehicle_combinations)}')
            self.stdout.write(f'Total units: {total_units}')
            self.stdout.write(f'Front-end gross: {display_money(total_front_end)}')
            self.stdout.write(f'Back-end gross: {display_money(total_back_end)}')
            if options['reset']:
                self.stdout.write(f'Prior demo sales deleted: {deleted_sales}')
