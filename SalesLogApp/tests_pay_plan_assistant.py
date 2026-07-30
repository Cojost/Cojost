from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import PayPlanChangeRequest, PayPlanRule, PayPlanVersion, UserProfile
from .pay_plan_assistant import create_plain_text_change_draft


class PayPlanAssistantTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='plain-language-owner',
            password='test-password',
        )
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        assignment = self.user.pay_plan_assignments.get()
        self.version = assignment.pay_plan_version
        self.version.status = PayPlanVersion.ACTIVE
        self.version.save(update_fields=['status', 'updated_at'])
        self.version.rules.all().delete()
        PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Standard Volume Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [
                    {
                        'minimum_units': '12',
                        'maximum_units': '15.5',
                        'amount': '750.00',
                    },
                ],
                'tier_mode': 'highest_only',
            },
        )

    def test_plain_text_request_creates_review_draft_and_changes_only_target(self):
        change = create_plain_text_change_draft(
            self.user,
            'Change the standard bonus at 12 units from $750 to $1,000.',
            timezone.localdate() + timedelta(days=1),
        )

        self.assertEqual(change.status, PayPlanChangeRequest.NEEDS_REVIEW)
        self.assertEqual(change.source_version, self.version)
        self.assertEqual(change.draft_version.status, PayPlanVersion.REVIEW_REQUIRED)
        draft_rule = change.draft_version.rules.get(name='Standard Volume Bonus')
        self.assertEqual(
            draft_rule.configuration['tiers'][0]['amount'],
            '1000.00',
        )
        self.version.refresh_from_db()
        self.assertEqual(self.version.status, PayPlanVersion.ACTIVE)
        self.assertEqual(
            self.version.rules.get().configuration['tiers'][0]['amount'],
            '750.00',
        )

    def test_unrecognized_request_does_not_leave_a_draft(self):
        version_count = PayPlanVersion.objects.count()

        with self.assertRaises(ValidationError):
            create_plain_text_change_draft(
                self.user,
                'Please make everything better.',
                timezone.localdate() + timedelta(days=1),
            )

        self.assertEqual(PayPlanVersion.objects.count(), version_count)
        self.assertFalse(PayPlanChangeRequest.objects.exists())
