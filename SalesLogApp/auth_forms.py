from allauth.account import app_settings
from allauth.account.adapter import get_adapter
from allauth.account.forms import AddEmailForm, SignupForm
from allauth.account.models import EmailAddress
from allauth.core import ratelimit
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AdminUserCreationForm,
    PasswordChangeForm,
    UserChangeForm,
)
from django.core.validators import MinLengthValidator
from django.db import IntegrityError, transaction
from django.views.decorators.debug import sensitive_variables

from .auth_identity import (
    EMAIL_UNAVAILABLE_MESSAGE,
    USERNAME_UNAVAILABLE_MESSAGE,
    NormalizedIdentityCollision,
    email_addresses_matching_email,
    normalize_email,
    normalize_username,
    users_matching_email,
    users_matching_username,
)


class UsernameChangeRejected(Exception):
    """A self-service username update rejected after form validation."""


def _replace_collision_error(error, *, codes, message):
    if any(item.code in codes for item in error.error_list):
        raise forms.ValidationError(message, code='identity_unavailable') from error
    raise error


def _email_is_used_by_another_account(value, *, user_id=None):
    users = users_matching_email(value)
    addresses = email_addresses_matching_email(value)
    if user_id is not None:
        users = users.exclude(pk=user_id)
        addresses = addresses.exclude(user_id=user_id)
    return users.exists() or addresses.exists()


class ProfilePasswordChangeForm(PasswordChangeForm):
    """Share the password-proof throttle with username changes."""

    def __init__(self, user, *args, request=None, **kwargs):
        self.request = request
        super().__init__(user, *args, **kwargs)

    def clean_old_password(self):
        if self.request is not None and not ratelimit.consume(
            self.request,
            action='password_proof',
            user=self.user,
        ):
            raise forms.ValidationError(
                'Too many password attempts. '
                'Please wait a minute and try again.',
                code='rate_limited',
            )
        return super().clean_old_password()


