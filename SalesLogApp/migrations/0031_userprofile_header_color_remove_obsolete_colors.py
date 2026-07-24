from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0030_userprofile'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='header_color',
            field=models.CharField(
                choices=[
                    ('red', 'Red'),
                    ('orange', 'Orange'),
                    ('yellow', 'Yellow'),
                    ('green', 'Green'),
                    ('blue', 'Blue'),
                    ('gray', 'Gray'),
                    ('pink', 'Pink'),
                    ('purple', 'Purple'),
                ],
                default='blue',
                max_length=10,
            ),
        ),
        migrations.RemoveField(
            model_name='userprofile',
            name='background_color',
        ),
        migrations.RemoveField(
            model_name='userprofile',
            name='graph_primary_color',
        ),
        migrations.RemoveField(
            model_name='userprofile',
            name='graph_secondary_color',
        ),
    ]
