from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .commission_engine.constants import RULE_SCOPE_PERIOD, RULE_SCOPE_PER_SALE
from .commission_engine.engine import (
    build_eligibility_context,
    build_period_context,
    calculate_sale_commission_for_version,
    evaluate_rules,
    resolve_pay_plan_version,
)
from .commission_engine.results import PeriodCalculationResult
from .commission_engine.vehicle_conditions import normalize_vehicle_condition
from .commission_service import CommissionEngineService
from .models import (
    CommissionSandbox, PayPlanAssignment, PayPlanRule, PayPlanRuleCondition,
    PayPlanVersion, Sale, SandboxHypotheticalDeal, SandboxResult, SandboxRun,
    ScenarioHistory,
)
from .pay_plan_domain.adapters import VersionAdapter
from .pay_plan_domain.compiler import PayPlanCompiler
from .pay_plan_domain.services import (
    CanonicalPlanStorageService, ExplanationBuilder,
)
from .pay_plan_management import PayPlanActivationService
from .pay_plan_scope import OwnedPayPlanRuleService


SANDBOX_ENGINE_VERSION = 'commission-engine-v2.1'
SANDBOX_SCHEMA_VERSION = '1.0'


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _deal_snapshot(item):
    return {
        'source_type': (
            SandboxResult.PRODUCTION
            if isinstance(item, Sale)
            else SandboxResult.HYPOTHETICAL
        ),
        'source_id': item.pk,
        'deal_number': item.dealNumber,
        'customer': item.customer,
        'sale_date': item.date.isoformat(),
        'front_end_gross': str(item.frontEnd),
        'back_end_gross': str(item.backend),
        'unit_count': str(item.count),
        'vehicle_condition': item.vehicle_condition,
        'acquisition_source': item.acquisition_source,
        'sale_type': item.sale_type,
        'custom_pay_plan_fields': _json_safe(
            item.custom_pay_plan_fields or {},
        ),
    }


def _version_fingerprint(version):
    return VersionAdapter.to_canonical(version).fingerprint


def _condition_positions(sales):
    positions = {}
    by_condition = defaultdict(list)
    for sale in sales:
        condition = normalize_vehicle_condition(
            getattr(sale, 'vehicle_condition', None),
        )
        if condition:
            by_condition[condition].append(sale)
    for condition_sales in by_condition.values():
        condition_sales.sort(key=lambda item: (
            item.date,
            getattr(item, 'created_at', None) or timezone.make_aware(
                timezone.datetime.min
            ),
            item.pk or 0,
        ))
        running = Decimal('0')
        for sale in condition_sales:
            credit = Decimal(str(getattr(sale, 'count', 0) or 0))
            positions[id(sale)] = (running, running + credit)
            running += credit
    return positions


