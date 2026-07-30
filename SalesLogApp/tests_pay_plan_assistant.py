from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
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

    def create_change(self, text):
        return create_plain_text_change_draft(
            self.user, text, timezone.localdate() + timedelta(days=1),
        )

    def added_tier(self, change, rule_name='Standard Volume Bonus'):
        rule = change.draft_version.rules.get(name=rule_name)
        return rule, next(
            tier for tier in rule.configuration['tiers']
            if Decimal(str(tier['minimum_units'])) == Decimal('8')
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

    def test_pay_amount_at_units_adds_tier(self):
        change = self.create_change('Pay $250 at 8 units')
        rule, tier = self.added_tier(change)

        self.assertEqual(tier['amount'], '250.00')
        self.assertEqual(tier['maximum_units'], '11.5')
        self.assertEqual(change.parsed_actions[-1]['action_type'], 'add_volume_tier')
        self.assertEqual(rule.calculation_scope, 'period')

    def test_explicit_add_unit_bonus_adds_tier(self):
        change = self.create_change('Add a unit bonus at 8 units for $250')
        self.assertEqual(self.added_tier(change)[1]['amount'], '250.00')

    def test_supported_amount_before_threshold_phrases(self):
        phrases = (
            'Give me $250 when I reach 8 units',
            'Start paying $250 once I hit 8 cars',
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                change = self.create_change(phrase)
                self.assertEqual(self.added_tier(change)[1]['amount'], '250.00')
                change.draft_version.delete()

    def test_supported_threshold_before_amount_phrases(self):
        phrases = (
            'Create an 8 unit bonus worth $250',
            'At 8 units I receive a $250 bonus',
            '8 units pays $250',
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                change = self.create_change(phrase)
                self.assertEqual(self.added_tier(change)[1]['amount'], '250.00')
                change.draft_version.delete()

    def test_vehicle_deal_and_sale_synonyms(self):
        for noun in ('vehicle', 'deal', 'sale'):
            with self.subTest(noun=noun):
                change = self.create_change(
                    f'Pay 250 dollars at 8 {noun}s',
                )
                self.assertEqual(self.added_tier(change)[1]['amount'], '250.00')
                change.draft_version.delete()

    def test_half_unit_threshold_is_supported(self):
        change = self.create_change('Pay $250.00 at 8.5 units')
        rule = change.draft_version.rules.get(name='Standard Volume Bonus')
        tier = next(
            item for item in rule.configuration['tiers']
            if item['minimum_units'] == '8.5'
        )
        self.assertEqual(tier['maximum_units'], '11.5')

    def test_existing_tiers_are_preserved_and_overlap_is_adjusted(self):
        rule = self.version.rules.get()
        rule.configuration = {
            'tiers': [
                {'minimum_units': '5', 'maximum_units': '11.5', 'amount': '100'},
                {'minimum_units': '12', 'maximum_units': '15.5', 'amount': '750'},
            ],
            'tier_mode': 'cumulative',
        }
        rule.save(update_fields=['configuration', 'updated_at'])

        change = self.create_change('Pay $250 at 8 units')
        draft_rule = change.draft_version.rules.get()
        self.assertEqual(draft_rule.configuration['tier_mode'], 'cumulative')
        self.assertEqual(
            [tier['minimum_units'] for tier in draft_rule.configuration['tiers']],
            ['5', '8', '12'],
        )
        self.assertEqual(
            draft_rule.configuration['tiers'][0]['maximum_units'], '7.5',
        )
        self.assertTrue(change.warnings)
        self.assertEqual(
            change.parsed_actions[0]['action_type'],
            'adjust_volume_tier_range',
        )

    def test_creates_standard_rule_when_none_exists_without_conditions(self):
        self.version.rules.all().delete()
        change = self.create_change('Pay $250 at 8 units')
        rule = change.draft_version.rules.get()

        self.assertEqual(rule.rule_type, 'volume_bonus')
        self.assertEqual(rule.calculation_scope, 'period')
        self.assertEqual(rule.configuration['tier_mode'], 'highest_only')
        self.assertFalse(rule.conditions.exists())

    def test_dealership_specific_rule_is_not_copied_into_standard_bonus(self):
        self.version.rules.all().delete()
        special = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Green Pea Volume Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [{'minimum_units': '5', 'amount': '500'}],
                'tier_mode': 'highest_only',
            },
        )
        special.conditions.create(
            field_name='green_pea', operator='is_true', value=True,
        )

        change = self.create_change('Pay $250 at 8 units')
        standard = change.draft_version.rules.get(name='Standard Volume Bonus')
        cloned_special = change.draft_version.rules.get(
            name='Green Pea Volume Bonus',
        )
        self.assertFalse(standard.conditions.exists())
        self.assertEqual(
            cloned_special.configuration['tiers'],
            [{'minimum_units': '5', 'amount': '500'}],
        )

    def test_duplicate_threshold_requests_specific_clarification(self):
        rule = self.version.rules.get()
        rule.configuration['tiers'].insert(
            0, {'minimum_units': '8', 'maximum_units': '11.5', 'amount': '200'},
        )
        rule.save(update_fields=['configuration', 'updated_at'])
        version_count = PayPlanVersion.objects.count()

        with self.assertRaisesMessage(
            ValidationError,
            'already has a tier beginning at 8 units that pays $200.00',
        ):
            self.create_change('Pay $250 at 8 units')

        self.assertEqual(PayPlanVersion.objects.count(), version_count)

    def test_missing_amount_and_threshold_are_rejected(self):
        for phrase in ('Pay a bonus at 8 units', 'Pay $250 as a bonus'):
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValidationError):
                    self.create_change(phrase)

    def test_active_plan_remains_unchanged(self):
        original = deepcopy(self.version.rules.get().configuration)
        self.create_change('Pay $250 at 8 units')
        self.version.rules.get().refresh_from_db()
        self.assertEqual(self.version.rules.get().configuration, original)

    def test_new_rule_is_owned_by_users_inactive_draft_only(self):
        self.version.rules.all().delete()
        other = get_user_model().objects.create_user(
            username='other-owner', password='test-password',
        )
        other_version = other.pay_plan_assignments.get().pay_plan_version
        other_rule = PayPlanRule.objects.create(
            pay_plan_version=other_version,
            name='Dealer-specific Volume Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [{'minimum_units': '8', 'amount': '999'}],
                'tier_mode': 'highest_only',
            },
        )
        other_rule.conditions.create(
            field_name='green_pea', operator='is_true', value=True,
        )

        change = self.create_change('Pay $250 at 8 units')
        draft_rule = change.draft_version.rules.get()
        self.assertEqual(
            draft_rule.pay_plan_version.pay_plan.owner_user, self.user,
        )
        self.assertEqual(change.draft_version.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertFalse(draft_rule.conditions.exists())
        other_rule.refresh_from_db()
        self.assertEqual(other_rule.configuration['tiers'][0]['amount'], '999')

    def test_authenticated_view_posts_exact_request_and_reaches_review(self):
        original = deepcopy(self.version.rules.get().configuration)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('pay_plan_assistant'),
            {
                'request_text': 'Pay $250 at 8 units',
                'effective_date': (
                    timezone.localdate() + timedelta(days=1)
                ).isoformat(),
                'confirm_retroactive': 'on',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.resolver_match.url_name, 'replacement_pay_plan_review',
        )
        self.assertContains(response, 'Pay $250 at 8 units')
        self.assertContains(response, '250.00')
        change = PayPlanChangeRequest.objects.get(user=self.user)
        self.assertEqual(change.draft_version.status, PayPlanVersion.REVIEW_REQUIRED)
        self.version.rules.get().refresh_from_db()
        self.assertEqual(self.version.rules.get().configuration, original)
