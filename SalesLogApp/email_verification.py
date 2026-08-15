from dataclasses import dataclass
from datetime import timedelta
from email import policy
from email.parser import Parser
import hashlib
import hmac
import ipaddress
import math
from urllib.parse import urlsplit

from allauth.account.internal.flows.email_verification import (
    send_verification_email_to_address,
)
from allauth.account.models import EmailAddress
from allauth.core.context import request_context
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.mail import get_connection
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend
from django.core.exceptions import ValidationError
from django.core.validators import DomainNameValidator, validate_email
from django.db import IntegrityError, transaction
from django.test.client import RequestFactory
from django.utils import timezone

from .models import EmailVerificationDispatch


ALREADY_VERIFIED = 'already_verified'
UNVERIFIED_ADDRESS = 'unverified_address'
MISSING_ADDRESS = 'missing_address'
INVALID_EMAIL = 'invalid_email'
OWNERSHIP_CONFLICT = 'ownership_conflict'
COOLDOWN = 'cooldown'
INACTIVE = 'inactive'

SUPPORTED_PRODUCTION_EMAIL_BACKEND = (
    'django.core.mail.backends.smtp.EmailBackend'
)
LOCAL_ONLY_DOMAIN_SUFFIXES = (
    '.home.arpa',
    '.internal',
    '.invalid',
    '.lan',
    '.local',
    '.localdomain',
    '.localhost',
    '.onion',
    '.test',
)
RESERVED_PUBLIC_TEST_DOMAINS = (
    'example.com',
    'example.net',
    'example.org',
)


class ProductionEmailConfigurationError(Exception):
    """Raised without including secret or recipient configuration values."""


def _configuration_error():
    return ProductionEmailConfigurationError(
        'Production email delivery configuration is invalid.'
    )


def _public_hostname(value):
    candidate = str(value or '').strip().casefold()
    if candidate.startswith('[') and candidate.endswith(']'):
        candidate = candidate[1:-1]
    candidate = candidate.rstrip('.')
    if not candidate:
        raise _configuration_error()

    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        # This workflow deliberately requires a public DNS hostname rather
        # than attempting to classify or connect to an IP literal.
        raise _configuration_error()

    try:
        DomainNameValidator()(candidate)
    except ValidationError:
        raise _configuration_error() from None

    if '.' not in candidate:
        raise _configuration_error()
    if candidate == 'localhost' or candidate.endswith(LOCAL_ONLY_DOMAIN_SUFFIXES):
        raise _configuration_error()
    if any(
        candidate == domain or candidate.endswith(f'.{domain}')
        for domain in RESERVED_PUBLIC_TEST_DOMAINS
    ):
        raise _configuration_error()
    return candidate


