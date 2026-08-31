from unittest.mock import patch

from allauth.account import app_settings as account_settings
from allauth.account.app_settings import LoginMethod
from allauth.account.models import EmailAddress
from allauth.socialaccount import app_settings as socialaccount_settings
from django.conf import settings
from django.contrib import admin
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db import IntegrityError
from django.forms import ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse

from .admin import (
    NormalizedEmailAddressAdmin,
    NormalizedUserAdmin,
)
from .account_adapter import StewLogAccountAdapter
from .auth_forms import (
    NormalizedAddEmailForm,
    NormalizedAdminUserChangeForm,
    NormalizedEmailAddressAdminForm,
    NormalizedSignupForm,
)
from .auth_identity import (
    EMAIL_UNAVAILABLE_MESSAGE,
    NormalizedIdentityCollision,
)
from .auth_views import NormalizedEmailView, NormalizedSignupView
from .management.commands.auth_identity_readiness import (
    build_auth_identity_readiness_report,
)
from .models import PayPlan, UserProfile


PASSWORD = 'A-strong-local-test-password-482!'


def identity_request(path='/accounts/signup/'):
    request = RequestFactory().post(path)
    request.session = {}
    return request


def admin_identity_request(path, data):
    request = RequestFactory().post(path, data=data)
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


