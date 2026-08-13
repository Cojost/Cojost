from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
import unicodedata

from django.utils import timezone

from .ask_stew_provider import configured_ask_stew_gateway
from .models import PayPlanEligibility, Sale
from .plan_requirements import ActivePayPlanService, PlanRequirementService
from .services import sales_month_context
from .templatetags.pay_plan_display import rule_explanation


ACTIVE_PLAN_EXPLANATION = 'active_plan_explanation'
RECORDED_SALE_EXPLANATION = 'recorded_sale_explanation'
CURRENT_MONTH_SUMMARY = 'current_month_summary'
BONUS_PROGRESS = 'bonus_progress'
ELIGIBILITY_EXPLANATION = 'eligibility_explanation'
CLARIFICATION = 'clarification'

SUPPORTED_INTENTS = frozenset({
    ACTIVE_PLAN_EXPLANATION,
    RECORDED_SALE_EXPLANATION,
    CURRENT_MONTH_SUMMARY,
    BONUS_PROGRESS,
    ELIGIBILITY_EXPLANATION,
    CLARIFICATION,
})

DECLINED_CHANGE = 'declined_change'
DECLINED_HYPOTHETICAL = 'declined_hypothetical'
DECLINED_SECURITY = 'declined_security'
DECLINED_UNSUPPORTED = 'declined_unsupported'

SAFE_RULE_TYPES = frozenset({
    'front_gross_percentage',
    'back_gross_percentage',
    'minimum_commission',
    'maximum_commission',
    'flat_per_deal',
    'vehicle_spiff',
    'per_unit_bonus',
    'volume_bonus',
})

MAX_ASK_STEW_QUESTION_CHARS = 1000
ALLOWED_QUESTION_PUNCTUATION = frozenset("#'.,?!%@$&-/:")
QUESTION_TRANSLATION = str.maketrans({
    '\u2018': "'",
    '\u2019': "'",
    '\u201c': '"',
    '\u201d': '"',
    '\u2013': '-',
    '\u2014': '-',
})
OBFUSCATION_TRANSLATION = str.maketrans({
    '@': 'a',
    '$': 's',
    '0': 'o',
    '1': 'i',
    '3': 'e',
})

SECURITY_PATTERN = re.compile(
    r"\b(?:another|other) users?\b|"
    r"\busers?\s+\d+\b|"
    r"\bignore (?:all |any |earlier |prior |previous |system |the )?instructions?\b|"
    r"\b(?:system prompt|api key|private data|hidden instructions?|secrets?)\b|"
    r"\bshow\b.*\bprivate\b"
)
HYPOTHETICAL_PATTERN = re.compile(
    r'\b(?:what if|what would|would i|hypothetical|suppose|scenario)\b|'
    r'\bproject(?:ion|ed|ing)?\b|'
    r'\bif i (?:sell|sold|add|added|had|have)\b|'
    r'\b(?:sell|sold|add|added)\b.*\bmore\b'
)
MUTATION_PATTERN = re.compile(
    r'\b(?:change|edit|delete|remove|activate|deactivate|replace|upload|create|'
    r'add|set|modify|save|update|submit|write|insert)\b'
)
DEAL_NUMBER_PATTERN = re.compile(
    r'\bdeal(?:\s+number)?\s*(?:#|no\.?\s*)?(\d{1,12})\b',
    re.IGNORECASE,
)

_REQUEST_END = r'[?.!]?'
_SALE_NUMBER = r'(?:deal\s*(?:number\s*)?(?:#\s*)?\d{1,12})'
_SALE_LABEL = (
    r'(?:(?:recorded\s+)?(?:sale|deal)\s+for\s+'
    r"[a-z0-9][a-z0-9'-]{0,39}(?:\s+[a-z0-9][a-z0-9'-]{0,39}){0,3})"
)
_AMBIGUOUS_SALE = r'(?:(?:my\s+)?(?:recorded\s+)?(?:sale|deal))'
_SALE_REFERENCE = rf'(?:{_SALE_NUMBER}|{_SALE_LABEL}|{_AMBIGUOUS_SALE})'

