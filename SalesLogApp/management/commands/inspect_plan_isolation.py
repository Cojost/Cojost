from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from SalesLogApp.models import (
    PayPlanActivationEvent,
    PayPlanAssignment,
    PayPlanDocument,
    PayPlanEligibility,
)
from SalesLogApp.plan_requirements import (
    ActivePayPlanService,
    PlanRequirementService,
)


class Command(BaseCommand):
    help = 'Inspect user/pay-plan ownership and plan-backed requirement isolation.'

    def add_arguments(self, parser):
        parser.add_argument('identifier')
        parser.add_argument(
            '--repair', action='store_true',
            help='Deactivate cross-owner assignments and clear unbacked eligibility fields.',
        )

    def resolve_user(self, identifier):
        User = get_user_model()
        query = (
            User.objects.filter(id=int(identifier))
            if identifier.isdigit()
            else User.objects.filter(username__iexact=identifier)
        )
        user = query.first()
        if user is None and not identifier.isdigit():
            user = User.objects.filter(email__iexact=identifier).first()
        if user is None:
            raise CommandError(f'No user found for identifier: {identifier}')
        return user

    @transaction.atomic
    def repair(self, user):
        repaired = []
        assignments = PayPlanAssignment.objects.select_related(
            'pay_plan_version__pay_plan',
        ).filter(user=user, is_active=True)
        for assignment in assignments:
            if assignment.pay_plan_version.pay_plan.owner_user_id != user.id:
                assignment.is_active = False
                assignment.save(update_fields=['is_active', 'updated_at'])
                repaired.append(
                    f'deactivated cross-owner assignment {assignment.id}'
                )
        requirements = PlanRequirementService.get_for_user(user)
        field_map = {
            'nps': ('nps_status', PayPlanEligibility.NPS_PENDING),
            'green_pea': ('green_pea', None),
            'ar': ('ar_requirement_met', None),
            'training': ('training_requirements_met', None),
            'calls': ('call_requirement_met', None),
            'video': ('video_requirement_met', None),
        }
        for requirement, (field, empty_value) in field_map.items():
            if requirements.get(requirement) is None:
                changed = PayPlanEligibility.objects.filter(user=user).exclude(
                    **{field: empty_value},
                ).update(**{field: empty_value})
                if changed:
                    repaired.append(
                        f'cleared {field} on {changed} unbacked eligibility record(s)'
                    )
        return repaired

    def handle(self, *args, **options):
        user = self.resolve_user(options['identifier'])
        if options['repair']:
            self.stdout.write('=== BEFORE SAFE REPAIR ===')
            self.write_report(user)
            repaired = self.repair(user)
            self.stdout.write('Repairs: ' + ('; '.join(repaired) or 'none'))
            self.stdout.write('=== AFTER SAFE REPAIR ===')
        self.write_report(user)

    def write_report(self, user):
        active = ActivePayPlanService.get_for_user(user)
        requirements = PlanRequirementService.get_for_user(user, active)
        self.stdout.write(f'User ID: {user.id}')
        self.stdout.write(f'Username: {user.username}')
        self.stdout.write(f'Active-plan status: {active.status}')
        self.stdout.write(
            f'Active plan ID: {getattr(active.plan, "id", None)}'
        )
        self.stdout.write(
            f'Active version ID: {getattr(active.version, "id", None)}'
        )
        self.stdout.write(
            f'Active version: {getattr(active.version, "version_name", None)}'
        )
        self.stdout.write('Requirement rules:')
        for key in ('nps', 'ar', 'green_pea', 'training', 'calls', 'video'):
            self.stdout.write(f'  {key}: {requirements.get(key) or "none"}')

        cross_assignments = []
        for assignment in PayPlanAssignment.objects.select_related(
            'pay_plan_version__pay_plan',
        ).filter(user=user):
            owner_id = assignment.pay_plan_version.pay_plan.owner_user_id
            if owner_id != user.id:
                cross_assignments.append((assignment.id, owner_id))
        self.stdout.write(f'Cross-owner assignments: {cross_assignments or "none"}')

        cross_documents = []
        for document in PayPlanDocument.objects.select_related(
            'onboarding', 'pay_plan', 'pay_plan_version__pay_plan',
        ).filter(user=user):
            owners = {
                document.onboarding.user_id,
                document.pay_plan.owner_user_id if document.pay_plan_id else user.id,
                (
                    document.pay_plan_version.pay_plan.owner_user_id
                    if document.pay_plan_version_id else user.id
                ),
            }
            if owners != {user.id}:
                cross_documents.append((document.id, sorted(owners)))
        self.stdout.write(f'Cross-owner documents: {cross_documents or "none"}')

        cross_events = []
        for event in PayPlanActivationEvent.objects.select_related(
            'version__pay_plan',
        ).filter(user=user):
            if event.version.pay_plan.owner_user_id != user.id:
                cross_events.append(event.id)
        self.stdout.write(f'Cross-owner calculation/audit records: {cross_events or "none"}')
        self.stdout.write('Cached requirement keys: none (no plan cache is configured)')

        unbacked = []
        for eligibility in PayPlanEligibility.objects.filter(user=user):
            if requirements.get('nps') is None and eligibility.nps_status != PayPlanEligibility.NPS_PENDING:
                unbacked.append(f'{eligibility.month_start}:nps')
            if requirements.get('green_pea') is None and eligibility.green_pea is not None:
                unbacked.append(f'{eligibility.month_start}:green_pea')
            if requirements.get('ar') is None and eligibility.ar_requirement_met is not None:
                unbacked.append(f'{eligibility.month_start}:ar')
            if requirements.get('training') is None and eligibility.training_requirements_met is not None:
                unbacked.append(f'{eligibility.month_start}:training')
            if requirements.get('calls') is None and eligibility.call_requirement_met is not None:
                unbacked.append(f'{eligibility.month_start}:calls')
            if requirements.get('video') is None and eligibility.video_requirement_met is not None:
                unbacked.append(f'{eligibility.month_start}:video')
        self.stdout.write(f'Goal cards not backed by active plan: {unbacked or "none"}')