@override_settings(
    ALLOWED_HOSTS=['testserver'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class SignupIdentityValidationTests(TestCase):
    def create_identity(
        self,
        username,
        email,
        *,
        address_email=None,
        verified=True,
        primary=True,
    ):
        user = get_user_model().objects.create_user(
            username=username,
            email=email,
            password=PASSWORD,
        )
        address = EmailAddress.objects.create(
            user=user,
            email=address_email or email,
            verified=verified,
            primary=primary,
        )
        return user, address

    def signup_data(self, **overrides):
        data = {
            'username': 'NewCustomer',
            'email': 'new-customer@example.com',
            'password1': PASSWORD,
            'password2': PASSWORD,
        }
        data.update(overrides)
        return data

    def test_signup_rejects_case_and_whitespace_normalized_username(self):
        self.create_identity('ExistingCustomer', 'existing@example.com')

        response = self.client.post(
            reverse('account_signup'),
            self.signup_data(
                username='  existingcustomer  ',
                email='other@example.com',
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('username', response.context['form'].errors)
        self.assertFalse(
            get_user_model().objects.filter(email='other@example.com').exists()
        )

    def test_signup_rejects_normalized_user_email_without_changing_owner(self):
        owner, address = self.create_identity(
            'ExistingOwner',
            'owner@example.com',
        )
        profile_state = tuple(
            UserProfile.objects.filter(user=owner).values_list('pk', 'user_id')
        )
        pay_plan_state = tuple(
            PayPlan.objects.filter(owner_user=owner).values_list(
                'pk', 'owner_user_id'
            )
        )

        response = self.client.post(
            reverse('account_signup'),
            self.signup_data(email='  OWNER@EXAMPLE.COM  '),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        owner.refresh_from_db()
        address.refresh_from_db()
        self.assertEqual(address.user_id, owner.pk)
        self.assertTrue(address.primary)
        self.assertTrue(address.verified)
        self.assertEqual(
            profile_state,
            tuple(
                UserProfile.objects.filter(user=owner).values_list(
                    'pk', 'user_id'
                )
            ),
        )
        self.assertEqual(
            pay_plan_state,
            tuple(
                PayPlan.objects.filter(owner_user=owner).values_list(
                    'pk', 'owner_user_id'
                )
            ),
        )

    def test_signup_rejects_unverified_allauth_address_on_another_account(self):
        owner, address = self.create_identity(
            'AddressOwner',
            'compatibility@example.com',
            address_email='claimed@example.com',
            verified=False,
            primary=False,
        )

        response = self.client.post(
            reverse('account_signup'),
            self.signup_data(email='  CLAIMED@EXAMPLE.COM  '),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        self.assertIn('unavailable', str(response.context['form'].errors['email']))
        self.assertNotIn(owner.username, response.content.decode())
        address.refresh_from_db()
        self.assertFalse(address.verified)
        self.assertEqual(address.user_id, owner.pk)

    def test_signup_form_normalizes_storage_and_preserves_username_casing(self):
        form = NormalizedSignupForm(
            data=self.signup_data(
                username='  DisplayCaseName  ',
                email='  New.Owner@Example.COM  ',
            )
        )
        self.assertTrue(form.is_valid(), form.errors)

        user, response = form.try_save(identity_request())

        self.assertIsNone(response)
        self.assertEqual(user.username, 'DisplayCaseName')
        self.assertEqual(user.email, 'new.owner@example.com')
        address = EmailAddress.objects.get(user=user)
        self.assertEqual(address.email, 'new.owner@example.com')

    def test_signup_identity_save_rolls_back_and_marks_a_race_collision(self):
        form = NormalizedSignupForm(
            data=self.signup_data(
                username='RaceCandidate',
                email='race-address@example.com',
            )
        )
        self.assertTrue(form.is_valid(), form.errors)
        winner = get_user_model().objects.create_user(
            username='RaceWinner',
            email='winner-compatibility@example.com',
            password=PASSWORD,
        )
        EmailAddress.objects.create(
            user=winner,
            # allauth's exact pre-insert lookup misses this spelling, while
            # the AUTH-1B functional index rejects it.
            email='RACE-ADDRESS@EXAMPLE.COM',
            verified=False,
            primary=False,
        )

        with self.assertRaises(NormalizedIdentityCollision):
            form.try_save(identity_request())

        self.assertIn('email', form.errors)
        self.assertFalse(
            get_user_model().objects.filter(username='RaceCandidate').exists()
        )
        self.assertFalse(
            UserProfile.objects.filter(user__username='RaceCandidate').exists()
        )
        self.assertFalse(
            PayPlan.objects.filter(owner_user__username='RaceCandidate').exists()
        )

    def test_signup_rolls_back_if_allauth_discards_a_newly_claimed_address(self):
        form = NormalizedSignupForm(
            data=self.signup_data(
                username='DiscardedAddressCandidate',
                email='claimed-during-save@example.com',
            )
        )
        self.assertTrue(form.is_valid(), form.errors)
        winner = get_user_model().objects.create_user(
            username='CleanupRaceWinner',
            email='winner-cleanup@example.com',
            password=PASSWORD,
        )
        EmailAddress.objects.create(
            user=winner,
            email='claimed-during-save@example.com',
            verified=False,
            primary=False,
        )

        with self.assertRaises(NormalizedIdentityCollision):
            form.try_save(identity_request())

        self.assertIn('email', form.errors)
        self.assertFalse(
            get_user_model().objects.filter(
                username='DiscardedAddressCandidate'
            ).exists()
        )

    def test_signup_view_turns_a_race_collision_into_a_form_response(self):
        def collide(form, request):
            form.add_error('email', EMAIL_UNAVAILABLE_MESSAGE)
            raise NormalizedIdentityCollision

        with patch.object(
            NormalizedSignupForm,
            'try_save',
            autospec=True,
            side_effect=collide,
        ):
            response = self.client.post(
                reverse('account_signup'),
                self.signup_data(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        self.assertFalse(
            get_user_model().objects.filter(username='NewCustomer').exists()
        )


@override_settings(
    ALLOWED_HOSTS=['testserver'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class AccountEmailIdentityValidationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='AccountOwner',
            email='account-owner@example.com',
            password=PASSWORD,
        )
        self.primary = EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )
        self.other = get_user_model().objects.create_user(
            username='OtherAccount',
            email='other-compatibility@example.com',
            password=PASSWORD,
        )
        self.claimed = EmailAddress.objects.create(
            user=self.other,
            email='claimed-address@example.com',
            verified=False,
            primary=False,
        )

    def test_account_add_email_rejects_unverified_normalized_collision(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('account_email'),
            {
                'email': '  CLAIMED-ADDRESS@EXAMPLE.COM  ',
                'action_add': 'Add Email',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        self.assertIn('unavailable', str(response.context['form'].errors['email']))
        self.assertNotIn(self.other.username, response.content.decode())
        self.assertEqual(
            EmailAddress.objects.filter(
                email='claimed-address@example.com'
            ).count(),
            1,
        )
        self.primary.refresh_from_db()
        self.claimed.refresh_from_db()
        self.assertTrue(self.primary.primary)
        self.assertTrue(self.primary.verified)
        self.assertFalse(self.claimed.verified)

    def test_add_email_form_marks_an_integrity_race_after_rollback(self):
        form = NormalizedAddEmailForm(
            user=self.user,
            data={'email': 'new-race-address@example.com'},
        )
        self.assertTrue(form.is_valid(), form.errors)
        contender = get_user_model().objects.create_user(
            username='EmailRaceWinner',
            email='winner@example.com',
            password=PASSWORD,
        )
        EmailAddress.objects.create(
            user=contender,
            email='new-race-address@example.com',
            verified=False,
            primary=False,
        )

        with self.assertRaises(NormalizedIdentityCollision):
            form.save(identity_request('/accounts/email/'))

        self.assertIn('email', form.errors)

    def test_add_email_form_preserves_successful_allauth_delivery_flow(self):
        form = NormalizedAddEmailForm(
            user=self.user,
            data={'email': '  NEW-ADDRESS@EXAMPLE.COM  '},
        )
        self.assertTrue(form.is_valid(), form.errors)
        request = identity_request('/accounts/email/')

        with patch(
            'allauth.account.internal.flows.email_verification.'
            'send_verification_email_to_address',
            return_value=True,
        ) as deliver:
            address = form.save(request)

        self.assertEqual(address.email, 'new-address@example.com')
        self.assertEqual(address.user_id, self.user.pk)
        deliver.assert_called_once_with(request, address)

    def test_account_email_view_turns_a_race_into_a_form_response(self):
        self.client.force_login(self.user)

        def collide(form, request):
            form.add_error('email', EMAIL_UNAVAILABLE_MESSAGE)
            raise NormalizedIdentityCollision

        with patch.object(
            NormalizedAddEmailForm,
            'save',
            autospec=True,
            side_effect=collide,
        ):
            response = self.client.post(
                reverse('account_email'),
                {
                    'email': 'available-before-race@example.com',
                    'action_add': 'Add Email',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        self.assertFalse(
            EmailAddress.objects.filter(
                email='available-before-race@example.com'
            ).exists()
        )


@override_settings(ALLOWED_HOSTS=['testserver'])
class AdminIdentityValidationTests(TestCase):
    def setUp(self):
        self.first = get_user_model().objects.create_user(
            username='FirstAdminTarget',
            email='first@example.com',
            password=PASSWORD,
        )
        self.first_address = EmailAddress.objects.create(
            user=self.first,
            email=self.first.email,
            verified=True,
            primary=True,
        )
        self.second = get_user_model().objects.create_user(
            username='SecondAdminTarget',
            email='second@example.com',
            password=PASSWORD,
        )
        self.second_address = EmailAddress.objects.create(
            user=self.second,
            email=self.second.email,
            verified=False,
            primary=True,
        )

    def user_change_data(self, user, **overrides):
        data = {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'email': user.email,
            'is_active': 'on' if user.is_active else '',
            'is_staff': 'on' if user.is_staff else '',
            'is_superuser': 'on' if user.is_superuser else '',
            'last_login': (
                user.last_login.isoformat() if user.last_login else ''
            ),
            'date_joined': user.date_joined.isoformat(),
            'groups': [],
            'user_permissions': [],
        }
        data.update(overrides)
        return data

    def test_registered_admins_inherit_existing_behavior_and_use_safe_forms(self):
        user_admin = admin.site._registry[get_user_model()]
        address_admin = admin.site._registry[EmailAddress]

        self.assertIsInstance(user_admin, NormalizedUserAdmin)
        self.assertIsInstance(user_admin, DjangoUserAdmin)
        self.assertEqual(user_admin.fieldsets, DjangoUserAdmin.fieldsets)
        self.assertEqual(user_admin.add_fieldsets, DjangoUserAdmin.add_fieldsets)
        self.assertEqual(user_admin.list_display, DjangoUserAdmin.list_display)
        self.assertIs(user_admin.form, NormalizedAdminUserChangeForm)
        self.assertIsInstance(address_admin, NormalizedEmailAddressAdmin)
        self.assertEqual(
            address_admin.list_display,
            ('email', 'user', 'primary', 'verified'),
        )
        self.assertIn('make_verified', address_admin.actions)
        self.assertIs(address_admin.form, NormalizedEmailAddressAdminForm)

    def test_user_change_form_rejects_normalized_username_collision(self):
        form = NormalizedAdminUserChangeForm(
            instance=self.first,
            data=self.user_change_data(
                self.first,
                username='  secondadmintarget  ',
            ),
        )

        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)
        self.first.refresh_from_db()
        self.assertEqual(self.first.username, 'FirstAdminTarget')

    def test_user_change_form_rejects_normalized_email_collision(self):
        form = NormalizedAdminUserChangeForm(
            instance=self.first,
            data=self.user_change_data(
                self.first,
                email='  SECOND@EXAMPLE.COM  ',
            ),
        )

        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)
        self.first.refresh_from_db()
        self.assertEqual(self.first.email, 'first@example.com')

    def test_user_change_form_normalizes_email_but_preserves_username_case(self):
        form = NormalizedAdminUserChangeForm(
            instance=self.first,
            data=self.user_change_data(
                self.first,
                username='  PreservedDisplayCase  ',
                email='  UNIQUE.ADMIN@EXAMPLE.COM  ',
            ),
        )
        self.assertTrue(form.is_valid(), form.errors)

        changed = form.save(commit=False)
        user_admin = admin.site._registry[get_user_model()]
        user_admin.save_model(
            identity_request('/admin/auth/user/'),
            changed,
            form,
            change=True,
        )

        self.assertEqual(changed.username, 'PreservedDisplayCase')
        self.assertEqual(changed.email, 'unique.admin@example.com')
        self.assertEqual(changed.pk, self.first.pk)
        self.first_address.refresh_from_db()
        self.assertFalse(self.first_address.primary)
        self.assertTrue(self.first_address.verified)
        replacement = EmailAddress.objects.get(
            user=self.first,
            email='unique.admin@example.com',
        )
        self.assertTrue(replacement.primary)
        self.assertFalse(replacement.verified)
        report = build_auth_identity_readiness_report()
        self.assertEqual(report['missing_matching_allauth_address_count'], 0)
        self.assertEqual(report['primary_email_mismatch_count'], 0)

    def test_primary_email_address_admin_edit_synchronizes_user_email(self):
        form = NormalizedEmailAddressAdminForm(
            instance=self.first_address,
            data={
                'user': self.first.pk,
                'email': '  PRIMARY.EDITED@EXAMPLE.COM  ',
                'verified': False,
                'primary': True,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

        changed = form.save(commit=False)
        address_admin = admin.site._registry[EmailAddress]
        address_admin.save_model(
            identity_request('/admin/account/emailaddress/'),
            changed,
            form,
            change=True,
        )

        self.first.refresh_from_db()
        self.first_address.refresh_from_db()
        self.assertEqual(self.first.email, 'primary.edited@example.com')
        self.assertEqual(
            self.first_address.email,
            'primary.edited@example.com',
        )
        self.assertTrue(self.first_address.primary)
        self.assertFalse(self.first_address.verified)
        report = build_auth_identity_readiness_report()
        self.assertEqual(report['missing_matching_allauth_address_count'], 0)
        self.assertEqual(report['primary_email_mismatch_count'], 0)

    def test_email_address_admin_rejects_unverified_normalized_collision(self):
        form = NormalizedEmailAddressAdminForm(
            data={
                'user': self.first.pk,
                'email': '  SECOND@EXAMPLE.COM  ',
                'verified': False,
                'primary': False,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)
        self.second_address.refresh_from_db()
        self.assertFalse(self.second_address.verified)
        self.assertTrue(self.second_address.primary)

    def test_multiple_blank_compatibility_emails_remain_valid(self):
        self.first.email = ''
        self.first.save(update_fields=['email'])
        self.second.email = ''
        self.second.save(update_fields=['email'])

        self.assertEqual(
            get_user_model().objects.filter(email='').count(),
            2,
        )

    def test_user_admin_race_returns_a_safe_message_instead_of_500(self):
        request = admin_identity_request(
            '/admin/auth/user/1/change/',
            {'username': 'SecondAdminTarget', 'email': self.first.email},
        )
        user_admin = admin.site._registry[get_user_model()]

        with patch.object(
            DjangoUserAdmin,
            'changeform_view',
            side_effect=IntegrityError('simulated normalized username race'),
        ):
            response = user_admin.changeform_view(
                request,
                object_id=str(self.first.pk),
            )

        self.assertEqual(response.status_code, 302)
        rendered_messages = ' '.join(
            str(message) for message in get_messages(request)
        )
        self.assertIn('username is unavailable', rendered_messages)

    def test_email_address_admin_race_returns_a_safe_message(self):
        request = admin_identity_request(
            '/admin/account/emailaddress/1/change/',
            {
                'email': self.second_address.email,
                'user': self.first.pk,
            },
        )
        address_admin = admin.site._registry[EmailAddress]

        with patch(
            'allauth.account.admin.EmailAddressAdmin.changeform_view',
            side_effect=IntegrityError('simulated normalized email race'),
        ):
            response = address_admin.changeform_view(
                request,
                object_id=str(self.first_address.pk),
            )

        self.assertEqual(response.status_code, 302)
        rendered_messages = ' '.join(
            str(message) for message in get_messages(request)
        )
        self.assertIn('email address is unavailable', rendered_messages)


@override_settings(
    ALLOWED_HOSTS=['testserver'],
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class AuthenticationBehaviorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='UsernameLoginOnly',
            email='login-address@example.com',
            password=PASSWORD,
        )
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )

    def test_custom_views_shadow_only_the_two_allauth_write_routes(self):
        self.assertIs(
            resolve(reverse('account_signup')).func.view_class,
            NormalizedSignupView,
        )
        self.assertIs(
            resolve(reverse('account_email')).func.view_class,
            NormalizedEmailView,
        )

    def test_username_login_still_works_and_email_login_remains_disabled(self):
        self.assertFalse(hasattr(settings, 'ACCOUNT_LOGIN_METHODS'))
        self.assertEqual(
            account_settings.LOGIN_METHODS,
            frozenset({LoginMethod.USERNAME}),
        )

        username_response = self.client.post(
            reverse('account_login'),
            {'login': self.user.username, 'password': PASSWORD},
        )
        self.assertEqual(username_response.status_code, 302)
        self.assertEqual(
            int(self.client.session['_auth_user_id']),
            self.user.pk,
        )

        self.client.logout()
        email_response = self.client.post(
            reverse('account_login'),
            {'login': self.user.email, 'password': PASSWORD},
        )
        self.assertEqual(email_response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_admin_username_authentication_and_social_link_defaults_are_unchanged(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=['is_staff', 'is_superuser'])

        self.assertTrue(
            self.client.login(username=self.user.username, password=PASSWORD)
        )
        self.assertFalse(socialaccount_settings.EMAIL_AUTHENTICATION)
        self.assertFalse(
            socialaccount_settings.EMAIL_AUTHENTICATION_AUTO_CONNECT
        )

    def test_shared_adapter_normalizes_social_style_identity_input(self):
        adapter = StewLogAccountAdapter()

        self.assertEqual(
            adapter.clean_username('  PreservedSocialCase  '),
            'PreservedSocialCase',
        )
        self.assertEqual(
            adapter.clean_email('  NEW.SOCIAL@EXAMPLE.COM  '),
            'new.social@example.com',
        )
        with self.assertRaises(ValidationError):
            adapter.clean_username('  usernameloginonly  ')