SUPPORTED_REQUEST_PATTERNS = (
    (
        ACTIVE_PLAN_EXPLANATION,
        re.compile(
            rf'(?:please\s+)?(?:explain|describe)\s+my\s+'
            rf'(?:active\s+)?(?:pay\s+plan|commission\s+plan|plan|commission\s+rules?){_REQUEST_END}'
        ),
    ),
    (
        ACTIVE_PLAN_EXPLANATION,
        re.compile(
            rf'(?:what\s+(?:is|are)|how\s+does)\s+my\s+'
            rf'(?:active\s+)?(?:pay\s+plan|commission\s+plan|commission\s+rules?)'
            rf'(?:\s+work)?{_REQUEST_END}'
        ),
    ),
    (
        RECORDED_SALE_EXPLANATION,
        re.compile(
            rf'(?:please\s+)?explain\s+{_SALE_REFERENCE}'
            rf'(?:\s+(?:commission|calculation|result))?{_REQUEST_END}'
        ),
    ),
    (
        RECORDED_SALE_EXPLANATION,
        re.compile(
            rf'why\s+was\s+{_SALE_REFERENCE}\s+calculated'
            rf'(?:\s+that\s+way)?{_REQUEST_END}'
        ),
    ),
    (
        RECORDED_SALE_EXPLANATION,
        re.compile(
            rf'how\s+was\s+{_SALE_REFERENCE}(?:\s+commission)?\s+calculated{_REQUEST_END}'
        ),
    ),
    (
        CURRENT_MONTH_SUMMARY,
        re.compile(
            rf'(?:what\s+are|explain|show\s+me)\s+my\s+'
            rf'(?:current[- ]month|this\s+month(?:\'s)?)\s+'
            rf'(?:commission\s+)?(?:totals?|earnings?|total\s+commission\s+earnings?){_REQUEST_END}'
        ),
    ),
    (
        CURRENT_MONTH_SUMMARY,
        re.compile(
            rf'how\s+much\s+commission\s+(?:have\s+i\s+earned|did\s+i\s+earn)\s+'
            rf'this\s+month{_REQUEST_END}'
        ),
    ),
    (
        BONUS_PROGRESS,
        re.compile(
            rf'how\s+many\s+(?:credited\s+)?units\s+'
            rf'(?:do\s+i\s+need\s+(?:for|to\s+reach)|until)\s+my\s+'
            rf'next\s+bonus(?:\s+tier)?{_REQUEST_END}'
        ),
    ),
    (
        BONUS_PROGRESS,
        re.compile(
            rf'(?:explain|what\s+is)\s+my\s+(?:current\s+)?bonus\s+progress{_REQUEST_END}'
        ),
    ),
    (
        ELIGIBILITY_EXPLANATION,
        re.compile(
            rf'which\s+eligibility\s+information\s+is\s+(?:still\s+)?missing{_REQUEST_END}'
        ),
    ),
    (
        ELIGIBILITY_EXPLANATION,
        re.compile(
            rf'why\s+is\s+my\s+eligibility\s+requirement\s+not\s+satisfied{_REQUEST_END}'
        ),
    ),
    (
        ELIGIBILITY_EXPLANATION,
        re.compile(
            rf'(?:explain|what\s+is)\s+my\s+(?:current\s+)?eligibility'
            rf'(?:\s+requirements?)?{_REQUEST_END}'
        ),
    ),
)


@dataclass(frozen=True)
class AskStewIntentDecision:
    category: str
    allowed: bool
    response: str = ''


@dataclass(frozen=True)
class DeterministicExplanation:
    intent: str
    facts: dict
    answer: str


@dataclass(frozen=True)
class AskStewAnswer:
    intent: str
    answer: str
    provider_status: str
    provider_used: bool = False
    notice: str = ''


def _normalized_question(question: str):
    if not isinstance(question, str) or len(question) > MAX_ASK_STEW_QUESTION_CHARS:
        return None
    normalized = unicodedata.normalize('NFKC', question).translate(
        QUESTION_TRANSLATION
    )
    if any(unicodedata.category(character).startswith('C') for character in normalized):
        return None
    normalized = ' '.join(normalized.casefold().strip().split())
    if not normalized or len(normalized) > MAX_ASK_STEW_QUESTION_CHARS:
        return None
    for character in normalized:
        if character.isascii() and (character.isalnum() or character.isspace()):
            continue
        if character not in ALLOWED_QUESTION_PUNCTUATION:
            return None
    scan_text = normalized.translate(OBFUSCATION_TRANSLATION)
    scan_text = re.sub(r'(?<=[a-z])[^a-z0-9\s](?=[a-z])', '', scan_text)
    return normalized, scan_text