class SelfServiceUsernameChangeForm(forms.Form):
    """Change only the authenticated user's username after password proof."""

    username = forms.CharField(
        label='New username',
        max_length=150,
        help_text=(
            '150 characters or fewer. Letters, numbers, and @/./+/-/_ only.'
        ),
        widget=forms.TextInput(attrs={
            'autocomplete': 'username',
            'autocapitalize': 'none',
            'spellcheck': 'false',
        }),
    )
    current_password = forms.CharField(
        label='Current password',
        strip=False,
        widget=forms.PasswordInput(attrs={
            'autocomplete': 'current-password',
        }),
    )

    def __init__(self, user, *args, request=None, **kwargs):
        self.user = user
        self.request = request
        super().__init__(*args, **kwargs)
        username_field = user._meta.get_field(user.USERNAME_FIELD)
        self.fields['username'].max_length = username_field.max_length
        self.fields['username'].widget.attrs['maxlength'] = (
            username_field.max_length
        )
        minimum_length = app_settings.USERNAME_MIN_LENGTH
        self.fields['username'].min_length = minimum_length
        self.fields['username'].validators.append(
            MinLengthValidator(minimum_length)
        )
        self.fields['username'].widget.attrs['minlength'] = minimum_length
        if not self.is_bound:
            self.initial['username'] = user.username

    def clean_username(self):
        value = normalize_username(self.cleaned_data.get('username'))
        # Keep the installed allauth username policy while performing the
        # current-user-aware uniqueness check below.
        value = get_adapter().clean_username(value, shallow=True)
        return value

    @sensitive_variables('value')
    def clean_current_password(self):
        value = self.cleaned_data.get('current_password')
        if self.request is not None and not ratelimit.consume(
            self.request,
            action='password_proof',
            user=self.user,
        ):
            raise forms.ValidationError(
                'Too many username change attempts. '
                'Please wait a minute and try again.',
                code='rate_limited',
            )
        if not self.user.has_usable_password():
            raise forms.ValidationError(
                'Set a password in Security before changing your username.',
                code='password_not_set',
            )
        if not self.user.check_password(value):
            raise forms.ValidationError(
                'Your current password was incorrect.',
                code='password_incorrect',
            )
        return value

    @sensitive_variables('cleaned_data', 'password')
    def clean(self):
        cleaned_data = super().clean()
        username = cleaned_data.get('username')
        password = cleaned_data.get('current_password')
        # Do not disclose candidate availability until password proof passes.
        if username and password:
            if username == self.user.username:
                self.add_error(
                    'username',
                    forms.ValidationError(
                        'Enter a username different from your current username.',
                        code='unchanged',
                    ),
                )
            elif (
                users_matching_username(username)
                .exclude(pk=self.user.pk)
                .exists()
            ):
                self.add_error(
                    'username',
                    forms.ValidationError(
                        USERNAME_UNAVAILABLE_MESSAGE,
                        code='identity_unavailable',
                    ),
                )
        return cleaned_data

    def _reject(self, field, message, code):
        self.add_error(field, forms.ValidationError(message, code=code))
        raise UsernameChangeRejected

    @sensitive_variables('password')
    def save(self):
        if not self.is_bound or not self.is_valid():
            raise ValueError('Cannot save an invalid username change form.')

        username = self.cleaned_data['username']
        password = self.cleaned_data['current_password']
        user_model = get_user_model()
        try:
            with transaction.atomic():
                locked_user = (
                    user_model._default_manager.select_for_update()
                    .get(pk=self.user.pk)
                )
                # Recheck both proof and normalized uniqueness after locking
                # the current identity. The database index closes the gap
                # between this query and the update.
                if not locked_user.check_password(password):
                    self._reject(
                        'current_password',
                        'Your current password was incorrect.',
                        'password_incorrect',
                    )
                if users_matching_username(username).exclude(
                    pk=locked_user.pk,
                ).exists():
                    self._reject(
                        'username',
                        USERNAME_UNAVAILABLE_MESSAGE,
                        'identity_unavailable',
                    )
                locked_user.username = username
                locked_user.save(update_fields=['username'])
        except IntegrityError as error:
            # Only translate a database failure after confirming the
            # normalized identity collision outside the rolled-back block.
            if users_matching_username(username).exclude(pk=self.user.pk).exists():
                self.add_error(
                    'username',
                    forms.ValidationError(
                        USERNAME_UNAVAILABLE_MESSAGE,
                        code='identity_unavailable',
                    ),
                )
                raise UsernameChangeRejected from error
            raise

        self.user.username = username
        return self.user


class NormalizedSignupForm(SignupForm):
    """Keep allauth signup policy while matching normalized database indexes."""

    def clean_username(self):
        try:
            value = super().clean_username()
        except forms.ValidationError as error:
            _replace_collision_error(
                error,
                codes={'username_taken', 'unique'},
                message=USERNAME_UNAVAILABLE_MESSAGE,
            )
        value = normalize_username(value)
        if users_matching_username(value).exists():
            raise forms.ValidationError(
                USERNAME_UNAVAILABLE_MESSAGE,
                code='identity_unavailable',
            )
        return value

    def clean_email(self):
        try:
            value = super().clean_email()
        except forms.ValidationError as error:
            _replace_collision_error(
                error,
                codes={'email_taken', 'duplicate_email', 'unique'},
                message=EMAIL_UNAVAILABLE_MESSAGE,
            )
        value = normalize_email(value)
        if (
            users_matching_email(value).exists()
            or email_addresses_matching_email(value).exists()
        ):
            raise forms.ValidationError(
                EMAIL_UNAVAILABLE_MESSAGE,
                code='identity_unavailable',
            )
        return value

    def _add_race_collision_errors(self):
        username = self.cleaned_data.get('username')
        email = self.cleaned_data.get('email')
        collisions = False
        if username and users_matching_username(username).exists():
            self.add_error('username', USERNAME_UNAVAILABLE_MESSAGE)
            collisions = True
        if email and (
            users_matching_email(email).exists()
            or email_addresses_matching_email(email).exists()
        ):
            self.add_error('email', EMAIL_UNAVAILABLE_MESSAGE)
            collisions = True
        return collisions

    def try_save(self, request):
        try:
            # allauth writes auth_user first and account_emailaddress second.
            # Keep both writes, including user post_save work, in one rollback
            # boundary without wrapping complete_signup or email delivery.
            with transaction.atomic():
                user, response = super().try_save(request)
                email = self.cleaned_data.get('email')
                if user is not None and email and not (
                    email_addresses_matching_email(email)
                    .filter(user_id=user.pk)
                    .exists()
                ):
                    # allauth can deliberately discard an address that became
                    # unavailable after form validation. A required-email
                    # StewLog signup must not commit that partial identity.
                    self.add_error('email', EMAIL_UNAVAILABLE_MESSAGE)
                    raise NormalizedIdentityCollision
                return user, response
        except IntegrityError:
            if self._add_race_collision_errors():
                raise NormalizedIdentityCollision
            raise


