from allauth.account.forms import AddEmailForm, SignupForm
from allauth.account.models import EmailAddress
from django import forms
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.db import IntegrityError, transaction

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
