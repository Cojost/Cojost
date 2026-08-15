from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from allauth.account.adapter import DefaultAccountAdapter
from django.urls import reverse


TEAM_INVITATION_RESUME_SESSION_KEY = 'team_invitation_verification_resume'


class StewLogAccountAdapter(DefaultAccountAdapter):
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
