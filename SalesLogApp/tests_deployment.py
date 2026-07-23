from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from SalesLog.settings import env_bool, env_list


class DeploymentConfigurationTests(SimpleTestCase):
    def test_environment_value_helpers(self):
        self.assertFalse(env_bool('SETTING_THAT_IS_NOT_DEFINED', False))
        self.assertEqual(env_list('SETTING_THAT_IS_NOT_DEFINED'), [])

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
