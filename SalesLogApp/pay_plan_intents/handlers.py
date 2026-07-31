from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from django.core.exceptions import ValidationError

from SalesLogApp.models import (
    PayPlanRule,
    PayPlanRuleCondition,
)
from SalesLogApp.commission_engine.validators import validate_condition
from SalesLogApp.plan_requirements import ActivePayPlanService

from .contract import (
    CandidateTarget,
    IntentAction,
    IntentResolution,
    PayPlanIntent,
    ProposedChange,
    TargetType,
)


CHANGE_ACTIONS = {
    IntentAction.CHANGE,
    IntentAction.REPLACE,
    IntentAction.INCREASE,
    IntentAction.DECREASE,
}


def active_version_for_user(user):
    result = ActivePayPlanService.get_for_user(user)
    if result.status != 'active':
        raise ValidationError(
            result.error or 'An active pay plan is required.'
        )
    return result.version


def candidate_label(rule) -> str:
    conditions = list(rule.conditions.all())
    vehicle_values = [
        str(condition.value).replace('_', ' ').title()
        for condition in conditions
        if condition.field_name == 'vehicle_condition'
        and condition.operator == 'equals'
    ]
    if vehicle_values:
        return ' and '.join(vehicle_values) + ' vehicles'
    metric = (rule.configuration or {}).get('unit_metric')
    if metric == 'monthly_new_units':
        return 'New vehicles'
    if metric == 'monthly_used_units':
        return 'Used vehicles'
    if conditions:
        return rule.name
    return 'All qualifying sales'


def candidate_contract(rule) -> CandidateTarget:
    return CandidateTarget(
        selector=str(rule.semantic_key),
        label=candidate_label(rule),
        rule_name=rule.name,
        applies_to=candidate_label(rule),
    )


class BaseTargetHandler:
    target_type = ''
    target_label = ''

    def resolve(
        self,
        user,
        intent: PayPlanIntent,
        *,
        selected_target: str | None = None,
    ) -> IntentResolution:
        raise NotImplementedError

    def apply(self, user, draft, intent, proposal):
        raise NotImplementedError

    @staticmethod
    def _select(candidates, selected_target):
        if selected_target:
            selected = [
                item for item in candidates
                if str(item.semantic_key) == str(selected_target)
            ]
            if len(selected) != 1:
                raise ValidationError(
                    'The selected rule is not available in your active plan.'
                )
            return selected[0]
        return candidates[0] if len(candidates) == 1 else None

    def _multiple_resolution(self, intent, candidates):
        contracts = tuple(candidate_contract(item) for item in candidates)
        labels = [item.applies_to for item in contracts]
        natural = _natural_join(labels)
        question = (
            f'I found separate {self.target_label.lower()} rules for '
            f'{natural}. Which one would you like to change?'
        )
        resolved = intent.with_resolution(
            ambiguities=('multiple_candidate_rules',),
            clarification_question=question,
            candidate_targets=contracts,
        )
        return IntentResolution('clarification', resolved, message=question)

    @staticmethod
    def _draft_rule(draft, selector):
        try:
            return draft.rules.get(semantic_key=selector)
        except PayPlanRule.DoesNotExist as exc:
            raise ValidationError(
                'The selected rule was not cloned into the inactive draft.'
            ) from exc


