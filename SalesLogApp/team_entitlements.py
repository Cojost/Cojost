from dataclasses import dataclass

from django.conf import settings
from django.utils.module_loading import import_string


@dataclass(frozen=True)
class TeamEntitlement:
    tier: str
    source: str

    @property
    def has_pro_access(self):
        return self.tier in {'pro', 'founder_pro'}


def founder_allowlist_entitlement(user):
    """Development-only transition seam; never proof of production payment."""
    founder_ids = {str(value) for value in settings.TEAMS_FOUNDER_USER_IDS}
    if (
        settings.DEBUG
        and not settings.BILLING_ENFORCEMENT_ENABLED
        and user.is_authenticated
        and str(user.pk) in founder_ids
    ):
        return TeamEntitlement(tier='founder_pro', source='founder_allowlist')
    return TeamEntitlement(tier='basic', source='default')


def billing_owned_entitlement(user):
    from .billing_entitlements import get_billing_entitlement

    billing = get_billing_entitlement(user)
    if billing.has_pro_access:
        return TeamEntitlement(tier=billing.tier, source=billing.source)
    legacy = founder_allowlist_entitlement(user)
    if legacy.has_pro_access:
        return legacy
    return TeamEntitlement(tier='basic', source='billing')


def get_team_entitlement(user):
    backend = import_string(settings.TEAMS_ENTITLEMENT_BACKEND)
    entitlement = backend(user)
    if not isinstance(entitlement, TeamEntitlement):
        raise TypeError('The Teams entitlement backend must return TeamEntitlement.')
    return entitlement


def can_create_team(user):
    return (
        settings.TEAMS_FEATURE_ENABLED
        and user.is_authenticated
        and get_team_entitlement(user).has_pro_access
    )


def can_use_teams(user):
    """Return entitlement eligibility; the no-team explainer remains public to users."""
    if not settings.TEAMS_FEATURE_ENABLED or not user.is_authenticated:
        return False
    if get_team_entitlement(user).has_pro_access:
        return True
    from .models import TeamInvitation, TeamMembership

    return (
        TeamMembership.objects.filter(
            user=user,
            status__in=[TeamMembership.INVITED, TeamMembership.ACTIVE],
            team__is_active=True,
        ).exists()
        or TeamInvitation.objects.filter(
            intended_user=user,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        ).exists()
    )
