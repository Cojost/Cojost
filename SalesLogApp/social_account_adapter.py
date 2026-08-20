from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


EXISTING_EMAIL_MESSAGE = (
    'A STEW Log account already uses that email. Sign in with your existing '
    'username and password, verify your email if needed, then connect Google '
    'from Profile.'
)
UNVERIFIED_GOOGLE_EMAIL_MESSAGE = (
    'Google did not provide a verified email address. Choose another Google '
    'account or use STEW Log email and password sign-in.'
)


class StewLogSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Fail-closed Google rollout and safe existing-account matching."""

    def get_provider(self, request, provider, client_id=None):
        if provider == 'google' and not settings.GOOGLE_LOGIN_ENABLED:
            raise PermissionDenied('Google sign-in is not enabled.')
        return super().get_provider(request, provider, client_id=client_id)

    def list_apps(self, request, provider=None, client_id=None):
        apps = super().list_apps(
            request,
            provider=provider,
            client_id=client_id,
        )
        if provider not in {None, 'google'}:
            return apps

        non_google_apps = [app for app in apps if app.provider != 'google']
        if not settings.GOOGLE_LOGIN_ENABLED:
            return non_google_apps

        # Google credentials have one source of truth: environment-backed
        # settings. Ignore legacy database rows to avoid MultipleObjectsReturned
        # and accidental use of an old client secret.
        settings_google_apps = [
            app
            for app in apps
            if app.provider == 'google' and app.pk is None
        ]
        return non_google_apps + settings_google_apps

    def authenticate_by_email(self, sociallogin):
        match = super().authenticate_by_email(sociallogin)
        if match is None:
            return None

        user, email = match
        verified_user_ids = set(
            EmailAddress.objects.filter(
                email__iexact=email,
                verified=True,
            ).values_list('user_id', flat=True)
        )
        if verified_user_ids == {user.pk}:
            return user, email
        return None

    def pre_social_login(self, request, sociallogin):
        super().pre_social_login(request, sociallogin)
        if sociallogin.account.provider != 'google' or sociallogin.is_existing:
            return

        verified_emails = {
            address.email
            for address in sociallogin.email_addresses
            if address.verified and address.email
        }
        if not verified_emails:
            messages.error(request, UNVERIFIED_GOOGLE_EMAIL_MESSAGE)
            raise ImmediateHttpResponse(redirect('account_login'))

        User = get_user_model()
        email_collision = any(
            EmailAddress.objects.filter(email__iexact=email).exists()
            or User.objects.filter(email__iexact=email).exists()
            for email in verified_emails
        )
        if not email_collision:
            return

        messages.error(request, EXISTING_EMAIL_MESSAGE)
        raise ImmediateHttpResponse(redirect('account_login'))
