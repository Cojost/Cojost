from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0028_archivedsale_user_dailyactivity_monthlygoal_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='archivedsale',
            name='split_with_name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='sale',
            name='split_with_name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
