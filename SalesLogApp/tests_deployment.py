import ast
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from allauth.account.internal.flows.email_verification import (
    get_email_verification_url,
)
from allauth.account.internal.flows.password_reset import (
    get_reset_password_from_key_url,
)
from django.conf import settings
from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from SalesLog.settings import env_bool, env_list


class DeploymentConfigurationTests(SimpleTestCase):
    def test_environment_value_helpers(self):
        self.assertFalse(env_bool('SETTING_THAT_IS_NOT_DEFINED', False))
        self.assertEqual(env_list('SETTING_THAT_IS_NOT_DEFINED'), [])

    def test_environment_lists_parse_transition_hosts_and_origins(self):
        with patch.dict(os.environ, {
            'TEST_ALLOWED_HOSTS': (
                'stewlog.com, www.stewlog.com, stewlog.onrender.com'
            ),
            'TEST_TRUSTED_ORIGINS': (
                'https://stewlog.com,https://www.stewlog.com,'
                'https://stewlog.onrender.com'
            ),
        }):
            self.assertEqual(env_list('TEST_ALLOWED_HOSTS'), [
                'stewlog.com',
                'www.stewlog.com',
                'stewlog.onrender.com',
            ])
            self.assertEqual(env_list('TEST_TRUSTED_ORIGINS'), [
                'https://stewlog.com',
                'https://www.stewlog.com',
                'https://stewlog.onrender.com',
            ])

    def test_production_settings_enable_proxy_and_transport_security(self):
        production_environment = {
            'DEBUG': 'false',
            'SECRET_KEY': 'safe-test-only-production-settings-value',
            'ALLOWED_HOSTS': (
                'stewlog.com,www.stewlog.com,stewlog.onrender.com'
            ),
            'CSRF_TRUSTED_ORIGINS': (
                'https://stewlog.com,https://www.stewlog.com,'
                'https://stewlog.onrender.com'
            ),
            'EMAIL_HOST': 'smtp.example.test',
            'DEFAULT_FROM_EMAIL': 'no-reply@mail.stewlog.com',
        }
        settings_path = Path(__file__).resolve().parents[1] / 'SalesLog' / 'settings.py'
        with patch.dict(os.environ, production_environment, clear=True):
            production = runpy.run_path(str(settings_path))

        self.assertEqual(production['ALLOWED_HOSTS'], [
            'stewlog.com',
            'www.stewlog.com',
            'stewlog.onrender.com',
        ])
        self.assertEqual(production['CSRF_TRUSTED_ORIGINS'], [
            'https://stewlog.com',
            'https://www.stewlog.com',
            'https://stewlog.onrender.com',
        ])
        self.assertEqual(
            production['SECURE_PROXY_SSL_HEADER'],
            ('HTTP_X_FORWARDED_PROTO', 'https'),
        )
        self.assertTrue(production['SECURE_SSL_REDIRECT'])
        self.assertTrue(production['SESSION_COOKIE_SECURE'])
        self.assertTrue(production['CSRF_COOKIE_SECURE'])
        self.assertGreater(production['SECURE_HSTS_SECONDS'], 0)
        self.assertTrue(production['SECURE_HSTS_INCLUDE_SUBDOMAINS'])
        self.assertFalse(production['SECURE_HSTS_PRELOAD'])

    def test_settings_have_no_hardcoded_production_host_or_secret_key(self):
        settings_path = Path(__file__).resolve().parents[1] / 'SalesLog' / 'settings.py'
        source = settings_path.read_text(encoding='utf-8')
        self.assertNotIn('stewlog.com', source)
        self.assertNotIn('stewlog.onrender.com', source)

        tree = ast.parse(source)
        hardcoded_secret_assignments = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == 'SECRET_KEY'
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]
        self.assertEqual(hardcoded_secret_assignments, [])

    def test_sites_framework_is_not_active(self):
        self.assertNotIn('django.contrib.sites', settings.INSTALLED_APPS)
        self.assertFalse(hasattr(settings, 'SITE_ID'))

    def test_djstripe_uuid_webhook_route_and_signature_validation_are_enabled(self):
        route = reverse(
            'djstripe:djstripe_webhook_by_uuid',
            kwargs={'uuid': uuid.uuid4()},
        )
        self.assertRegex(route, r'^/stripe/webhook/[0-9a-f-]+/$')
        self.assertEqual(
            settings.DJSTRIPE_WEBHOOK_VALIDATION,
            'verify_signature',
        )

    @override_settings(ALLOWED_HOSTS=['stewlog.com'])
    def test_allauth_email_urls_use_secure_request_host(self):
        request = RequestFactory().get(
            '/',
            secure=True,
            HTTP_HOST='stewlog.com',
        )
        reset_url = get_reset_password_from_key_url(
            request,
            'opaque-test-reset-key',
        )
        verification_url = get_email_verification_url(
            request,
            SimpleNamespace(key='opaque-test-verification-key'),
        )

        self.assertTrue(reset_url.startswith('https://stewlog.com/accounts/'))
        self.assertTrue(
            verification_url.startswith('https://stewlog.com/accounts/')
        )

    def test_social_callback_routes_are_stable_for_dashboard_configuration(self):
        self.assertEqual(
            reverse('google_callback'),
            '/accounts/google/login/callback/',
        )
        self.assertEqual(
            reverse('apple_callback'),
            '/accounts/apple/login/callback/',
        )

    def test_whitenoise_is_configured_after_security_middleware(self):
        security_index = settings.MIDDLEWARE.index(
            'django.middleware.security.SecurityMiddleware'
        )
        whitenoise_index = settings.MIDDLEWARE.index(
            'whitenoise.middleware.WhiteNoiseMiddleware'
        )
        self.assertEqual(whitenoise_index, security_index + 1)
        self.assertEqual(settings.STATIC_URL, '/static/')
        self.assertEqual(
            settings.STORAGES['staticfiles']['BACKEND'],
            'django.contrib.staticfiles.storage.StaticFilesStorage',
        )

    def test_local_database_falls_back_to_sqlite(self):
        self.assertEqual(
            settings.DATABASES['default']['ENGINE'],
            'django.db.backends.sqlite3',
        )

    def test_commission_pages_redirect_anonymous_users_to_login(self):
        response = self.client.get(reverse('view_sales'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response.url)

    def test_local_email_does_not_use_smtp_backend(self):
        self.assertNotEqual(
            settings.EMAIL_BACKEND,
            'django.core.mail.backends.smtp.EmailBackend',
        )


class LocalSignupTests(TestCase):
    def test_allauth_signup_uses_shared_header_and_logo(self):
        response = self.client.get('/accounts/signup/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '/static/SalesLogApp/images/stewlog-logo.png',
        )
        self.assertContains(response, 'class="site-logo"')

    def test_allauth_signup_does_not_require_local_smtp_server(self):
        response = self.client.post(
            '/accounts/signup/',
            {
                'username': 'beta-user',
                'email': 'beta-user@example.com',
                'password1': 'A-strong-local-test-password-482!',
                'password2': 'A-strong-local-test-password-482!',
            },
        )

        self.assertNotEqual(response.status_code, 500)
        self.assertTrue(User.objects.filter(username='beta-user').exists())