def _validated_sender_address():
    raw_sender = str(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip()
    if not raw_sender or '\r' in raw_sender or '\n' in raw_sender:
        raise _configuration_error()

    header = Parser(policy=policy.default).parsestr(
        f'From: {raw_sender}\n\n'
    )['From']
    if (
        header is None
        or header.defects
        or len(header.addresses) != 1
        or len(header.groups) != 1
        or header.groups[0].display_name is not None
    ):
        raise _configuration_error()
    parsed_address = header.addresses[0]
    address = parsed_address.addr_spec
    if not address or not parsed_address.domain:
        raise _configuration_error()
    try:
        validate_email(address)
    except ValidationError:
        raise _configuration_error() from None
    _public_hostname(parsed_address.domain)
    return address


def _validate_smtp_configuration():
    _public_hostname(getattr(settings, 'EMAIL_HOST', ''))

    port = getattr(settings, 'EMAIL_PORT', None)
    if isinstance(port, bool):
        raise _configuration_error()
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise _configuration_error() from None
    if not 1 <= port <= 65535:
        raise _configuration_error()

    use_tls = getattr(settings, 'EMAIL_USE_TLS', False)
    use_ssl = getattr(settings, 'EMAIL_USE_SSL', False)
    if not isinstance(use_tls, bool) or not isinstance(use_ssl, bool):
        raise _configuration_error()
    if use_tls == use_ssl:
        # Production SMTP must use exactly one supported encrypted transport.
        raise _configuration_error()

    timeout = getattr(settings, 'EMAIL_TIMEOUT', None)
    if isinstance(timeout, bool):
        raise _configuration_error()
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise _configuration_error() from None
    stale_seconds = (
        settings.EMAIL_VERIFICATION_PENDING_STALE_MINUTES * 60
    )
    if not math.isfinite(timeout) or timeout <= 0 or timeout >= stale_seconds:
        raise _configuration_error()


def validate_production_email_delivery_configuration():
    """Fail closed before production verification delivery can mutate data."""
    if settings.DEBUG:
        return
    if settings.EMAIL_VERIFICATION_PUBLIC_BASE_URL != 'https://stewlog.com':
        raise _configuration_error()
    allowed_hosts = {
        str(host).strip().casefold().rstrip('.')
        for host in settings.ALLOWED_HOSTS
    }
    if 'stewlog.com' not in allowed_hosts:
        raise _configuration_error()

    _validated_sender_address()
    try:
        connection = get_connection(
            backend=settings.EMAIL_BACKEND,
            fail_silently=False,
        )
    except Exception:
        raise _configuration_error() from None
    if (
        settings.EMAIL_BACKEND != SUPPORTED_PRODUCTION_EMAIL_BACKEND
        or type(connection) is not SMTPEmailBackend
    ):
        raise _configuration_error()
    _validate_smtp_configuration()


@dataclass(frozen=True)
class VerificationAssessment:
    category: str
    eligible: bool
    canonical_email: str = ''
    address_id: int | None = None
    skip_reason: str = ''


@dataclass(frozen=True)
class VerificationDispatchResult:
    outcome: str
    category: str
    repaired_address: bool = False


def _cooldown_cutoff():
    return timezone.now() - timedelta(
        minutes=settings.EMAIL_VERIFICATION_RESEND_COOLDOWN_MINUTES,
    )


def _pending_stale_cutoff():
    return timezone.now() - timedelta(
        minutes=settings.EMAIL_VERIFICATION_PENDING_STALE_MINUTES,
    )


def _recipient_digest(email):
    normalized = email.strip().casefold().encode('utf-8')
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        normalized,
        hashlib.sha256,
    ).hexdigest()


def has_verified_email(user):
    return EmailAddress.objects.filter(user=user, verified=True).exists()


def assess_verification_user(user, *, check_cooldown=True):
    if not user.is_active:
        return VerificationAssessment(INACTIVE, False, skip_reason=INACTIVE)
    if has_verified_email(user):
        return VerificationAssessment(
            ALREADY_VERIFIED,
            False,
            skip_reason=ALREADY_VERIFIED,
        )

    canonical_email = (user.email or '').strip()
    try:
        validate_email(canonical_email)
    except ValidationError:
        return VerificationAssessment(
            INVALID_EMAIL,
            False,
            skip_reason=INVALID_EMAIL,
        )

    user_model = get_user_model()
    conflicting_user = user_model.objects.filter(
        email__iexact=canonical_email,
    ).exclude(pk=user.pk).exists()
    conflicting_address = EmailAddress.objects.filter(
        email__iexact=canonical_email,
    ).exclude(user_id=user.pk).exists()
    matching_addresses = list(
        EmailAddress.objects.filter(
            user_id=user.pk,
            email__iexact=canonical_email,
        ).order_by('pk')[:2]
    )
    if conflicting_user or conflicting_address or len(matching_addresses) > 1:
        return VerificationAssessment(
            OWNERSHIP_CONFLICT,
            False,
            skip_reason=OWNERSHIP_CONFLICT,
        )

    category = UNVERIFIED_ADDRESS if matching_addresses else MISSING_ADDRESS
    address_id = matching_addresses[0].pk if matching_addresses else None
    digest = _recipient_digest(canonical_email)
    recent_completed = EmailVerificationDispatch.objects.filter(
        recipient_digest=digest,
        status__in=(
            EmailVerificationDispatch.SENT,
            EmailVerificationDispatch.SKIPPED,
        ),
        attempted_at__gte=_cooldown_cutoff(),
    ).exists()
    fresh_pending = EmailVerificationDispatch.objects.filter(
        recipient_digest=digest,
        status=EmailVerificationDispatch.PENDING,
        attempted_at__gte=_pending_stale_cutoff(),
    ).exists()
    if check_cooldown and (recent_completed or fresh_pending):
        return VerificationAssessment(
            category,
            False,
            canonical_email=canonical_email,
            address_id=address_id,
            skip_reason=COOLDOWN,
        )
    return VerificationAssessment(
        category,
        True,
        canonical_email=canonical_email,
        address_id=address_id,
    )


