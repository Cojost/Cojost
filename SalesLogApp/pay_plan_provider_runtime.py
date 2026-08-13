from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import PayPlanAssistantUsageEvent


@dataclass(frozen=True)
class ProviderAuthorization:
    allowed: bool
    status: str
    event_id: int | None = None


def stable_rollout_eligible(user, configuration):
    if not configuration.ready or user is None or not user.is_authenticated:
        return False
    # Zero percent is the rollout kill switch, including when an allowlist is
    # still present in deployment configuration.
    if configuration.rollout_percent == 0:
        return False
    if configuration.allowed_user_ids:
        return user.pk in configuration.allowed_user_ids
    if configuration.rollout_percent == 100:
        return True
    digest = hashlib.sha256(
        f'stewlog-pay-plan-assistant-v1:{user.pk}'.encode('utf-8')
    ).digest()
    bucket = int.from_bytes(digest[:8], 'big') % 100
    return bucket < configuration.rollout_percent


def privacy_safe_identifier(user):
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        f'pay-plan-assistant-user:{user.pk}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def conversation_reference(conversation_key):
    if not conversation_key:
        return ''
    return hashlib.sha256(str(conversation_key).encode('utf-8')).hexdigest()


def duration_bucket(duration_ms):
    if duration_ms < 250:
        return 'under_250ms'
    if duration_ms < 1000:
        return '250_999ms'
    if duration_ms < 3000:
        return '1_3s'
    if duration_ms < 10000:
        return '3_10s'
    return 'over_10s'


class ProviderUsageRecorder:
    """Records allowlisted operational metadata and atomically reserves quota."""

    STATUS_MAP = {
        'used': PayPlanAssistantUsageEvent.SUCCESS,
        'success': PayPlanAssistantUsageEvent.SUCCESS,
        'provider_timeout': PayPlanAssistantUsageEvent.TIMEOUT,
        'provider_refusal': PayPlanAssistantUsageEvent.REFUSAL,
        'provider_unavailable': PayPlanAssistantUsageEvent.UNAVAILABLE,
        'unavailable': PayPlanAssistantUsageEvent.UNAVAILABLE,
        'invalid_provider_output': PayPlanAssistantUsageEvent.INVALID_OUTPUT,
        'rate_limited': PayPlanAssistantUsageEvent.RATE_LIMITED,
        'disabled': PayPlanAssistantUsageEvent.DISABLED,
        'rollout_excluded': PayPlanAssistantUsageEvent.ROLLOUT_EXCLUDED,
        'missing_credentials': PayPlanAssistantUsageEvent.CONFIGURATION_ERROR,
        'unsupported_provider': PayPlanAssistantUsageEvent.CONFIGURATION_ERROR,
        'invalid_configuration': PayPlanAssistantUsageEvent.CONFIGURATION_ERROR,
        'configuration_error': PayPlanAssistantUsageEvent.CONFIGURATION_ERROR,
        'not_needed': PayPlanAssistantUsageEvent.SUCCESS,
    }

    def __init__(
        self,
        user,
        *,
        conversation_key='',
        model_name='',
        prevent_duplicate_reference=False,
    ):
        self.user = user
        self.conversation_ref = conversation_reference(conversation_key)
        self.model_name = (model_name or '')[:100]
        self.prevent_duplicate_reference = prevent_duplicate_reference

    @staticmethod
    def _day_start():
        now = timezone.localtime()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _reserve_provider_attempt(self, configuration):
        # AuthenticationMiddleware exposes request.user through a
        # SimpleLazyObject. Lock the configured user model rather than the
        # proxy's Python type so the production request path is supported.
        get_user_model().objects.select_for_update().get(pk=self.user.pk)
        if self.prevent_duplicate_reference and self.conversation_ref:
            duplicate_exists = PayPlanAssistantUsageEvent.objects.filter(
                user=self.user,
                route=PayPlanAssistantUsageEvent.PROVIDER,
                conversation_ref=self.conversation_ref,
            ).exists()
            if duplicate_exists:
                return ProviderAuthorization(False, 'duplicate_submission')
        attempts = PayPlanAssistantUsageEvent.objects.filter(
            user=self.user,
            route=PayPlanAssistantUsageEvent.PROVIDER,
            created_at__gte=self._day_start(),
        ).count()
        if attempts >= configuration.daily_request_limit:
            return ProviderAuthorization(False, 'rate_limited')
        event = PayPlanAssistantUsageEvent.objects.create(
            user=self.user,
            route=PayPlanAssistantUsageEvent.PROVIDER,
            status=PayPlanAssistantUsageEvent.UNAVAILABLE,
            duration_ms=0,
            duration_bucket=duration_bucket(0),
            model_name=self.model_name,
            conversation_ref=self.conversation_ref,
        )
        return ProviderAuthorization(True, 'authorized', event.pk)

    @transaction.atomic
    def authorize_provider_attempt(self, configuration):
        if not stable_rollout_eligible(self.user, configuration):
            return ProviderAuthorization(False, 'rollout_excluded')
        return self._reserve_provider_attempt(configuration)

    @transaction.atomic
    def authorize_ask_stew_attempt(self, configuration):
        """Reserve quota only after the centralized CX-3 entitlement passes."""

        from .ask_stew_entitlements import ask_stew_ai_authorized

        if not configuration.ready:
            return ProviderAuthorization(False, 'configuration_error')
        if not ask_stew_ai_authorized(self.user):
            return ProviderAuthorization(False, 'rollout_excluded')
        return self._reserve_provider_attempt(configuration)

    def record_deterministic(self, status, duration_ms):
        PayPlanAssistantUsageEvent.objects.create(
            user=self.user,
            route=PayPlanAssistantUsageEvent.DETERMINISTIC,
            status=self.STATUS_MAP.get(
                status, PayPlanAssistantUsageEvent.UNAVAILABLE,
            ),
            duration_ms=max(0, int(duration_ms)),
            duration_bucket=duration_bucket(duration_ms),
            model_name=self.model_name,
            conversation_ref=self.conversation_ref,
        )

    def finalize_provider_attempt(self, event_id, status, duration_ms, metadata=None):
        metadata = metadata or {}
        PayPlanAssistantUsageEvent.objects.filter(
            pk=event_id,
            user=self.user,
            route=PayPlanAssistantUsageEvent.PROVIDER,
        ).update(
            status=self.STATUS_MAP.get(
                status, PayPlanAssistantUsageEvent.UNAVAILABLE,
            ),
            duration_ms=max(0, int(duration_ms)),
            duration_bucket=duration_bucket(duration_ms),
            input_tokens=_safe_nonnegative_int(metadata.get('input_tokens')),
            output_tokens=_safe_nonnegative_int(metadata.get('output_tokens')),
            provider_request_id=str(metadata.get('request_id') or '')[:100],
        )


def _safe_nonnegative_int(value):
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
