from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('SalesLogApp', '0050_nps_survey_projection'),
    ]

    operations = [
        migrations.AlterField(
            model_name='payplanconversation',
            name='status',
            field=models.CharField(
                choices=[
                    ('open', 'Open'),
                    ('resolved', 'Resolved'),
                    ('cancelled', 'Cancelled'),
                    ('expired', 'Expired'),
                    ('stale', 'Stale'),
                ],
                default='open',
                max_length=16,
            ),
        ),
    ]
