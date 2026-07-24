import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0027_commissionadjustment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='archivedsale',
            name='user',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='archived_sales', to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name='archivedsale',
            index=models.Index(fields=['user', 'date'], name='archive_user_date_idx'),
        ),
        migrations.CreateModel(
            name='DailyActivity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(default=django.utils.timezone.localdate)),
                ('leads_taken', models.PositiveIntegerField(default=0)),
                ('phone_calls_made', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-date'],
                'indexes': [models.Index(fields=['user', 'date'], name='activity_user_date_idx')],
                'constraints': [
                    models.UniqueConstraint(fields=('user', 'date'), name='unique_daily_activity_user_date'),
                    models.CheckConstraint(condition=models.Q(('leads_taken__gte', 0)), name='activity_leads_nonnegative'),
                    models.CheckConstraint(condition=models.Q(('phone_calls_made__gte', 0)), name='activity_calls_nonnegative'),
                ],
            },
        ),
        migrations.CreateModel(
            name='MonthlyGoal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('month_start', models.DateField()),
                ('target_units', models.DecimalField(decimal_places=1, default=0, max_digits=8)),
                ('target_commission', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-month_start'],
                'indexes': [models.Index(fields=['user', 'month_start'], name='goal_user_month_idx')],
                'constraints': [
                    models.UniqueConstraint(fields=('user', 'month_start'), name='unique_monthly_goal_user_month'),
                    models.CheckConstraint(condition=models.Q(('target_units__gte', 0)), name='goal_units_nonnegative'),
                    models.CheckConstraint(condition=models.Q(('target_commission__gte', 0)), name='goal_commission_nonnegative'),
                ],
            },
        ),
    ]
