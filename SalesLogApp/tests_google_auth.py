import os
from pathlib import Path
import runpy
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.core.context import request_context
from allauth.socialaccount.internal.flows.login import complete_login
from allauth.socialaccount.models import SocialAccount, SocialApp
from allauth.socialaccount.providers.google.provider import GoogleProvider
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .social_account_adapter import (
    EXISTING_EMAIL_MESSAGE,
    UNVERIFIED_GOOGLE_EMAIL_MESSAGE,
)


GOOGLE_PROVIDER_SETTINGS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
        'OAUTH_PKCE_ENABLED': True,
        'EMAIL_AUTHENTICATION': True,
        'APPS': [
            {
                'name': 'STEW Log Google Sign-In',
                'client_id': 'unit-test-client.apps.googleusercontent.com',
                'secret': 'unit-test-secret',
                'key': '',
            },
        ],
    },
}

GOOGLE_ENABLED_SETTINGS = {
    'GOOGLE_LOGIN_ENABLED': True,
    'GOOGLE_OAUTH_CLIENT_ID': 'unit-test-client.apps.googleusercontent.com',
    'GOOGLE_OAUTH_CLIENT_SECRET': 'unit-test-secret',
    'SOCIALACCOUNT_PROVIDERS': GOOGLE_PROVIDER_SETTINGS,
    'SOCIALACCOUNT_LOGIN_ON_GET': False,
    'SOCIALACCOUNT_STORE_TOKENS': False,
    'SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT': True,
}


