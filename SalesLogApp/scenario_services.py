"""Application services for saved Commission Sandbox scenarios.

The services in this module deliberately operate on the existing
``CommissionSandbox`` aggregate.  A scenario always owns a sandbox-only
``PayPlanVersion``; production versions and production sales are inputs, never
mutable scenario state.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .commission_engine.engine import resolve_pay_plan_version
from .models import (
    CommissionSandbox,
    PayPlanRule,
    PayPlanRuleCondition,
    PayPlanVersion,
    SandboxHypotheticalDeal,
    SandboxResult,
    SandboxRun,
    ScenarioHistory,
)
from .pay_plan_domain.adapters import VersionAdapter
from .pay_plan_domain.compiler import PayPlanCompiler
from .pay_plan_domain.services import CanonicalPlanStorageService
from .sandbox_services import (
    SANDBOX_ENGINE_VERSION,
    SANDBOX_SCHEMA_VERSION,
    SandboxCompiler,
    ScenarioRunner,
)


SCENARIO_ENGINE_VERSION = SANDBOX_ENGINE_VERSION
SCENARIO_SCHEMA_VERSION = SANDBOX_SCHEMA_VERSION
_MONEY_QUANTUM = Decimal("0.01")
_PERCENT_QUANTUM = Decimal("0.01")


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _money(value):
    try:
        return Decimal(str(value or 0)).quantize(_MONEY_QUANTUM)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _money_string(value):
    return format(_money(value), ".2f")


def _percentage_string(value):
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(_PERCENT_QUANTUM), ".2f")
    except (InvalidOperation, TypeError, ValueError):
        return None


def _hash_payload(payload):
    return hashlib.sha256(
        json.dumps(
            _json_safe(payload),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _owned_scenario(user, scenario, *, for_update=False):
    queryset = CommissionSandbox.objects.select_related(
        "source_version__pay_plan",
        "draft_version__pay_plan",
        "source_scenario",
    )
    if for_update:
        queryset = queryset.select_for_update()
    if isinstance(scenario, CommissionSandbox):
        lookup = {"pk": scenario.pk}
    else:
        lookup = {"public_id": scenario}
    try:
        return queryset.get(owner=user, **lookup)
    except CommissionSandbox.DoesNotExist as exc:
        raise PermissionDenied("Scenario not found.") from exc


def _assert_owner(user, version):
    if version.pay_plan.owner_user_id != user.pk:
        raise PermissionDenied("The pay-plan version does not belong to this user.")


def _assert_editable(scenario):
    if scenario.status != CommissionSandbox.DRAFT:
        raise ValidationError(
            "Only a draft scenario can be changed. Restore an archived scenario "
            "or duplicate a converted scenario first."
        )


def _normalized_name(name):
    name = (name or "").strip()
    if not name:
        raise ValidationError({"scenario_name": "Scenario name is required."})
    if len(name) > 150:
        raise ValidationError({
            "scenario_name": "Scenario names cannot exceed 150 characters.",
        })
    return name


def _validate_available_name(user, name, *, exclude=None):
    name = _normalized_name(name)
    queryset = CommissionSandbox.objects.filter(
        owner=user,
        scenario_name__iexact=name,
    ).exclude(status=CommissionSandbox.ARCHIVED)
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)
    if queryset.exists():
        raise ValidationError({
            "scenario_name": "You already have an active scenario with this name.",
        })
    return name


def _unique_version_name(plan, prefix):
    base = (prefix or "Scenario").strip()[:100] or "Scenario"
    candidate = base
    suffix = 2
    while plan.versions.filter(version_name=candidate).exists():
        marker = f" ({suffix})"
        candidate = f"{base[:100 - len(marker)]}{marker}"
        suffix += 1
    return candidate


def _version_fingerprint(version):
    """Fingerprint every calculation-relevant part of a stored version."""

    canonical = VersionAdapter.to_canonical(version)
    return _hash_payload({
        "canonical": canonical.to_dict(),
        "effective_start_date": version.effective_start_date,
        "effective_end_date": version.effective_end_date,
        "default_backend_percentage": version.default_backend_percentage,
        "default_backend_minimum": version.default_backend_minimum,
        "default_backend_maximum": version.default_backend_maximum,
    })


def _scenario_input_fingerprint(user, scenario):
    runner_fingerprint = ScenarioRunner.current_input_fingerprint(
        user,
        scenario,
        scenario.replay_mode,
        scenario.replay_start_date,
        scenario.replay_end_date,
    )
    return _hash_payload({
        "runner": runner_fingerprint,
        "assumptions": scenario.assumptions,
        "replay_filters": scenario.replay_filters,
        "draft_version": _version_fingerprint(scenario.draft_version),
    })


def _live_version_fingerprint(user, scenario):
    production_sales, _ = ScenarioRunner.collect_inputs(
        user,
        scenario,
        scenario.replay_mode,
        scenario.replay_start_date,
        scenario.replay_end_date,
    )
    fingerprints = {
        _version_fingerprint(resolve_pay_plan_version(user, sale.date))
        for sale in production_sales
    }
    return (
        hashlib.sha256(":".join(sorted(fingerprints)).encode("utf-8")).hexdigest()
        if fingerprints else ""
    )


class ScenarioHistoryService:
    """Owner-scoped, JSON-safe scenario audit entries."""

    @staticmethod
    def record(user, scenario, action, summary, metadata=None):
        scenario = _owned_scenario(user, scenario)
        entry = ScenarioHistory(
            scenario=scenario,
            actor=user,
            action=action,
            summary=summary,
            metadata=_json_safe(metadata or {}),
        )
        entry.full_clean()
        entry.save()
        return entry


class ScenarioCloneService:
    """Deep cloning for versions, rules, conditions, and hypothetical deals."""

    @staticmethod
    def clone_version(
        user,
        source_version,
        *,
        is_sandbox=True,
        status=None,
        previous_version=None,
        origin_scenario=None,
        version_name=None,
        version_number=None,
        effective_start_date=None,
    ):
        _assert_owner(user, source_version)
        plan = source_version.pay_plan
        status = status or PayPlanVersion.DRAFT
        prefix = version_name or (
            f"Scenario {timezone.now():%Y%m%d%H%M%S%f}"
            if is_sandbox
            else "Scenario Draft"
        )
        clone = PayPlanVersion(
            pay_plan=plan,
            version_name=_unique_version_name(plan, prefix),
            version_number=version_number,
            effective_start_date=(
                effective_start_date or source_version.effective_start_date
            ),
            effective_end_date=(
                source_version.effective_end_date
                if (
                    source_version.effective_end_date is None
                    or source_version.effective_end_date
                    >= (effective_start_date or source_version.effective_start_date)
                )
                else None
            ),
            status=status,
            source_type=PayPlanVersion.SOURCE_MANUAL,
            source_filename=source_version.source_filename,
            previous_version=previous_version or source_version,
            parser_version=source_version.parser_version,
            processing_status=("sandbox" if is_sandbox else "needs_review"),
            processing_errors=deepcopy(source_version.processing_errors),
            processing_warnings=deepcopy(source_version.processing_warnings),
            is_sandbox=is_sandbox,
            origin_scenario=origin_scenario,
            default_backend_percentage=source_version.default_backend_percentage,
            default_backend_minimum=source_version.default_backend_minimum,
            default_backend_maximum=source_version.default_backend_maximum,
            created_by=user,
        )
        clone.full_clean()
        clone.save()

        source_rules = source_version.rules.prefetch_related("conditions").order_by(
            "sort_order", "id",
        )
        for source_rule in source_rules:
            rule = PayPlanRule(
                pay_plan_version=clone,
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
            rule.full_clean()
            rule.save()
            conditions = []
            for source_condition in source_rule.conditions.all():
                condition = PayPlanRuleCondition(
                    rule=rule,
                    field_name=source_condition.field_name,
                    operator=source_condition.operator,
                    value=deepcopy(source_condition.value),
                    sort_order=source_condition.sort_order,
                )
                condition.full_clean()
                conditions.append(condition)
            PayPlanRuleCondition.objects.bulk_create(conditions)
        return clone

    @staticmethod
    def clone_hypothetical_sales(source_scenario, target_scenario):
        if source_scenario.owner_id != target_scenario.owner_id:
            raise PermissionDenied(
                "Hypothetical deals cannot be copied between users."
            )
        excluded = {"id", "sandbox_id", "created_at", "updated_at"}
        clones = []
        for source in source_scenario.hypothetical_deals.all():
            values = {}
            for field in SandboxHypotheticalDeal._meta.concrete_fields:
                if field.attname in excluded or field.name in excluded:
                    continue
                values[field.attname] = deepcopy(getattr(source, field.attname))
            clones.append(SandboxHypotheticalDeal(
                sandbox=target_scenario,
                **values,
            ))
        SandboxHypotheticalDeal.objects.bulk_create(clones)
        return clones

    @staticmethod
    def unique_duplicate_name(user, source_name):
        base = f"Copy of {source_name}"[:150]
        if not CommissionSandbox.objects.filter(
            owner=user,
            scenario_name__iexact=base,
        ).exclude(status=CommissionSandbox.ARCHIVED).exists():
            return base
        number = 2
        while True:
            suffix = f" ({number})"
            candidate = f"{base[:150 - len(suffix)]}{suffix}"
            if not CommissionSandbox.objects.filter(
                owner=user,
                scenario_name__iexact=candidate,
            ).exclude(status=CommissionSandbox.ARCHIVED).exists():
                return candidate
            number += 1

    @classmethod
    @transaction.atomic
    def clone_scenario(
        cls,
        user,
        source,
        *,
        name,
        description=None,
        action="scenario_saved_as",
    ):
        source = _owned_scenario(user, source, for_update=True)
        name = _validate_available_name(user, name)
        draft = cls.clone_version(
            user,
            source.draft_version,
            is_sandbox=True,
            previous_version=source.source_version,
        )
        scenario = CommissionSandbox(
            owner=user,
            source_version=source.source_version,
            draft_version=draft,
            source_scenario=source,
            scenario_name=name,
            scenario_notes=(
                source.scenario_notes if description is None else description
            ),
            status=CommissionSandbox.DRAFT,
            replay_mode=source.replay_mode,
            replay_start_date=source.replay_start_date,
            replay_end_date=source.replay_end_date,
            assumptions=deepcopy(source.assumptions),
            replay_filters=deepcopy(source.replay_filters),
            revision=1,
            saved_revision=1,
            last_calculated_revision=0,
            last_saved_at=timezone.now(),
        )
        scenario.full_clean()
        scenario.save()
        cls.clone_hypothetical_sales(source, scenario)
        report = SandboxCompiler.compile(scenario)
        scenario.validation_summary = ScenarioValidationService.summarize(report)
        scenario.save(update_fields=["validation_summary", "updated_at"])
        ScenarioHistoryService.record(
            user,
            scenario,
            action,
            "Scenario created as an independent copy.",
            {
                "source_scenario_id": source.pk,
                "source_version_id": source.source_version_id,
                "rule_count": draft.rules.count(),
                "hypothetical_sale_count": scenario.hypothetical_deals.count(),
            },
        )
        if not report.errors:
            ScenarioCalculationService.recalculate(user, scenario)
        return _owned_scenario(user, scenario)

    @classmethod
    def duplicate(cls, user, scenario):
        source = _owned_scenario(user, scenario)
        return cls.clone_scenario(
            user,
            source,
            name=cls.unique_duplicate_name(user, source.scenario_name),
            description=source.scenario_notes,
            action="scenario_duplicated",
        )


class ScenarioValidationService:
    """Compile and expose a serialization-safe validation result."""

    @staticmethod
    def summarize(report):
        return {
            "valid": not bool(report.errors),
            "critical_error_count": len(report.errors),
            "warning_count": len(report.warnings),
            "errors": [asdict(item) for item in report.errors],
            "warnings": [asdict(item) for item in report.warnings],
            "statistics": deepcopy(report.statistics),
            "compiled_rule_count": report.compiled_rule_count,
            "skipped_rules": deepcopy(report.skipped_rules),
            "unsupported_clauses": list(report.unsupported_clauses),
        }

    @classmethod
    def validate(cls, user, scenario):
        scenario = _owned_scenario(user, scenario)
        report = SandboxCompiler.compile(scenario)
        summary = cls.summarize(report)
        CommissionSandbox.objects.filter(pk=scenario.pk).update(
            validation_summary=summary,
        )
        scenario.validation_summary = summary
        return report


class ScenarioCalculationService:
    """Replay a scenario with the injected draft version and persist a snapshot."""

    @staticmethod
    def _component_totals(results, explanation_field):
        totals = {
            "front_end": Decimal("0"),
            "back_end": Decimal("0"),
            "bonuses": Decimal("0"),
            "minimums_applied": Decimal("0"),
            "packs_deducted": Decimal("0"),
        }
        warning_count = 0
        for result in results:
            explanation = getattr(result, explanation_field, None) or {}
            warning_count += len(explanation.get("warnings") or ())
            for rule in explanation.get("rules") or ():
                if not rule.get("applied"):
                    continue
                amount = _money(rule.get("amount"))
                category = rule.get("category")
                rule_type = rule.get("rule_type", "")
                if category == "front_end":
                    totals["front_end"] += amount
                elif category == "back_end":
                    totals["back_end"] += amount
                elif category in {"bonus", "spiff"}:
                    totals["bonuses"] += amount
                if (
                    category == "minimum_adjustment"
                    or rule_type == "minimum_commission"
                ):
                    totals["minimums_applied"] += amount
                metadata = rule.get("metadata") or {}
                for key in ("pack_amount", "pack", "soft_pack"):
                    if metadata.get(key) not in (None, ""):
                        totals["packs_deducted"] += abs(_money(metadata[key]))
                        break
        return totals, warning_count

    @classmethod
    def _summary(cls, run, report):
        results = list(run.results.all())
        live, live_warnings = cls._component_totals(
            results, "actual_explanation",
        )
        scenario, scenario_warnings = cls._component_totals(
            results, "explanation",
        )
        # Period rules are represented on the period result, not repeated on
        # each deal.  Add them exactly once to each side of the comparison.
        live["bonuses"] += _money(
            (run.statistics or {}).get("actual_period_bonus")
        )
        scenario["bonuses"] += _money(
            (run.statistics or {}).get("sandbox_period_bonus")
        )
        counts = {
            SandboxResult.HIGHER: 0,
            SandboxResult.LOWER: 0,
            SandboxResult.UNCHANGED: 0,
        }
        for item in results:
            counts[item.comparison] = counts.get(item.comparison, 0) + 1
        sale_count = len(results)
        average = (
            run.sandbox_total / sale_count if sale_count else Decimal("0")
        )
        return {
            "currency": "USD",
            "live_total": _money_string(run.actual_total),
            "scenario_total": _money_string(run.sandbox_total),
            "difference": _money_string(run.difference),
            "percentage_change": _percentage_string(run.percent_change),
            "average_commission_per_deal": _money_string(average),
            "sale_count": sale_count,
            "increased_deals": counts[SandboxResult.HIGHER],
            "decreased_deals": counts[SandboxResult.LOWER],
            "unchanged_deals": counts[SandboxResult.UNCHANGED],
            "warning_count": (
                len(report.warnings) + live_warnings + scenario_warnings
            ),
            "error_count": len(report.errors),
            "engine_version": SCENARIO_ENGINE_VERSION,
            "schema_version": SCENARIO_SCHEMA_VERSION,
            "live_components": {
                key: _money_string(value) for key, value in live.items()
            },
            "scenario_components": {
                key: _money_string(value) for key, value in scenario.items()
            },
        }

    @classmethod
    @transaction.atomic
    def recalculate(
        cls,
        user,
        scenario,
        mode=None,
        start=None,
        end=None,
    ):
        scenario = _owned_scenario(user, scenario, for_update=True)
        _assert_editable(scenario)
        mode = mode or scenario.replay_mode
        start = scenario.replay_start_date if start is None else start
        end = scenario.replay_end_date if end is None else end
        if start and end and end < start:
            raise ValidationError(
                {"replay_end_date": "Replay end date cannot precede start date."}
            )
        if mode not in dict(CommissionSandbox.REPLAY_MODE_CHOICES):
            raise ValidationError({"replay_mode": "Select a valid replay mode."})

        settings_changed = (
            mode != scenario.replay_mode
            or start != scenario.replay_start_date
            or end != scenario.replay_end_date
        )
        if settings_changed:
            scenario.replay_mode = mode
            scenario.replay_start_date = start
            scenario.replay_end_date = end
            scenario.revision += 1
            scenario.save(update_fields=[
                "replay_mode",
                "replay_start_date",
                "replay_end_date",
                "revision",
                "updated_at",
            ])

        report = SandboxCompiler.compile(scenario)
        validation = ScenarioValidationService.summarize(report)
        if report.errors:
            scenario.validation_summary = validation
            scenario.save(update_fields=["validation_summary", "updated_at"])
            raise ValidationError([
                f"{item.code}: {item.message}" for item in report.errors
            ])

        run_kwargs = {
            "mode": mode,
            "period_start": start,
            "period_end": end,
        }
        # Newer runners support a force keyword.  Keeping this feature-detected
        # preserves compatibility with existing Phase 5A installations.
        if "force" in inspect.signature(ScenarioRunner.run).parameters:
            run_kwargs["force"] = True
        run = ScenarioRunner.run(user, scenario, **run_kwargs)
        now = timezone.now()
        input_fingerprint = _scenario_input_fingerprint(user, scenario)
        source_fingerprint = _version_fingerprint(scenario.source_version)
        live_version_fingerprint = _live_version_fingerprint(user, scenario)
        summary = cls._summary(run, report)

        run.engine_version = SCENARIO_ENGINE_VERSION
        run.schema_version = SCENARIO_SCHEMA_VERSION
        run.source_fingerprint = source_fingerprint
        run.live_version_fingerprint = live_version_fingerprint
        run.save(update_fields=[
            "engine_version",
            "schema_version",
            "source_fingerprint",
            "live_version_fingerprint",
        ])
        scenario.calculation_summary = summary
        scenario.validation_summary = validation
        scenario.calculation_input_fingerprint = input_fingerprint
        scenario.calculation_source_fingerprint = source_fingerprint
        scenario.calculation_engine_version = SCENARIO_ENGINE_VERSION
        scenario.last_calculated_revision = scenario.revision
        scenario.last_calculated_at = now
        scenario.save(update_fields=[
            "calculation_summary",
            "validation_summary",
            "calculation_input_fingerprint",
            "calculation_source_fingerprint",
            "calculation_engine_version",
            "last_calculated_revision",
            "last_calculated_at",
            "updated_at",
        ])
        return run

    @classmethod
    def stale_reasons(cls, user, scenario):
        scenario = _owned_scenario(user, scenario)
        if not scenario.last_calculated_at:
            return ["Scenario has not been calculated."]
        reasons = []
        if scenario.last_calculated_revision != scenario.revision:
            reasons.append("Scenario rules or replay settings changed.")
        if scenario.calculation_engine_version != SCENARIO_ENGINE_VERSION:
            reasons.append("The commission calculation engine changed.")
        if (
            scenario.calculation_source_fingerprint
            != _version_fingerprint(scenario.source_version)
        ):
            reasons.append("The source pay plan changed.")
        latest_run = scenario.runs.first()
        if latest_run is not None:
            try:
                current_live_fingerprint = _live_version_fingerprint(
                    user, scenario,
                )
            except (ValidationError, PermissionDenied):
                reasons.append("The live pay-plan baseline is unavailable.")
            else:
                if (
                    latest_run.live_version_fingerprint
                    != current_live_fingerprint
                ):
                    reasons.append("The live pay-plan assignment changed.")
        try:
            current_input = _scenario_input_fingerprint(user, scenario)
        except (ValidationError, PermissionDenied):
            reasons.append("The scenario can no longer be compiled.")
        else:
            if current_input != scenario.calculation_input_fingerprint:
                reasons.append(
                    "Historical sales, hypothetical sales, or assumptions changed."
                )
        return reasons


class ScenarioService:
    """Lifecycle operations for a saved scenario."""

    @staticmethod
    def get(user, scenario, *, for_update=False):
        return _owned_scenario(user, scenario, for_update=for_update)

    @classmethod
    @transaction.atomic
    def save(cls, user, scenario, description=None, assumptions=None):
        scenario = _owned_scenario(user, scenario, for_update=True)
        _assert_editable(scenario)
        changed = False
        if description is not None and description != scenario.scenario_notes:
            scenario.scenario_notes = description
            changed = True
        if assumptions is not None:
            if not isinstance(assumptions, dict):
                raise ValidationError({
                    "assumptions": "Scenario assumptions must be a JSON object.",
                })
            if assumptions != scenario.assumptions:
                scenario.assumptions = deepcopy(assumptions)
                changed = True
        if changed:
            scenario.revision += 1

        report = SandboxCompiler.compile(scenario)
        scenario.validation_summary = ScenarioValidationService.summarize(report)
        scenario.saved_revision = scenario.revision
        scenario.last_saved_at = timezone.now()
        scenario.full_clean()
        scenario.save(update_fields=[
            "scenario_notes",
            "assumptions",
            "revision",
            "saved_revision",
            "last_saved_at",
            "validation_summary",
            "updated_at",
        ])
        ScenarioHistoryService.record(
            user,
            scenario,
            "scenario_saved",
            "Scenario changes saved.",
            {"revision": scenario.revision},
        )
        if not report.errors:
            ScenarioCalculationService.recalculate(user, scenario)
        return _owned_scenario(user, scenario)

    @classmethod
    def save_as(cls, user, scenario, name, description=""):
        source = _owned_scenario(user, scenario)
        description = (
            source.scenario_notes if description is None else description
        )
        return ScenarioCloneService.clone_scenario(
            user,
            source,
            name=name,
            description=description,
            action="scenario_saved_as",
        )

    @classmethod
    @transaction.atomic
    def rename(cls, user, scenario, name):
        scenario = _owned_scenario(user, scenario, for_update=True)
        _assert_editable(scenario)
        old_name = scenario.scenario_name
        scenario.scenario_name = _validate_available_name(
            user, name, exclude=scenario,
        )
        scenario.full_clean()
        scenario.save(update_fields=["scenario_name", "updated_at"])
        ScenarioHistoryService.record(
            user,
            scenario,
            "scenario_renamed",
            "Scenario renamed.",
            {"old_name": old_name, "new_name": scenario.scenario_name},
        )
        return scenario

    @classmethod
    @transaction.atomic
    def archive(cls, user, scenario, confirmed=False):
        if not confirmed:
            raise ValidationError("Confirm archiving before continuing.")
        scenario = _owned_scenario(user, scenario, for_update=True)
        if scenario.status == CommissionSandbox.ARCHIVED:
            return scenario
        if scenario.status == CommissionSandbox.CONVERTED:
            raise ValidationError(
                "Converted scenarios are retained as conversion audit records."
            )
        scenario.status = CommissionSandbox.ARCHIVED
        scenario.save(update_fields=["status", "updated_at"])
        ScenarioHistoryService.record(
            user, scenario, "scenario_archived", "Scenario archived."
        )
        return scenario

    @classmethod
    @transaction.atomic
    def restore(cls, user, scenario, confirmed=False):
        if not confirmed:
            raise ValidationError("Confirm restoration before continuing.")
        scenario = _owned_scenario(user, scenario, for_update=True)
        if scenario.status != CommissionSandbox.ARCHIVED:
            raise ValidationError("Only archived scenarios can be restored.")
        _validate_available_name(user, scenario.scenario_name, exclude=scenario)
        scenario.status = CommissionSandbox.DRAFT
        scenario.full_clean()
        scenario.save(update_fields=["status", "updated_at"])
        ScenarioHistoryService.record(
            user, scenario, "scenario_restored", "Scenario restored."
        )
        return scenario

    @classmethod
    @transaction.atomic
    def delete(cls, user, scenario, confirmed=False):
        if not confirmed:
            raise ValidationError("Confirm permanent deletion before continuing.")
        scenario = _owned_scenario(user, scenario, for_update=True)
        if scenario.status != CommissionSandbox.ARCHIVED:
            raise ValidationError(
                "Archive a scenario before permanently deleting it."
            )
        if PayPlanVersion.objects.filter(origin_scenario=scenario).exists():
            raise ValidationError(
                "This scenario is part of a pay-plan audit trail and cannot be deleted."
            )
        if scenario.duplicates.exists():
            raise ValidationError(
                "This scenario is the recorded source of another scenario and "
                "must be retained for audit history."
            )
        draft = scenario.draft_version
        scenario_id = scenario.pk
        scenario.delete()
        if draft.is_sandbox:
            draft.delete()
        return scenario_id

    @classmethod
    @transaction.atomic
    def reset(
        cls,
        user,
        scenario,
        retain_hypothetical_sales=True,
        retain_replay_settings=True,
    ):
        scenario = _owned_scenario(user, scenario, for_update=True)
        _assert_editable(scenario)
        old_draft = scenario.draft_version
        fresh_draft = ScenarioCloneService.clone_version(
            user,
            scenario.source_version,
            is_sandbox=True,
            previous_version=scenario.source_version,
        )
        scenario.draft_version = fresh_draft
        scenario.calculation_summary = {}
        scenario.validation_summary = {}
        scenario.calculation_input_fingerprint = ""
        scenario.calculation_source_fingerprint = ""
        scenario.calculation_engine_version = ""
        scenario.last_calculated_revision = 0
        scenario.last_calculated_at = None
        scenario.revision += 1
        scenario.saved_revision = scenario.revision
        scenario.last_saved_at = timezone.now()
        if not retain_replay_settings:
            scenario.replay_mode = CommissionSandbox.REPLAY
            scenario.replay_start_date = None
            scenario.replay_end_date = None
            scenario.assumptions = {}
            scenario.replay_filters = {}
        scenario.full_clean()
        scenario.save()
        if not retain_hypothetical_sales:
            scenario.hypothetical_deals.all().delete()
        old_draft.delete()
        ScenarioHistoryService.record(
            user,
            scenario,
            "scenario_reset",
            "Scenario rules reset to a fresh isolated copy of the source plan.",
            {
                "retained_hypothetical_sales": retain_hypothetical_sales,
                "retained_replay_settings": retain_replay_settings,
            },
        )
        ScenarioCalculationService.recalculate(user, scenario)
        return _owned_scenario(user, scenario)


class ScenarioComparisonService:
    """Compare independently calculated scenarios and semantic rule snapshots."""

    @staticmethod
    def _rule_snapshot(rule):
        conditions = sorted(
            (
                item.field_name,
                item.operator,
                _json_safe(item.value),
            )
            for item in rule.conditions.all()
        )
        return {
            "semantic_key": str(rule.semantic_key),
            "name": rule.name,
            "description": rule.description,
            "rule_type": rule.rule_type,
            "scope": rule.calculation_scope,
            "condition_mode": rule.condition_group_operator,
            "conditions": conditions,
            "configuration": _json_safe(deepcopy(rule.configuration)),
            "active": rule.is_active,
            "priority": rule.sort_order,
        }

    @staticmethod
    def _structural_signature(snapshot):
        configuration = snapshot["configuration"]
        identity_configuration = {
            key: configuration.get(key)
            for key in (
                "gross_field",
                "unit_metric",
                "tier_model",
                "business_identifier",
            )
            if key in configuration
        }
        return _hash_payload({
            "rule_type": snapshot["rule_type"],
            "scope": snapshot["scope"],
            "conditions": snapshot["conditions"],
            "identity_configuration": identity_configuration,
        })

    @staticmethod
    def _changed_fields(before, after):
        fields = []
        for key in (
            "name",
            "description",
            "rule_type",
            "scope",
            "condition_mode",
            "conditions",
            "active",
            "priority",
        ):
            if before[key] != after[key]:
                fields.append(key)
        if before["configuration"] != after["configuration"]:
            config_keys = sorted(
                set(before["configuration"]) | set(after["configuration"])
            )
            fields.extend(
                f"configuration.{key}"
                for key in config_keys
                if before["configuration"].get(key)
                != after["configuration"].get(key)
            )
        return fields

    @classmethod
    def compare_rules(cls, baseline_version, candidate_version):
        baseline = [
            cls._rule_snapshot(rule)
            for rule in baseline_version.rules.prefetch_related(
                "conditions",
            ).order_by("sort_order", "id")
        ]
        candidate = [
            cls._rule_snapshot(rule)
            for rule in candidate_version.rules.prefetch_related(
                "conditions",
            ).order_by("sort_order", "id")
        ]
        unused_baseline = {item["semantic_key"]: item for item in baseline}
        pairs = []
        unmatched_candidate = []
        for item in candidate:
            match = unused_baseline.pop(item["semantic_key"], None)
            if match is None:
                unmatched_candidate.append(item)
            else:
                pairs.append((match, item))

        # Independently-created but equivalent rules may have different UUIDs.
        # Use a deterministic structural identity only after lineage matching.
        by_structure = {}
        for item in unused_baseline.values():
            by_structure.setdefault(cls._structural_signature(item), []).append(item)
        added = []
        for item in unmatched_candidate:
            bucket = by_structure.get(cls._structural_signature(item), [])
            if bucket:
                match = bucket.pop(0)
                unused_baseline.pop(match["semantic_key"], None)
                pairs.append((match, item))
            else:
                added.append(item)

        modified = []
        unchanged = []
        for before, after in pairs:
            changed_fields = cls._changed_fields(before, after)
            payload = {
                "semantic_key": after["semantic_key"],
                "before": before,
                "after": after,
                "changed_fields": changed_fields,
            }
            (modified if changed_fields else unchanged).append(payload)
        return {
            "added": added,
            "removed": list(unused_baseline.values()),
            "modified": modified,
            "unchanged": unchanged,
        }

    @staticmethod
    def _result_key(result):
        if result.deal_kind == SandboxResult.HYPOTHETICAL:
            snapshot = result.sale_snapshot or {}
            # Scenario duplication necessarily creates new hypothetical-deal
            # primary keys.  Deal number is unique within a scenario; date is
            # included to make the comparison identity explicit and readable.
            return "hypothetical:{number}:{date}".format(
                number=snapshot.get("deal_number", ""),
                date=(
                    snapshot.get("sale_date")
                    or snapshot.get("date", "")
                ),
            )
        if result.source_key:
            return result.source_key
        if result.production_sale_id:
            return f"production:{result.production_sale_id}"
        if result.hypothetical_deal_id:
            return f"hypothetical:{result.hypothetical_deal_id}"
        return f"snapshot:{result.pk}"

    @staticmethod
    def _responsible_rule(result):
        actual = {
            str(item.get("rule_name") or item.get("rule_id")): _money(
                item.get("amount")
            )
            for item in (result.actual_explanation or {}).get("rules", ())
            if item.get("applied")
        }
        sandbox = {
            str(item.get("rule_name") or item.get("rule_id")): _money(
                item.get("amount")
            )
            for item in (result.explanation or {}).get("rules", ())
            if item.get("applied")
        }
        candidates = set(actual) | set(sandbox)
        if not candidates:
            return ""
        return max(
            candidates,
            key=lambda key: abs(sandbox.get(key, 0) - actual.get(key, 0)),
        )

    @classmethod
    def compare(cls, user, scenarios, start=None, end=None):
        if not scenarios:
            raise ValidationError("Select at least one scenario to compare.")
        if len(scenarios) > 3:
            raise ValidationError("Compare no more than three scenarios at once.")
        owned = [_owned_scenario(user, item) for item in scenarios]
        if len({item.pk for item in owned}) != len(owned):
            raise ValidationError("Select each scenario only once.")

        entries = []
        deal_rows = {}
        for scenario in owned:
            if scenario.status == CommissionSandbox.DRAFT:
                report = SandboxCompiler.compile(scenario)
                if report.errors:
                    raise ValidationError([
                        f"{item.code}: {item.message}"
                        for item in report.errors
                    ])
                run = ScenarioRunner.run(
                    user,
                    scenario,
                    mode=scenario.replay_mode,
                    period_start=start,
                    period_end=end,
                )
                summary = ScenarioCalculationService._summary(run, report)
            else:
                if (
                    start != scenario.replay_start_date
                    or end != scenario.replay_end_date
                ):
                    raise ValidationError(
                        f"{scenario.scenario_name} is read-only and can only "
                        "be compared with its saved replay range. Duplicate or "
                        "restore it to calculate a different range."
                    )
                run = scenario.runs.filter(
                    mode=scenario.replay_mode,
                    period_start=scenario.replay_start_date,
                    period_end=scenario.replay_end_date,
                ).prefetch_related("results").first()
                if run is None:
                    raise ValidationError(
                        f"{scenario.scenario_name} has no saved calculation."
                    )
                summary = deepcopy(scenario.calculation_summary)
            results = list(run.results.all())
            entries.append({
                "scenario": scenario,
                "run": run,
                "summary": summary,
                "difference_from_live": _money_string(run.difference),
                "percentage_change": _percentage_string(run.percent_change),
                "rules": cls.compare_rules(
                    scenario.source_version,
                    scenario.draft_version,
                ),
            })
            for result in results:
                key = cls._result_key(result)
                row = deal_rows.setdefault(key, {
                    "source_key": key,
                    "sale": deepcopy(result.sale_snapshot),
                    "live_amount": _money_string(result.actual_commission),
                    "scenarios": {},
                })
                row["scenarios"][str(scenario.public_id)] = {
                    "amount": _money_string(result.sandbox_commission),
                    "difference": _money_string(result.difference),
                    "percentage_change": _percentage_string(
                        result.percent_change
                    ),
                    "comparison": result.comparison,
                    "primary_rule": cls._responsible_rule(result),
                }
        return {
            "live": {
                "total": (
                    _money_string(entries[0]["run"].actual_total)
                    if entries else "0.00"
                ),
                "label": "Live plan",
            },
            "scenarios": entries,
            "deals": list(deal_rows.values()),
        }


class ScenarioConversionService:
    """Materialize a review-only production draft; activation is separate."""

    @classmethod
    @transaction.atomic
    def convert(cls, user, scenario, effective_start_date):
        scenario = _owned_scenario(user, scenario, for_update=True)
        existing = PayPlanVersion.objects.select_related("pay_plan").filter(
            origin_scenario=scenario,
            pay_plan__owner_user=user,
        ).first()
        if existing is not None:
            return existing
        _assert_editable(scenario)
        if not effective_start_date:
            raise ValidationError({
                "effective_start_date": "Choose an effective start date.",
            })

        ScenarioCalculationService.recalculate(user, scenario)
        scenario = _owned_scenario(user, scenario, for_update=True)
        report = SandboxCompiler.compile(scenario)
        if report.errors:
            raise ValidationError([
                f"{item.code}: {item.message}" for item in report.errors
            ])
        if not report.executable_rules:
            raise ValidationError(
                "A scenario with no executable rules cannot become a pay-plan draft."
            )

        plan = scenario.source_version.pay_plan
        # Lock the plan's versions while allocating the next immutable number.
        list(
            PayPlanVersion.objects.select_for_update().filter(
                pay_plan=plan,
                is_sandbox=False,
            ).values_list("pk", flat=True)
        )
        number = (
            plan.versions.filter(is_sandbox=False).aggregate(
                value=Max("version_number")
            )["value"]
            or 0
        ) + 1
        version = ScenarioCloneService.clone_version(
            user,
            scenario.draft_version,
            is_sandbox=False,
            status=PayPlanVersion.REVIEW_REQUIRED,
            previous_version=scenario.source_version,
            origin_scenario=scenario,
            version_name=f"Version {number}",
            version_number=number,
            effective_start_date=effective_start_date,
        )
        version.processing_status = "needs_review"
        version.processing_errors = []
        version.processing_warnings = [
            item.message for item in report.warnings
        ]
        version.full_clean()
        version.save(update_fields=[
            "processing_status",
            "processing_errors",
            "processing_warnings",
            "updated_at",
        ])

        canonical = VersionAdapter.to_canonical(version)
        production_report = PayPlanCompiler.compile(canonical)
        if production_report.errors or not production_report.executable_rules:
            raise ValidationError(
                "The converted pay-plan draft failed deterministic compilation."
            )
        CanonicalPlanStorageService.store_compilation(
            version,
            canonical,
            production_report,
        )
        scenario.status = CommissionSandbox.CONVERTED
        scenario.saved_revision = scenario.revision
        scenario.last_saved_at = timezone.now()
        scenario.save(update_fields=[
            "status",
            "saved_revision",
            "last_saved_at",
            "updated_at",
        ])
        ScenarioHistoryService.record(
            user,
            scenario,
            "scenario_converted",
            "Scenario converted to a pay-plan draft for review.",
            {
                "pay_plan_version_id": version.pk,
                "version_number": version.version_number,
                "effective_start_date": effective_start_date,
            },
        )
        return version
