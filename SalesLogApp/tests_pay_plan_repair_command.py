from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from SalesLogApp.models import PayPlanAssignment, PayPlanOnboarding, Sale


class RepairPayPlanAssignmentsCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='repair-user',
            password='test-password',
        )
        self.onboarding = self.user.pay_plan_onboarding
        self.assignment = PayPlanAssignment.objects.get(user=self.user)
        self.version = self.onboarding.current_version

    def test_command_backdates_current_version_and_assignment_to_earliest_sale(self):
        sale_date = timezone.localdate().replace(day=1)
        later_date = sale_date + timedelta(days=10)
        self.assignment.effective_start_date = later_date
        self.assignment.save(update_fields=['effective_start_date', 'updated_at'])
        self.version.effective_start_date = later_date
        self.version.save(update_fields=['effective_start_date', 'updated_at'])
        self.onboarding.status = PayPlanOnboarding.ACTIVE
        self.onboarding.save(update_fields=['status', 'updated_at'])
        Sale.objects.create(
            user=self.user,
            customer='Repair Buyer',
            dealNumber=7001,
            count='1.0',
            frontEnd='1000.00',
            backend='250.00',
            date=sale_date,
        )

        call_command('repair_pay_plan_assignments', '--username', self.user.username)

        self.assignment.refresh_from_db()
        self.version.refresh_from_db()
        self.assertEqual(self.assignment.effective_start_date, sale_date)
        self.assertEqual(self.version.effective_start_date, sale_date)

    def test_command_errors_for_unknown_username(self):
        with self.assertRaisesMessage(Exception, 'Unknown username(s): missing-user'):
            call_command('repair_pay_plan_assignments', '--username', 'missing-user')