def build_verification_request(*, user, base_url, session_values=None):
    parsed = urlsplit(base_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('The email verification public base URL is invalid.')
    if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise ValueError('The email verification public base URL must not include a path.')
    request = RequestFactory().get(
        '/',
        secure=parsed.scheme == 'https',
        HTTP_HOST=parsed.netloc,
    )
    request.user = user
    request.session = dict(session_values or {})
    request._messages = FallbackStorage(request)
    return request


def _reserve_dispatch(user_id, source):
    user_model = get_user_model()
    with transaction.atomic():
        user = user_model.objects.select_for_update().get(pk=user_id)
        assessment = assess_verification_user(user)
        if not assessment.eligible:
            return None, assessment, False

        digest = _recipient_digest(assessment.canonical_email)
        dispatch = (
            EmailVerificationDispatch.objects.select_for_update()
            .filter(recipient_digest=digest)
            .first()
        )
        if dispatch and dispatch.user_id != user.pk:
            return None, VerificationAssessment(
                OWNERSHIP_CONFLICT,
                False,
                skip_reason=OWNERSHIP_CONFLICT,
            ), False

        repaired_address = False
        address = None
        if assessment.address_id is not None:
            address = EmailAddress.objects.get(pk=assessment.address_id)
        else:
            primary = not EmailAddress.objects.filter(
                user_id=user.pk,
                primary=True,
            ).exists()
            address = EmailAddress.objects.create(
                user=user,
                email=assessment.canonical_email,
                primary=primary,
                verified=False,
            )
            repaired_address = True

        if dispatch:
            dispatch.source = source
            dispatch.status = EmailVerificationDispatch.PENDING
            dispatch.completed_at = None
            dispatch.save(update_fields=[
                'source',
                'status',
                'attempted_at',
                'completed_at',
            ])
        else:
            dispatch = EmailVerificationDispatch.objects.create(
                user=user,
                recipient_digest=digest,
                source=source,
                status=EmailVerificationDispatch.PENDING,
            )
        return (dispatch, address), assessment, repaired_address


def dispatch_verification_email(*, user, request, source):
    validate_production_email_delivery_configuration()
    try:
        reservation, assessment, repaired_address = _reserve_dispatch(
            user.pk,
            source,
        )
    except IntegrityError:
        current = assess_verification_user(user)
        if not current.eligible:
            return VerificationDispatchResult('skipped', current.category)
        return VerificationDispatchResult('failed', current.category)

    if reservation is None:
        return VerificationDispatchResult('skipped', assessment.category)
    dispatch, address = reservation

    try:
        with request_context(request):
            sent = send_verification_email_to_address(
                request,
                address,
                signup=False,
            )
    except Exception:
        _finalize_dispatch(
            dispatch,
            status=EmailVerificationDispatch.FAILED,
        )
        return VerificationDispatchResult(
            'failed',
            assessment.category,
            repaired_address,
        )

    status = (
        EmailVerificationDispatch.SENT
        if sent
        else EmailVerificationDispatch.SKIPPED
    )
    finalized = _finalize_dispatch(dispatch, status=status)
    if not finalized:
        return VerificationDispatchResult(
            'failed',
            assessment.category,
            repaired_address,
        )
    return VerificationDispatchResult(
        'sent' if sent else 'skipped',
        assessment.category,
        repaired_address,
    )


def _finalize_dispatch(dispatch, *, status):
    """Finalize only the reservation generation owned by this worker."""
    return EmailVerificationDispatch.objects.filter(
        pk=dispatch.pk,
        status=EmailVerificationDispatch.PENDING,
        attempted_at=dispatch.attempted_at,
    ).update(
        status=status,
        completed_at=timezone.now(),
    ) == 1
