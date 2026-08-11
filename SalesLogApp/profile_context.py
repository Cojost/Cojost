from django.conf import settings

from .models import UserProfile


def get_user_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def appearance(request):
    if not request.user.is_authenticated:
        return {}
    profile = get_user_profile(request.user)
    return {
        'sales_profile': profile,
        'billing_feature_enabled': settings.BILLING_FEATURE_ENABLED,
        'teams_feature_enabled': settings.TEAMS_FEATURE_ENABLED,
        'appearance': {
            'theme_mode': profile.theme_mode,
            'header_color': profile.header_color,
        },
    }
