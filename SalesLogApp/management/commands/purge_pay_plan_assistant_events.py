from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from SalesLogApp.models import PayPlanAssistantUsageEvent


class Command(BaseCommand):
    help = 'Delete Pay Plan Assistant operational events older than retention.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=settings.PAY_PLAN_ASSISTANT_EVENT_RETENTION_DAYS,
        )

    def handle(self, *args, **options):
        days = options['days']
        if days < 1:
            self.stderr.write('Retention days must be at least 1.')
            return
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = PayPlanAssistantUsageEvent.objects.filter(
            created_at__lt=cutoff,
        ).delete()
        self.stdout.write(f'deleted_events={deleted}')
