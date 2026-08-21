from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse


class BrandedAccountExperienceTests(TestCase):
    def test_login_uses_landing_page_brand_and_account_copy(self):
        response = self.client.get(reverse('account_login'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="auth-body header-theme-blue"')
        self.assertContains(
            response,
            '/static/SalesLogApp/images/stewlog-wordmark.png',
        )
        self.assertContains(response, 'Welcome back')
        self.assertContains(response, 'Track every deal. Understand every dollar.')
        self.assertContains(response, reverse('account_reset_password'))
        self.assertContains(response, reverse('account_signup'))
        self.assertNotContains(response, '<h1>Sales Log</h1>', html=True)

    def test_signup_explains_trial_and_checkout_without_hardcoded_price(self):
        response = self.client.get(reverse('account_signup'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Start your 30-day trial')
        self.assertContains(response, 'Create account and continue')
        self.assertContains(response, 'A payment method is required at checkout.')
        self.assertContains(
            response,
            'Your current plan and price will be shown before you confirm.',
        )
        self.assertContains(response, reverse('account_login'))
        self.assertNotContains(response, '$7.99')

    def test_password_reset_uses_the_same_branded_entrance_shell(self):
        response = self.client.get(reverse('account_reset_password'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="auth-card"')
        self.assertContains(
            response,
            '/static/SalesLogApp/images/stewlog-wordmark.png',
        )
        self.assertContains(response, 'Password Reset')

    def test_invalid_login_keeps_field_errors_inside_branded_form(self):
        response = self.client.post(
            reverse('account_login'),
            {'login': 'missing-user', 'password': 'incorrect-password'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="auth-form"')
        self.assertContains(response, 'role="alert"')
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_signup_preserves_unique_email_validation(self):
        user = get_user_model().objects.create_user(
            username='existing-owner',
            email='owner@example.com',
            password='safe-test-password',
        )
        user.emailaddress_set.create(
            email=user.email,
            verified=True,
            primary=True,
        )

        response = self.client.post(
            reverse('account_signup'),
            {
                'username': 'new-owner',
                'email': 'OWNER@example.com',
                'password1': 'A-strong-local-test-password-482!',
                'password2': 'A-strong-local-test-password-482!',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="auth-field-errors"')
        self.assertFalse(
            get_user_model().objects.filter(username='new-owner').exists()
        )

    def test_google_button_is_not_promised_while_provider_is_unavailable(self):
        for route_name in ('account_login', 'account_signup'):
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                self.assertNotContains(response, 'Continue with Google')

    def test_auth_stylesheet_is_collectstatic_discoverable(self):
        self.assertIsNotNone(finders.find('SalesLogApp/css/auth.css'))
