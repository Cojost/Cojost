from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from allauth.account.adapter import DefaultAccountAdapter
from django.urls import reverse

from .billing_onboarding import (
    billing_onboarding_handoff_name,
    billing_onboarding_redirect_name,
)


TEAM_INVITATION_RESUME_SESSION_KEY = 'team_invitation_verification_resume'


class StewLogAccountAdapter(DefaultAccountAdapter):
    def get_signup_redirect_url(self, request):
        redirect_name = billing_onboarding_redirect_name(request.user)
        if redirect_name:
            return reverse(redirect_name)
        handoff_name = billing_onboarding_handoff_name(request.user)
        if handoff_name:
            return reverse(handoff_name)
        return super().get_signup_redirect_url(request)

    def get_login_redirect_url(self, request):
        redirect_name = billing_onboarding_redirect_name(request.user)
        if redirect_name:
            return reverse(redirect_name)
        handoff_name = billing_onboarding_handoff_name(request.user)
        if handoff_name:
            return reverse(handoff_name)
        return super().get_login_redirect_url(request)

    def get_email_verification_redirect_url(self, email_address):
        redirect_name = billing_onboarding_redirect_name(email_address.user)
        if redirect_name:
            return reverse(redirect_name)
        handoff_name = billing_onboarding_handoff_name(email_address.user)
        if handoff_name:
            return reverse(handoff_name)
        return super().get_email_verification_redirect_url(email_address)

    def get_email_confirmation_url(self, request, emailconfirmation):
        url = super().get_email_confirmation_url(request, emailconfirmation)
        if not request or not getattr(request, 'session', {}).get(
            TEAM_INVITATION_RESUME_SESSION_KEY,
        ):
            return url
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault('next', reverse('team_invitation_accept'))
        return urlunsplit(parsed._replace(query=urlencode(query)))
