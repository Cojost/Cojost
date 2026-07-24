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
    THEME_CHOICES = [
        (LIGHT, 'Light'),
        (DARK, 'Dark'),
        (SYSTEM, 'Use device setting'),
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
    updated_at = models.DateTimeField(auto_now=True)

    def reset_header_color(self):
        self.header_color = 'blue'

    def __str__(self):
        return f'Profile for {self.user}'
