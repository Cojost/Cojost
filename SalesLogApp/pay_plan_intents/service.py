from __future__ import annotations

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction

from SalesLogApp.models import PayPlanChangeRequest
from SalesLogApp.commission_engine.validators import validate_condition
from SalesLogApp.pay_plan_management import create_manual_draft, preview_version
from SalesLogApp.pay_plan_domain.adapters import VersionAdapter
from SalesLogApp.pay_plan_domain.compiler import PayPlanCompiler
from SalesLogApp.pay_plan_domain.services import CanonicalPlanStorageService

from .contract import IntentResolution, PayPlanIntent
from .handlers import TARGET_HANDLER_REGISTRY, active_version_for_user
from .interpreter import DeterministicIntentInterpreter


def interpret_request(
    source_text: str,
    *,
    effective_date: date | None = None,
) -> PayPlanIntent:
    return DeterministicIntentInterpreter().interpret(
        source_text,
        effective_date=effective_date,
    )


def resolve_intent(
    user,
    intent: PayPlanIntent,
    *,
    selected_target: str | None = None,
) -> IntentResolution:
    # Ownership is resolved before returning even a partial interpretation.
    active_version_for_user(user)
    if intent.missing_information or intent.ambiguities:
        return IntentResolution(
            'clarification',
            intent,
            message=intent.clarification_question,
        )
    handler = TARGET_HANDLER_REGISTRY.get(intent.target_type)
    if handler is None:
        message = (
            'I understood part of the request, but that pay-plan target is '
            'not supported safely yet. No draft was created.'
        )
        return IntentResolution('unsupported', intent, message=message)
    return handler.resolve(
        user,
        intent,
        selected_target=selected_target,
    )


@transaction.atomic
def create_draft_from_intent(
    user,
    intent: PayPlanIntent,
    effective_date: date,
    *,
    selected_target: str | None = None,
    expected_source_version_id: int | None = None,
    expected_current_value: str | None = None,
):
    """Confirmation boundary: this is the first operation allowed to mutate."""
    resolution = resolve_intent(
        user,
        intent,
        selected_target=selected_target,
    )
    if not resolution.may_create_draft:
        raise ValidationError(
            resolution.message
            or resolution.intent.clarification_question
            or 'The request needs clarification before a draft can be created.'
        )
    proposal = resolution.proposal
    if (
        expected_source_version_id is not None
        and proposal.source_version_id != expected_source_version_id
    ):
        raise ValidationError(
            'Your active pay plan changed after interpretation. Review the '
            'request again before creating a draft.'
        )
    if (
        expected_current_value is not None
        and proposal.current_display != expected_current_value
    ):
        raise ValidationError(
            'The current rule value changed after interpretation. Review the '
            'request again before creating a draft.'
        )
    source = active_version_for_user(user)
    if source.id != proposal.source_version_id:
        raise ValidationError(
            'Your active pay plan changed after interpretation. Review the '
            'request again before creating a draft.'
        )
    handler = TARGET_HANDLER_REGISTRY[intent.target_type]
    draft = create_manual_draft(user, effective_date)
    actions, warnings = handler.apply(
        user, draft, intent, proposal,
    )
    _validate_draft(draft)
    draft.processing_warnings = warnings
    draft.processing_errors = []
    draft.processing_status = 'needs_review'
    draft.activation_reason = f'Plain-language request: {intent.source_text}'
    draft.save(update_fields=[
        'processing_warnings', 'processing_errors', 'processing_status',
        'activation_reason', 'updated_at',
    ])
    preview = preview_version(user, draft)
    return PayPlanChangeRequest.objects.create(
        user=user,
        source_version=source,
        draft_version=draft,
        request_text=intent.source_text,
        parsed_actions=actions,
        warnings=warnings,
        preview={
            'intent': intent.as_dict(),
            'interpretation': {
                'action': proposal.action_type,
                'target': proposal.target_label,
                'from': proposal.current_display,
                'to': proposal.new_display,
                'applies_to': proposal.applies_to,
                'effective_date': effective_date.isoformat(),
            },
            'sales_tested': preview['sales_tested'],
            'calculated_count': preview['calculated_count'],
            'estimated_total': str(preview['estimated_total']),
        },
    )


def _validate_draft(draft):
    draft.full_clean()
    for rule in draft.rules.prefetch_related('conditions'):
        rule.full_clean()
        for condition in rule.conditions.all():
            validate_condition(condition.as_dict())
    canonical = VersionAdapter.to_canonical(draft)
    report = PayPlanCompiler.compile(canonical)
    if report.errors:
        messages = '; '.join(item.message for item in report.errors)
        raise ValidationError(
            f'The proposed draft failed pay-plan domain validation: {messages}'
        )
    CanonicalPlanStorageService.store_compilation(draft, canonical, report)
