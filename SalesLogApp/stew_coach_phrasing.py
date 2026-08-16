"""SC-4 Stew Coach phrasing over allowlisted SC-2/SC-3 facts.

This module never computes new numbers. Every sentence is built only from the
SC-3 presentation context (already rounded copies of frozen SC-2 facts). The
optional AI provider reuses the Ask Stew gateway, which can only confirm the
server-owned allowlisted sentences; it can never generate, alter, or reorder
customer-visible text. Every provider failure falls back to the deterministic
coach message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .ask_stew_provider import (
    FACT_SENTENCE_BOUNDARY,
    MAX_FACT_SELECTIONS,
    configured_ask_stew_gateway,
)
from .stew_coach_presentation import UNAVAILABLE_DISPLAY

PHRASING_VERSION = 'sc4.v1'
COACH_INTENT = 'stew_coach_month_projection'

DUPLICATE_NOTICE = (
    'That coaching request was already processed. No duplicate AI request '
    'was sent.'
)
RATE_LIMITED_NOTICE = (
    'The daily AI limit has been reached. This coaching note comes directly '
    'from StewLog\u2019s verified projections.'
)
UNAVAILABLE_NOTICE = (
    'AI wording is temporarily unavailable. This coaching note comes '
    'directly from StewLog\u2019s verified projections.'
)


class StewCoachPhrasingError(Exception):
    """Raised when a safe allowlisted coach message cannot be built."""


@dataclass(frozen=True)
class StewCoachMessage:
    message: str
    provider_status: str
    provider_used: bool = False
    notice: str = ''


def _period_sentence(presentation: Mapping[str, Any]) -> str:
    month_label = presentation['month_start'].strftime('%B %Y')
    period_status = presentation['period_status']
    total = presentation['total_selling_days']
    if period_status == 'future':
        return f'{month_label} has not started yet.'
    if period_status == 'complete':
        return f'{month_label} is complete after {total} selling days.'
    completed = presentation['completed_selling_days']
    remaining = presentation['remaining_selling_days']
    return (
        f'You have completed {completed} of {total} selling days in '
        f'{month_label}, with {remaining} remaining.'
    )


def _metric_sentences(
    row: Mapping[str, Any],
    period_status: str,
) -> tuple[str, ...]:
    label = row['label']
    status = row['status']
    if status == 'no_goal':
        return (f'No goal is set for {label.lower()} this month.',)
    if status == 'insufficient_data':
        return (f'{label} cannot be projected yet.',)
    if status == 'goal_reached':
        return (
            f'{label} goal reached: {row["actual"]} recorded against a goal '
            f'of {row["goal"]}.',
        )
    if period_status == 'complete':
        return (
            f'{label} finished at {row["actual"]} against a goal of '
            f'{row["goal"]}.',
        )
    if status == 'on_pace':
        return (
            f'{label} on pace: {row["actual"]} recorded so far toward a goal '
            f'of {row["goal"]}, projected to finish at {row["projected"]}.',
        )
    sentences = [
        f'{label} behind pace: {row["actual"]} recorded so far toward a goal '
        f'of {row["goal"]}, projected to finish at {row["projected"]}.'
    ]
    if row['required_pace'] != UNAVAILABLE_DISPLAY:
        sentences.append(
            f'Averaging {row["required_pace"]} per remaining selling day '
            f'reaches the {label.lower()} goal.'
        )
    return tuple(sentences)


def coach_sentences(presentation: Mapping[str, Any]) -> tuple[str, ...]:
    """Allowlisted coach sentences built only from SC-3 presented values."""

    if not isinstance(presentation, Mapping) or not presentation.get(
        'available'
    ):
        raise StewCoachPhrasingError(
            'A coach message requires an available verified projection.'
        )
    sentences: list[str] = [_period_sentence(presentation)]
    for row in presentation['rows']:
        sentences.extend(
            _metric_sentences(row, presentation['period_status'])
        )
    sentences.extend(presentation['diagnostics'])
    if not sentences or len(sentences) > MAX_FACT_SELECTIONS:
        raise StewCoachPhrasingError(
            'The coach message exceeded its allowlisted sentence limit.'
        )
    joined = ' '.join(sentences)
    round_trip = tuple(
        sentence.strip()
        for sentence in FACT_SENTENCE_BOUNDARY.split(joined)
        if sentence.strip()
    )
    if round_trip != tuple(sentences):
        raise StewCoachPhrasingError(
            'The coach message sentences were not provider-safe.'
        )
    return tuple(sentences)


def deterministic_coach_message(presentation: Mapping[str, Any]) -> str:
    """The canonical SC-4 coach message with no provider involved."""

    return ' '.join(coach_sentences(presentation))


def coach_provider_notice(status: str) -> str:
    if status in {'used', 'not_requested'}:
        return ''
    if status == 'duplicate_submission':
        return DUPLICATE_NOTICE
    if status == 'rate_limited':
        return RATE_LIMITED_NOTICE
    return UNAVAILABLE_NOTICE


def phrase_coach_message(
    user,
    presentation: Mapping[str, Any],
    *,
    submission_token: str = '',
    http_client=None,
) -> StewCoachMessage:
    """Phrase the coach message through the bounded Ask Stew gateway.

    The gateway enforces pilot entitlement, provider configuration, the
    atomic daily quota, and submission idempotency. On any failure the
    deterministic message is returned unchanged with an explanatory notice.
    """

    sentences = coach_sentences(presentation)
    deterministic = ' '.join(sentences)
    gateway = configured_ask_stew_gateway(
        user,
        submission_token=submission_token,
        http_client=http_client,
    )
    result = gateway.explain(
        question='',
        intent=COACH_INTENT,
        facts={'phrasing_version': PHRASING_VERSION},
        deterministic_explanation=deterministic,
    )
    return StewCoachMessage(
        result.answer,
        result.status,
        result.provider_used,
        coach_provider_notice(result.status),
    )
