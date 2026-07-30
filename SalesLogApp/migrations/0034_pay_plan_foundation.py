import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0033_sale_and_archive_sale_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Industry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('slug', models.SlugField(max_length=100, unique=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name_plural': 'industries',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='PayPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150)),
                ('description', models.TextField(blank=True)),
                ('dealership_name', models.CharField(blank=True, max_length=150)),
                ('is_template', models.BooleanField(default=False)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('industry', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='pay_plans', to='SalesLogApp.industry')),
                ('owner_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='pay_plans', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
                'indexes': [
                    models.Index(fields=['owner_user', 'industry', 'is_active'], name='payplan_owner_ind_active_idx'),
                ],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('is_template', True), ('owner_user__isnull', False), _connector='OR'), name='payplan_template_or_owner'),
                    models.UniqueConstraint(condition=models.Q(('owner_user__isnull', False)), fields=('owner_user', 'industry', 'name'), name='unique_owned_payplan_name'),
                    models.UniqueConstraint(condition=models.Q(('is_template', True), ('owner_user__isnull', True)), fields=('industry', 'name'), name='unique_system_template_name'),
                ],
            },
        ),
        migrations.CreateModel(
            name='PayPlanVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version_name', models.CharField(max_length=100)),
                ('effective_start_date', models.DateField()),
                ('effective_end_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('active', 'Active'), ('inactive', 'Inactive'), ('archived', 'Archived')], default='draft', max_length=16)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('pay_plan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='SalesLogApp.payplan')),
            ],
            options={
                'ordering': ['pay_plan', '-effective_start_date', '-id'],
                'indexes': [
                    models.Index(fields=['pay_plan', 'status', 'effective_start_date'], name='planver_status_start_idx'),
                ],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('effective_end_date__isnull', True), ('effective_end_date__gte', models.F('effective_start_date')), _connector='OR'), name='payplanversion_valid_dates'),
                    models.UniqueConstraint(fields=('pay_plan', 'version_name'), name='unique_payplan_version_name'),
                    models.UniqueConstraint(condition=models.Q(('effective_end_date__isnull', True), ('status', 'active')), fields=('pay_plan',), name='unique_open_active_plan_version'),
                ],
            },
        ),
        migrations.CreateModel(
            name='PayPlanAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('effective_start_date', models.DateField()),
                ('effective_end_date', models.DateField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('pay_plan_version', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='assignments', to='SalesLogApp.payplanversion')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pay_plan_assignments', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['user', '-effective_start_date', '-id'],
                'indexes': [
                    models.Index(fields=['user', 'is_active', 'effective_start_date'], name='planassign_user_active_idx'),
                ],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('effective_end_date__isnull', True), ('effective_end_date__gte', models.F('effective_start_date')), _connector='OR'), name='payplanassignment_valid_dates'),
                    models.UniqueConstraint(fields=('user', 'pay_plan_version', 'effective_start_date'), name='unique_user_plan_assignment_start'),
                    models.UniqueConstraint(condition=models.Q(('effective_end_date__isnull', True), ('is_active', True)), fields=('user',), name='unique_open_active_assignment'),
                ],
            },
        ),
    ]
