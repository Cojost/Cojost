"""Compatibility entry points for the Pay Plan Intent Driver V2.

Interpretation and candidate resolution are side-effect free.  The legacy
``create_plain_text_change_draft`` function remains for trusted callers that
have already confirmed draft creation; UI callers use the two-step workflow
in ``views.pay_plan_assistant``.
"""

from __future__ import annotations

from .pay_plan_intents.service import (
    create_draft_from_intent,
    interpret_request,
    resolve_intent,
)


def create_plain_text_change_draft(user, request_text, effective_date):
    intent = interpret_request(
        request_text,
        effective_date=effective_date,
    )
    return create_draft_from_intent(
        user,
        intent,
        effective_date,
    )


__all__ = [
    'create_draft_from_intent',
    'create_plain_text_change_draft',
    'interpret_request',
    'resolve_intent',
]