class MinimumHandler(BaseTargetHandler):
    def __init__(self, target_type, category, label):
        self.target_type = target_type
        self.category = category
        self.target_label = label

    def _candidates(self, version, intent):
        candidates = []
        for rule in version.rules.prefetch_related('conditions').filter(
            is_active=True,
            rule_type='minimum_commission',
        ):
            categories = (rule.configuration or {}).get(
                'applies_to_categories', [],
            )
            if self.category not in categories:
                continue
            if _scope_matches(rule, intent.target_scope):
                candidates.append(rule)
        return candidates

    def resolve(self, user, intent, *, selected_target=None):
        version = active_version_for_user(user)
        if intent.action not in CHANGE_ACTIONS:
            return _unsupported_action(intent, self.target_label)
        candidates = self._candidates(version, intent)
        if not candidates:
            amount = intent.new_value or Decimal('0')
            side = 'front-end' if self.category == 'front_end' else 'back-end'
            question = (
                f'Your current plan does not have a {side} minimum. '
                f'Would you like to add a ${amount.quantize(Decimal("0.01"))} '
                'minimum?'
            )
            resolved = intent.with_resolution(
                missing_information=('existing_target_rule',),
                clarification_question=question,
            )
            return IntentResolution('clarification', resolved, message=question)
        if len(candidates) > 1 and not selected_target:
            return self._multiple_resolution(intent, candidates)
        rule = self._select(candidates, selected_target)
        current = Decimal(str(rule.configuration['minimum_amount']))
        proposal = ProposedChange(
            action_type='change_minimum_commission',
            target_type=self.target_type,
            target_label=self.target_label,
            rule_selector=str(rule.semantic_key),
            rule_name=rule.name,
            current_value=current,
            new_value=intent.new_value,
            applies_to=candidate_label(rule),
            source_version_id=version.id,
        )
        return IntentResolution('proposed', intent, proposal=proposal)

    def apply(self, user, draft, intent, proposal):
        rule = self._draft_rule(draft, proposal.rule_selector)
        configuration = deepcopy(rule.configuration)
        configuration['minimum_amount'] = str(
            intent.new_value.quantize(Decimal('0.01'))
        )
        rule.configuration = configuration
        rule.full_clean()
        rule.save(update_fields=['configuration', 'updated_at'])
        return [proposal.as_action()], []


class RuleValueHandler(BaseTargetHandler):
    def __init__(
        self,
        target_type,
        label,
        rule_types,
        field_name,
        *,
        category=None,
        percentage=False,
    ):
        self.target_type = target_type
        self.target_label = label
        self.rule_types = tuple(rule_types)
        self.field_name = field_name
        self.category = category
        self.percentage = percentage

    def _candidates(self, version, intent):
        candidates = []
        for rule in version.rules.prefetch_related('conditions').filter(
            is_active=True, rule_type__in=self.rule_types,
        ):
            if self.category and rule.rule_type in {
                'minimum_commission', 'maximum_commission',
            }:
                categories = rule.configuration.get(
                    'applies_to_categories', [],
                )
                if self.category not in categories:
                    continue
            if _scope_matches(rule, intent.target_scope):
                candidates.append(rule)
        return candidates

    def resolve(self, user, intent, *, selected_target=None):
        version = active_version_for_user(user)
        if intent.action not in CHANGE_ACTIONS:
            return _unsupported_action(intent, self.target_label)
        candidates = self._candidates(version, intent)
        if not candidates:
            question = (
                f'Your plan does not currently have {self.target_label.lower()}. '
                'Would you like to add it?'
            )
            resolved = intent.with_resolution(
                missing_information=('existing_target_rule',),
                clarification_question=question,
            )
            return IntentResolution('clarification', resolved, message=question)
        if len(candidates) > 1 and not selected_target:
            return self._multiple_resolution(intent, candidates)
        rule = self._select(candidates, selected_target)
        raw_current = (rule.configuration or {}).get(self.field_name)
        current = (
            Decimal(str(raw_current)) if raw_current not in (None, '') else None
        )
        new_value = intent.percentage if self.percentage else intent.new_value
        proposal = ProposedChange(
            action_type=(
                'change_commission_rate'
                if self.percentage else f'change_{self.target_type}'
            ),
            target_type=self.target_type,
            target_label=self.target_label,
            rule_selector=str(rule.semantic_key),
            rule_name=rule.name,
            current_value=current,
            new_value=new_value,
            applies_to=candidate_label(rule),
            source_version_id=version.id,
        )
        return IntentResolution('proposed', intent, proposal=proposal)

    def apply(self, user, draft, intent, proposal):
        rule = self._draft_rule(draft, proposal.rule_selector)
        configuration = deepcopy(rule.configuration)
        value = intent.percentage if self.percentage else intent.new_value
        configuration[self.field_name] = str(value)
        rule.configuration = configuration
        rule.full_clean()
        rule.save(update_fields=['configuration', 'updated_at'])
        return [proposal.as_action()], []


