from __future__ import annotations

from dataclasses import dataclass
import re

from django.conf import settings


SUPPORTED_PROVIDERS = {'openai'}
MODEL_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$')


@dataclass(frozen=True)
class ProviderConfiguration:
    state: str
    provider: str
    model: str
    timeout_seconds: int | None
    rollout_percent: int | None
    allowed_user_ids: tuple[int, ...]
    daily_request_limit: int | None
    max_input_chars: int | None
    max_response_bytes: int | None
    max_output_tokens: int | None
    errors: tuple[str, ...] = ()

    @property
    def ready(self):
        return self.state == 'ready'

    @property
    def external_requests_enabled(self):
        return bool(settings.PAY_PLAN_ASSISTANT_PROVIDER_ENABLED and self.ready)


def _bounded_integer(name, value, minimum, maximum, errors):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f'{name} must be an integer.')
        return None
    if parsed < minimum or parsed > maximum:
        errors.append(f'{name} must be between {minimum} and {maximum}.')
        return None
    return parsed


def load_provider_configuration(*, credentials_available):
    provider = str(settings.PAY_PLAN_ASSISTANT_PROVIDER or '').strip().lower()
    model = str(settings.PAY_PLAN_ASSISTANT_MODEL or '').strip()
    if not settings.PAY_PLAN_ASSISTANT_PROVIDER_ENABLED:
        return ProviderConfiguration(
            state='disabled',
            provider=provider,
            model=model,
            timeout_seconds=None,
            rollout_percent=None,
            allowed_user_ids=(),
            daily_request_limit=None,
            max_input_chars=None,
            max_response_bytes=None,
            max_output_tokens=None,
        )
    if provider not in SUPPORTED_PROVIDERS:
        return ProviderConfiguration(
            state='unsupported_provider',
            provider=provider,
            model=model,
            timeout_seconds=None,
            rollout_percent=None,
            allowed_user_ids=(),
            daily_request_limit=None,
            max_input_chars=None,
            max_response_bytes=None,
            max_output_tokens=None,
            errors=('The configured provider is not supported.',),
        )

    errors = []
    if not MODEL_PATTERN.fullmatch(model):
        errors.append('PAY_PLAN_ASSISTANT_MODEL is invalid.')
    timeout = _bounded_integer(
        'PAY_PLAN_ASSISTANT_TIMEOUT_SECONDS',
        settings.PAY_PLAN_ASSISTANT_TIMEOUT_SECONDS,
        1,
        60,
        errors,
    )
    rollout = _bounded_integer(
        'PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT',
        settings.PAY_PLAN_ASSISTANT_ROLLOUT_PERCENT,
        0,
        100,
        errors,
    )
    daily_limit = _bounded_integer(
        'PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT',
        settings.PAY_PLAN_ASSISTANT_DAILY_REQUEST_LIMIT,
        1,
        1000,
        errors,
    )
    max_input_chars = _bounded_integer(
        'PAY_PLAN_ASSISTANT_MAX_PROVIDER_INPUT_CHARS',
        settings.PAY_PLAN_ASSISTANT_MAX_PROVIDER_INPUT_CHARS,
        1000,
        20000,
        errors,
    )
    max_response_bytes = _bounded_integer(
        'PAY_PLAN_ASSISTANT_MAX_PROVIDER_RESPONSE_BYTES',
        settings.PAY_PLAN_ASSISTANT_MAX_PROVIDER_RESPONSE_BYTES,
        1024,
        1048576,
        errors,
    )
    max_output_tokens = _bounded_integer(
        'PAY_PLAN_ASSISTANT_MAX_OUTPUT_TOKENS',
        settings.PAY_PLAN_ASSISTANT_MAX_OUTPUT_TOKENS,
        64,
        4000,
        errors,
    )
    allowed_user_ids = []
    for raw_user_id in settings.PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS:
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            errors.append('PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS must contain integers.')
            continue
        if user_id <= 0:
            errors.append('PAY_PLAN_ASSISTANT_ALLOWED_USER_IDS must contain positive IDs.')
            continue
        allowed_user_ids.append(user_id)
    if errors:
        state = 'invalid_configuration'
    elif not credentials_available:
        state = 'missing_credentials'
    else:
        state = 'ready'
    return ProviderConfiguration(
        state=state,
        provider=provider,
        model=model,
        timeout_seconds=timeout,
        rollout_percent=rollout,
        allowed_user_ids=tuple(sorted(set(allowed_user_ids))),
        daily_request_limit=daily_limit,
        max_input_chars=max_input_chars,
        max_response_bytes=max_response_bytes,
        max_output_tokens=max_output_tokens,
        errors=tuple(errors),
    )