class SandboxManager:
    @staticmethod
    def get_for_user(user, public_id, *, for_update=False):
        queryset = CommissionSandbox.objects.select_related(
            'source_version__pay_plan', 'draft_version__pay_plan',
        )
        if for_update:
            queryset = queryset.select_for_update()
        try:
            return queryset.get(public_id=public_id, owner=user)
        except CommissionSandbox.DoesNotExist as exc:
            raise PermissionDenied('Sandbox not found.') from exc

    @classmethod
    @transaction.atomic
    def create(cls, owner, source_version, scenario_name, scenario_notes=''):
        if source_version.pay_plan.owner_user_id != owner.id:
            raise PermissionDenied('The source pay plan does not belong to this user.')
        if source_version.is_sandbox:
            raise ValidationError(
                'Start a sandbox from a production pay-plan version.',
            )
        source_rules = list(
            source_version.rules.prefetch_related('conditions').order_by(
                'sort_order', 'id',
            )
        )
        token = hashlib.sha256(
            f'{owner.pk}:{source_version.pk}:{timezone.now().isoformat()}'.encode()
        ).hexdigest()[:12]
        draft = PayPlanVersion.objects.create(
            pay_plan=source_version.pay_plan,
            version_name=f'Sandbox {token}',
            effective_start_date=source_version.effective_start_date,
            effective_end_date=source_version.effective_end_date,
            status=PayPlanVersion.DRAFT,
            source_type=PayPlanVersion.SOURCE_MANUAL,
            previous_version=source_version,
            created_by=owner,
            processing_status='sandbox',
            is_sandbox=True,
            default_backend_percentage=source_version.default_backend_percentage,
            default_backend_minimum=source_version.default_backend_minimum,
            default_backend_maximum=source_version.default_backend_maximum,
        )
        for source_rule in source_rules:
            clone = PayPlanRule.objects.create(
                pay_plan_version=draft,
                semantic_key=source_rule.semantic_key,
                name=source_rule.name,
                description=source_rule.description,
                rule_type=source_rule.rule_type,
                calculation_scope=source_rule.calculation_scope,
                condition_group_operator=source_rule.condition_group_operator,
                configuration=source_rule.configuration,
                is_active=source_rule.is_active,
                sort_order=source_rule.sort_order,
            )
            PayPlanRuleCondition.objects.bulk_create([
                PayPlanRuleCondition(
                    rule=clone, field_name=item.field_name,
                    operator=item.operator, value=item.value,
                    sort_order=item.sort_order,
                )
                for item in source_rule.conditions.all()
            ])
        sandbox = CommissionSandbox(
            owner=owner, source_version=source_version, draft_version=draft,
            scenario_name=scenario_name, scenario_notes=scenario_notes,
        )
        sandbox.full_clean()
        sandbox.save()
        SandboxCompiler.compile(sandbox)
        now = timezone.now()
        sandbox.last_saved_at = now
        sandbox.saved_revision = sandbox.revision
        sandbox.save(update_fields=[
            'last_saved_at', 'saved_revision', 'updated_at',
        ])
        ScenarioHistory.objects.create(
            scenario=sandbox,
            actor=owner,
            action='scenario_created',
            summary='Scenario created from a pay-plan version.',
            metadata={'source_version_id': source_version.id},
        )
        return sandbox

    @staticmethod
    @transaction.atomic
    def archive(user, sandbox):
        sandbox = SandboxManager.get_for_user(
            user, sandbox.public_id, for_update=True,
        )
        sandbox.status = CommissionSandbox.ARCHIVED
        sandbox.save(update_fields=['status', 'updated_at'])
        return sandbox

    @staticmethod
    @transaction.atomic
    def delete(user, sandbox):
        sandbox = SandboxManager.get_for_user(
            user, sandbox.public_id, for_update=True,
        )
        if sandbox.status != CommissionSandbox.DRAFT:
            raise ValidationError('Only a draft sandbox can be deleted.')
        draft = sandbox.draft_version
        sandbox.delete()
        draft.delete()


class SandboxCompiler:
    @staticmethod
    def compile(sandbox):
        if sandbox.draft_version.pay_plan.owner_user_id != sandbox.owner_id:
            raise PermissionDenied('Sandbox draft ownership is invalid.')
        canonical = VersionAdapter.to_canonical(sandbox.draft_version)
        report = PayPlanCompiler.compile(canonical)
        CanonicalPlanStorageService.store_compilation(
            sandbox.draft_version, canonical, report,
        )
        stored_report = dict(sandbox.draft_version.compilation_report)
        stored_report['dependencies'] = SandboxDependencyTracker.build(canonical)
        sandbox.draft_version.compilation_report = stored_report
        sandbox.draft_version.save(update_fields=[
            'compilation_report', 'updated_at',
        ])
        return report

    @staticmethod
    def invalidate(sandbox):
        sandbox.revision += 1
        sandbox.save(update_fields=['revision', 'updated_at'])


class SandboxDependencyTracker:
    """Describe inputs that can invalidate cached scenario calculations."""

    ALWAYS_REQUIRED = {
        'date', 'count', 'vehicle_condition', 'frontEnd', 'backend',
        'acquisition_source', 'custom_pay_plan_fields', 'sale_type',
        'split_with_name', 'dealNumber',
    }

    @classmethod
    def build(cls, canonical):
        dependency_map = {}
        for rule in canonical.rules:
            fields = {condition.field for condition in rule.conditions}
            configuration = rule.action.parameters
            gross_field = configuration.get('gross_field')
            if gross_field == 'front_end_gross':
                fields.add('frontEnd')
            elif gross_field == 'back_end_gross':
                fields.add('backend')
            metric = configuration.get('unit_metric', '')
            if 'unit' in metric:
                fields.update({'count', 'vehicle_condition', 'date'})
            if rule.scope == RULE_SCOPE_PERIOD:
                fields.update({'count', 'date'})
            dependency_map[rule.key] = {
                'scope': rule.scope,
                'fields': sorted(fields),
            }
        return dependency_map

    @classmethod
    def relevant_fields(cls, version):
        fields = set(cls.ALWAYS_REQUIRED)
        dependencies = (version.compilation_report or {}).get(
            'dependencies', {},
        )
        for dependency in dependencies.values():
            fields.update(dependency.get('fields') or ())
        return fields