def classify_ask_stew_question(question: str) -> AskStewIntentDecision:
    normalized_question = _normalized_question(question)
    if normalized_question is None:
        return AskStewIntentDecision(
            DECLINED_UNSUPPORTED,
            False,
            'Ask a question about your recorded sales, current pay plan, '
            'commission totals, bonuses, or eligibility.',
        )
    normalized, scan_text = normalized_question
    if SECURITY_PATTERN.search(normalized) or SECURITY_PATTERN.search(scan_text):
        return AskStewIntentDecision(
            DECLINED_SECURITY,
            False,
            'I can explain only information from your own StewLog account. '
            'I cannot reveal private data, hidden instructions, or system settings.',
        )
    if HYPOTHETICAL_PATTERN.search(normalized) or HYPOTHETICAL_PATTERN.search(scan_text):
        return AskStewIntentDecision(
            DECLINED_HYPOTHETICAL,
            False,
            'Hypothetical and projected commission questions are not available '
            'in this version. I can explain sales already recorded in StewLog.',
        )
    if MUTATION_PATTERN.search(normalized) or MUTATION_PATTERN.search(scan_text):
        return AskStewIntentDecision(
            DECLINED_CHANGE,
            False,
            'Ask Stew AI is read-only and cannot make that change. Use My Pay '
            'Plan for pay-plan drafts or the appropriate StewLog page for other updates.',
        )
    for intent, pattern in SUPPORTED_REQUEST_PATTERNS:
        if pattern.fullmatch(normalized):
            return AskStewIntentDecision(intent, True)
    return AskStewIntentDecision(
        DECLINED_UNSUPPORTED,
        False,
        'I can help explain your active pay plan, a recorded deal, current-month '
        'commission, bonus progress, or eligibility. Try one of those topics.',
    )


def _money(value) -> str:
    return f'${Decimal(str(value or 0)):,.2f}'


def _units(value) -> str:
    number = Decimal(str(value or 0))
    return format(number.quantize(Decimal('0.1')), 'f')


def _active_plan_explanation(user) -> DeterministicExplanation:
    active = ActivePayPlanService.get_for_user(user)
    if active.status != 'active':
        answer = (
            'StewLog could not find one active, owner-matched pay plan for today. '
            'Open My Pay Plan to review your current plan status.'
        )
        return DeterministicExplanation(
            ACTIVE_PLAN_EXPLANATION,
            {'plan_status': 'not_available'},
            answer,
        )
    rules = list(
        active.version.rules.filter(is_active=True)
        .prefetch_related('conditions')
        .order_by('sort_order', 'id')
    )
    summaries = [
        rule_explanation(rule)
        for rule in rules
        if rule.rule_type in SAFE_RULE_TYPES
    ][:20]
    if len(summaries) < len(rules):
        summaries.append('Additional enabled rules are included in this plan.')
    facts = {
        'plan_name': active.plan.name,
        'status': 'Active',
        'effective_date': active.assignment.effective_start_date.isoformat(),
        'enabled_rule_count': len(rules),
        'rule_explanations': summaries,
    }
    rule_text = ' '.join(summaries) if summaries else (
        'No enabled commission rules are currently listed.'
    )
    answer = (
        f'Your active pay plan is {active.plan.name}. It has {len(rules)} enabled '
        f'rule{"s" if len(rules) != 1 else ""}. {rule_text}'
    )
    return DeterministicExplanation(ACTIVE_PLAN_EXPLANATION, facts, answer)


def _month_context(user, month_start=None):
    month_start = (month_start or timezone.localdate()).replace(day=1)
    return sales_month_context(user, month_start)


def _current_month_explanation(user) -> DeterministicExplanation:
    context = _month_context(user)
    diagnostics = context['commission_diagnostics']
    facts = {
        'month': context['selected_month'].strftime('%B %Y'),
        'credited_units': _units(context['total_count']),
        'front_end_commission': _money(context['total_front_end']),
        'back_end_commission': _money(context['total_back_end']),
        'bonuses': _money(context['total_bonus']),
        'adjustments': _money(context['total_adjustments']),
        'total_commission': _money(context['total_commission']),
        'sales_calculated': diagnostics['calculated_count'],
        'sales_needing_review': diagnostics['excluded_count'],
    }
    answer = (
        f'For {facts["month"]}, StewLog shows {facts["credited_units"]} credited '
        f'units, {facts["front_end_commission"]} in front-end commission, '
        f'{facts["back_end_commission"]} in back-end commission, '
        f'{facts["bonuses"]} in bonuses, and {facts["adjustments"]} in '
        f'adjustments. Your current total is {facts["total_commission"]}. '
        f'{facts["sales_needing_review"]} recorded sale(s) still need review.'
    )
    return DeterministicExplanation(CURRENT_MONTH_SUMMARY, facts, answer)


