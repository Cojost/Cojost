from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from SalesLogApp.models import FounderGrant


class Command(BaseCommand):
    help = 'Revoke an unused founder grant by its non-secret public UUID.'

    def add_arguments(self, parser):
        parser.add_argument('grant_id')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            grant = FounderGrant.objects.select_for_update().get(
                public_id=options['grant_id']
            )
        except (FounderGrant.DoesNotExist, ValueError) as exc:
            raise CommandError('Founder grant not found.') from exc
        if grant.redeemed_user_id is not None or grant.redemption_count:
            raise CommandError('Only unused founder grants can be revoked.')
        if grant.revoked_at is None:
            grant.revoked_at = timezone.now()
            grant.save(update_fields=['revoked_at'])
        self.stdout.write('Unused founder grant revoked.')