class SandboxRuleEditor:
    @staticmethod
    def _locked_draft(sandbox):
        locked = CommissionSandbox.objects.select_for_update().get(
            pk=sandbox.pk,
        )
        if locked.status != CommissionSandbox.DRAFT:
            raise ValidationError('Only a draft sandbox can be edited.')
        return locked

    @staticmethod
    def _rule(sandbox, rule_id):
        try:
            return PayPlanRule.objects.prefetch_related('conditions').get(
                pk=rule_id, pay_plan_version=sandbox.draft_version,
                pay_plan_version__is_sandbox=True,
            )
        except PayPlanRule.DoesNotExist as exc:
            raise PermissionDenied('Sandbox rule not found.') from exc

    @classmethod
    @transaction.atomic
    def save(cls, sandbox, *, rule=None, data):
        locked = cls._locked_draft(sandbox)
        created = rule is None
        if rule is None:
            rule = PayPlanRule(pay_plan_version=locked.draft_version)
        elif rule.pay_plan_version_id != locked.draft_version_id:
            raise PermissionDenied('Rule does not belong to this sandbox.')
        rule.name = data['name']
        rule.rule_type = data['rule_type']
        rule.calculation_scope = data['calculation_scope']
        rule.condition_group_operator = data.get('condition_group_operator', 'all')
        configuration = dict(data.get('configuration') or {})
        submitted_conditions = list(data.get('conditions') or [])
        disabled_conditions = [
            {
                key: value for key, value in item.items()
                if key != 'enabled'
            }
            for item in submitted_conditions
            if item.get('enabled', True) is False
        ]
        if disabled_conditions:
            configuration['_sandbox_disabled_conditions'] = disabled_conditions
        else:
            configuration.pop('_sandbox_disabled_conditions', None)
        rule.configuration = configuration
        rule.is_active = bool(data.get('is_active', True))
        rule.sort_order = int(data.get(
            'sort_order', locked.draft_version.rules.count() + 1,
        ))
        rule.full_clean()
        rule.save()
        rule.conditions.all().delete()
        for index, condition_data in enumerate(
            (
                item for item in submitted_conditions
                if item.get('enabled', True) is not False
            ),
            1,
        ):
            operator = condition_data['operator']
            stored_value = (
                operator == 'is_true'
                if operator in {'is_true', 'is_false'}
                else condition_data.get('value')
            )
            condition = PayPlanRuleCondition(
                rule=rule,
                field_name=condition_data['field_name'],
                operator=operator,
                value=stored_value,
                sort_order=index,
            )
            condition.full_clean()
            condition.save()
        SandboxCompiler.invalidate(locked)
        ScenarioHistory.objects.create(
            scenario=locked,
            actor=locked.owner,
            action='rule_added' if created else 'rule_updated',
            summary=(
                f'Added sandbox rule "{rule.name}".'
                if created else f'Updated sandbox rule "{rule.name}".'
            ),
            metadata={
                'semantic_key': str(rule.semantic_key),
                'rule_type': rule.rule_type,
            },
        )
        return rule

    @classmethod
    @transaction.atomic
    def duplicate(cls, sandbox, rule_id):
        source = cls._rule(sandbox, rule_id)
        return cls.save(sandbox, data={
            'name': f'{source.name} Copy',
            'rule_type': source.rule_type,
            'calculation_scope': source.calculation_scope,
            'condition_group_operator': source.condition_group_operator,
            'configuration': source.configuration,
            'is_active': source.is_active,
            'sort_order': source.sort_order + 1,
            'conditions': [
                {
                    'field_name': item.field_name,
                    'operator': item.operator,
                    'value': item.value,
                }
                for item in source.conditions.all()
            ] + list(
                {
                    **item,
                    'enabled': False,
                }
                for item in (source.configuration or {}).get(
                    '_sandbox_disabled_conditions', [],
                )
            ),
        })

    @classmethod
    @transaction.atomic
    def toggle(cls, sandbox, rule_id):
        locked = cls._locked_draft(sandbox)
        rule = cls._rule(locked, rule_id)
        rule.is_active = not rule.is_active
        rule.save(update_fields=['is_active', 'updated_at'])
        SandboxCompiler.invalidate(locked)
        ScenarioHistory.objects.create(
            scenario=locked,
            actor=locked.owner,
            action='rule_enabled' if rule.is_active else 'rule_disabled',
            summary=(
                f'{"Enabled" if rule.is_active else "Disabled"} '
                f'sandbox rule "{rule.name}".'
            ),
            metadata={'semantic_key': str(rule.semantic_key)},
        )
        return rule

    @classmethod
    @transaction.atomic
    def delete(cls, sandbox, rule_id):
        locked = cls._locked_draft(sandbox)
        rule = cls._rule(locked, rule_id)
        rule_name = rule.name
        semantic_key = str(rule.semantic_key)
        rule.delete()
        SandboxCompiler.invalidate(locked)
        ScenarioHistory.objects.create(
            scenario=locked,
            actor=locked.owner,
            action='rule_deleted',
            summary=f'Deleted sandbox rule "{rule_name}".',
            metadata={'semantic_key': semantic_key},
        )

    @classmethod
    @transaction.atomic
    def move(cls, sandbox, rule_id, direction):
        locked = cls._locked_draft(sandbox)
        rule = cls._rule(locked, rule_id)
        delta = -1 if direction == 'up' else 1
        rule.sort_order = max(0, rule.sort_order + delta)
        rule.save(update_fields=['sort_order', 'updated_at'])
        SandboxCompiler.invalidate(locked)
        ScenarioHistory.objects.create(
            scenario=locked,
            actor=locked.owner,
            action='rule_priority_changed',
            summary=f'Changed priority for sandbox rule "{rule.name}".',
            metadata={
                'semantic_key': str(rule.semantic_key),
                'sort_order': rule.sort_order,
            },
        )
        return rule


