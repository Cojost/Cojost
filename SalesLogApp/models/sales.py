# models.py

from django.db import models
from decimal import Decimal
from datetime import date, timedelta
from django.db.models import Sum
from django.contrib.auth.models import User
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone


class SaleType(models.TextChoices):
    AUTOMOTIVE = 'automotive', 'Automotive'


class Commission(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, )
    frontend_minimum = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    frontend_maximum = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    total_calculated_front_end = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, default=Decimal('0.00'))
    opt_out_front = models.BooleanField(default=False)

    backend_minimum = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    backend_maximum = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    total_calculated_back_end = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal('0'))
    opt_out_back = models.BooleanField(default=False)
    
    

    bonus_levels = models.ManyToManyField('BonusLevel', blank=True, related_name='commissions')

    def calculate_front_end(self, front_end):
        """ Calculate front-end commission while considering opt-out, min, and max """
        if self.opt_out_front:
            return Decimal('0')

        front_end_decimal = Decimal(str(front_end))
        
        if self.total_calculated_front_end is None:
            return Decimal('0')

        calculated_front = front_end_decimal * self.total_calculated_front_end

        # Apply minimum and maximum checks
        if self.frontend_minimum is not None:
            calculated_front = max(calculated_front, self.frontend_minimum)
        if self.frontend_maximum is not None:
            calculated_front = min(calculated_front, self.frontend_maximum)

        return calculated_front
    
    

    def calculate_backend(self, back_end):
        """ Calculate back-end commission while considering opt-out, min, and max """
        if self.opt_out_back:
            return Decimal('0')

        backend_decimal = Decimal(str(back_end))
        if backend_decimal == 0:
            return Decimal('0')

        calculated_back = backend_decimal * self.total_calculated_back_end

        # Apply minimum and maximum checks
        if self.backend_minimum is not None:
            calculated_back = max(calculated_back, self.backend_minimum)
        if self.backend_maximum is not None:
            calculated_back = min(calculated_back, self.backend_maximum)

        return calculated_back

class BonusLevel(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True)
    count_threshold = models.PositiveIntegerField()  # The minimum count required for this bonus
    amount = models.DecimalField(max_digits=10, decimal_places=2)  # Bonus amount for this level
    active = models.BooleanField(default=True)  # Indicates if the bonus is currently active
    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name='bonus_levels_set')
    tied_to_units = models.BooleanField(default=False)  # New field to indicate if the bonus is tied to units

    class Meta:
        indexes = [
            models.Index(
                fields=['commission', 'active', 'count_threshold'],
                name='bonus_comm_active_count_idx',
            ),
        ]


    def __str__(self):
        return f"Bonus Level {self.count_threshold}: ${self.amount} (Active: {self.active})"