def _owned_sale_from_question(user, question):
    number_match = DEAL_NUMBER_PATTERN.search(question or '')
    if number_match:
        matches = list(
            Sale.objects.filter(
                user=user,
                dealNumber=int(number_match.group(1)),
            ).order_by('-date', '-id')[:2]
        )
        return matches[0] if len(matches) == 1 else None

    normalized = (question or '').casefold()
    candidates = []
    for sale in Sale.objects.filter(user=user).order_by('-date', '-id')[:100]:
        customer_label = str(sale.customer or '').strip().casefold()
        if customer_label and customer_label in normalized:
            candidates.append(sale)
    return candidates[0] if len(candidates) == 1 else None


def _sale_explanation(user, question) -> DeterministicExplanation:
    sale = _owned_sale_from_question(user, question)
    if sale is None:
        return DeterministicExplanation(
            CLARIFICATION,
            {},
            'Which recorded deal do you mean? Include the deal number shown in '
            'your Sales Log so I can explain the owner-matched calculation.',
        )
    context = _month_context(user, sale.date)
    diagnostic = context['sale_diagnostics_by_id'].get(sale.pk)
    safe_label = (
        f'Deal #{sale.dealNumber} on '
        f'{sale.date:%b.} {sale.date.day}, {sale.date:%Y}'
    )
    if diagnostic is None or not diagnostic.calculated:
        return DeterministicExplanation(
            RECORDED_SALE_EXPLANATION,
            {
                'sale_label': safe_label,
                'status': 'Needs review',
                'credited_units': _units(sale.unit_credit),
            },
            f'{safe_label} currently needs review, so StewLog does not have a '
            'complete commission result to explain. Check the recorded sale '
            'information and the pay plan active on its sale date.',
        )
    facts = {
        'sale_label': safe_label,
        'credited_units': _units(diagnostic.unit_credit),
        'front_end_commission': _money(diagnostic.frontend_commission),
        'back_end_commission': _money(diagnostic.backend_commission),
        'per_sale_bonus': _money(diagnostic.bonus_commission),
        'total_commission': _money(diagnostic.total_commission),
    }
    answer = (
        f'{safe_label} received {facts["credited_units"]} credited units. '
        f'StewLog calculated {facts["front_end_commission"]} in front-end '
        f'commission, {facts["back_end_commission"]} in back-end commission, '
        f'and {facts["per_sale_bonus"]} in per-sale bonuses, for a total of '
        f'{facts["total_commission"]}. Monthly bonuses are calculated separately '
        'from credited units for the month.'
    )
    return DeterministicExplanation(RECORDED_SALE_EXPLANATION, facts, answer)


def _bonus_explanation(user) -> DeterministicExplanation:
    context = _month_context(user)
    bonus = context['commission_diagnostics']['unit_bonus']
    next_tier = bonus.get('next_tier')
    facts = {
        'month': context['selected_month'].strftime('%B %Y'),
        'credited_units': _units(bonus.get('units')),
        'bonus_units': _units(bonus.get('bonus_units', bonus.get('units'))),
        'bonus_earned': _money(bonus.get('amount')),
        'units_to_next_tier': (
            _units(bonus.get('units_needed'))
            if bonus.get('units_needed') is not None else None
        ),
        'next_tier_amount': (
            _money(next_tier.get('amount'))
            if next_tier and next_tier.get('amount') not in (None, '') else None
        ),
        'eligibility_confirmation_required': bool(
            bonus.get('qualification_pending')
        ),
    }
    if facts['eligibility_confirmation_required']:
        progress = (
            'Eligibility information is still required before StewLog can '
            'confirm progress to the next bonus tier.'
        )
    elif facts['units_to_next_tier'] is not None:
        progress = f'{facts["units_to_next_tier"]} more credited units are needed for the next tier.'
    elif next_tier is None and bonus.get('current_tier'):
        progress = 'You have reached the highest configured bonus tier.'
    else:
        progress = 'No next monthly unit-bonus tier is currently configured.'
    answer = (
        f'For {facts["month"]}, you have {facts["credited_units"]} credited '
        f'units and {facts["bonus_earned"]} in monthly unit bonuses. {progress}'
    )
    return DeterministicExplanation(BONUS_PROGRESS, facts, answer)


def _boolean_status(value, *, true_label='Met', false_label='Not met'):
    if value is True:
        return true_label
    if value is False:
        return false_label
    return 'Missing'