class SandboxCalculator:
    @staticmethod
    def _rules(user, version, scope):
        return list(OwnedPayPlanRuleService.active_rules_for_user(
            user, version, scope=scope,
        ))

    @classmethod
    def calculate_period(cls, user, version, sales):
        sales = list(sales)
        OwnedPayPlanRuleService.validate_version_owner(user, version)
        metrics = {
            **build_period_context(sales),
            '_sales': sales,
            '_condition_positions': _condition_positions(sales),
        }
        result = PeriodCalculationResult(
            user=user, pay_plan=version.pay_plan, pay_plan_version=version,
            period_start=min((item.date for item in sales), default=None),
            period_end=max((item.date for item in sales), default=None),
        )
        per_sale_rules = cls._rules(user, version, RULE_SCOPE_PER_SALE)
        for sale in sales:
            item = calculate_sale_commission_for_version(
                user, sale, version, metrics, rules=per_sale_rules,
            )
            result.sale_results.append(item)
            result.total += item.total
            result.base_commission += item.base_commission
            result.bonuses += item.bonuses
            result.spiffs += item.spiffs
            result.adjustments += item.adjustments
            result.deductions += item.deductions
        period_context = {
            **metrics,
            **build_eligibility_context(user, result.period_start),
            'period_start': result.period_start,
            'period_end': result.period_end,
        }
        evaluate_rules(
            result,
            cls._rules(user, version, RULE_SCOPE_PERIOD),
            period_context,
        )
        return result


