from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model

from .models import Commission, Sale, BonusLevel
from .services import commission_totals


class CommissionCreditTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user('commission-user', password='pw')
        self.commission = Commission.objects.create(
            user=self.user,
            total_calculated_front_end=Decimal('0.10'),
            total_calculated_back_end=Decimal('0.10'),
        )

    def test_half_deal_properties_and_commission(self):
        sale = Sale.objects.create(
            user=self.user, customer='Half', dealNumber=99001,
            count=Decimal('0.5'), frontEnd=100, backend=50, date=timezone.localdate()
        )
        self.assertEqual(sale.unit_credit, Decimal('0.5'))
        self.assertEqual(sale.commission_credit_multiplier, Decimal('0.5'))
        # base commission would be 10 + 5 = 15, payable should be half
        self.assertEqual(sale.calculate_frontEnd + sale.calculate_backend, Decimal('7.5'))

    def test_full_deal_properties_and_commission(self):
        sale = Sale.objects.create(
            user=self.user, customer='Full', dealNumber=99002,
            count=Decimal('1.0'), frontEnd=100, backend=50, date=timezone.localdate()
        )
        self.assertEqual(sale.unit_credit, Decimal('1'))
        self.assertEqual(sale.commission_credit_multiplier, Decimal('1.0'))
        self.assertEqual(sale.calculate_frontEnd + sale.calculate_backend, Decimal('15.0'))

    def test_double_count_deal_units_and_commission(self):
        sale = Sale.objects.create(
            user=self.user, customer='Double', dealNumber=99003,
            count=Decimal('2.0'), frontEnd=100, backend=50, date=timezone.localdate()
        )
        self.assertEqual(sale.unit_credit, Decimal('2'))
        self.assertEqual(sale.commission_credit_multiplier, Decimal('1.0'))
        # commission should not be doubled
        self.assertEqual(sale.calculate_frontEnd + sale.calculate_backend, Decimal('15.0'))

    def test_half_and_double_deal_influence_on_monthly_bonus(self):
        # Bonus threshold of 2 units
        bonus = BonusLevel.objects.create(
            user=self.user, count_threshold=2, amount=Decimal('100'), active=True,
            commission=self.commission
        )
        # Add a half deal and a full deal -> units = 1.5, no bonus yet
        Sale.objects.create(user=self.user, customer='A', dealNumber=99010, count=Decimal('0.5'), frontEnd=100, backend=0, date=timezone.localdate())
        Sale.objects.create(user=self.user, customer='B', dealNumber=99011, count=Decimal('1.0'), frontEnd=100, backend=0, date=timezone.localdate())
        totals = commission_totals(self.user, list(Sale.objects.filter(user=self.user)))
        self.assertEqual(totals['units'], Decimal('1.5'))
        self.assertEqual(totals['bonus'], Decimal('0'))
        # Add a double-count deal -> units = 3.5 -> threshold met and bonus applies
        Sale.objects.create(user=self.user, customer='C', dealNumber=99012, count=Decimal('2.0'), frontEnd=100, backend=0, date=timezone.localdate())
        totals = commission_totals(self.user, list(Sale.objects.filter(user=self.user)))
        self.assertEqual(totals['units'], Decimal('3.5'))
        self.assertEqual(totals['bonus'], Decimal('100'))
