from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from SalesLogApp.models import AskStewConversation


class Command(BaseCommand):
    help = 'Delete Ask Stew conversations older than the configured retention.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=settings.ASK_STEW_AI_CONVERSATION_RETENTION_DAYS,
        )

    def handle(self, *args, **options):
        days = options['days']
        if days < 1:
            raise CommandError('Retention days must be at least 1.')
        cutoff = timezone.now() - timedelta(days=days)
        conversations = AskStewConversation.objects.filter(
            updated_at__lt=cutoff,
        )
        conversation_count = conversations.count()
        conversations.delete()
        self.stdout.write(f'deleted_conversations={conversation_count}')
