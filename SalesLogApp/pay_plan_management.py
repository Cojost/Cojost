from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .access import get_or_create_onboarding, uses_new_engine
from .commission_service import (
    CommissionEngineService,
    STATUS_CALCULATION_ERROR,
    STATUS_CONFIGURATION_ERROR,
)
from .models import (
    PayPlanActivationEvent,
    PayPlanAssignment,
    PayPlanChangePattern,
    PayPlanChangeRequest,
    PayPlanDocument,
    PayPlanRule,
    PayPlanRuleCondition,
    PayPlanVersion,
    Sale,
)
from .pay_plan_imports import (
    PARSER_VERSION, apply_import_draft_to_version, build_upload_import_draft,
    parse_description_to_import_draft,
)


def _next_version_number(plan) -> int:
    value = plan.versions.aggregate(value=Max('version_number'))['value']
    if value is None:
        value = plan.versions.count()
    return value + 1


def _active_version_for_user(user):
    assignment = (
        PayPlanAssignment.objects.select_related('pay_plan_version__pay_plan')
        .filter(user=user, is_active=True)
        .order_by('-effective_start_date', '-id')
        .first()
    )
    if assignment is None:
        return None
    version = assignment.pay_plan_version
    if version.pay_plan.owner_user_id != user.id:
        raise ValidationError(
            'The active assignment references another user’s pay plan.'
        )
    return version


def _create_version(user, plan_name, effective_start_date, source_type, source_filename):
    if not uses_new_engine(user):
        raise ValidationError('This account is not assigned to the new pay-plan engine.')
    onboarding = get_or_create_onboarding(user)
    current = _active_version_for_user(user) or onboarding.current_version
    plan = current.pay_plan if current else onboarding.current_pay_plan
    if plan is None or plan.owner_user_id != user.id:
        raise ValidationError('No user-owned pay plan is available for replacement.')
    if plan_name and plan.name != plan_name:
        from .models import PayPlan
        plan = PayPlan.objects.create(
            industry=plan.industry,
            owner_user=user,
            name=plan_name,
            description=plan.description,
            dealership_name=plan.dealership_name,
            is_active=False,
        )
    number = _next_version_number(plan)
    return PayPlanVersion.objects.create(
        pay_plan=plan,
        version_name=f'Version {number}',
        version_number=number,
        effective_start_date=effective_start_date,
        status=PayPlanVersion.REVIEW_REQUIRED,
        source_type=source_type,
        source_filename=source_filename,
        previous_version=current,
        parser_version=PARSER_VERSION,
        processing_status='needs_review',
        created_by=user,
    )


def create_replacement_draft(user, uploaded_files, plan_name, effective_start_date):
    uploaded_files = list(uploaded_files)
    version = _create_version(
        user, plan_name, effective_start_date, PayPlanVersion.SOURCE_UPLOAD,
        uploaded_files[0].name if uploaded_files else '',
    )
    onboarding = get_or_create_onboarding(user)
    documents = []
    for order, upload in enumerate(uploaded_files, start=1):
        documents.append(PayPlanDocument.objects.create(
            user=user,
            onboarding=onboarding,
            pay_plan=version.pay_plan,
            pay_plan_version=version,
            original_filename=upload.name,
            file=upload,
            mime_type=upload.content_type,
            file_size=upload.size,
            document_type=(
                PayPlanDocument.PDF
                if upload.content_type == 'application/pdf'
                else PayPlanDocument.IMAGE
            ),
            status=PayPlanDocument.NEEDS_REVIEW,
            page_order=order,
            parser_version=PARSER_VERSION,
            last_processed_at=timezone.now(),
        ))
    draft = build_upload_import_draft(documents, version.pay_plan.name)
    result = apply_import_draft_to_version(version, draft, overwrite=True)
    warnings = list(draft.get('warnings') or [])
    if result['rejected_rules']:
        warnings.extend(result['rejected_rules'])
    version.processing_warnings = warnings
    version.processing_errors = (
        ['No usable rules were extracted. Manual review is required.']
        if result['created_rules'] == 0 else []
    )
    version.processing_status = 'needs_review'
    version.save(update_fields=[
        'processing_warnings', 'processing_errors', 'processing_status', 'updated_at',
    ])
    for document in documents:
        document.processing_warnings = warnings
        document.processing_errors = version.processing_errors
        document.save(update_fields=[
            'processing_warnings', 'processing_errors', 'updated_at',
        ])
    return version


