import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

VIN_VALIDATOR = RegexValidator(
    regex=r'^[A-HJ-NPR-Z0-9]{17}$',
    message='Enter a valid 17-character VIN without I, O, or Q.',
)
STOCK_VALIDATOR = RegexValidator(
    regex=r'^[A-Z0-9][A-Z0-9 ._/-]*$',
    message='Use letters, numbers, spaces, hyphens, periods, slashes, or underscores.',
)


def normalize_catalog_name(value):
    return re.sub(r'\s+', ' ', (value or '').strip()).casefold()


def display_catalog_name(value):
    return re.sub(r'\s+', ' ', (value or '').strip()).title()


def next_vehicle_year():
    return timezone.localdate().year + 1


class VehicleMake(models.Model):
    name = models.CharField(max_length=100)
    normalized_name = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(default=True)
    verified = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='created_vehicle_makes',
    )

    class Meta:
        ordering = ['name']
        indexes = [models.Index(fields=['active', 'name'], name='vehicle_make_active_idx')]

    def clean(self):
        self.name = display_catalog_name(self.name)
        self.normalized_name = normalize_catalog_name(self.name)
        if not self.normalized_name:
            raise ValidationError({'name': 'Enter a make.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class VehicleModel(models.Model):
    make = models.ForeignKey(VehicleMake, on_delete=models.PROTECT, related_name='vehicle_models')
    name = models.CharField(max_length=100)
    normalized_name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    verified = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='created_vehicle_models',
    )

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['make', 'normalized_name'], name='unique_vehicle_model_per_make'
            ),
        ]
        indexes = [models.Index(fields=['make', 'active', 'name'], name='vehicle_model_lookup_idx')]

    def clean(self):
        self.name = display_catalog_name(self.name)
        self.normalized_name = normalize_catalog_name(self.name)
        if not self.normalized_name:
            raise ValidationError({'name': 'Enter a model.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.make} {self.name}'


class Vehicle(models.Model):
    sale = models.OneToOneField('Sale', on_delete=models.CASCADE, related_name='vehicle')
    year = models.PositiveSmallIntegerField(
        db_index=True, validators=[MinValueValidator(2000)]
    )
    make = models.ForeignKey(VehicleMake, on_delete=models.PROTECT, related_name='vehicles')
    model = models.ForeignKey(VehicleModel, on_delete=models.PROTECT, related_name='vehicles')
    mileage = models.PositiveIntegerField(
        validators=[MaxValueValidator(10_000_000)]
    )
    stock_number = models.CharField(
        max_length=50, db_index=True, validators=[STOCK_VALIDATOR]
    )
    vin = models.CharField(max_length=17, db_index=True, validators=[VIN_VALIDATOR])

    class Meta:
        indexes = [
            models.Index(fields=['year', 'make', 'model'], name='vehicle_reporting_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(year__gte=2000), name='vehicle_year_gte_2000'),
            models.CheckConstraint(
                condition=models.Q(mileage__lte=10_000_000),
                name='vehicle_mileage_reasonable',
            ),
        ]

    def clean(self):
        self.stock_number = (self.stock_number or '').strip().upper()
        self.vin = (self.vin or '').strip().upper()
        if self.year and self.year > next_vehicle_year():
            raise ValidationError({'year': 'Select a year no later than next year.'})
        if self.model_id and self.make_id and self.model.make_id != self.make_id:
            raise ValidationError({'model': 'Select a model belonging to the selected make.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ArchivedVehicle(models.Model):
    archived_sale = models.OneToOneField(
        'ArchivedSale', on_delete=models.CASCADE, related_name='vehicle'
    )
    year = models.PositiveSmallIntegerField(db_index=True)
    make_name = models.CharField(max_length=100, db_index=True)
    model_name = models.CharField(max_length=100, db_index=True)
    mileage = models.PositiveIntegerField()
    stock_number = models.CharField(max_length=50, db_index=True)
    vin = models.CharField(max_length=17, db_index=True)

    class Meta:
        indexes = [
            models.Index(
                fields=['year', 'make_name', 'model_name'],
                name='archive_vehicle_report_idx',
            ),
        ]
