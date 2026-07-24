import SalesLogApp.models.profile
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def create_existing_profiles(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    UserProfile = apps.get_model('SalesLogApp', 'UserProfile')
    for user_id in User.objects.values_list('pk', flat=True).iterator():
        UserProfile.objects.get_or_create(user_id=user_id)


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0029_archivedsale_split_with_name_sale_split_with_name'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('avatar', models.ImageField(blank=True, upload_to=SalesLogApp.models.profile.profile_avatar_upload_path, validators=[SalesLogApp.models.profile.validate_avatar_file])),
                ('theme_mode', models.CharField(choices=[('light', 'Light'), ('dark', 'Dark'), ('system', 'Use device setting')], default='system', max_length=10)),
                ('background_color', models.CharField(default='#f4f6f8', max_length=7, validators=[SalesLogApp.models.profile.hex_color_validator])),
                ('graph_primary_color', models.CharField(default='#3498db', max_length=7, validators=[SalesLogApp.models.profile.hex_color_validator])),
                ('graph_secondary_color', models.CharField(default='#194f85', max_length=7, validators=[SalesLogApp.models.profile.hex_color_validator])),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='sales_profile', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.RunPython(create_existing_profiles, migrations.RunPython.noop),
    ]
