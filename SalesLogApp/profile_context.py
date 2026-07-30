from .models import UserProfile


def get_user_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def appearance(request):
    if not request.user.is_authenticated:
        return {}
    profile = get_user_profile(request.user)
    try:
        print('DEBUG APPEARANCE: user=', request.user.username, 'header_color=', profile.header_color, 'theme_mode=', profile.theme_mode)
    except Exception:
        pass
    return {
        'sales_profile': profile,
        'appearance': {
            'theme_mode': profile.theme_mode,
            'header_color': profile.header_color,
        },
    }