def create_pasted_replacement_draft(
    user, pasted_text, plan_name, effective_start_date,
):
    text = (pasted_text or '').strip()
    if not text:
        raise ValidationError('Paste pay-plan text before continuing.')
    version = _create_version(
        user, plan_name, effective_start_date, PayPlanVersion.SOURCE_PASTE,
        'pasted-pay-plan-text',
    )
    draft = parse_description_to_import_draft(text, version.pay_plan.name)
    draft['source'] = 'paste'
    result = apply_import_draft_to_version(version, draft, overwrite=True)
    warnings = list(draft.get('warnings') or [])
    warnings.append(
        'This version was created from pasted text. Compare every extracted '
        'rule with the original plan before activation.'
    )
    if result['rejected_rules']:
        warnings.extend(result['rejected_rules'])
    version.processing_warnings = warnings
    version.processing_errors = (
        ['No usable rules were extracted from the pasted text.']
        if result['created_rules'] == 0 else []
    )
    version.processing_status = 'needs_review'
    version.save(update_fields=[
        'processing_warnings', 'processing_errors', 'processing_status',
        'updated_at',
    ])
    return version


def create_manual_draft(user, effective_start_date):
    current = _active_version_for_user(user)
    if current is None:
        raise ValidationError('An active plan is required before manual editing.')
    version = _create_version(
        user, current.pay_plan.name, effective_start_date,
        PayPlanVersion.SOURCE_MANUAL, '',
    )
    version.default_backend_percentage = current.default_backend_percentage
    version.default_backend_minimum = current.default_backend_minimum
    version.default_backend_maximum = current.default_backend_maximum
    version.save(update_fields=[
        'default_backend_percentage', 'default_backend_minimum',
        'default_backend_maximum', 'updated_at',
    ])
    from .pay_plan_scope import OwnedPayPlanRuleService
    OwnedPayPlanRuleService.validate_clone_ownership(user, current, version)
    for source_rule in current.rules.prefetch_related('conditions').all():
        rule = PayPlanRule.objects.create(
            pay_plan_version=version,
            semantic_key=source_rule.semantic_key,
            name=source_rule.name,
            description=source_rule.description,
            rule_type=source_rule.rule_type,
            calculation_scope=source_rule.calculation_scope,
            condition_group_operator=source_rule.condition_group_operator,
            configuration=deepcopy(source_rule.configuration),
            is_active=source_rule.is_active,
            sort_order=source_rule.sort_order,
        )
        PayPlanRuleCondition.objects.bulk_create([
            PayPlanRuleCondition(
                rule=rule,
                field_name=condition.field_name,
                operator=condition.operator,
                value=deepcopy(condition.value),
                sort_order=condition.sort_order,
            )
            for condition in source_rule.conditions.all()
        ])
    version.processing_status = 'needs_review'
    if not version.rules.exists():
        version.processing_errors = [
            'The active version had no rules to clone. Add rules before activation.'
        ]
    version.save(update_fields=[
        'processing_status', 'processing_errors', 'updated_at',
    ])
    return version


def reload_existing_document(user, document, effective_start_date):
    if document.user_id != user.id:
        raise ValidationError('The source document does not belong to this user.')
    if not document.is_available:
        raise ValidationError('The source file is no longer available. Upload a replacement.')
    version = _create_version(
        user, document.pay_plan.name, effective_start_date,
        PayPlanVersion.SOURCE_RELOAD, document.original_filename,
    )
    copied = PayPlanDocument.objects.create(
        user=user,
        onboarding=document.onboarding,
        pay_plan=version.pay_plan,
        pay_plan_version=version,
        original_filename=document.original_filename,
        file=document.file.name,
        mime_type=document.mime_type,
        file_size=document.file_size,
        document_type=document.document_type,
        status=PayPlanDocument.NEEDS_REVIEW,
        page_order=document.page_order,
        parser_version=PARSER_VERSION,
        last_processed_at=timezone.now(),
    )
    draft = build_upload_import_draft([copied], version.pay_plan.name)
    result = apply_import_draft_to_version(version, draft, overwrite=True)
    version.processing_warnings = draft['warnings']
    version.processing_errors = (
        ['No usable rules were extracted. Manual review is required.']
        if result['created_rules'] == 0 else result['rejected_rules']
    )
    version.processing_status = 'needs_review'
    version.save(update_fields=[
        'processing_warnings', 'processing_errors', 'processing_status', 'updated_at',
    ])
    return version


