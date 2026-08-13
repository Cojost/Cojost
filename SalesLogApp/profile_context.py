from django.conf import settings

from .ask_stew_entitlements import ask_stew_ai_authorized
from .models import UserProfile


def get_user_profile(user):
    profile = UserProfile.objects.filter(user=user).first()
    return profile if profile is not None else UserProfile(user=user)


def appearance(request):
    if not request.user.is_authenticated:
        return {}
    profile = get_user_profile(request.user)
    return {
        'sales_profile': profile,
        'billing_feature_enabled': settings.BILLING_FEATURE_ENABLED,
        'teams_feature_enabled': settings.TEAMS_FEATURE_ENABLED,
        'ask_stew_ai_authorized': ask_stew_ai_authorized(request.user),
        'appearance': {
            'theme_mode': profile.theme_mode,
            'header_color': profile.header_color,
        },
    }