class ComparisonEngine:
    @staticmethod
    def compare(actual, sandbox):
        actual = Decimal(str(actual))
        sandbox = Decimal(str(sandbox))
        difference = sandbox - actual
        percent = (
            (difference / abs(actual) * Decimal('100')).quantize(Decimal('0.0001'))
            if actual != 0 else None
        )
        comparison = (
            SandboxResult.HIGHER if difference > 0
            else SandboxResult.LOWER if difference < 0
            else SandboxResult.UNCHANGED
        )
        return {
            'actual': actual, 'sandbox': sandbox, 'difference': difference,
            'percent_change': percent, 'comparison': comparison,
        }

    @staticmethod
    def compare_sessions(sandboxes, runs):
        by_id = {run.sandbox_id: run for run in runs}
        return [{
            'sandbox': sandbox,
            'run': by_id.get(sandbox.id),
        } for sandbox in sandboxes]


class ScenarioRunner:
    @staticmethod
    def _group_monthly(items):
        groups = defaultdict(list)
        for item in items:
            groups[(item.date.year, item.date.month)].append(item)
        return [groups[key] for key in sorted(groups)]

    @staticmethod
    def _fingerprint(sandbox, items, mode, period_start, period_end):
        dependency_fields = SandboxDependencyTracker.relevant_fields(
            sandbox.draft_version,
        )
        payload = {
            'revision': sandbox.revision,
            'canonical': sandbox.draft_version.canonical_fingerprint,
            'source': _version_fingerprint(sandbox.source_version),
            'engine_version': SANDBOX_ENGINE_VERSION,
            'schema_version': SANDBOX_SCHEMA_VERSION,
            'mode': mode,
            'start': str(period_start or ''),
            'end': str(period_end or ''),
            'assumptions': _json_safe(sandbox.assumptions or {}),
            'replay_filters': _json_safe(sandbox.replay_filters or {}),
            'items': [{
                'kind': type(item).__name__,
                'id': item.pk,
                'values': {
                    field: _json_safe(getattr(item, field, None))
                    for field in sorted(dependency_fields)
                },
            } for item in items],
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(',', ':'),
        ).encode()).hexdigest()

    @staticmethod
    def collect_inputs(user, sandbox, mode, period_start=None, period_end=None):
        sandbox = SandboxManager.get_for_user(user, sandbox.public_id)
        production_sales = []
        if mode in {SandboxRun.REPLAY, SandboxRun.MIXED}:
            queryset = Sale.objects.filter(user=user)
            if period_start:
                queryset = queryset.filter(date__gte=period_start)
            if period_end:
                queryset = queryset.filter(date__lte=period_end)
            production_sales = list(queryset.order_by('date', 'pk'))
        hypothetical = (
            list(sandbox.hypothetical_deals.all())
            if mode in {SandboxRun.PROJECTION, SandboxRun.MIXED} else []
        )
        return production_sales, hypothetical

    @classmethod
    def current_input_fingerprint(
        cls, user, sandbox, mode, period_start=None, period_end=None,
    ):
        production_sales, hypothetical = cls.collect_inputs(
            user, sandbox, mode, period_start, period_end,
        )
        return cls._fingerprint(
            sandbox,
            production_sales + hypothetical,
            mode,
            period_start,
            period_end,
        )

    @classmethod
    @transaction.atomic
    def run(
        cls, user, sandbox, *, mode, period_start=None, period_end=None,
        force=False,
    ):
        sandbox = SandboxManager.get_for_user(user, sandbox.public_id)
        report = SandboxCompiler.compile(sandbox)
        if report.errors:
            raise ValidationError([
                f'{item.code}: {item.message}' for item in report.errors
            ])
        production_sales, hypothetical = cls.collect_inputs(
            user, sandbox, mode, period_start, period_end,
        )
        all_items = production_sales + hypothetical
        fingerprint = cls._fingerprint(
            sandbox, all_items, mode, period_start, period_end,
        )
        if not force:
            cached = sandbox.runs.filter(
                sandbox_revision=sandbox.revision,
                input_fingerprint=fingerprint,
                engine_version=SANDBOX_ENGINE_VERSION,
                schema_version=SANDBOX_SCHEMA_VERSION,
            ).prefetch_related('results').first()
            if cached:
                return cached

        sandbox_by_object = {}
        sandbox_period_total = Decimal('0')
        sandbox_period_bonus = Decimal('0')
        for group in cls._group_monthly(all_items):
            period = SandboxCalculator.calculate_period(
                user, sandbox.draft_version, group,
            )
            sandbox_period_total += period.total
            sandbox_period_bonus += sum(
                (
                    item.amount for item in period.line_items
                    if item.scope == RULE_SCOPE_PERIOD and item.applied
                ),
                Decimal('0'),
            )
            for item in period.sale_results:
                sandbox_by_object[id(item.sale)] = item

        actual_by_object = {}
        actual_period_total = Decimal('0')
        actual_period_bonus = Decimal('0')
        for month_group in cls._group_monthly(production_sales):
            by_version = defaultdict(list)
            for sale in month_group:
                version = resolve_pay_plan_version(user, sale.date)
                by_version[version.pk].append((version, sale))
            for version_sales in by_version.values():
                version = version_sales[0][0]
                sales = [item[1] for item in version_sales]
                period = SandboxCalculator.calculate_period(user, version, sales)
                actual_period_total += period.total
                actual_period_bonus += sum(
                    (
                        item.amount for item in period.line_items
                        if item.scope == RULE_SCOPE_PERIOD and item.applied
                    ),
                    Decimal('0'),
                )
                for item in period.sale_results:
                    actual_by_object[id(item.sale)] = item

        aggregate = ComparisonEngine.compare(
            actual_period_total, sandbox_period_total,
        )
        run = SandboxRun(
            sandbox=sandbox, mode=mode, period_start=period_start,
            period_end=period_end, sandbox_revision=sandbox.revision,
            input_fingerprint=fingerprint,
            engine_version=SANDBOX_ENGINE_VERSION,
            schema_version=SANDBOX_SCHEMA_VERSION,
            source_fingerprint=_version_fingerprint(sandbox.source_version),
            live_version_fingerprint=hashlib.sha256(
                ':'.join(
                    sorted({
                        _version_fingerprint(item.pay_plan_version)
                        for item in actual_by_object.values()
                        if getattr(item.pay_plan_version, 'pk', None)
                    })
                ).encode()
            ).hexdigest() if actual_by_object else '',
            actual_total=aggregate['actual'],
            sandbox_total=aggregate['sandbox'],
            difference=aggregate['difference'],
            percent_change=aggregate['percent_change'],
            statistics={
                'sales_tested': len(all_items),
                'production_sales': len(production_sales),
                'hypothetical_deals': len(hypothetical),
                'actual_period_bonus': str(actual_period_bonus),
                'sandbox_period_bonus': str(sandbox_period_bonus),
                'average_difference': str(
                    aggregate['difference'] / len(all_items)
                    if all_items else Decimal('0')
                ),
                'compiled_rule_count': report.compiled_rule_count,
            },
            validation_report={
                'errors': [asdict(item) for item in report.errors],
                'warnings': [asdict(item) for item in report.warnings],
                'statistics': report.statistics,
            },
        )
        run.full_clean()
        run.save()
        changes = []
        for item in all_items:
            sandbox_result = sandbox_by_object[id(item)]
            actual_result = actual_by_object.get(id(item))
            comparison = ComparisonEngine.compare(
                actual_result.total if actual_result else Decimal('0'),
                sandbox_result.total,
            )
            result = SandboxResult(
                run=run,
                deal_kind=(
                    SandboxResult.PRODUCTION
                    if isinstance(item, Sale)
                    else SandboxResult.HYPOTHETICAL
                ),
                source_key=(
                    f'production:{item.pk}'
                    if isinstance(item, Sale)
                    else f'hypothetical:{item.pk}'
                ),
                sale_snapshot=_deal_snapshot(item),
                production_sale=item if isinstance(item, Sale) else None,
                hypothetical_deal=(
                    item if isinstance(item, SandboxHypotheticalDeal) else None
                ),
                actual_commission=comparison['actual'],
                sandbox_commission=comparison['sandbox'],
                difference=comparison['difference'],
                percent_change=comparison['percent_change'],
                comparison=comparison['comparison'],
                explanation=_json_safe(
                    ExplanationBuilder.from_calculation(sandbox_result)
                ),
                actual_explanation=(
                    _json_safe(ExplanationBuilder.from_calculation(actual_result))
                    if actual_result else {}
                ),
            )
            result.full_clean()
            changes.append(result)
        SandboxResult.objects.bulk_create(changes)
        differences = [item.difference for item in changes]
        run.statistics.update({
            'largest_increase': str(max(differences, default=Decimal('0'))),
            'largest_decrease': str(min(differences, default=Decimal('0'))),
        })
        run.save(update_fields=['statistics'])
        return run