class CommissionAdjustment(models.Model):
    BONUS = 'bonus'
    DEDUCTION = 'deduction'
    KIND_CHOICES = [
        (BONUS, 'Bonus'),
        (DEDUCTION, 'Deduction'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    commission = models.ForeignKey(
        Commission,
        on_delete=models.CASCADE,
        related_name='adjustments',
    )
    description = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(
                fields=['commission', 'user', 'active'],
                name='comm_adj_owner_active_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name='commission_adjustment_nonnegative',
            ),
        ]

    @property
    def signed_amount(self):
        if self.kind == self.DEDUCTION:
            return -self.amount
        return self.amount

    def __str__(self):
        return f"{self.get_kind_display()}: {self.description} (${self.amount})"

    
def get_commission_multiplier(deal_count):
    count = Decimal(str(deal_count or 1))
    if count == Decimal('0.5'):
        return Decimal('0.5')
    return Decimal('1')


def calculate_bonus(sales, bonus_levels):
    """Return the amount from the highest active unit tier reached."""
    total_count = sum((s.unit_credit for s in sales), Decimal('0'))
    qualifying_levels = (
        bonus_level for bonus_level in bonus_levels
        if bonus_level.active and total_count >= bonus_level.count_threshold
    )
    highest_level = max(
        qualifying_levels,
        key=lambda bonus_level: bonus_level.count_threshold,
        default=None,
    )
    return highest_level.amount if highest_level else Decimal('0.00')

class Customer(models.Model):

    name = models.CharField(max_length=100)
    dealNumber = models.IntegerField(unique=True)

class BaseSale(models.Model):
    VEHICLE_CONDITION_CHOICES = [
        ('new', 'New'),
        ('used', 'Used'),
        ('retired_sslp', 'Retired SSLP'),
    ]
    ACQUISITION_SOURCE_CHOICES = [
        ('', 'Not applicable'),
        ('street_curb', 'Street / Curb'),
        ('current_service_customer', 'Current Service Customer'),
        ('trade_in', 'Trade-in'),
        ('auction', 'Auction'),
        ('dealer_purchase', 'Dealer purchase'),
        ('other', 'Other'),
    ]
    customer = models.CharField(max_length=100)
    dealNumber = models.IntegerField(unique=True)
    count = models.DecimalField(default=1, max_digits=2, decimal_places=1)
    split_with_name = models.CharField(max_length=100, blank=True, default='')
    frontEnd = models.DecimalField(max_digits=10, decimal_places=2)
    backend = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    vehicle_condition = models.CharField(
        max_length=16,
        choices=VEHICLE_CONDITION_CHOICES,
        blank=True,
        default='',
        help_text='Required when the pay plan has different new and used rules.',
    )
    acquisition_source = models.CharField(
        max_length=32,
        choices=ACQUISITION_SOURCE_CHOICES,
        blank=True,
        default='',
        help_text='Select how the dealership acquired this vehicle, when applicable.',
    )
    custom_pay_plan_fields = models.JSONField(
        default=dict,
        blank=True,
        help_text='Additional sale-level facts requested by an imported pay plan.',
    )
    sale_type = models.CharField(
        max_length=32,
        choices=SaleType.choices,
        default=SaleType.AUTOMOTIVE,
        db_index=True,
    )

    class Meta:
        abstract = True

class Sale(BaseSale):
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    COUNT_CHOICES = [
        (Decimal('2.0'), '2'),
        (Decimal('1.0'), '1'),
        (Decimal('0.5'), '0.5'),
    ]

    class Meta:
        indexes = [
            models.Index(fields=['user', 'date'], name='sale_user_date_idx'),
            models.Index(
                fields=['user', 'sale_type', 'date'],
                name='sale_owner_type_date_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(sale_type=SaleType.AUTOMOTIVE),
                name='sale_supported_type',
            ),
        ]
   



    @property
    def total_count(self):
        return Sale.objects.filter(dealNumber=self.dealNumber).aggregate(total_count=Sum('count'))['total_count'] or 0

    @property
    def calculate_total_count(self):
        today = date.today()
        start_date = today - timedelta(days=5)
       
        return Sale.objects.filter(dealNumber=self.dealNumber).aggregate(total_count=Sum('count'))['total_count'] or 0
    
    @property
    def calculate_frontEnd(self):
        commission_settings = Commission.objects.filter(user=self.user).first()
        if not commission_settings:
            return Decimal('0')  # Handle the case where no commission settings exist
        # Calculate commission and apply sale-level commission multiplier
        # (half-deals pay half; double-count deals count as 2 units but pay 100%).
        commission = commission_settings.calculate_front_end(self.frontEnd)
        return commission * self.commission_credit_multiplier

    @property
    def calculate_backend(self):
        commission_settings = Commission.objects.filter(user=self.user).first()
        if not commission_settings:
            return Decimal('0')  # Handle the case where no commission settings exist
        commission = commission_settings.calculate_backend(self.backend)
        return commission * self.commission_credit_multiplier

    @property
    def unit_credit(self):
        return Decimal(str(self.count or 0))

    @property
    def commission_credit_multiplier(self):
        count = Decimal(str(self.count or 0))
        if count == Decimal('0.5'):
            return Decimal('0.5')
        return Decimal('1.0')

    @property
    def commission_total(self):
        """Return this sale's front-end and back-end commission combined."""
        return self.calculate_frontEnd + self.calculate_backend

class ArchivedSale(BaseSale):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='archived_sales',
    )
    archived_on = models.DateField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'date'], name='archive_user_date_idx'),
            models.Index(
                fields=['user', 'sale_type', 'date'],
                name='archive_owner_type_date_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(sale_type=SaleType.AUTOMOTIVE),
                name='archive_supported_type',
            ),
        ]


class DailyActivity(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField(default=timezone.localdate)
    leads_taken = models.PositiveIntegerField(default=0)
    phone_calls_made = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'date'], name='unique_daily_activity_user_date'
            ),
            models.CheckConstraint(
                condition=models.Q(leads_taken__gte=0), name='activity_leads_nonnegative'
            ),
            models.CheckConstraint(
                condition=models.Q(phone_calls_made__gte=0),
                name='activity_calls_nonnegative',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'date'], name='activity_user_date_idx'),
        ]

    def clean(self):
        super().clean()
        if self.date and self.date > timezone.localdate():
            raise ValidationError({'date': 'Activity dates cannot be in the future.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class MonthlyGoal(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    month_start = models.DateField()
    target_units = models.DecimalField(max_digits=8, decimal_places=1, default=0)
    target_commission = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-month_start']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'month_start'], name='unique_monthly_goal_user_month'
            ),
            models.CheckConstraint(
                condition=models.Q(target_units__gte=0), name='goal_units_nonnegative'
            ),
            models.CheckConstraint(
                condition=models.Q(target_commission__gte=0),
                name='goal_commission_nonnegative',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'month_start'], name='goal_user_month_idx'),
        ]

    def clean(self):
        super().clean()
        if self.month_start and self.month_start.day != 1:
            raise ValidationError({'month_start': 'Month must be the first day of its month.'})

    def save(self, *args, **kwargs):
        if self.month_start:
            self.month_start = self.month_start.replace(day=1)
        self.full_clean()
        return super().save(*args, **kwargs)