class VolumeBonusHandler(BaseTargetHandler):
    target_type = TargetType.VOLUME_BONUS_TIER
    target_label = 'Volume bonus'

    def _candidates(self, version, intent):
        candidates = list(
            version.rules.prefetch_related('conditions').filter(
                is_active=True, rule_type='volume_bonus',
            )
        )
        if intent.target_scope == 'green_pea':
            candidates = [
                item for item in candidates
                if 'green pea' in item.name.lower()
            ]
        elif intent.target_scope == 'standard':
            standard = [
                item for item in candidates
                if 'standard' in item.name.lower()
                or 'green pea' not in item.name.lower()
            ]
            candidates = standard
        else:
            standard = [
                item for item in candidates
                if 'standard' in item.name.lower()
            ]
            if standard:
                candidates = standard
            else:
                candidates = [
                    item for item in candidates
                    if not item.conditions.exists()
                ]
        return candidates

    def resolve(self, user, intent, *, selected_target=None):
        version = active_version_for_user(user)
        if intent.unit_threshold <= 0 or intent.amount <= 0:
            raise ValidationError(
                'Volume-bonus thresholds and amounts must be greater than zero.'
            )
        if intent.unit_threshold % Decimal('0.5'):
            raise ValidationError(
                'Volume-bonus thresholds must use whole or half units.'
            )
        candidates = self._candidates(version, intent)
        action = intent.action
        if action not in {*CHANGE_ACTIONS, IntentAction.ADD}:
            return _unsupported_action(intent, self.target_label)
        if action in CHANGE_ACTIONS:
            matching = []
            for rule in candidates:
                for tier in rule.configuration.get('tiers', []):
                    minimum = Decimal(str(tier['minimum_units']))
                    maximum = (
                        Decimal(str(tier['maximum_units']))
                        if tier.get('maximum_units') not in (None, '') else None
                    )
                    if intent.unit_threshold >= minimum and (
                        maximum is None or intent.unit_threshold <= maximum
                    ):
                        matching.append((rule, tier))
                        break
            if not matching:
                question = (
                    f'No volume-bonus tier containing {intent.unit_threshold} '
                    'units exists. Would you like to add one?'
                )
                resolved = intent.with_resolution(
                    missing_information=('existing_target_rule',),
                    clarification_question=question,
                )
                return IntentResolution('clarification', resolved, message=question)
            rules = [item[0] for item in matching]
            if len(rules) > 1 and not selected_target:
                return self._multiple_resolution(intent, rules)
            rule = self._select(rules, selected_target)
            tier = next(item[1] for item in matching if item[0] == rule)
            current = Decimal(str(tier['amount']))
            mode = 'change'
        else:
            if len(candidates) > 1 and not selected_target:
                return self._multiple_resolution(intent, candidates)
            rule = self._select(candidates, selected_target) if candidates else None
            current = None
            mode = 'add'
        proposal = ProposedChange(
            action_type=(
                'change_volume_tier_amount'
                if mode == 'change' else 'add_volume_tier'
            ),
            target_type=self.target_type,
            target_label=self.target_label,
            rule_selector=str(rule.semantic_key) if rule else None,
            rule_name=rule.name if rule else 'Standard Volume Bonus',
            current_value=current,
            new_value=intent.amount,
            applies_to=candidate_label(rule) if rule else 'All qualifying sales',
            source_version_id=version.id,
            details={
                'minimum_units': intent.unit_threshold,
                'mode': mode,
            },
        )
        return IntentResolution('proposed', intent, proposal=proposal)

    def apply(self, user, draft, intent, proposal):
        if proposal.rule_selector:
            rule = self._draft_rule(draft, proposal.rule_selector)
        else:
            rule = PayPlanRule(
                pay_plan_version=draft,
                name='Standard Volume Bonus',
                rule_type='volume_bonus',
                calculation_scope='period',
                configuration={'tiers': [], 'tier_mode': 'highest_only'},
                sort_order=draft.rules.count() + 1,
            )
        configuration = deepcopy(rule.configuration)
        tiers = sorted(
            deepcopy(configuration.get('tiers') or []),
            key=lambda item: Decimal(str(item['minimum_units'])),
        )
        threshold = intent.unit_threshold
        actions = []
        warnings = []
        if proposal.details['mode'] == 'change':
            for tier in tiers:
                minimum = Decimal(str(tier['minimum_units']))
                maximum = (
                    Decimal(str(tier['maximum_units']))
                    if tier.get('maximum_units') not in (None, '') else None
                )
                if threshold >= minimum and (
                    maximum is None or threshold <= maximum
                ):
                    tier['amount'] = str(intent.amount.quantize(Decimal('0.01')))
                    break
        else:
            if any(
                Decimal(str(item['minimum_units'])) == threshold
                for item in tiers
            ):
                existing = next(
                    item for item in tiers
                    if Decimal(str(item['minimum_units'])) == threshold
                )
                raise ValidationError(
                    f'{rule.name} already has a tier beginning at {threshold} '
                    f'units that pays '
                    f'${Decimal(str(existing["amount"])).quantize(Decimal("0.01"))}. '
                    'Ask to change that tier instead.'
                )
            previous = next((
                item for item in reversed(tiers)
                if Decimal(str(item['minimum_units'])) < threshold
            ), None)
            following = next((
                item for item in tiers
                if Decimal(str(item['minimum_units'])) > threshold
            ), None)
            if previous is not None and (
                previous.get('maximum_units') in (None, '')
                or Decimal(str(previous['maximum_units'])) >= threshold
            ):
                old_maximum = previous.get('maximum_units')
                new_maximum = threshold - Decimal('0.5')
                previous['maximum_units'] = str(new_maximum)
                actions.append({
                    'action_type': 'adjust_volume_tier_range',
                    'target_key': (
                        f'{rule.name}:{previous["minimum_units"]}:maximum'
                    ),
                    'rule_name': rule.name,
                    'old_value': old_maximum,
                    'new_value': str(new_maximum),
                })
                warnings.append(
                    f'{rule.name}: adjusted the preceding tier maximum to '
                    f'{new_maximum} units to prevent overlap.'
                )
            maximum = (
                Decimal(str(following['minimum_units'])) - Decimal('0.5')
                if following else None
            )
            tier = {
                'minimum_units': str(threshold),
                'amount': str(intent.amount.quantize(Decimal('0.01'))),
            }
            if maximum is not None:
                tier['maximum_units'] = str(maximum)
            tiers.append(tier)
            tiers.sort(key=lambda item: Decimal(str(item['minimum_units'])))
        configuration['tiers'] = tiers
        rule.configuration = configuration
        rule.full_clean()
        if rule.pk:
            rule.save(update_fields=['configuration', 'updated_at'])
        else:
            rule.save()
        actions.append(proposal.as_action())
        return actions, warnings


