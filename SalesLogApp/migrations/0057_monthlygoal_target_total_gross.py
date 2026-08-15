from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('SalesLogApp', '0056_emailverificationdispatch'),
    ]

    operations = [
        migrations.AddField(
            model_name='monthlygoal',
            name='target_total_gross',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=12,
            ),
        ),
        migrations.AddConstraint(
            model_name='monthlygoal',
            constraint=models.CheckConstraint(
                condition=models.Q(target_total_gross__gte=0),
                name='goal_gross_nonnegative',
            ),
        ),
    ]
