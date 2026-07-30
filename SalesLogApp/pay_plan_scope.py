from __future__ import annotations

from django.core.exceptions import ValidationError

from .commission_engine.exceptions import PayPlanResolutionError


class OwnedPayPlanRuleService:
    """Single ownership boundary for rules consumed by calculations."""

    @staticmethod
    def validate_version_owner(user, version):
        owner_id = version.pay_plan.owner_user_id
        if owner_id != user.id:
            raise PayPlanResolutionError(
                'The selected pay-plan version does not belong to this user.'
            )
        return version

    @classmethod
    def active_rules_for_user(cls, user, version, *, scope=None):
        cls.validate_version_owner(user, version)
        rules = version.rules.filter(is_active=True)
        if scope is not None:
            rules = rules.filter(calculation_scope=scope)
        return rules.order_by('sort_order', 'id')

    @staticmethod
    def validate_clone_ownership(user, source_version, target_version):
        source_owner = source_version.pay_plan.owner_user_id
        target_owner = target_version.pay_plan.owner_user_id
        if source_owner != user.id or target_owner != user.id:
            raise ValidationError(
                'Rules may only be copied between pay-plan versions owned by '
                'the same user.'
            )