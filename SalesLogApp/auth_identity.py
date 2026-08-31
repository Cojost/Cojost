from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Lower, Trim
from django.utils.translation import gettext_lazy as _

from allauth.account.models import EmailAddress


EMAIL_UNAVAILABLE_MESSAGE = _(
    'This email address is unavailable. Please use a different email address.'
)
USERNAME_UNAVAILABLE_MESSAGE = _(
    'This username is unavailable. Please choose a different username.'
)


class NormalizedIdentityCollision(Exception):
    """A normalized identity collision that has been attached to a form."""


def normalize_email(value):
    return (value or '').strip().lower()


def normalize_username(value):
    return (value or '').strip()


def _matching_normalized(queryset, field_name, value, *, alias):
    normalized = (value or '').strip().lower()
    if not normalized:
        return queryset.none()
    return queryset.alias(
        **{alias: Lower(Trim(F(field_name)))},
    ).filter(**{alias: normalized})


def users_matching_email(value):
    user_model = get_user_model()
    return _matching_normalized(
        user_model._default_manager.all(),
        user_model.get_email_field_name(),
        value,
        alias='_auth_identity_normalized_user_email',
    )


def users_matching_username(value):
    user_model = get_user_model()
    return _matching_normalized(
        user_model._default_manager.all(),
        user_model.USERNAME_FIELD,
        value,
        alias='_auth_identity_normalized_username',
    )


def email_addresses_matching_email(value):
    return _matching_normalized(
        EmailAddress.objects.all(),
        'email',
        value,
        alias='_auth_identity_normalized_address_email',
    )


@transaction.atomic
def synchronize_primary_address_from_user(user):
    """Apply an admin User.email edit to allauth without sending email."""
    normalized = normalize_email(user.email)
    if not normalized:
        # Blank compatibility emails remain supported for legacy/admin users.
        return None

    addresses = EmailAddress.objects.select_for_update().filter(user_id=user.pk)
    target = _matching_normalized(
        addresses,
        'email',
        normalized,
        alias='_auth_identity_admin_target_email',
    ).order_by('pk').first()
    addresses.filter(primary=True).exclude(
        pk=target.pk if target else None,
    ).update(primary=False)
    if target is None:
        target = EmailAddress.objects.create(
            user_id=user.pk,
            email=normalized,
            primary=True,
            verified=False,
        )
    else:
        update_fields = []
        if target.email != normalized:
            target.email = normalized
            update_fields.append('email')
        if not target.primary:
            target.primary = True
            update_fields.append('primary')
        if update_fields:
            target.save(update_fields=update_fields)
    return target


@transaction.atomic
def synchronize_user_from_addresses(user_id):
    """Keep the compatibility email aligned after an allauth admin write."""
    user_model = get_user_model()
    user = user_model._default_manager.select_for_update().get(pk=user_id)
    addresses = EmailAddress.objects.filter(user_id=user_id)
    address = addresses.filter(primary=True).order_by('pk').first()
    if address is None and user.email:
        address = _matching_normalized(
            addresses,
            'email',
            user.email,
            alias='_auth_identity_current_compatibility_email',
        ).order_by('pk').first()
    if address is None:
        address = addresses.order_by('-verified', 'pk').first()
    normalized = normalize_email(address.email) if address else ''
    if user.email != normalized:
        user_model._default_manager.filter(pk=user_id).update(email=normalized)
        user.email = normalized
    return user