def preview_version(user, version):
    if version.pay_plan.owner_user_id != user.id:
        raise ValidationError('This pay-plan version does not belong to the user.')
    sales = list(
        Sale.objects.filter(
            user=user,
            date__gte=version.effective_start_date,
        ).filter(
            date__lte=version.effective_end_date
        ) if version.effective_end_date else
        Sale.objects.filter(user=user, date__gte=version.effective_start_date)
    )
    return CommissionEngineService.preview_sales(user, sales, version)


def _serialize_report(report):
    return {
        'sales_tested': report.get('sales_tested', len(report.get('results', []))),
        'calculated_count': report.get('calculated_count', 0),
        'excluded_count': report.get('excluded_count', 0),
        'valid_zero_count': report.get('valid_zero_count', 0),
        'missing_information_count': report.get('missing_information_count', 0),
        'no_matching_rule_count': report.get('no_matching_rule_count', 0),
        'estimated_total': str(
            report.get('estimated_total', report.get('total_commission', Decimal('0.00')))
        ),
    }


class PayPlanActivationService:
    @classmethod
    @transaction.atomic
    def activate(cls, user, draft_plan, warnings_approved=False, reason='User approved replacement'):
        if draft_plan.pay_plan.owner_user_id != user.id:
            raise ValidationError('This draft does not belong to the signed-in user.')
        if draft_plan.is_sandbox:
            raise ValidationError(
                'Sandbox drafts must use the sandbox activation workflow.'
            )
        if draft_plan.status not in {
            PayPlanVersion.DRAFT, PayPlanVersion.REVIEW_REQUIRED,
        }:
            raise ValidationError('Only a draft awaiting review can be activated.')

        PayPlanVersion.objects.select_for_update().filter(
            pay_plan=draft_plan.pay_plan
        ).exists()
        assignments = PayPlanAssignment.objects.select_for_update().filter(user=user)
        rules = list(draft_plan.rules.filter(is_active=True).prefetch_related('conditions'))
        bonus_rule_types = {
            'volume_bonus',
            'per_unit_bonus',
            'period_qualification_bonus',
            'survey_count_bonus',
            'acquisition_bonus',
            'vehicle_spiff',
        }
        for rule in rules:
            if rule.rule_type not in bonus_rule_types:
                continue
            configuration = dict(rule.configuration or {})
            configuration.setdefault(
                'effective_start_date',
                draft_plan.effective_start_date.isoformat(),
            )
            if configuration != rule.configuration:
                rule.configuration = configuration
                rule.save(update_fields=['configuration', 'updated_at'])
        if not rules and draft_plan.default_backend_percentage is None:
            raise ValidationError('Activation blocked: no usable active rules were extracted.')
        for rule in rules:
            rule.full_clean()
            for condition in rule.conditions.all():
                from .commission_engine.validators import validate_condition
                validate_condition(condition.as_dict())
        if draft_plan.processing_errors:
            raise ValidationError('Activation blocked until processing errors are resolved.')
        if draft_plan.processing_warnings and not warnings_approved:
            raise ValidationError('Confirm the parser warnings before activation.')

        preview = preview_version(user, draft_plan)
        if preview['sales_tested'] and preview['calculated_count'] == 0:
            raise ValidationError(
                'Activation blocked because the draft would leave every applicable sale unmatched.'
            )

        previous_assignment = (
            assignments.filter(is_active=True)
            .order_by('-effective_start_date', '-id')
            .first()
        )
        previous = previous_assignment.pay_plan_version if previous_assignment else None
        if previous and previous.pay_plan.owner_user_id != user.id:
            raise ValidationError(
                'Activation blocked: the previous assignment references '
                'another user’s pay plan.'
            )
        applicable_sales = list(Sale.objects.filter(
            user=user, date__gte=draft_plan.effective_start_date,
        ))
        previous_total = CommissionEngineService.calculate_sales(
            user, applicable_sales,
        )['total_commission']
        now = timezone.now()
        if previous_assignment:
            previous_end = draft_plan.effective_start_date - timedelta(days=1)
            if previous_end >= previous_assignment.effective_start_date:
                previous_assignment.effective_end_date = previous_end
                previous_assignment.save(update_fields=['effective_end_date', 'updated_at'])
            else:
                previous_assignment.is_active = False
                previous_assignment.save(update_fields=['is_active', 'updated_at'])
        if previous and previous.pk != draft_plan.pk:
            previous.status = PayPlanVersion.INACTIVE
            previous.deactivated_at = now
            previous.effective_end_date = (
                previous_assignment.effective_end_date
                if previous_assignment and previous_assignment.is_active else
                previous.effective_end_date
            )
            previous.save(update_fields=[
                'status', 'deactivated_at', 'effective_end_date', 'updated_at',
            ])
            if previous.pay_plan_id != draft_plan.pay_plan_id:
                previous.pay_plan.is_active = False
                previous.pay_plan.save(update_fields=['is_active', 'updated_at'])

        draft_plan.status = PayPlanVersion.ACTIVE
        draft_plan.activated_at = now
        draft_plan.activation_reason = reason
        draft_plan.processing_status = 'active'
        draft_plan.save(update_fields=[
            'status', 'activated_at', 'activation_reason',
            'processing_status', 'updated_at',
        ])
        if not draft_plan.pay_plan.is_active:
            draft_plan.pay_plan.is_active = True
            draft_plan.pay_plan.save(update_fields=['is_active', 'updated_at'])
        PayPlanAssignment.objects.create(
            user=user,
            pay_plan_version=draft_plan,
            effective_start_date=draft_plan.effective_start_date,
            effective_end_date=draft_plan.effective_end_date,
            is_active=True,
        )
        onboarding = get_or_create_onboarding(user)
        onboarding.current_pay_plan = draft_plan.pay_plan
        onboarding.current_version = draft_plan
        onboarding.status = onboarding.ACTIVE
        onboarding.completed_at = now
        onboarding.last_error = ''
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status',
            'completed_at', 'last_error', 'updated_at',
        ])

        recalculation = CommissionEngineService.calculate_sales(
            user, applicable_sales,
        )
        if any(
            item.status in {STATUS_CONFIGURATION_ERROR, STATUS_CALCULATION_ERROR}
            for item in recalculation['results']
        ):
            raise ValidationError('Activation rolled back because recalculation failed.')
        report = {
            **_serialize_report(preview),
            'previous_total': str(previous_total),
            'new_total': str(recalculation['total_commission']),
            'difference': str(recalculation['total_commission'] - previous_total),
        }
        PayPlanActivationEvent.objects.create(
            user=user,
            version=draft_plan,
            previous_version=previous if previous != draft_plan else None,
            action=PayPlanActivationEvent.ACTIVATED,
            reason=reason,
            report=report,
        )
        change_request = PayPlanChangeRequest.objects.filter(
            draft_version=draft_plan,
            status=PayPlanChangeRequest.NEEDS_REVIEW,
        ).first()
        if change_request is not None:
            change_request.status = PayPlanChangeRequest.APPLIED
            change_request.reviewed_at = now
            change_request.preview = {
                **change_request.preview,
                'activation_report': report,
            }
            change_request.save(update_fields=[
                'status', 'reviewed_at', 'preview',
            ])
            for action in change_request.parsed_actions:
                pattern, _ = PayPlanChangePattern.objects.get_or_create(
                    action_type=action['action_type'],
                    target_key=action['target_key'],
                )
                pattern.approved_count += 1
                pattern.example_request = change_request.request_text
                pattern.last_approved_at = now
                pattern.save(update_fields=[
                    'approved_count', 'example_request', 'last_approved_at',
                ])
        return report


def recalculate_commissions(user):
    version = _active_version_for_user(user)
    if version is None or version.status != PayPlanVersion.ACTIVE:
        raise ValidationError('An active pay plan is required before recalculation.')
    sales = list(Sale.objects.filter(
        user=user,
        date__gte=version.effective_start_date,
    ))
    report = CommissionEngineService.calculate_sales(user, sales)
    serialized = {
        'sales_tested': len(sales),
        'calculated_count': report['calculated_count'],
        'excluded_count': report['excluded_count'],
        'valid_zero_count': sum(
            1 for item in report['results']
            if item.calculated and item.total_commission == Decimal('0.00')
        ),
        'new_total': str(report['total_commission']),
        'results': report['results'],
    }
    PayPlanActivationEvent.objects.create(
        user=user,
        version=version,
        action=PayPlanActivationEvent.RECALCULATED,
        reason='Manual user recalculation',
        report={key: value for key, value in serialized.items() if key != 'results'},
    )
    return serialized