class NormalizedAddEmailForm(AddEmailForm):
    """Reject every normalized address collision, including unverified rows."""

    def clean_email(self):
        try:
            value = super().clean_email()
        except forms.ValidationError as error:
            _replace_collision_error(
                error,
                codes={'email_taken', 'duplicate_email', 'unique'},
                message=EMAIL_UNAVAILABLE_MESSAGE,
            )
        value = normalize_email(value)
        if _email_is_used_by_another_account(value, user_id=self.user.pk):
            raise forms.ValidationError(
                EMAIL_UNAVAILABLE_MESSAGE,
                code='identity_unavailable',
            )
        # The same account cannot add a duplicate EmailAddress either.
        if email_addresses_matching_email(value).filter(user=self.user).exists():
            raise forms.ValidationError(
                EMAIL_UNAVAILABLE_MESSAGE,
                code='identity_unavailable',
            )
        return value

    def _add_race_collision_error(self):
        value = self.cleaned_data.get('email')
        if not value:
            return False
        if (
            _email_is_used_by_another_account(value, user_id=self.user.pk)
            or email_addresses_matching_email(value).filter(user=self.user).exists()
        ):
            self.add_error('email', EMAIL_UNAVAILABLE_MESSAGE)
            return True
        return False

    def save(self, request):
        try:
            # allauth owns the add/get-or-create transaction boundaries. Keep
            # its successful verification-delivery timing unchanged; those
            # boundaries have already rolled back before an IntegrityError is
            # re-raised here.
            return super().save(request)
        except IntegrityError:
            if self._add_race_collision_error():
                raise NormalizedIdentityCollision
            raise


class NormalizedAdminUserCreationForm(AdminUserCreationForm):
    def clean_username(self):
        try:
            value = super().clean_username()
        except forms.ValidationError as error:
            _replace_collision_error(
                error,
                codes={'username_taken', 'unique'},
                message=USERNAME_UNAVAILABLE_MESSAGE,
            )
        value = normalize_username(value)
        if users_matching_username(value).exists():
            raise forms.ValidationError(
                USERNAME_UNAVAILABLE_MESSAGE,
                code='identity_unavailable',
            )
        return value


class NormalizedAdminUserChangeForm(UserChangeForm):
    def clean_username(self):
        value = normalize_username(self.cleaned_data.get('username'))
        matches = users_matching_username(value)
        if self.instance.pk:
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            raise forms.ValidationError(
                USERNAME_UNAVAILABLE_MESSAGE,
                code='identity_unavailable',
            )
        return value

    def clean_email(self):
        value = normalize_email(self.cleaned_data.get('email'))
        if not value:
            return ''
        user_id = self.instance.pk if self.instance.pk else None
        if _email_is_used_by_another_account(value, user_id=user_id):
            raise forms.ValidationError(
                EMAIL_UNAVAILABLE_MESSAGE,
                code='identity_unavailable',
            )
        return value


class NormalizedEmailAddressAdminForm(forms.ModelForm):
    class Meta:
        model = EmailAddress
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        value = normalize_email(cleaned_data.get('email'))
        if not value:
            return cleaned_data
        cleaned_data['email'] = value

        addresses = email_addresses_matching_email(value)
        if self.instance.pk:
            addresses = addresses.exclude(pk=self.instance.pk)
        user = cleaned_data.get('user')
        users = users_matching_email(value)
        if user is not None:
            users = users.exclude(pk=user.pk)
        if addresses.exists() or users.exists():
            self.add_error('email', EMAIL_UNAVAILABLE_MESSAGE)
        return cleaned_data
