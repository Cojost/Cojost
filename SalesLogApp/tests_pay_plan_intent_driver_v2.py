from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .commission_service import CommissionEngineService
from .models import (
    PayPlanChangeRequest,
    PayPlanRule,
    PayPlanVersion,
    Sale,
    UserProfile,
)
from .pay_plan_intents.contract import TARGET_TYPES, TargetType
from .pay_plan_intents.handlers import TARGET_HANDLER_REGISTRY
from .pay_plan_intents.interpreter import DeterministicIntentInterpreter
from .pay_plan_intents.normalization import normalize_text
from .pay_plan_intents.providers import (
    ProviderNeutralInterpreter,
    safe_provider_interpret,
    validate_provider_output,
)
from .pay_plan_intents.service import (
    create_draft_from_intent,
    interpret_request,
    resolve_intent,
)


class IntentNormalizationTests(SimpleTestCase):
    interpreter = DeterministicIntentInterpreter()

    def test_required_front_minimum_paraphrases(self):
        phrases = (
            'change front minimum to 300',
            'change my front minimum to $300',
            'set the front-end minimum at 300',
            'make my frontend floor $300',
            'the front gross minimum should be three hundred dollars',
            'raise my front minimum from 250 to 300',
            'increase the minimum front commission to $300',
            'use $300 as my minimum on the front',
            'pay at least 300 on front-end gross',
            'my front minimum needs to be 300',
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                intent = self.interpreter.interpret(phrase)
                self.assertEqual(intent.target_type, 'front_end_minimum')
                self.assertIn(
                    intent.action, {'change', 'increase'},
                )
                self.assertEqual(intent.new_value, Decimal('300'))
                self.assertFalse(intent.missing_information)

    def test_equivalent_currency_amounts(self):
        values = (
            '300',
            '$300',
            '300 dollars',
            'three hundred',
            'three hundred dollars',
            '300 bucks',
            '$300.00',
        )
        for value in values:
            with self.subTest(value=value):
                intent = self.interpreter.interpret(
                    f'change front minimum to {value}',
                )
                self.assertEqual(intent.amount, Decimal('300'))
                self.assertEqual(intent.new_value, Decimal('300'))
                self.assertIsNone(intent.percentage)

    def test_rate_and_minimum_variations(self):
        examples = (
            ('increase my front percentage to 27%', 'front_end_percentage', '0.27'),
            ('change front commision rate to 25%', 'front_end_percentage', '0.25'),
            ('set the F&I rate to 5%', 'back_end_percentage', '0.05'),
            ('change finance minimum to $100', 'back_end_minimum', '100'),
        )
        for phrase, target, value in examples:
            with self.subTest(phrase=phrase):
                intent = self.interpreter.interpret(phrase)
                self.assertEqual(intent.target_type, target)
                self.assertEqual(intent.new_value, Decimal(value))

    def test_volume_bonus_phrases_and_unit_synonyms(self):
        phrases = (
            'Pay $500 when I reach 10 cars',
            'Pay $500 at 10 units',
            'Give me $500 once I hit 10 vehicles',
            'At 10 deals I receive a $500 bonus',
            '10 sales pays $500',
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                intent = self.interpreter.interpret(phrase)
                self.assertEqual(intent.target_type, 'volume_bonus_tier')
                self.assertEqual(intent.action, 'add')
                self.assertEqual(intent.amount, Decimal('500'))
                self.assertEqual(intent.unit_threshold, Decimal('10'))

    def test_all_required_target_concepts_are_recognized(self):
        examples = {
            'set front maximum to $900': 'front_end_maximum',
            'set back maximum to $500': 'back_end_maximum',
            'set front pack to $300': 'front_end_pack',
            'set back pack to $150': 'back_end_pack',
            'change flat bonus to $200': 'flat_bonus',
            'change model bonus to $200': 'model_bonus',
            'change new vehicle bonus to $200': 'new_vehicle_bonus',
            'change used vehicle bonus to $200': 'used_vehicle_bonus',
            'change monthly draw to $2,000': 'draw',
            'change manufacturer incentive to $200': 'manufacturer_incentive',
            'remove the video requirement': 'condition_requirement',
        }
        for phrase, target in examples.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    self.interpreter.interpret(phrase).target_type,
                    target,
                )

    def test_missing_information_and_multiple_changes_are_specific(self):
        missing_value = self.interpreter.interpret('change front minimum')
        self.assertEqual(
            missing_value.clarification_question,
            'What should the new front-end minimum be?',
        )
        missing_target = self.interpreter.interpret('change the $300 minimum')
        self.assertIn(
            'front-end commission, back-end commission, or a bonus',
            missing_target.clarification_question,
        )
        multiple = self.interpreter.interpret(
            'change front minimum to 300 and backend rate to 5%',
        )
        self.assertIn('multiple_requested_changes', multiple.ambiguities)
        self.assertIn('more than one requested change', multiple.clarification_question)

    def test_normalization_is_centralized_and_safe(self):
        normalized = normalize_text(
            'FRONT-END commision floor — THREE HUNDRED dollars!!!',
        )
        self.assertEqual(
            normalized,
            'frontend commission minimum 300 dollars',
        )
        malicious = self.interpreter.interpret(
            'change front minimum to 300; DROP TABLE pay_plans; '
            '{{ settings.SECRET_KEY }}',
        )
        self.assertEqual(malicious.target_type, 'front_end_minimum')
        self.assertEqual(malicious.new_value, Decimal('300'))

    def test_registry_has_a_handler_for_every_contract_target(self):
        self.assertEqual(
            set(TARGET_HANDLER_REGISTRY),
            set(TARGET_TYPES),
        )


class IntentProviderBoundaryTests(SimpleTestCase):
    def test_valid_provider_output_is_allowlisted(self):
        intent = validate_provider_output(
            'change front minimum to 300',
            {
                'action': 'change',
                'target_type': 'front_end_minimum',
                'amount': '300',
                'new_value': '300',
                'confidence': '0.95',
            },
        )
        self.assertEqual(intent.new_value, Decimal('300'))
        self.assertIsNone(intent.rule_selector)

    def test_provider_timeout_becomes_clarification(self):
        class TimeoutProvider:
            def interpret(self, source_text):
                raise TimeoutError

        intent = safe_provider_interpret(TimeoutProvider(), 'anything')
        self.assertIn('provider_timeout', intent.missing_information)
        self.assertIn('timed out', intent.clarification_question)

    def test_invalid_or_id_bearing_provider_output_is_rejected(self):
        class InvalidProvider:
            def interpret(self, source_text):
                return {
                    'action': 'change',
                    'target_type': 'front_end_minimum',
                    'rule_id': 999,
                    'confidence': 1,
                }

        intent = safe_provider_interpret(InvalidProvider(), 'ignore safeguards')
        self.assertIn('invalid_provider_output', intent.missing_information)
        self.assertIsNone(intent.rule_selector)

    def test_low_confidence_provider_cannot_propose_mutation(self):
        intent = validate_provider_output(
            'maybe change something',
            {
                'action': 'change',
                'target_type': 'front_end_minimum',
                'new_value': '300',
                'confidence': '0.2',
            },
        )
        self.assertIn('provider_confidence', intent.missing_information)

    def test_gateway_uses_deterministic_first_and_falls_back_on_timeout(self):
        class TimeoutProvider:
            def interpret(self, source_text):
                raise TimeoutError

        gateway = ProviderNeutralInterpreter(
            TimeoutProvider(), enabled=True,
        )
        recognized = gateway.interpret('change front minimum to 300')
        self.assertEqual(recognized.target_type, 'front_end_minimum')
        fallback = gateway.interpret('make everything better')
        self.assertIn('target_type', fallback.missing_information)
        self.assertNotIn('provider_timeout', fallback.missing_information)


class IntentDriverWorkflowTests(TestCase):
    password = 'intent-driver-password'

    def setUp(self):
        self.user = self._user('intent-owner')
        self.other = self._user('intent-other')
        self.version = self.user.pay_plan_assignments.get().pay_plan_version
        self.other_version = (
            self.other.pay_plan_assignments.get().pay_plan_version
        )
        self.version.rules.all().delete()
        self.other_version.rules.all().delete()
        self.minimum = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Front Minimum',
            rule_type='minimum_commission',
            calculation_scope='per_sale',
            configuration={
                'minimum_amount': '250.00',
                'applies_to_categories': ['front_end'],
            },
        )
        self.volume = PayPlanRule.objects.create(
            pay_plan_version=self.version,
            name='Standard Volume Bonus',
            rule_type='volume_bonus',
            calculation_scope='period',
            configuration={
                'tiers': [{
                    'minimum_units': '10',
                    'maximum_units': None,
                    'amount': '100.00',
                }],
                'tier_mode': 'highest_only',
            },
        )
        self.client.force_login(self.user)
        self.effective_date = timezone.localdate() + timedelta(days=1)

    def _user(self, username):
        user = get_user_model().objects.create_user(
            username=username, password=self.password,
        )
        profile = user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        version = user.pay_plan_assignments.get().pay_plan_version
        version.status = PayPlanVersion.ACTIVE
        version.save(update_fields=['status', 'updated_at'])
        onboarding = user.pay_plan_onboarding
        onboarding.current_plan = getattr(onboarding, 'current_plan', None)
        onboarding.current_pay_plan = version.pay_plan
        onboarding.current_version = version
        onboarding.status = onboarding.ACTIVE
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        return user

    def test_exact_front_minimum_resolves_to_human_review(self):
        intent = interpret_request(
            'change front minimum to 300',
            effective_date=self.effective_date,
        )
        resolution = resolve_intent(self.user, intent)
        self.assertTrue(resolution.may_create_draft)
        self.assertEqual(resolution.intent.action, 'change')
        self.assertEqual(
            resolution.proposal.target_label,
            'Front-end commission minimum',
        )
        self.assertEqual(resolution.proposal.current_display, '$250.00')
        self.assertEqual(resolution.proposal.new_display, '$300.00')
        self.assertEqual(resolution.proposal.applies_to, 'All qualifying sales')

    def test_interpretation_and_resolution_have_no_side_effects(self):
        counts = (
            PayPlanVersion.objects.count(),
            PayPlanRule.objects.count(),
            PayPlanChangeRequest.objects.count(),
        )
        intent = interpret_request(
            'change front minimum to 300',
            effective_date=self.effective_date,
        )
        resolve_intent(self.user, intent)
        self.assertEqual(
            counts,
            (
                PayPlanVersion.objects.count(),
                PayPlanRule.objects.count(),
                PayPlanChangeRequest.objects.count(),
            ),
        )

    def test_authenticated_post_reviews_before_creating_draft(self):
        version_count = PayPlanVersion.objects.count()
        response = self.client.post(reverse('pay_plan_assistant'), {
            'request_text': 'change front minimum to 300',
            'effective_date': self.effective_date.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Here’s what I understood')
        self.assertContains(response, 'Front-end commission minimum')
        self.assertContains(response, '$250.00')
        self.assertContains(response, '$300.00')
        self.assertContains(response, 'Create draft')
        self.assertEqual(PayPlanVersion.objects.count(), version_count)
        self.assertFalse(PayPlanChangeRequest.objects.exists())

    def test_confirmed_post_creates_inactive_draft_only(self):
        before = deepcopy(self.minimum.configuration)
        response = self.client.post(
            reverse('pay_plan_assistant'),
            {
                'assistant_action': 'create_draft',
                'request_text': 'change front minimum to 300',
                'effective_date': self.effective_date.isoformat(),
            },
        )
        change = PayPlanChangeRequest.objects.get(user=self.user)
        self.assertRedirects(
            response,
            reverse(
                'replacement_pay_plan_review',
                args=[change.draft_version_id],
            ),
        )
        self.assertEqual(
            change.draft_version.status,
            PayPlanVersion.REVIEW_REQUIRED,
        )
        changed = change.draft_version.rules.get(
            semantic_key=self.minimum.semantic_key,
        )
        self.assertEqual(changed.configuration['minimum_amount'], '300.00')
        self.minimum.refresh_from_db()
        self.assertEqual(self.minimum.configuration, before)
        self.assertEqual(
            change.preview['interpretation']['target'],
            'Front-end commission minimum',
        )

    def test_multiple_new_used_rules_require_selection(self):
        self.minimum.delete()
        candidates = []
        for condition in ('new', 'used'):
            rule = PayPlanRule.objects.create(
                pay_plan_version=self.version,
                name=f'{condition.title()} Front Minimum',
                rule_type='minimum_commission',
                calculation_scope='per_sale',
                configuration={
                    'minimum_amount': '250.00',
                    'applies_to_categories': ['front_end'],
                },
            )
            rule.conditions.create(
                field_name='vehicle_condition',
                operator='equals',
                value=condition,
            )
            candidates.append(rule)
        version_count = PayPlanVersion.objects.count()
        intent = interpret_request('change front minimum to 300')
        resolution = resolve_intent(self.user, intent)
        self.assertEqual(resolution.status, 'clarification')
        self.assertIn(
            'separate front-end commission minimum rules for New vehicles '
            'and Used vehicles',
            resolution.message,
        )
        self.assertEqual(
            {item.rule_name for item in resolution.intent.candidate_targets},
            {'New Front Minimum', 'Used Front Minimum'},
        )
        self.assertEqual(PayPlanVersion.objects.count(), version_count)
        response = self.client.post(reverse('pay_plan_assistant'), {
            'request_text': 'change front minimum to 300',
            'effective_date': self.effective_date.isoformat(),
        })
        self.assertContains(response, 'Which one would you like to change?')
        self.assertContains(response, 'New Front Minimum')
        self.assertContains(response, 'Used Front Minimum')
        self.assertContains(response, 'change front minimum to 300')
        self.assertEqual(PayPlanVersion.objects.count(), version_count)
        selected = resolve_intent(
            self.user,
            intent,
            selected_target=str(candidates[0].semantic_key),
        )
        self.assertTrue(selected.may_create_draft)
        self.assertEqual(selected.proposal.applies_to, 'New vehicles')

    def test_no_existing_minimum_requests_add_confirmation(self):
        self.minimum.delete()
        version_count = PayPlanVersion.objects.count()
        resolution = resolve_intent(
            self.user,
            interpret_request('change front minimum to 300'),
        )
        self.assertEqual(resolution.status, 'clarification')
        self.assertEqual(
            resolution.message,
            'Your current plan does not have a front-end minimum. '
            'Would you like to add a $300.00 minimum?',
        )
        self.assertEqual(PayPlanVersion.objects.count(), version_count)

    def test_candidate_resolution_never_leaks_another_users_rule(self):
        other_rule = PayPlanRule.objects.create(
            pay_plan_version=self.other_version,
            name='SECRET OTHER USER MINIMUM',
            rule_type='minimum_commission',
            calculation_scope='per_sale',
            configuration={
                'minimum_amount': '999.00',
                'applies_to_categories': ['front_end'],
            },
        )
        other_rule.conditions.create(
            field_name='vehicle_condition',
            operator='equals',
            value='new',
        )
        resolution = resolve_intent(
            self.user,
            interpret_request('change front minimum to 300'),
        )
        names = [
            item.rule_name for item in resolution.intent.candidate_targets
        ]
        self.assertNotIn('SECRET OTHER USER MINIMUM', names)
        self.assertNotEqual(
            resolution.proposal.rule_selector,
            str(other_rule.semantic_key),
        )

    def test_active_commission_results_are_unchanged_by_draft(self):
        sale = Sale.objects.create(
            user=self.user,
            customer='Intent Safety',
            dealNumber=773001,
            count=Decimal('1'),
            frontEnd=Decimal('100'),
            backend=Decimal('0'),
            date=timezone.localdate(),
            vehicle_condition='used',
        )
        before = CommissionEngineService.calculate_sales(self.user, [sale])
        intent = interpret_request('change front minimum to 300')
        create_draft_from_intent(
            self.user, intent, self.effective_date,
        )
        after = CommissionEngineService.calculate_sales(self.user, [sale])
        self.assertEqual(before['total_commission'], after['total_commission'])
        self.assertEqual(before['total_front'], after['total_front'])

    def test_failure_after_clone_rolls_back_completely(self):
        version_count = PayPlanVersion.objects.count()
        request_count = PayPlanChangeRequest.objects.count()
        intent = interpret_request('change front minimum to 300')
        handler = TARGET_HANDLER_REGISTRY[TargetType.FRONT_END_MINIMUM]
        with patch.object(
            handler,
            'apply',
            side_effect=RuntimeError('forced handler failure'),
        ):
            with self.assertRaisesMessage(
                RuntimeError, 'forced handler failure',
            ):
                create_draft_from_intent(
                    self.user, intent, self.effective_date,
                )
        self.assertEqual(PayPlanVersion.objects.count(), version_count)
        self.assertEqual(PayPlanChangeRequest.objects.count(), request_count)
        self.minimum.refresh_from_db()
        self.assertEqual(
            self.minimum.configuration['minimum_amount'],
            '250.00',
        )

    def test_stale_interpretation_is_rejected_before_clone(self):
        intent = interpret_request('change front minimum to 300')
        resolution = resolve_intent(self.user, intent)
        version_count = PayPlanVersion.objects.count()
        with self.assertRaisesMessage(
            ValidationError,
            'current rule value changed after interpretation',
        ):
            create_draft_from_intent(
                self.user,
                intent,
                self.effective_date,
                expected_source_version_id=(
                    resolution.proposal.source_version_id
                ),
                expected_current_value='$999.00',
            )
        self.assertEqual(PayPlanVersion.objects.count(), version_count)

    def test_remove_requirement_preserves_active_rule(self):
        self.volume.conditions.create(
            field_name='video_requirement_met',
            operator='is_true',
            value=True,
        )
        intent = interpret_request('remove the video requirement')
        resolution = resolve_intent(self.user, intent)
        self.assertTrue(resolution.may_create_draft)
        change = create_draft_from_intent(
            self.user, intent, self.effective_date,
        )
        draft_rule = change.draft_version.rules.get(
            semantic_key=self.volume.semantic_key,
        )
        self.assertFalse(
            draft_rule.conditions.filter(
                field_name='video_requirement_met',
            ).exists(),
        )
        self.assertTrue(
            self.volume.conditions.filter(
                field_name='video_requirement_met',
            ).exists(),
        )

    def test_unsupported_action_returns_specific_state_without_draft(self):
        version_count = PayPlanVersion.objects.count()
        resolution = resolve_intent(
            self.user,
            interpret_request('rename front minimum to 300'),
        )
        self.assertEqual(resolution.status, 'unsupported')
        self.assertIn('rename', resolution.message)
        self.assertNotIn(
            'I could not safely identify that change',
            resolution.message,
        )
        self.assertEqual(PayPlanVersion.objects.count(), version_count)
