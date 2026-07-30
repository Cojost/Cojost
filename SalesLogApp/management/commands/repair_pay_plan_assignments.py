from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from SalesLogApp.access import sync_active_onboarding_assignment


class Command(BaseCommand):
    help = (
        'Repair active pay-plan onboarding records by aligning the current '
        'version and active assignment dates with the user\'s earliest sale.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            action='append',
            dest='usernames',
            help='Limit repairs to one or more usernames. Repeat the flag to target multiple users.',
        )

    def handle(self, *args, **options):
        usernames = options.get('usernames') or []
        User = get_user_model()
        users = User.objects.all().order_by('username')
        if usernames:
            users = users.filter(username__in=usernames)
            found = set(users.values_list('username', flat=True))
            missing = [username for username in usernames if username not in found]
            if missing:
                raise CommandError(
                    'Unknown username(s): ' + ', '.join(sorted(missing))
                )

        checked = 0
        repaired = 0
        skipped = 0
        for user in users:
            checked += 1
            result = sync_active_onboarding_assignment(user)
            if result is None:
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(
                        f'Skipped {user.username}: no active onboarding with an active current version.'
                    )
                )
                continue
            if result['changed']:
                repaired += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Repaired {user.username}: version {result['version'].id}, "
                        f"assignment {result['assignment'].id}, start {result['desired_start']}"
                    )
                )
            else:
                self.stdout.write(
                    f"No changes for {user.username}: assignment {result['assignment'].id} already aligned to {result['desired_start']}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'Checked {checked} user(s); repaired {repaired}; skipped {skipped}.'
            )
        )