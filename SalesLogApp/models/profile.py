import uuid

from PIL import Image
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models


hex_color_validator = RegexValidator(
    regex=r'^#[0-9A-Fa-f]{6}$',
    message='Enter a color in #RRGGBB format.',
)


def profile_avatar_upload_path(instance, filename):
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'png'
    if extension not in {'jpg', 'jpeg', 'png', 'webp'}:
        extension = 'png'
    return f'profile_avatars/{instance.user_id}/{uuid.uuid4().hex}.{extension}'


def validate_avatar_file(upload):
    if upload.size > 2 * 1024 * 1024:
        raise ValidationError('Profile pictures must be 2 MB or smaller.')
    position = upload.tell()
    try:
        image = Image.open(upload)
        if image.format not in {'JPEG', 'PNG', 'WEBP'}:
            raise ValidationError('Upload a JPEG, PNG, or WebP image.')
        if image.width > 10000 or image.height > 10000:
            raise ValidationError('Profile picture dimensions are too large.')
        image.verify()
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError('Upload a valid image file.') from exc
    finally:
        upload.seek(position)


class UserProfile(models.Model):
    LIGHT = 'light'
    DARK = 'dark'
    SYSTEM = 'system'
    LEGACY = 'legacy'
    PAY_PLAN_V2 = 'pay_plan_v2'
    NEW_ENGINE = 'new_engine'
    THEME_CHOICES = [
        (LIGHT, 'Light'),
        (DARK, 'Dark'),
        (SYSTEM, 'Use device setting'),
    ]
    COMMISSION_SYSTEM_CHOICES = [
        (LEGACY, 'Legacy commission system'),
        (PAY_PLAN_V2, 'Pay Plan engine'),
        # Backward-compatible value retained for existing rows.
        (NEW_ENGINE, 'New pay-plan engine (legacy value)'),
    ]
    HEADER_COLOR_CHOICES = [
        ('red', 'Red'),
        ('orange', 'Orange'),
        ('yellow', 'Yellow'),
        ('green', 'Green'),
        ('blue', 'Blue'),
        ('gray', 'Gray'),
        ('pink', 'Pink'),
        ('purple', 'Purple'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sales_profile',
    )
    avatar = models.ImageField(
        upload_to=profile_avatar_upload_path,
        blank=True,
        validators=[validate_avatar_file],
    )
    theme_mode = models.CharField(
        max_length=10, choices=THEME_CHOICES, default=SYSTEM
    )
    header_color = models.CharField(
        max_length=10, choices=HEADER_COLOR_CHOICES, default='blue'
    )
    commission_system = models.CharField(
        max_length=20,
        choices=COMMISSION_SYSTEM_CHOICES,
        default=LEGACY,
        db_index=True,
    )
    updated_at = models.DateTimeField(auto_now=True)

    def reset_header_color(self):
        self.header_color = 'blue'

    @property
    def avatar_available(self):
        """Return false when a persisted avatar path has lost its file."""
        if not self.avatar or not self.avatar.name:
            return False
        prefix = f'profile_avatars/{self.user_id}/'
        if not self.avatar.name.startswith(prefix):
            return False
        filename = self.avatar.name.removeprefix(prefix)
        if not filename or '/' in filename or '\\' in filename:
            return False
        try:
            return self.avatar.storage.exists(self.avatar.name)
        except OSError:
            return False

    def __str__(self):
        return f'Profile for {self.user}'