class SandboxActivationService:
    @staticmethod
    @transaction.atomic
    def activate(user, sandbox, *, effective_start_date, confirmed=False):
        if not confirmed:
            raise ValidationError('Confirm activation before continuing.')
        sandbox = SandboxManager.get_for_user(
            user, sandbox.public_id, for_update=True,
        )
        if sandbox.status != CommissionSandbox.DRAFT:
            raise ValidationError('Only a draft sandbox can be activated.')
        compilation = SandboxCompiler.compile(sandbox)
        if compilation.errors or not compilation.executable_rules:
            raise ValidationError('Sandbox validation failed; activation was not performed.')
        # Final replay is required before materializing the production draft.
        ScenarioRunner.run(
            user, sandbox, mode=SandboxRun.REPLAY,
            period_start=effective_start_date,
        )
        plan = sandbox.source_version.pay_plan
        number = (
            plan.versions.filter(is_sandbox=False).aggregate(
                value=Max('version_number')
            )['value'] or 0
        ) + 1
        production = PayPlanVersion.objects.create(
            pay_plan=plan,
            version_name=f'Version {number}',
            version_number=number,
            effective_start_date=effective_start_date,
            status=PayPlanVersion.REVIEW_REQUIRED,
            source_type=PayPlanVersion.SOURCE_MANUAL,
            previous_version=sandbox.source_version,
            created_by=user,
            processing_status='needs_review',
            processing_warnings=[
                item.message for item in compilation.warnings
            ],
            canonical_schema_version=sandbox.draft_version.canonical_schema_version,
            canonical_payload=sandbox.draft_version.canonical_payload,
            canonical_fingerprint=sandbox.draft_version.canonical_fingerprint,
            compilation_report=sandbox.draft_version.compilation_report,
            default_backend_percentage=sandbox.draft_version.default_backend_percentage,
            default_backend_minimum=sandbox.draft_version.default_backend_minimum,
            default_backend_maximum=sandbox.draft_version.default_backend_maximum,
        )
        for index, candidate in enumerate(compilation.executable_rules, 1):
            configuration = dict(candidate['configuration'])
            configuration.pop('_sandbox_disabled_conditions', None)
            rule = PayPlanRule.objects.create(
                pay_plan_version=production,
                semantic_key=candidate['key'],
                name=candidate['name'],
                rule_type=candidate['rule_type'],
                calculation_scope=candidate['calculation_scope'],
                condition_group_operator=candidate['condition_group_operator'],
                configuration=configuration,
                is_active=True,
                sort_order=index,
            )
            PayPlanRuleCondition.objects.bulk_create([
                PayPlanRuleCondition(
                    rule=rule, field_name=item['field_name'],
                    operator=item['operator'],
                    value=(
                        item.get('value')
                        if item['operator'] not in {'is_true', 'is_false'}
                        else item['operator'] == 'is_true'
                    ),
                    sort_order=order,
                )
                for order, item in enumerate(candidate['conditions'], 1)
            ])
        report = PayPlanActivationService.activate(
            user, production, warnings_approved=True,
            reason=f'Activated from sandbox {sandbox.public_id}',
        )
        sandbox.status = CommissionSandbox.ARCHIVED
        sandbox.save(update_fields=['status', 'updated_at'])
        return {'version': production, 'activation_report': report}


@dataclass(frozen=True)
class SimulationRequest:
    sandbox_id: str
    mode: str
    period_start: date | None = None
    period_end: date | None = None
    natural_language: str = ''
    intent: dict[str, Any] | None = None


class SandboxSimulationInterface:
    """Future AI/API seam; callers submit structured requests, never prompts to calculators."""

    @staticmethod
    def execute(user, request: SimulationRequest):
        sandbox = SandboxManager.get_for_user(user, request.sandbox_id)
        return ScenarioRunner.run(
            user, sandbox, mode=request.mode,
            period_start=request.period_start, period_end=request.period_end,
        )
