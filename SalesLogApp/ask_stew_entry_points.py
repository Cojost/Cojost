from __future__ import annotations

from urllib.parse import urlencode

from django.urls import reverse

from .models.sales import Sale


ACTIVE_PLAN_PROMPT = 'active-plan'
MONTH_SUMMARY_PROMPT = 'month-summary'
BONUS_PROGRESS_PROMPT = 'bonus-progress'
ELIGIBILITY_PROMPT = 'eligibility'
RECORDED_SALE_PROMPT = 'recorded-sale'

CONTEXTUAL_QUESTIONS = {
    ACTIVE_PLAN_PROMPT: 'How am I paid?',
    MONTH_SUMMARY_PROMPT: 'What have I made this month?',
    BONUS_PROGRESS_PROMPT: 'How close am I to my next bonus?',
    ELIGIBILITY_PROMPT: 'What eligibility information am I missing?',
}

SOURCE_DESTINATIONS = {
    'dashboard': ('Dashboard', 'view_sales'),
    'commission': ('Commission', 'view_commission'),
    'eligibility': ('Monthly Eligibility', 'pay_plan_eligibility'),
}


def build_contextual_ask_stew_url(prompt, source, *, deal_number=None):
    """Build a URL from allowlisted prompt and source identifiers only."""

    if prompt not in CONTEXTUAL_QUESTIONS and prompt != RECORDED_SALE_PROMPT:
        raise ValueError('Unsupported Ask Stew contextual prompt.')
    if source not in SOURCE_DESTINATIONS:
        raise ValueError('Unsupported Ask Stew contextual source.')
    query = {'prompt': prompt, 'source': source}
    if prompt == RECORDED_SALE_PROMPT:
        if deal_number is None:
            raise ValueError('A recorded-sale prompt requires a deal number.')
        query['deal'] = str(deal_number)
    return f"{reverse('ask_stew_ai')}?{urlencode(query)}"


def resolve_contextual_question(user, prompt, *, deal_number=None):
    """Resolve a safe prompt, owner-scoping recorded-sale references."""

    if prompt in CONTEXTUAL_QUESTIONS:
        return CONTEXTUAL_QUESTIONS[prompt]
    if prompt != RECORDED_SALE_PROMPT:
        return ''
    try:
        normalized_deal_number = int(str(deal_number or ''))
    except (TypeError, ValueError):
        return ''
    if normalized_deal_number <= 0:
        return ''
    if not Sale.objects.filter(
        user=user,
        dealNumber=normalized_deal_number,
    ).exists():
        return ''
    return f'Break down deal #{normalized_deal_number}.'


def contextual_source(source):
    """Return a fixed, local destination or no contextual source."""

    destination = SOURCE_DESTINATIONS.get(str(source or ''))
    if destination is None:
        return None
    label, route_name = destination
    key = str(source)
    return {
        'key': key,
        'label': label,
        'url': reverse(route_name),
        'new_conversation_url': (
            f"{reverse('ask_stew_ai')}?{urlencode({'source': key})}"
        ),
    }


def contextual_entry_points(source, *, sales=()):
    """Build the fixed links shown on an authorized current-period page."""

    links = {
        'active_plan': build_contextual_ask_stew_url(
            ACTIVE_PLAN_PROMPT,
            source,
        ),
        'month_summary': build_contextual_ask_stew_url(
            MONTH_SUMMARY_PROMPT,
            source,
        ),
        'bonus_progress': build_contextual_ask_stew_url(
            BONUS_PROGRESS_PROMPT,
            source,
        ),
        'eligibility': build_contextual_ask_stew_url(
            ELIGIBILITY_PROMPT,
            source,
        ),
    }
    for sale in sales:
        sale.ask_stew_explain_url = build_contextual_ask_stew_url(
            RECORDED_SALE_PROMPT,
            source,
            deal_number=sale.dealNumber,
        )
    return links
