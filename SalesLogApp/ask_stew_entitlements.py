from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def _configured_pilot_user_ids() -> frozenset[int]:
    """Return only valid immutable user IDs; invalid entries never grant access."""

    user_ids = set()
    for raw_user_id in getattr(settings, 'ASK_STEW_AI_PILOT_USER_IDS', ()):
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            continue
        if user_id > 0:
            user_ids.add(user_id)
    return frozenset(user_ids)


def ask_stew_ai_authorized(user) -> bool:
    """Central CX-3 entitlement boundary; customer access defaults to denied."""

    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    if getattr(settings, 'ASK_STEW_AI_LAB_ONLY', True):
        return False
    return user.pk in _configured_pilot_user_ids()


def ask_stew_ai_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not ask_stew_ai_authorized(request.user):
            from .access import uses_new_engine

            messages.info(
                request,
                'Ask Stew AI is available only to authorized pilot accounts.',
            )
            destination = 'my_pay_plan' if uses_new_engine(request.user) else 'view_commission'
            return redirect(destination)
        return view_func(request, *args, **kwargs)

    return wrapper
