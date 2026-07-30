from django.conf import settings
from django.db import migrations
from django.db.models import Min
from django.utils import timezone


LEGACY_PLAN_NAME = 'Legacy Automotive Pay Plan'
LEGACY_VERSION_NAME = 'Imported Legacy Settings'


def seed_automotive_foundation(apps, schema_editor):
    Industry = apps.get_model('SalesLogApp', 'Industry')
    PayPlan = apps.get_model('SalesLogApp', 'PayPlan')
    PayPlanVersion = apps.get_model('SalesLogApp', 'PayPlanVersion')
    PayPlanAssignment = apps.get_model('SalesLogApp', 'PayPlanAssignment')
    Sale = apps.get_model('SalesLogApp', 'Sale')
    ArchivedSale = apps.get_model('SalesLogApp', 'ArchivedSale')
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))

    automotive, _ = Industry.objects.update_or_create(
        slug='automotive',
        defaults={'name': 'Automotive', 'is_active': True},
    )

    for user in User.objects.all().iterator():
        live_date = Sale.objects.filter(user_id=user.pk).aggregate(
            value=Min('date')
        )['value']
        archived_date = ArchivedSale.objects.filter(user_id=user.pk).aggregate(
            value=Min('date')
        )['value']
        start_date = min(
            (value for value in (live_date, archived_date) if value),
            default=timezone.localdate(user.date_joined),
        )

        plan, _ = PayPlan.objects.get_or_create(
            owner_user_id=user.pk,
            industry_id=automotive.pk,
            name=LEGACY_PLAN_NAME,
            defaults={
                'description': (
                    'Compatibility plan created from the existing automotive '
                    'commission foundation. Rules will be migrated in a later stage.'
                ),
                'is_template': False,
                'is_active': True,
            },
        )
        version, _ = PayPlanVersion.objects.get_or_create(
            pay_plan_id=plan.pk,
            version_name=LEGACY_VERSION_NAME,
            defaults={
                'effective_start_date': start_date,
                'status': 'active',
            },
        )
        PayPlanAssignment.objects.get_or_create(
            user_id=user.pk,
            pay_plan_version_id=version.pk,
            effective_start_date=start_date,
            defaults={'is_active': True},
        )


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0034_pay_plan_foundation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            seed_automotive_foundation,
            migrations.RunPython.noop,
        ),
    ]
