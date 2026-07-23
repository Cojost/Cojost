# models.py

from django.db import models
from decimal import Decimal
from datetime import date, timedelta
from django.db.models import Sum
from django.contrib.auth.models import User
from django.utils import timezone


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

    
def calculate_bonus(sales, bonus_levels):
    """Return the amount from the highest active unit tier reached."""
    total_count = sum(s.count for s in sales)
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
    customer = models.CharField(max_length=100)
    dealNumber = models.IntegerField(unique=True)
    count = models.DecimalField(default=1, max_digits=2, decimal_places=1)
    frontEnd = models.DecimalField(max_digits=10, decimal_places=2)
    backend = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()

    class Meta:
        abstract = True

class Sale(BaseSale):
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    COUNT_CHOICES = [
        (2, '2'),
        (1, '1'),
        (0.5, '0.5'),
    ]

    class Meta:
        indexes = [
            models.Index(fields=['user', 'date'], name='sale_user_date_idx'),
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
        return commission_settings.calculate_front_end(self.frontEnd)

    @property
    def calculate_backend(self):
        commission_settings = Commission.objects.filter(user=self.user).first()
        if not commission_settings:
            return Decimal('0')  # Handle the case where no commission settings exist
        return commission_settings.calculate_backend(self.backend)

    @property
    def commission_total(self):
        """Return this sale's front-end and back-end commission combined."""
        commission_settings = Commission.objects.filter(user=self.user).first()
        if not commission_settings:
            return Decimal('0')
        return (
            commission_settings.calculate_front_end(self.frontEnd)
            + commission_settings.calculate_backend(self.backend)
        )

class ArchivedSale(BaseSale):
       archived_on = models.DateField(auto_now_add=True)
