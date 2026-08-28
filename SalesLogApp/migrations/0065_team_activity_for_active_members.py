from django.db import migrations, models


def enable_active_member_activity(apps, schema_editor):
    Team = apps.get_model('SalesLogApp', 'Team')
    TeamMembership = apps.get_model('SalesLogApp', 'TeamMembership')
    TeamActivity = apps.get_model('SalesLogApp', 'TeamActivity')
    Sale = apps.get_model('SalesLogApp', 'Sale')

    active_team_ids = Team.objects.filter(is_active=True).values_list('id', flat=True)
    memberships = TeamMembership.objects.filter(
        team_id__in=active_team_ids,
        status='active',
    ).iterator()
    for membership in memberships:
        TeamMembership.objects.filter(pk=membership.pk).update(
            sharing_preference='individual_and_totals',
        )
        TeamActivity.objects.filter(membership_id=membership.pk).update(
            is_visible=True,
        )
        sales = Sale.objects.filter(user_id=membership.user_id)
        if membership.joined_at:
            sales = sales.filter(date__gte=membership.joined_at.date())
        for sale in sales.iterator():
            TeamActivity.objects.update_or_create(
                sale_id=sale.pk,
                defaults={
                    'team_id': membership.team_id,
                    'membership_id': membership.pk,
                    'activity_type': 'sale',
                    'unit_credit': sale.count,
                    'activity_date': sale.date,
                    'is_visible': True,
                },
            )


class Migration(migrations.Migration):

    dependencies = [
        ('SalesLogApp', '0064_ask_stew_ai1a_lab'),
    ]

    operations = [
        migrations.AlterField(
            model_name='teammembership',
            name='sharing_preference',
            field=models.CharField(
                choices=[
                    ('individual_and_totals', 'Individual activity and totals'),
                    ('totals_only', 'Totals only'),
                    ('paused', 'Pause all sharing'),
                ],
                default='individual_and_totals',
                editable=False,
                max_length=28,
            ),
        ),
        migrations.RunPython(enable_active_member_activity, migrations.RunPython.noop),
    ]
