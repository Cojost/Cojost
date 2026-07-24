import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from SalesLogApp.models import VehicleMake, VehicleModel
from SalesLogApp.models.vehicles import display_catalog_name, normalize_catalog_name

BASE_URL = 'https://vpic.nhtsa.dot.gov/api/vehicles'


def fetch_results(path):
    request = Request(f'{BASE_URL}/{path}?format=json', headers={'User-Agent': 'SalesLogApp/1.0'})
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response).get('Results', [])
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise CommandError(f'NHTSA vPIC catalog request failed: {exc}') from exc


class Command(BaseCommand):
    help = 'Synchronize the local make/model catalog from the official NHTSA vPIC API.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--make', help='Sync one existing or NHTSA make name.')

    def handle(self, *args, **options):
        requested = normalize_catalog_name(options.get('make'))
        makes = fetch_results('GetMakesForVehicleType/car')
        if requested:
            makes = [
                item for item in makes
                if normalize_catalog_name(item.get('MakeName')) == requested
            ]
            if not makes:
                raise CommandError('The requested make was not found in NHTSA vPIC.')
        make_count = model_count = 0
        with transaction.atomic():
            for item in makes:
                name = display_catalog_name(item.get('MakeName'))
                if not name:
                    continue
                make, created = VehicleMake.objects.update_or_create(
                    normalized_name=normalize_catalog_name(name),
                    defaults={'name': name, 'verified': True, 'active': True},
                )
                make_count += int(created)
                for model_item in fetch_results(
                    f"GetModelsForMakeId/{quote(str(item['MakeId']))}"
                ):
                    model_name = display_catalog_name(model_item.get('Model_Name'))
                    if not model_name:
                        continue
                    _, created = VehicleModel.objects.update_or_create(
                        make=make, normalized_name=normalize_catalog_name(model_name),
                        defaults={'name': model_name, 'verified': True, 'active': True},
                    )
                    model_count += int(created)
            if options['dry_run']:
                transaction.set_rollback(True)
        suffix = ' (dry run; rolled back)' if options['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f'Catalog sync complete: {make_count} makes and {model_count} models added{suffix}.'
        ))
