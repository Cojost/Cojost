from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse


class PublicLandingPageTests(TestCase):
    def test_root_is_public_landing_page_for_anonymous_visitors(self):
        response = self.client.get(reverse('landing_page'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'landing_page.html')
        self.assertContains(response, 'Know your commission')
        self.assertContains(response, 'before payday.')
        self.assertContains(response, 'Track every deal.')
        self.assertContains(response, 'Understand every dollar.')
        self.assertContains(response, 'Built by a car salesperson')

    def test_landing_page_uses_real_account_routes_and_trial_disclosure(self):
        response = self.client.get(reverse('landing_page'))

        self.assertContains(response, f'href="{reverse("account_login")}"')
        self.assertContains(response, f'href="{reverse("account_signup")}"')
        self.assertContains(response, 'Try Basic Monthly for 30 days')
        self.assertContains(response, 'Other standard plans start without a trial')
        self.assertContains(response, 'payment method is collected')
        self.assertContains(
            response,
            'Current plan options and pricing are shown before you confirm checkout.',
        )
        self.assertNotContains(response, '$7.99')

    def test_landing_page_has_production_social_metadata(self):
        response = self.client.get(reverse('landing_page'))

        self.assertContains(
            response,
            '<link rel="canonical" href="https://stewlog.com/">',
            html=True,
        )
        self.assertContains(
            response,
            'content="https://stewlog.com/static/SalesLogApp/images/stewlog-og.png"',
        )
        self.assertContains(response, 'property="og:title"')
        self.assertContains(response, 'name="twitter:card"')

    def test_landing_page_assets_are_collectstatic_discoverable(self):
        for path in (
            'SalesLogApp/css/landing.css',
            'SalesLogApp/images/stewlog-mark.png',
            'SalesLogApp/images/stewlog-wordmark.png',
            'SalesLogApp/images/stewlog-og.png',
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(finders.find(path))

    def test_authenticated_root_redirects_to_dashboard(self):
        user = get_user_model().objects.create_user(
            username='landing-owner',
            password='safe-test-password',
        )
        self.client.force_login(user)

        response = self.client.get(reverse('landing_page'))

        self.assertRedirects(
            response,
            reverse('view_sales'),
            fetch_redirect_response=False,
        )

    def test_root_rejects_unsafe_methods(self):
        response = self.client.post(reverse('landing_page'))

        self.assertEqual(response.status_code, 405)
