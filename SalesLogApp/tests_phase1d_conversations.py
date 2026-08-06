from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    PayPlanChangeRequest,
    PayPlanConversation,
    PayPlanConversationTurn,
    PayPlanRule,
    PayPlanVersion,
    UserProfile,
)
from .pay_plan_conversations import (
    PENDING_INTENT_FIELDS,
    PayPlanConversationService,
)


@override_settings(
    PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=False,
    PAY_PLAN_ASSISTANT_MAX_TURNS=12,
    PAY_PLAN_ASSISTANT_CONVERSATION_TTL_HOURS=24,
)
class Phase1DConversationTests(TestCase):
    password = 'phase-1d-password'

    def setUp(self):
        self.user = self._user('phase-1d-owner')
        self.other = self._user('phase-1d-other')
        self.version = self.user.pay_plan_assignments.get().pay_plan_version
        self.version.rules.all().delete()
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
        self.effective_date = timezone.localdate() + timedelta(days=1)
        self.client.force_login(self.user)

    def _user(self, username):
        user = get_user_model().objects.create_user(
            username=username,
            password=self.password,
        )
        profile = user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=['commission_system', 'updated_at'])
        assignment = user.pay_plan_assignments.select_related(
            'pay_plan_version__pay_plan',
        ).get()
        version = assignment.pay_plan_version
        version.status = PayPlanVersion.ACTIVE
        version.save(update_fields=['status', 'updated_at'])
        onboarding = user.pay_plan_onboarding
        onboarding.current_pay_plan = version.pay_plan
        onboarding.current_version = version
        onboarding.status = onboarding.ACTIVE
        onboarding.save(update_fields=[
            'current_pay_plan', 'current_version', 'status', 'updated_at',
        ])
        return user

    def start(self, text='change front minimum'):
        return PayPlanConversationService.start(
            self.user, text, self.effective_date,
        )

    def test_follow_up_preserves_understood_fields_and_orders_unique_turns(self):
        outcome = self.start()
        self.assertEqual(outcome.resolution.status, 'clarification')
        conversation = outcome.conversation
        outcome = PayPlanConversationService.follow_up(
            self.user,
            conversation.conversation_key,
            response_text='$300',
        )
        self.assertTrue(outcome.resolution.may_create_draft)
        self.assertEqual(outcome.resolution.intent.target_type, 'front_end_minimum')
        self.assertEqual(outcome.resolution.intent.new_value, Decimal('300'))
        sequences = list(
            conversation.turns.order_by('sequence').values_list(
                'sequence', flat=True,
            )
        )
        self.assertEqual(sequences, [1, 2, 3, 4])
        self.assertEqual(len(sequences), len(set(sequences)))

    def test_sequence_constraint_is_the_concurrency_backstop(self):
        conversation = self.start().conversation
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PayPlanConversationTurn.objects.create(
                    conversation=conversation,
                    role=PayPlanConversationTurn.USER,
                    content='duplicate sequence',
                    sequence=1,
                )

    def test_conversation_and_all_actions_are_owner_scoped(self):
        conversation = self.start().conversation
        operations = (
            lambda: PayPlanConversationService.resume(
                self.other, conversation.conversation_key,
            ),
            lambda: PayPlanConversationService.follow_up(
                self.other, conversation.conversation_key, response_text='300',
            ),
            lambda: PayPlanConversationService.cancel(
                self.other, conversation.conversation_key,
            ),
            lambda: PayPlanConversationService.create_draft(
                self.other, conversation.conversation_key,
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(ObjectDoesNotExist):
                    operation()
        self.client.force_login(self.other)
        response = self.client.get(
            reverse('pay_plan_assistant'),
            {'conversation': conversation.conversation_key},
        )
        self.assertEqual(response.status_code, 404)

    def test_pending_intent_contains_only_validated_semantic_data(self):
        conversation = self.start().conversation
        self.assertEqual(set(conversation.pending_intent), PENDING_INTENT_FIELDS)
        serialized = str(conversation.pending_intent)
        for forbidden in (
            'source_text', 'normalized_text', 'rule_selector',
            'candidate_targets', 'database_id', 'user_id',
        ):
            self.assertNotIn(forbidden, serialized)

    @override_settings(PAY_PLAN_ASSISTANT_MAX_TURNS=2)
    def test_maximum_turns_are_enforced_without_extra_writes(self):
        conversation = self.start().conversation
        with self.assertRaisesMessage(ValidationError, 'turn limit'):
            PayPlanConversationService.follow_up(
                self.user, conversation.conversation_key, response_text='300',
            )
        self.assertEqual(conversation.turns.count(), 2)

    @override_settings(PAY_PLAN_ASSISTANT_CONVERSATION_TTL_HOURS=1)
    def test_ttl_expiration_prevents_draft_creation(self):
        conversation = self.start('change front minimum to 300').conversation
        PayPlanConversation.objects.filter(pk=conversation.pk).update(
            updated_at=timezone.now() - timedelta(hours=2),
        )
        outcome = PayPlanConversationService.resume(
            self.user, conversation.conversation_key,
        )
        self.assertEqual(outcome.conversation.status, PayPlanConversation.EXPIRED)
        with self.assertRaisesMessage(ValidationError, 'expired'):
            PayPlanConversationService.create_draft(
                self.user, conversation.conversation_key,
            )
        self.assertFalse(PayPlanChangeRequest.objects.exists())

    def test_cancel_and_start_over_have_safe_separate_lifecycles(self):
        conversation = self.start().conversation
        PayPlanConversationService.cancel(
            self.user, conversation.conversation_key,
        )
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, PayPlanConversation.CANCELLED)
        self.assertFalse(conversation.pending_intent)
        with self.assertRaisesMessage(ValidationError, 'cancelled'):
            PayPlanConversationService.create_draft(
                self.user, conversation.conversation_key,
            )
        restarted = PayPlanConversationService.start_over(
            self.user, conversation.conversation_key,
        ).conversation
        self.assertNotEqual(restarted.pk, conversation.pk)
        self.assertEqual(restarted.status, PayPlanConversation.OPEN)

        begun = PayPlanConversationService.begin_existing(
            self.user,
            restarted.conversation_key,
            'change front minimum to 300',
            self.effective_date,
        )
        self.assertEqual(begun.conversation.pk, restarted.pk)
        self.assertEqual(begun.conversation.turns.count(), 2)
        self.assertTrue(begun.resolution.may_create_draft)

    def test_empty_start_over_page_has_one_primary_request_form(self):
        conversation = self.start().conversation
        restarted = PayPlanConversationService.start_over(
            self.user, conversation.conversation_key,
        ).conversation
        response = self.client.get(
            reverse('pay_plan_assistant'),
            {'conversation': restarted.conversation_key},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="assistant-request"', count=1)
        self.assertNotContains(response, 'assistant-conversation-card')
        self.assertContains(
            response,
            f'name="conversation_key" value="{restarted.conversation_key}"',
        )

    def test_active_plan_change_marks_pending_conversation_stale(self):
        conversation = self.start('change front minimum to 300').conversation
        assignment = self.user.pay_plan_assignments.get()
        self.version.status = PayPlanVersion.INACTIVE
        self.version.save(update_fields=['status', 'updated_at'])
        replacement = PayPlanVersion.objects.create(
            pay_plan=self.version.pay_plan,
            version_name='Replacement active version',
            effective_start_date=timezone.localdate(),
            status=PayPlanVersion.ACTIVE,
        )
        assignment.pay_plan_version = replacement
        assignment.save(update_fields=['pay_plan_version', 'updated_at'])
        outcome = PayPlanConversationService.resume(
            self.user, conversation.conversation_key,
        )
        self.assertEqual(outcome.conversation.status, PayPlanConversation.STALE)
        with self.assertRaisesMessage(ValidationError, 'active pay plan changed'):
            PayPlanConversationService.create_draft(
                self.user, conversation.conversation_key,
            )

    def test_clarification_and_review_write_no_draft(self):
        version_count = PayPlanVersion.objects.count()
        clarification = self.start()
        self.assertEqual(clarification.resolution.status, 'clarification')
        self.assertEqual(PayPlanVersion.objects.count(), version_count)
        review = PayPlanConversationService.follow_up(
            self.user,
            clarification.conversation.conversation_key,
            response_text='300',
        )
        self.assertTrue(review.resolution.may_create_draft)
        self.assertEqual(PayPlanVersion.objects.count(), version_count)
        self.assertFalse(PayPlanChangeRequest.objects.exists())

    def test_explicit_confirmation_creates_inactive_draft_and_resolves(self):
        conversation = self.start('change front minimum to 300').conversation
        active_configuration = dict(self.minimum.configuration)
        change = PayPlanConversationService.create_draft(
            self.user, conversation.conversation_key,
        )
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, PayPlanConversation.RESOLVED)
        self.assertEqual(change.draft_version.status, PayPlanVersion.REVIEW_REQUIRED)
        self.version.refresh_from_db()
        self.minimum.refresh_from_db()
        self.assertEqual(self.version.status, PayPlanVersion.ACTIVE)
        self.assertEqual(self.minimum.configuration, active_configuration)
        with self.assertRaisesMessage(ValidationError, 'already resolved'):
            PayPlanConversationService.create_draft(
                self.user, conversation.conversation_key,
            )

    def test_ui_renders_accessible_history_actions_and_no_internal_selector(self):
        response = self.client.post(reverse('pay_plan_assistant'), {
            'assistant_action': 'start',
            'request_text': 'change front minimum to 300',
            'effective_date': self.effective_date.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Pay Plan Assistant conversation"')
        self.assertContains(response, '<strong>You</strong>', html=True)
        self.assertContains(response, '<strong>Pay Plan Assistant</strong>', html=True)
        self.assertContains(response, 'Cancel conversation')
        self.assertContains(response, 'Start over')
        self.assertContains(response, 'Here’s what I understood')
        self.assertContains(response, 'Create draft')
        self.assertNotContains(response, str(self.minimum.semantic_key))
        self.assertNotContains(response, 'expected_source_version_id')

    def test_follow_up_validation_preserves_entered_text(self):
        conversation = self.start().conversation
        long_answer = 'keep-this-answer-' + ('x' * 2000)
        response = self.client.post(reverse('pay_plan_assistant'), {
            'assistant_action': 'follow_up',
            'conversation_key': conversation.conversation_key,
            'response_text': long_answer,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'keep-this-answer-')
        self.assertContains(response, 'at most 2000 characters')
        self.assertEqual(conversation.turns.count(), 2)

    @override_settings(PAY_PLAN_ASSISTANT_PROVIDER_ENABLED=True)
    def test_missing_key_uses_safe_deterministic_clarification_ui(self):
        with patch.dict('os.environ', {}, clear=True):
            response = self.client.post(reverse('pay_plan_assistant'), {
                'assistant_action': 'start',
                'request_text': 'make the mystery amount better',
                'effective_date': self.effective_date.isoformat(),
            })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Using deterministic clarification')
        self.assertNotContains(response, 'OPENAI_API_KEY')
        self.assertNotContains(response, 'authentication')
        self.assertFalse(PayPlanChangeRequest.objects.exists())
