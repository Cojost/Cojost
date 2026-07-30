from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import Industry, PayPlan, PayPlanAssignment, PayPlanVersion


class PayPlanFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='foundation-user',
            password='test-password',
        )
        self.industry = Industry.objects.get(slug='automotive')
        self.assignment = self.user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan__industry'
        ).get()

    def test_new_user_receives_automotive_plan_and_assignment(self):
        version = self.assignment.pay_plan_version
        self.assertEqual(version.pay_plan.industry, self.industry)
        self.assertEqual(version.pay_plan.owner_user, self.user)
        self.assertEqual(version.status, PayPlanVersion.ACTIVE)
        self.assertTrue(version.pay_plan.is_active)
        self.assertTrue(self.assignment.is_active)
        self.assertIsNone(self.assignment.effective_end_date)

    def test_owned_plan_requires_an_owner(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PayPlan.objects.create(
                industry=self.industry,
                name='Invalid ownerless plan',
                is_template=False,
            )

    def test_version_end_date_cannot_precede_start_date(self):
        version = PayPlanVersion(
            pay_plan=self.assignment.pay_plan_version.pay_plan,
            version_name='Invalid dates',
            effective_start_date=date(2026, 2, 1),
            effective_end_date=date(2026, 1, 31),
        )
        with self.assertRaises(ValidationError):
            version.full_clean()

    def test_active_version_ranges_cannot_overlap(self):
        current = self.assignment.pay_plan_version
        current.effective_end_date = date(2027, 6, 30)
        current.save(update_fields=['effective_end_date'])
        overlapping = PayPlanVersion(
            pay_plan=current.pay_plan,
            version_name='Overlapping version',
            effective_start_date=date(2027, 6, 1),
            status=PayPlanVersion.ACTIVE,
        )
        with self.assertRaises(ValidationError):
            overlapping.full_clean()

    def test_user_cannot_receive_another_users_owned_plan(self):
        other = get_user_model().objects.create_user(username='other-user')
        assignment = PayPlanAssignment(
            user=self.user,
            pay_plan_version=other.pay_plan_assignments.get().pay_plan_version,
            effective_start_date=date(2027, 1, 1),
            is_active=False,
        )
        with self.assertRaises(ValidationError):
            assignment.full_clean()

    def test_active_assignment_ranges_cannot_overlap(self):
        current = self.assignment
        current.effective_end_date = date(2027, 12, 31)
        current.save(update_fields=['effective_end_date'])
        assignment = PayPlanAssignment(
            user=self.user,
            pay_plan_version=current.pay_plan_version,
            effective_start_date=date(2027, 12, 1),
        )
        with self.assertRaises(ValidationError):
            assignment.full_clean()