class RequirementHandler(BaseTargetHandler):
    target_type = TargetType.CONDITION_REQUIREMENT
    target_label = 'Condition requirement'

    def resolve(self, user, intent, *, selected_target=None):
        version = active_version_for_user(user)
        if intent.action not in {
            IntentAction.ADD, IntentAction.REMOVE,
            IntentAction.ENABLE, IntentAction.DISABLE,
        }:
            return _unsupported_action(intent, self.target_label)
        field = intent.conditions[0]['field_name']
        candidates = list(
            version.rules.prefetch_related('conditions').filter(
                is_active=True, rule_type='volume_bonus',
            )
        )
        if intent.action in {IntentAction.REMOVE, IntentAction.DISABLE}:
            candidates = [
                item for item in candidates
                if item.conditions.filter(field_name=field).exists()
            ]
        if not candidates:
            question = (
                'Your current plan does not have that requirement. '
                'Would you like to add it?'
            )
            resolved = intent.with_resolution(
                missing_information=('existing_target_rule',),
                clarification_question=question,
            )
            return IntentResolution('clarification', resolved, message=question)
        if len(candidates) > 1 and not selected_target:
            return self._multiple_resolution(intent, candidates)
        rule = self._select(candidates, selected_target)
        proposal = ProposedChange(
            action_type=(
                'remove_requirement'
                if intent.action in {IntentAction.REMOVE, IntentAction.DISABLE}
                else 'add_requirement'
            ),
            target_type=self.target_type,
            target_label=self.target_label,
            rule_selector=str(rule.semantic_key),
            rule_name=rule.name,
            current_value=None,
            new_value=None,
            applies_to=candidate_label(rule),
            source_version_id=version.id,
            details={'field_name': field},
        )
        return IntentResolution('proposed', intent, proposal=proposal)

    def apply(self, user, draft, intent, proposal):
        rule = self._draft_rule(draft, proposal.rule_selector)
        field = proposal.details['field_name']
        if proposal.action_type == 'remove_requirement':
            rule.conditions.filter(field_name=field).delete()
        elif not rule.conditions.filter(field_name=field).exists():
            validate_condition({
                'field_name': field,
                'operator': 'is_true',
                'value': None,
            })
            condition = PayPlanRuleCondition(
                rule=rule,
                field_name=field,
                operator='is_true',
                value=True,
                sort_order=rule.conditions.count() + 1,
            )
            condition.save()
        action = proposal.as_action()
        action['field_name'] = field
        return [action], []


