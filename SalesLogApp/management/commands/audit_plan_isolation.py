from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from SalesLogApp.models import (
    PayPlanActivationEvent,
    PayPlanAssignment,
    PayPlanDocument,
    PayPlanOnboarding,
    PayPlanRuleCondition,
    PayPlanVersion,
)


PER_SALE_FIELDS = {
    'vehicle_condition', 'make', 'model', 'year', 'is_cpo', 'deal_type',
    'front_end_gross', 'back_end_gross', 'total_gross', 'deal_credit',
    'sale_date',
}


class Command(BaseCommand):
    help = (
        'Fail when pay-plan ownership or rule-scope isolation violations are '
        'present. Safe for deployment checks; never modifies data.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'identifier', nargs='?',
            help='Optional username, email, or numeric user ID.',
        )

    def _users(self, identifier):
        User = get_user_model()
        if not identifier:
            return User.objects.all().order_by('id')
        if identifier.isdigit():
            users = User.objects.filter(id=int(identifier))
        else:
            users = User.objects.filter(username__iexact=identifier)
            if not users.exists():
                users = User.objects.filter(email__iexact=identifier)
        if not users.exists():
            raise CommandError(f'No user found for identifier: {identifier}')
        return users

    def handle(self, *args, **options):
        user_ids = set(self._users(options.get('identifier')).values_list(
            'id', flat=True,
        ))
        violations = []

        assignments = PayPlanAssignment.objects.select_related(
            'pay_plan_version__pay_plan',
        ).filter(user_id__in=user_ids)
        for assignment in assignments:
            owner_id = assignment.pay_plan_version.pay_plan.owner_user_id
            if owner_id != assignment.user_id:
                violations.append(
                    f'assignment:{assignment.id} user:{assignment.user_id} '
                    f'plan_owner:{owner_id}'
                )

        onboardings = PayPlanOnboarding.objects.select_related(
            'current_pay_plan', 'current_version__pay_plan',
        ).filter(user_id__in=user_ids)
        for onboarding in onboardings:
            if (
                onboarding.current_pay_plan_id
                and onboarding.current_pay_plan.owner_user_id != onboarding.user_id
            ):
                violations.append(f'onboarding:{onboarding.id} cross-owner plan')
            if (
                onboarding.current_version_id
                and onboarding.current_version.pay_plan.owner_user_id
                != onboarding.user_id
            ):
                violations.append(f'onboarding:{onboarding.id} cross-owner version')

        documents = PayPlanDocument.objects.select_related(
            'onboarding', 'pay_plan', 'pay_plan_version__pay_plan',
        ).filter(user_id__in=user_ids)
        for document in documents:
            owners = {document.onboarding.user_id}
            if document.pay_plan_id:
                owners.add(document.pay_plan.owner_user_id)
            if document.pay_plan_version_id:
                owners.add(document.pay_plan_version.pay_plan.owner_user_id)
            if owners != {document.user_id}:
                violations.append(
                    f'document:{document.id} user:{document.user_id} owners:{sorted(owners)}'
                )

        events = PayPlanActivationEvent.objects.select_related(
            'version__pay_plan', 'previous_version__pay_plan',
        ).filter(user_id__in=user_ids)
        for event in events:
            owners = {event.version.pay_plan.owner_user_id}
            if event.previous_version_id:
                owners.add(event.previous_version.pay_plan.owner_user_id)
            if owners != {event.user_id}:
                violations.append(
                    f'activation_event:{event.id} user:{event.user_id} owners:{sorted(owners)}'
                )

        versions = PayPlanVersion.objects.select_related(
            'pay_plan', 'previous_version__pay_plan',
        ).filter(pay_plan__owner_user_id__in=user_ids)
        for version in versions:
            if (
                version.previous_version_id
                and version.previous_version.pay_plan.owner_user_id
                != version.pay_plan.owner_user_id
            ):
                violations.append(f'version:{version.id} cross-owner predecessor')

        bad_conditions = PayPlanRuleCondition.objects.select_related(
            'rule__pay_plan_version__pay_plan',
        ).filter(
            rule__pay_plan_version__pay_plan__owner_user_id__in=user_ids,
            rule__pay_plan_version__status__in={
                PayPlanVersion.ACTIVE,
                PayPlanVersion.DRAFT,
                PayPlanVersion.REVIEW_REQUIRED,
            },
            rule__calculation_scope='period',
            field_name__in=PER_SALE_FIELDS,
        )
        for condition in bad_conditions:
            violations.append(
                f'condition:{condition.id} per-sale field '
                f'{condition.field_name} on period rule:{condition.rule_id}'
            )

        self.stdout.write(
            f'Audited {len(user_ids)} user(s), {assignments.count()} '
            f'assignment(s), and {versions.count()} version(s).'
        )
        if violations:
            for violation in violations:
                self.stderr.write(f'VIOLATION: {violation}')
            raise CommandError(
                f'Pay-plan isolation audit failed with {len(violations)} violation(s).'
            )
        self.stdout.write(self.style.SUCCESS('Pay-plan isolation audit passed.'))
