import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0031_userprofile_header_color_remove_obsolete_colors'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='VehicleMake',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('normalized_name', models.CharField(max_length=100, unique=True)),
                ('active', models.BooleanField(default=True)),
                ('verified', models.BooleanField(default=False)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_vehicle_makes', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['name'], 'indexes': [models.Index(fields=['active', 'name'], name='vehicle_make_active_idx')]},
        ),
        migrations.CreateModel(
            name='VehicleModel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('normalized_name', models.CharField(max_length=100)),
                ('active', models.BooleanField(default=True)),
                ('verified', models.BooleanField(default=False)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_vehicle_models', to=settings.AUTH_USER_MODEL)),
                ('make', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vehicle_models', to='SalesLogApp.vehiclemake')),
            ],
            options={
                'ordering': ['name'],
                'indexes': [models.Index(fields=['make', 'active', 'name'], name='vehicle_model_lookup_idx')],
                'constraints': [models.UniqueConstraint(fields=('make', 'normalized_name'), name='unique_vehicle_model_per_make')],
            },
        ),
        migrations.CreateModel(
            name='Vehicle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('year', models.PositiveSmallIntegerField(db_index=True, validators=[django.core.validators.MinValueValidator(2000)])),
                ('mileage', models.PositiveIntegerField(validators=[django.core.validators.MaxValueValidator(10000000)])),
                ('stock_number', models.CharField(db_index=True, max_length=50, validators=[django.core.validators.RegexValidator(message='Use letters, numbers, spaces, hyphens, periods, slashes, or underscores.', regex='^[A-Z0-9][A-Z0-9 ._/-]*$')])),
                ('vin', models.CharField(db_index=True, max_length=17, validators=[django.core.validators.RegexValidator(message='Enter a valid 17-character VIN without I, O, or Q.', regex='^[A-HJ-NPR-Z0-9]{17}$')])),
                ('make', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vehicles', to='SalesLogApp.vehiclemake')),
                ('model', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vehicles', to='SalesLogApp.vehiclemodel')),
                ('sale', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='vehicle', to='SalesLogApp.sale')),
            ],
            options={
                'indexes': [models.Index(fields=['year', 'make', 'model'], name='vehicle_reporting_idx')],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('year__gte', 2000)), name='vehicle_year_gte_2000'),
                    models.CheckConstraint(condition=models.Q(('mileage__lte', 10000000)), name='vehicle_mileage_reasonable'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ArchivedVehicle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('year', models.PositiveSmallIntegerField(db_index=True)),
                ('make_name', models.CharField(db_index=True, max_length=100)),
                ('model_name', models.CharField(db_index=True, max_length=100)),
                ('mileage', models.PositiveIntegerField()),
                ('stock_number', models.CharField(db_index=True, max_length=50)),
                ('vin', models.CharField(db_index=True, max_length=17)),
                ('archived_sale', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='vehicle', to='SalesLogApp.archivedsale')),
            ],
            options={'indexes': [models.Index(fields=['year', 'make_name', 'model_name'], name='archive_vehicle_report_idx')]},
        ),
    ]
