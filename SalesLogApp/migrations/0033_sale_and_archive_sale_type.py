from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0032_vehicle_catalog_and_vehicle_records'),
    ]

    operations = [
        migrations.AddField(
            model_name='sale',
            name='sale_type',
            field=models.CharField(
                choices=[('automotive', 'Automotive')],
                db_index=True,
                default='automotive',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='archivedsale',
            name='sale_type',
            field=models.CharField(
                choices=[('automotive', 'Automotive')],
                db_index=True,
                default='automotive',
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name='sale',
            index=models.Index(
                fields=['user', 'sale_type', 'date'],
                name='sale_owner_type_date_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='archivedsale',
            index=models.Index(
                fields=['user', 'sale_type', 'date'],
                name='archive_owner_type_date_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='sale',
            constraint=models.CheckConstraint(
                condition=models.Q(('sale_type', 'automotive')),
                name='sale_supported_type',
            ),
        ),
        migrations.AddConstraint(
            model_name='archivedsale',
            constraint=models.CheckConstraint(
                condition=models.Q(('sale_type', 'automotive')),
                name='archive_supported_type',
            ),
        ),
    ]
