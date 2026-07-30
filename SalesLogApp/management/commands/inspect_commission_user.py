from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from SalesLogApp.access import sync_active_onboarding_assignment
from SalesLogApp.commission_service import CommissionEngineService
from SalesLogApp.commission_engine.validators import normalize_percentage_rate
from SalesLogApp.models import (
    PayPlanAssignment, PayPlanOnboarding, PayPlanVersion, Sale, UserProfile,
)


class Command(BaseCommand):
    help = 'Inspect a user\'s commission engine, plan state, and per-sale calculation diagnostics.'

    def add_arguments(self, parser):
        parser.add_argument('identifier', help='Username, email, or numeric user id.')
        parser.add_argument('--repair', action='store_true', help='Apply safe repairs for assignment and activation consistency.')

    def _resolve_user(self, identifier):
        User = get_user_model()
        if identifier.isdigit():
            user = User.objects.filter(id=int(identifier)).first()
            if user:
                return user
        user = User.objects.filter(username__iexact=identifier).first()
        if user:
            return user
        user = User.objects.filter(email__iexact=identifier).first()
        if user:
            return user
        raise CommandError(f'No user found for identifier: {identifier}')

    def _repair_user(self, user):
        profile = getattr(user, 'sales_profile', None)
        onboarding = getattr(user, 'pay_plan_onboarding', None)
        if profile and onboarding and onboarding.current_version_id and profile.commission_system == UserProfile.LEGACY:
            profile.commission_system = UserProfile.PAY_PLAN_V2
            profile.save(update_fields=['commission_system', 'updated_at'])

        if onboarding and onboarding.status != PayPlanOnboarding.ACTIVE and onboarding.current_version_id:
            active_rule_count = onboarding.current_version.rules.filter(is_active=True).count()
            if active_rule_count > 0:
                onboarding.status = PayPlanOnboarding.ACTIVE
                onboarding.last_error = ''
                onboarding.save(update_fields=['status', 'last_error', 'updated_at'])

        sync_active_onboarding_assignment(user)

        active_assignments = list(
            PayPlanAssignment.objects.filter(user=user, is_active=True).order_by('-effective_start_date', '-id')
        )
        if len(active_assignments) > 1:
            keeper = active_assignments[0]
            PayPlanAssignment.objects.filter(
                user=user,
                is_active=True,
            ).exclude(pk=keeper.pk).update(is_active=False)

    def handle(self, *args, **options):
        user = self._resolve_user(options['identifier'])
        if options['repair']:
            self.stdout.write('=== BEFORE SAFE REPAIR ===')
            self._write_report(user)
            self._repair_user(user)
            self.stdout.write('=== AFTER SAFE REPAIR ===')
        self._write_report(user)

    def _write_report(self, user):
        plan_summary = CommissionEngineService.active_plan_summary(user)
        sales = list(Sale.objects.filter(user=user).order_by('date', 'dealNumber'))
        calculations = CommissionEngineService.calculate_sales(user, sales)

        self.stdout.write(f'User ID: {user.id}')
        self.stdout.write(f'Username: {user.username}')
        self.stdout.write(f'Email: {user.email}')
        self.stdout.write(f'Commission engine: {plan_summary["engine"]}')
        self.stdout.write(f'Legacy settings present: {plan_summary["legacy_settings_exists"]}')
        self.stdout.write(f'Legacy opt-out front: {plan_summary["legacy_opt_out_front"]}')
        self.stdout.write(f'Legacy opt-out back: {plan_summary["legacy_opt_out_back"]}')
        self.stdout.write(f'Legacy settings ignored: {plan_summary["legacy_ignored"]}')

        plan = plan_summary.get('plan')
        self.stdout.write(f'Number of plans owned: {getattr(user, "pay_plans", None).count() if hasattr(user, "pay_plans") else 0}')
        self.stdout.write('Plan versions:')
        for version in PayPlanVersion.objects.filter(
            pay_plan__owner_user=user
        ).prefetch_related('rules', 'documents').order_by('effective_start_date', 'id'):
            document = version.documents.filter(user=user).order_by('-uploaded_at').first()
            self.stdout.write(
                f'  version_id={version.id} name={version.version_name} '
                f'status={version.status} source={version.source_type} '
                f'effective={version.effective_start_date}..{version.effective_end_date or "open"} '
                f'active_rules={version.rules.filter(is_active=True).count()} '
                f'inactive_rules={version.rules.filter(is_active=False).count()}'
            )
            if version.processing_errors:
                self.stdout.write(f'    processing_errors={" | ".join(version.processing_errors)}')
            if version.processing_warnings:
                self.stdout.write(f'    processing_warnings={" | ".join(version.processing_warnings)}')
            if document:
                self.stdout.write(
                    f'    source_file={document.original_filename} '
                    f'available={document.is_available} parser={document.parser_version or version.parser_version}'
                )
        if plan:
            self.stdout.write(f'Active plan: {plan.name} (version id {plan_summary.get("pay_plan_version_id")})')
            self.stdout.write(f'Plan status: {plan_summary.get("plan_status")}')
            self.stdout.write(f'Effective start: {plan_summary.get("effective_start_date")}')
            self.stdout.write(f'Effective end: {plan_summary.get("effective_end_date")}')
            self.stdout.write(f'Imported filename: {plan_summary.get("imported_filename") or ""}')
            self.stdout.write(f'Imported at: {plan_summary.get("imported_at") or ""}')
            self.stdout.write(f'Active rules: {plan_summary.get("active_rule_count", 0)}')
            self.stdout.write(f'Inactive rules: {plan_summary.get("inactive_rule_count", 0)}')
            self.stdout.write(f'Front-end rules: {plan_summary.get("front_end_rule_count", 0)}')
            self.stdout.write(f'Back-end rules: {plan_summary.get("back_end_rule_count", 0)}')
            self.stdout.write(f'Unit bonus rules: {plan_summary.get("unit_bonus_rule_count", 0)}')
            self.stdout.write(f'Model-specific rules: {plan_summary.get("model_specific_rule_count", 0)}')
            self.stdout.write(f'New/used rules: {plan_summary.get("new_used_rule_count", 0)}')
            version = PayPlanVersion.objects.get(id=plan_summary['pay_plan_version_id'])
            self.stdout.write('Active backend rules:')
            self.stdout.write(
                '  default_backend_percentage='
                f'{version.default_backend_percentage} '
                f'minimum={version.default_backend_minimum} '
                f'maximum={version.default_backend_maximum}'
            )
            for rule in version.rules.filter(
                is_active=True,
                rule_type__in=['back_gross_percentage', 'flat_backend_commission'],
            ):
                rate = rule.configuration.get('rate')
                normalized = (
                    normalize_percentage_rate(rate) if rate is not None else None
                )
                self.stdout.write(
                    f'  rule_id={rule.id} name={rule.name} type={rule.rule_type} '
                    f'rate={rate} normalized_rate={normalized} '
                    f'configuration={rule.configuration}'
                )
            self.stdout.write('Unit-bonus rules:')
            for rule in version.rules.filter(
                is_active=True, rule_type__in=['volume_bonus', 'per_unit_bonus'],
            ):
                self.stdout.write(
                    f'  rule_id={rule.id} name={rule.name} configuration={rule.configuration}'
                )
            self.stdout.write('Draw rules:')
            for rule in version.rules.filter(is_active=True, rule_type='draw'):
                self.stdout.write(
                    f'  rule_id={rule.id} name={rule.name} configuration={rule.configuration}'
                )
        else:
            self.stdout.write('Active plan: none')

        self.stdout.write(f'Sales count: {len(sales)}')
        shared_deals = Sale.objects.filter(dealNumber__in=[sale.dealNumber for sale in sales]).exclude(user=user).count() if sales else 0
        self.stdout.write(f'Sales owned by another user unexpectedly: {shared_deals}')
        self.stdout.write('Per-sale diagnostics:')
        sales_by_id = {sale.id: sale for sale in sales}
        for item in calculations['results']:
            sale = sales_by_id[item.sale_id]
            self.stdout.write(
                f'  sale_id={item.sale_id} deal={sale.dealNumber} '
                f'front_gross=${item.frontend_gross:.2f} backend_gross=${item.backend_gross:.2f} '
                f'status={item.status} total=${item.total_commission:.2f} '
                f'front=${item.frontend_commission:.2f} back=${item.backend_commission:.2f} '
                f'bonus=${item.bonus_commission:.2f} '
                f'front_rule={item.frontend_rule or "none"} '
                f'backend_rule={item.backend_rule or "none"} '
                f'plan={item.plan_name} version={item.plan_version}'
            )
            if item.component_errors:
                self.stdout.write(f'    component_errors={item.component_errors}')
            if item.errors:
                self.stdout.write(f'    errors={" | ".join(item.errors)}')
            if item.explanation:
                self.stdout.write(f'    explanation={" | ".join(item.explanation)}')

        self.stdout.write(f'Total calculated commission: ${calculations["total_commission"]:.2f}')
        self.stdout.write(f'Calculated sales: {calculations["calculated_count"]}')
        self.stdout.write(f'Sales needing attention: {calculations["excluded_count"]}')
        self.stdout.write(
            f'Current period totals: front=${calculations["total_front"]:.2f} '
            f'back=${calculations["total_back"]:.2f} '
            f'unit_bonus=${calculations["period_unit_bonus"]:.2f} '
            f'total=${calculations["total_commission"]:.2f}'
        )
        if calculations.get('draw_progress'):
            self.stdout.write(f'Draw progress: {calculations["draw_progress"]}')
        if plan_summary['warnings']:
            self.stdout.write('Plan warnings:')
            for warning in plan_summary['warnings']:
                self.stdout.write(f'  - {warning}')
