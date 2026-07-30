from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from SalesLogApp.commission_service import CommissionEngineService
from SalesLogApp.pay_plan_imports import build_upload_import_draft
from SalesLogApp.pay_plan_management import (
    PayPlanActivationService,
    reload_existing_document,
)


class Command(BaseCommand):
    help = 'Safely recompile one user-owned uploaded pay plan.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument('--dry-run', action='store_true')
        mode.add_argument('--activate', action='store_true')

    def handle(self, *args, **options):
        try:
            user = get_user_model().objects.get(
                username__iexact=options['username'],
            )
        except get_user_model().DoesNotExist as exc:
            raise CommandError('No user matches that username.') from exc
        assignment = user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan'
        ).filter(is_active=True).order_by('-effective_start_date', '-id').first()
        if assignment is None:
            raise CommandError('The user has no active pay-plan assignment.')
        version = assignment.pay_plan_version
        documents = list(user.pay_plan_documents.filter(
            pay_plan__owner_user=user,
        ).order_by('-uploaded_at'))
        document = next((item for item in documents if item.is_available), None)
        if document is None:
            raise CommandError('No safely available user-owned source document exists.')
        draft = build_upload_import_draft([document], version.pay_plan.name)
        self.stdout.write(f'User: {user.username} ({user.pk})')
        self.stdout.write(f'Existing active version: {version.pk} — {version.version_name}')
        self.stdout.write(f'Source: {document.pk} — {document.original_filename}')
        self.stdout.write(f'Parser profile: {draft.get("parser_profile", "generic")}')
        for index, rule in enumerate(draft.get('rules', []), 1):
            self.stdout.write(
                f'{index}. {rule["name"]} [{rule["rule_type"]}] '
                f'config={rule["configuration"]} conditions={rule.get("conditions", [])}'
            )
        for warning in draft.get('warnings', []):
            self.stdout.write(self.style.WARNING(f'Warning: {warning}'))
        sales = list(user.sale_set.filter(
            date__gte=assignment.effective_start_date,
        ).order_by('date', 'pk'))
        before = CommissionEngineService.calculate_sales(user, sales)
        self.stdout.write(
            f'Current sales: {len(sales)}; current total: '
            f'${before["total_commission"]:.2f}'
        )
        if options['dry_run']:
            with transaction.atomic():
                proposed = reload_existing_document(
                    user, document, version.effective_start_date,
                )
                preview = CommissionEngineService.preview_sales(
                    user, sales, proposed,
                )
                self.stdout.write(
                    f'Proposed version: {proposed.version_name}; proposed total: '
                    f'${preview["estimated_total"]:.2f}; difference: '
                    f'${preview["estimated_total"] - before["total_commission"]:.2f}'
                )
                for item in preview['results']:
                    old = next(
                        result for result in before['results']
                        if result.sale_id == item.sale_id
                    )
                    if old.total_commission != item.total_commission:
                        self.stdout.write(
                            f'Sale {item.sale_id}: ${old.total_commission:.2f} -> '
                            f'${item.total_commission:.2f}'
                        )
                transaction.set_rollback(True)
            self.stdout.write('Dry run only: all temporary records were rolled back.')
            return
        proposed = reload_existing_document(
            user, document, version.effective_start_date,
        )
        self.stdout.write(
            f'Proposed immutable version: {proposed.pk} — {proposed.version_name}'
        )
        report = PayPlanActivationService.activate(
            user, proposed, warnings_approved=True,
            reason='Explicit recompile_pay_plan --activate command',
        )
        self.stdout.write(self.style.SUCCESS(
            f'Activated version {proposed.pk}. New total: '
            f'${report["new_total"]}; difference: ${report["difference"]}.'
        ))