def _eligibility_explanation(user) -> DeterministicExplanation:
    month_start = timezone.localdate().replace(day=1)
    requirements = PlanRequirementService.get_for_user(user)
    eligibility = PayPlanEligibility.objects.filter(
        user=user,
        month_start=month_start,
    ).first()
    labels = {
        'nps': 'NPS finance eligibility',
        'nps_bonus': 'NPS survey bonus information',
        'ar': 'appointment ratio',
        'green_pea': 'new-hire program status',
        'training': 'training requirements',
        'calls': 'call requirements',
        'video': 'video requirements',
        'holiday': 'Holiday Bonus Fund eligibility',
    }
    required = [key for key in labels if requirements.get(key)]
    if not required:
        return DeterministicExplanation(
            ELIGIBILITY_EXPLANATION,
            {'month': month_start.strftime('%B %Y'), 'requirements': []},
            'Your active pay plan does not currently list monthly eligibility '
            'information that needs to be confirmed.',
        )
    statuses = {}
    if eligibility is None:
        statuses = {key: 'Missing' for key in required}
    else:
        values = {
            'nps': eligibility.get_nps_status_display(),
            'nps_bonus': (
                f'{eligibility.nps_qualifying_surveys} qualifying and '
                f'{eligibility.nps_low_score_surveys} low-score surveys'
            ),
            'ar': _boolean_status(eligibility.ar_requirement_met),
            'green_pea': _boolean_status(
                eligibility.green_pea,
                true_label='Applies',
                false_label='Does not apply',
            ),
            'training': _boolean_status(eligibility.training_requirements_met),
            'calls': _boolean_status(eligibility.call_requirement_met),
            'video': _boolean_status(eligibility.video_requirement_met),
            'holiday': (
                'Forfeited'
                if eligibility.holiday_bonus_forfeited else _boolean_status(
                    eligibility.holiday_bonus_eligible,
                    true_label='Eligible',
                    false_label='Not eligible',
                )
            ),
        }
        statuses = {key: values[key] for key in required}
    missing = [labels[key] for key, value in statuses.items() if value in {'Missing', 'Pending or unknown'}]
    facts = {
        'month': month_start.strftime('%B %Y'),
        'requirements': [
            {'name': labels[key], 'status': statuses[key]} for key in required
        ],
        'missing_information': missing,
    }
    status_text = '; '.join(
        f'{labels[key]}: {statuses[key]}' for key in required
    )
    if missing:
        ending = 'Missing information: ' + ', '.join(missing) + '.'
    else:
        ending = 'All listed monthly eligibility information has been recorded.'
    answer = f'For {facts["month"]}, StewLog shows {status_text}. {ending}'
    return DeterministicExplanation(ELIGIBILITY_EXPLANATION, facts, answer)


EXPLANATION_BUILDERS = {
    ACTIVE_PLAN_EXPLANATION: lambda user, question: _active_plan_explanation(user),
    RECORDED_SALE_EXPLANATION: _sale_explanation,
    CURRENT_MONTH_SUMMARY: lambda user, question: _current_month_explanation(user),
    BONUS_PROGRESS: lambda user, question: _bonus_explanation(user),
    ELIGIBILITY_EXPLANATION: lambda user, question: _eligibility_explanation(user),
}


def _provider_notice(status):
    if status == 'used':
        return ''
    if status == 'duplicate_submission':
        return (
            'This question was already processed. No duplicate AI request '
            'was sent.'
        )
    if status == 'rate_limited':
        return (
            'The daily AI explanation limit has been reached. This answer comes '
            'directly from StewLog’s verified calculations.'
        )
    return (
        'AI wording is temporarily unavailable. This answer comes directly '
        'from StewLog’s verified calculations.'
    )


class AskStewService:
    @classmethod
    def answer(cls, user, question, *, submission_token='') -> AskStewAnswer:
        decision = classify_ask_stew_question(question)
        if not decision.allowed:
            return AskStewAnswer(
                decision.category,
                decision.response,
                'not_requested',
            )
        explanation = EXPLANATION_BUILDERS[decision.category](user, question)
        if explanation.intent == CLARIFICATION:
            return AskStewAnswer(
                CLARIFICATION,
                explanation.answer,
                'not_requested',
            )
        gateway = configured_ask_stew_gateway(
            user,
            submission_token=submission_token,
        )
        result = gateway.explain(
            question=question,
            intent=explanation.intent,
            facts=explanation.facts,
            deterministic_explanation=explanation.answer,
        )
        return AskStewAnswer(
            explanation.intent,
            result.answer,
            result.status,
            result.provider_used,
            _provider_notice(result.status),
        )