class GoogleSettingsValidationTests(SimpleTestCase):
    settings_path = (
        Path(__file__).resolve().parents[1] / 'SalesLog' / 'settings.py'
    )

    def test_enabled_rollout_requires_both_credentials(self):
        with patch.dict(os.environ, {
            'DEBUG': 'true',
            'GOOGLE_LOGIN_ENABLED': 'true',
        }, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                runpy.run_path(str(self.settings_path))

    def test_complete_environment_builds_one_settings_backed_google_app(self):
        with patch.dict(os.environ, {
            'DEBUG': 'true',
            'GOOGLE_LOGIN_ENABLED': 'true',
            'GOOGLE_OAUTH_CLIENT_ID': 'configured-client',
            'GOOGLE_OAUTH_CLIENT_SECRET': 'configured-secret',
        }, clear=True):
            configured = runpy.run_path(str(self.settings_path))

        google = configured['SOCIALACCOUNT_PROVIDERS']['google']
        self.assertEqual(len(google['APPS']), 1)
        self.assertEqual(google['APPS'][0]['client_id'], 'configured-client')
        self.assertEqual(google['APPS'][0]['secret'], 'configured-secret')
        self.assertTrue(google['OAUTH_PKCE_ENABLED'])
        self.assertTrue(google['EMAIL_AUTHENTICATION'])


class GoogleRolloutGateTests(TestCase):
    def test_database_social_app_cannot_bypass_disabled_rollout(self):
        SocialApp.objects.create(
            provider='google',
            name='Stale database client',
            client_id='stale-client.apps.googleusercontent.com',
            secret='stale-secret',
        )

        login = self.client.get(reverse('account_login'))
        signup = self.client.get(reverse('account_signup'))

        self.assertEqual(login.status_code, 200)
        self.assertEqual(signup.status_code, 200)
        self.assertNotContains(login, 'Continue with Google')
        self.assertNotContains(signup, 'Continue with Google')

    def test_profile_hides_google_management_while_disabled(self):
        user = get_user_model().objects.create_user(
            username='disabled-owner',
            email='disabled@example.com',
            password='test-password',
        )
        self.client.force_login(user)

        response = self.client.get(reverse('profile'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Manage Google sign-in')

    def test_direct_google_route_is_denied_while_disabled(self):
        self.assertEqual(
            self.client.get(reverse('google_login')).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse('google_login')).status_code,
            403,
        )


@override_settings(**GOOGLE_ENABLED_SETTINGS)
class GoogleProviderUiTests(TestCase):
    def test_login_and_signup_show_post_based_google_button(self):
        for route_name in ('account_login', 'account_signup'):
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Continue with Google')
                self.assertContains(response, 'social-provider-google')
                self.assertContains(response, 'method="post"')
                self.assertContains(
                    response,
                    'action="/accounts/google/login/?process=login"',
                )

    def test_stale_database_app_is_ignored_without_ambiguity(self):
        SocialApp.objects.create(
            provider='google',
            name='Stale database client',
            client_id='stale-client.apps.googleusercontent.com',
            secret='stale-secret',
        )

        response = self.client.get(reverse('account_login'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.content.decode().count('Continue with Google'),
            2,
        )
        self.assertNotContains(response, 'stale-client.apps.googleusercontent.com')

    def test_get_requires_confirmation_but_ui_post_redirects_to_google(self):
        confirmation = self.client.get(reverse('google_login'))
        self.assertEqual(confirmation.status_code, 200)

        response = self.client.post(reverse('google_login'))

        self.assertEqual(response.status_code, 302)
        redirect = urlsplit(response.url)
        query = parse_qs(redirect.query)
        self.assertEqual(redirect.scheme, 'https')
        self.assertEqual(redirect.netloc, 'accounts.google.com')
        self.assertEqual(
            query['client_id'],
            ['unit-test-client.apps.googleusercontent.com'],
        )
        self.assertEqual(
            query['redirect_uri'],
            ['http://testserver/accounts/google/login/callback/'],
        )
        self.assertEqual(set(query['scope'][0].split()), {'profile', 'email'})
        self.assertEqual(query['access_type'], ['online'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertIn('code_challenge', query)
        self.assertIn('state', query)
        self.assertNotIn('unit-test-secret', response.url)

    def test_profile_exposes_account_connection_management(self):
        user = get_user_model().objects.create_user(
            username='enabled-owner',
            email='enabled@example.com',
            password='test-password',
        )
        self.client.force_login(user)

        response = self.client.get(reverse('profile'))

        self.assertContains(response, 'Manage Google sign-in')
        self.assertContains(response, reverse('socialaccount_connections'))


@override_settings(**GOOGLE_ENABLED_SETTINGS)
class GoogleAccountLinkingTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def make_request(self):
        request = self.factory.get(reverse('google_callback'))
        request.user = AnonymousUser()
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda req: None).process_request(request)
        return request

    def make_social_login(self, request, *, uid, email, verified=True):
        app = SocialApp(
            provider='google',
            name='STEW Log Google Sign-In',
            client_id='unit-test-client.apps.googleusercontent.com',
            secret='unit-test-secret',
        )
        provider = GoogleProvider(request=request, app=app)
        sociallogin = provider.sociallogin_from_response(
            request,
            {
                'sub': uid,
                'email': email,
                'email_verified': verified,
                'given_name': 'Google',
                'family_name': 'User',
            },
        )
        sociallogin.state = {'process': 'login'}
        return sociallogin

    def complete(self, request, sociallogin):
        with request_context(request):
            return complete_login(request, sociallogin)

    def test_new_verified_google_identity_creates_one_normal_user(self):
        request = self.make_request()
        sociallogin = self.make_social_login(
            request,
            uid='google-new-user',
            email='new-google-user@example.com',
        )

        response = self.complete(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        users = get_user_model().objects.filter(
            email__iexact='new-google-user@example.com',
        )
        self.assertEqual(users.count(), 1)
        user = users.get()
        self.assertFalse(user.has_usable_password())
        self.assertTrue(
            EmailAddress.objects.filter(
                user=user,
                email__iexact='new-google-user@example.com',
                verified=True,
            ).exists()
        )
        self.assertTrue(
            SocialAccount.objects.filter(
                user=user,
                provider='google',
                uid='google-new-user',
            ).exists()
        )
        self.assertTrue(hasattr(user, 'pay_plan_onboarding'))

    def test_verified_existing_email_reuses_user_and_preserves_password(self):
        User = get_user_model()
        user = User.objects.create_user(
            username='existing-owner',
            email='owner@example.com',
            password='existing-password',
        )
        EmailAddress.objects.create(
            user=user,
            email='owner@example.com',
            verified=True,
            primary=True,
        )
        request = self.make_request()
        sociallogin = self.make_social_login(
            request,
            uid='google-existing-owner',
            email='OWNER@example.com',
        )

        response = self.complete(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.count(), 1)
        user.refresh_from_db()
        self.assertTrue(user.check_password('existing-password'))
        self.assertTrue(
            SocialAccount.objects.filter(
                user=user,
                provider='google',
                uid='google-existing-owner',
            ).exists()
        )

    def test_unverified_existing_email_is_blocked_without_mutation(self):
        User = get_user_model()
        user = User.objects.create_user(
            username='unverified-owner',
            email='unverified@example.com',
            password='existing-password',
        )
        EmailAddress.objects.create(
            user=user,
            email='unverified@example.com',
            verified=False,
            primary=True,
        )
        request = self.make_request()
        sociallogin = self.make_social_login(
            request,
            uid='google-unverified-owner',
            email='unverified@example.com',
        )

        response = self.complete(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('account_login'))
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(SocialAccount.objects.exists())
        user.refresh_from_db()
        self.assertTrue(user.check_password('existing-password'))
        self.assertEqual(
            [str(message) for message in get_messages(request)],
            [EXISTING_EMAIL_MESSAGE],
        )

    def test_google_identity_without_verified_email_is_blocked(self):
        request = self.make_request()
        sociallogin = self.make_social_login(
            request,
            uid='google-unverified-provider-email',
            email='provider-unverified@example.com',
            verified=False,
        )

        response = self.complete(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('account_login'))
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(SocialAccount.objects.exists())
        self.assertEqual(
            [str(message) for message in get_messages(request)],
            [UNVERIFIED_GOOGLE_EMAIL_MESSAGE],
        )