class UnsupportedTargetHandler(BaseTargetHandler):
    def __init__(self, target_type, label):
        self.target_type = target_type
        self.target_label = label

    def resolve(self, user, intent, *, selected_target=None):
        # Resolve the active plan first so even unsupported operations retain
        # the same ownership boundary.
        active_version_for_user(user)
        message = (
            f'I understood the target as {self.target_label.lower()}, but this '
            'operation is not yet supported safely. No draft was created.'
        )
        return IntentResolution('unsupported', intent, message=message)

    def apply(self, user, draft, intent, proposal):
        raise ValidationError('Unsupported intent handlers cannot mutate drafts.')


def _unsupported_action(intent, label):
    message = (
        f'The “{intent.action}” action is not supported for {label.lower()}. '
        'No draft was created.'
    )
    return IntentResolution('unsupported', intent, message=message)


def _scope_matches(rule, scope):
    if not scope:
        return True
    label = candidate_label(rule).lower()
    if scope == 'new':
        return 'new' in label
    if scope == 'used':
        return 'used' in label
    if scope == 'green_pea':
        return 'green pea' in rule.name.lower()
    if scope == 'standard':
        return 'green pea' not in rule.name.lower()
    return True


def _natural_join(values):
    unique = list(dict.fromkeys(values))
    if len(unique) <= 1:
        return unique[0] if unique else 'the matching rules'
    if len(unique) == 2:
        return f'{unique[0]} and {unique[1]}'
    return f'{", ".join(unique[:-1])}, and {unique[-1]}'


TARGET_HANDLER_REGISTRY = {
    TargetType.FRONT_END_MINIMUM: MinimumHandler(
        TargetType.FRONT_END_MINIMUM,
        'front_end',
        'Front-end commission minimum',
    ),
    TargetType.BACK_END_MINIMUM: MinimumHandler(
        TargetType.BACK_END_MINIMUM,
        'back_end',
        'Back-end commission minimum',
    ),
    TargetType.FRONT_END_PERCENTAGE: RuleValueHandler(
        TargetType.FRONT_END_PERCENTAGE,
        'Front-end commission percentage',
        ('front_gross_percentage',),
        'rate',
        percentage=True,
    ),
    TargetType.BACK_END_PERCENTAGE: RuleValueHandler(
        TargetType.BACK_END_PERCENTAGE,
        'Back-end commission percentage',
        ('back_gross_percentage',),
        'rate',
        percentage=True,
    ),
    TargetType.FRONT_END_MAXIMUM: RuleValueHandler(
        TargetType.FRONT_END_MAXIMUM,
        'Front-end commission maximum',
        ('maximum_commission',),
        'maximum_amount',
        category='front_end',
    ),
    TargetType.BACK_END_MAXIMUM: RuleValueHandler(
        TargetType.BACK_END_MAXIMUM,
        'Back-end commission maximum',
        ('maximum_commission',),
        'maximum_amount',
        category='back_end',
    ),
    TargetType.FRONT_END_PACK: RuleValueHandler(
        TargetType.FRONT_END_PACK,
        'Front-end pack',
        ('front_gross_percentage', 'progressive_unit_position_percentage'),
        'pack_amount',
    ),
    TargetType.BACK_END_PACK: RuleValueHandler(
        TargetType.BACK_END_PACK,
        'Back-end pack',
        ('back_gross_percentage',),
        'pack_amount',
    ),
    TargetType.VOLUME_BONUS_TIER: VolumeBonusHandler(),
    TargetType.FLAT_BONUS: RuleValueHandler(
        TargetType.FLAT_BONUS,
        'Flat bonus',
        ('flat_per_deal',),
        'amount',
    ),
    TargetType.DRAW: RuleValueHandler(
        TargetType.DRAW,
        'Draw',
        ('draw',),
        'amount',
    ),
    TargetType.CONDITION_REQUIREMENT: RequirementHandler(),
}

for unsupported_type, unsupported_label in {
    TargetType.MODEL_BONUS: 'Model bonus',
    TargetType.NEW_VEHICLE_BONUS: 'New-vehicle bonus',
    TargetType.USED_VEHICLE_BONUS: 'Used-vehicle bonus',
    TargetType.MANUFACTURER_INCENTIVE: 'Manufacturer incentive',
}.items():
    TARGET_HANDLER_REGISTRY[unsupported_type] = UnsupportedTargetHandler(
        unsupported_type, unsupported_label,
    )
