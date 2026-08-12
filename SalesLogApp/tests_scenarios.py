from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    CommissionSandbox,
    PayPlanRule,
    PayPlanRuleCondition,
    PayPlanVersion,
    Sale,
    SandboxHypotheticalDeal,
    SandboxRun,
    UserProfile,
)
from .forms import SandboxHypotheticalDealForm
from .sandbox_services import SandboxManager, SandboxRuleEditor
from .scenario_services import (
    ScenarioCalculationService,
    ScenarioCloneService,
    ScenarioComparisonService,
    ScenarioConversionService,
    ScenarioService,
)


class CommissionScenarioWorkflowTests(TestCase):
    password = "scenario-test-password"

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scenario-owner",
            password=self.password,
        )
        profile = self.user.sales_profile
        profile.commission_system = UserProfile.PAY_PLAN_V2
        profile.save(update_fields=["commission_system"])

        self.assignment = self.user.pay_plan_assignments.select_related(
            "pay_plan_version__pay_plan",
        ).get(is_active=True)
        self.source = self.assignment.pay_plan_version
        self.source.rules.all().delete()
        self.source_rule = PayPlanRule.objects.create(
            pay_plan_version=self.source,
            name="New Front 10%",
            description="Source rule",
            rule_type="front_gross_percentage",
            calculation_scope="per_sale",
            configuration={
                "rate": "0.10",
                "gross_field": "front_end_gross",
            },
            sort_order=1,
        )
        PayPlanRuleCondition.objects.create(
            rule=self.source_rule,
            field_name="vehicle_condition",
            operator="equals",
            value="new",
            sort_order=1,
        )
        self.sale_date = self.assignment.effective_start_date
        self.sale = Sale.objects.create(
            user=self.user,
            customer="Historical customer",
            dealNumber=880001,
            count=Decimal("1.0"),
            frontEnd=Decimal("1000.00"),
            backend=Decimal("0.00"),
            date=self.sale_date,
            vehicle_condition="new",
        )
        self.scenario = SandboxManager.create(
            self.user,
            self.source,
            "Promotion Offer",
            "Original notes",
        )

    def _set_rate(self, scenario, rate):
        rule = scenario.draft_version.rules.get(
            semantic_key=self.source_rule.semantic_key,
        )
        conditions = [
            {
                "field_name": condition.field_name,
                "operator": condition.operator,
                "value": condition.value,
            }
            for condition in rule.conditions.all()
        ]
        SandboxRuleEditor.save(
            scenario,
            rule=rule,
            data={
                "name": rule.name,
                "rule_type": rule.rule_type,
                "calculation_scope": rule.calculation_scope,
                "condition_group_operator": rule.condition_group_operator,
                "configuration": {
                    "rate": str(rate),
                    "gross_field": "front_end_gross",
                },
                "conditions": conditions,
                "is_active": True,
                "sort_order": rule.sort_order,
            },
        )
        scenario.refresh_from_db()
        return scenario.draft_version.rules.get(
            semantic_key=self.source_rule.semantic_key,
        )

    def _add_hypothetical(self, scenario, *, deal_number=889001):
        return SandboxHypotheticalDeal.objects.create(
            sandbox=scenario,
            label="Future deal",
            customer="Scenario customer",
            dealNumber=deal_number,
            count=Decimal("1.0"),
            split_with_name="",
            frontEnd=Decimal("2400.00"),
            backend=Decimal("1100.00"),
            date=self.sale_date,
            vehicle_condition="new",
        )

    def _hypothetical_payload(self, *, deal_number=889001, count="1"):
        return {
            "label": "Future deal",
            "customer": "Scenario customer",
            "dealNumber": str(deal_number),
            "count": count,
            "frontEnd": "2400.00",
            "backend": "1100.00",
            "date": self.sale_date.isoformat(),
            "vehicle_condition": "new",
            "acquisition_source": "",
        }

    def test_duplicate_hypothetical_post_is_validation_not_server_error(self):
        deal_number = 889101
        self._add_hypothetical(self.scenario, deal_number=deal_number)
        history_count = self.scenario.history.count()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse(
                "commission_sandbox_hypothetical",
                args=[self.scenario.public_id],
            ),
            self._hypothetical_payload(deal_number=deal_number),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            SandboxHypotheticalDealForm.DUPLICATE_DEAL_NUMBER_MESSAGE,
        )
        self.assertEqual(
            self.scenario.hypothetical_deals.filter(
                dealNumber=deal_number,
            ).count(),
            1,
        )
        self.assertEqual(self.scenario.history.count(), history_count)

    def test_same_hypothetical_deal_number_posts_to_different_sandboxes(self):
        second = SandboxManager.create(
            self.user,
            self.source,
            "Second projection",
        )
        deal_number = 889102
        self.client.force_login(self.user)

        for scenario in (self.scenario, second):
            with self.subTest(scenario=scenario.scenario_name):
                response = self.client.post(
                    reverse(
                        "commission_sandbox_hypothetical",
                        args=[scenario.public_id],
                    ),
                    self._hypothetical_payload(deal_number=deal_number),
                )
                self.assertEqual(response.status_code, 302)

        self.assertTrue(
            self.scenario.hypothetical_deals.filter(
                dealNumber=deal_number,
            ).exists(),
        )
        self.assertTrue(
            second.hypothetical_deals.filter(
                dealNumber=deal_number,
            ).exists(),
        )

    def test_hypothetical_unique_constraint_race_is_normal_validation(self):
        deal_number = 889103
        self._add_hypothetical(self.scenario, deal_number=deal_number)
        history_count = self.scenario.history.count()
        self.client.force_login(self.user)

        with patch.object(
            SandboxHypotheticalDealForm,
            "clean_dealNumber",
            return_value=deal_number,
        ), patch.object(SandboxHypotheticalDeal, "full_clean"):
            response = self.client.post(
                reverse(
                    "commission_sandbox_hypothetical",
                    args=[self.scenario.public_id],
                ),
                self._hypothetical_payload(deal_number=deal_number),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            SandboxHypotheticalDealForm.DUPLICATE_DEAL_NUMBER_MESSAGE,
        )
        self.assertEqual(
            self.scenario.hypothetical_deals.filter(
                dealNumber=deal_number,
            ).count(),
            1,
        )
        self.assertEqual(self.scenario.history.count(), history_count)

    def test_project_view_imported_plan_isolated_and_preserves_half_deal(self):
        self.source.source_type = PayPlanVersion.SOURCE_UPLOAD
        self.source.source_filename = "imported-plan.pdf"
        self.source.save(update_fields=[
            "source_type", "source_filename", "updated_at",
        ])
        imported_scenario = SandboxManager.create(
            self.user,
            self.source,
            "Imported plan projection",
        )
        hypothetical = self._add_hypothetical(
            imported_scenario,
            deal_number=889104,
        )
        hypothetical.count = Decimal("0.5")
        hypothetical.save(update_fields=["count", "updated_at"])
        sale_snapshot = list(
            Sale.objects.filter(user=self.user).values(
                "pk", "dealNumber", "frontEnd", "backend", "count",
            )
        )
        active_assignment_version = self.assignment.pay_plan_version_id
        active_production_versions = list(
            PayPlanVersion.objects.filter(
                pay_plan=self.source.pay_plan,
                status=PayPlanVersion.ACTIVE,
                is_sandbox=False,
            ).values_list("pk", flat=True)
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse(
                "commission_sandbox_project",
                args=[imported_scenario.public_id],
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        run = imported_scenario.runs.get(mode=SandboxRun.PROJECTION)
        result = run.results.get(hypothetical_deal=hypothetical)
        self.assertEqual(result.sandbox_commission, Decimal("120.00"))
        self.assertEqual(
            list(
                Sale.objects.filter(user=self.user).values(
                    "pk", "dealNumber", "frontEnd", "backend", "count",
                )
            ),
            sale_snapshot,
        )
        self.assignment.refresh_from_db()
        self.assertEqual(
            self.assignment.pay_plan_version_id,
            active_assignment_version,
        )
        self.assertEqual(
            list(
                PayPlanVersion.objects.filter(
                    pay_plan=self.source.pay_plan,
                    status=PayPlanVersion.ACTIVE,
                    is_sandbox=False,
                ).values_list("pk", flat=True)
            ),
            active_production_versions,
        )

    def test_owned_scenario_lock_targets_self_and_keeps_owner_filter(self):
        expected = object()
        with patch.object(
            CommissionSandbox.objects,
            "select_related",
        ) as select_related:
            selected = select_related.return_value
            locked = selected.select_for_update.return_value
            locked.get.return_value = expected

            result = ScenarioService.get(
                self.user,
                self.scenario.public_id,
                for_update=True,
            )

        self.assertIs(result, expected)
        select_related.assert_called_once_with(
            "source_version__pay_plan",
            "draft_version__pay_plan",
            "source_scenario",
        )
        selected.select_for_update.assert_called_once_with(of=("self",))
        locked.get.assert_called_once_with(
            owner=self.user,
            public_id=self.scenario.public_id,
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "PostgreSQL is required for the nullable outer-join locking regression.",
    )
    def test_postgresql_recalculation_locks_scenario_with_null_outer_join(self):
        self.assertIsNone(self.scenario.source_scenario_id)
        hypothetical = self._add_hypothetical(
            self.scenario,
            deal_number=889105,
        )

        run = ScenarioCalculationService.recalculate(
            self.user,
            self.scenario,
            mode=SandboxRun.PROJECTION,
        )

        self.assertEqual(run.results.get().hypothetical_deal_id, hypothetical.pk)

    def test_project_view_logs_unexpected_failure_without_exposing_details(self):
        self.client.force_login(self.user)
        with patch(
            "SalesLogApp.scenario_services."
            "ScenarioCalculationService.recalculate",
            side_effect=RuntimeError("private projection failure details"),
        ), self.assertLogs("SalesLogApp.views", level="ERROR") as logs:
            response = self.client.post(
                reverse(
                    "commission_sandbox_project",
                    args=[self.scenario.public_id],
                ),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "The projection could not be completed. Please try again.",
        )
        self.assertNotContains(response, "private projection failure details")
        self.assertTrue(
            any(
                "Unexpected sandbox projection failure." in entry
                for entry in logs.output
            )
        )

    def test_save_updates_same_scenario_and_draft(self):
        original_id = self.scenario.pk
        original_draft_id = self.scenario.draft_version_id
        original_created_at = self.scenario.created_at

        saved = ScenarioService.save(
            self.user,
            self.scenario,
            description="Updated notes",
            assumptions={"growth_target": "12.50"},
        )

        self.assertEqual(saved.pk, original_id)
        self.assertEqual(saved.draft_version_id, original_draft_id)
        self.assertEqual(saved.created_at, original_created_at)
        self.assertEqual(saved.scenario_notes, "Updated notes")
        self.assertEqual(saved.assumptions, {"growth_target": "12.50"})
        self.assertEqual(saved.saved_revision, saved.revision)
        self.assertIsNotNone(saved.last_saved_at)
        self.assertTrue(saved.history.filter(action="scenario_saved").exists())
        self.assertEqual(self.assignment.pay_plan_version_id, self.source.pk)

    def test_save_as_creates_complete_independent_snapshot(self):
        self._set_rate(self.scenario, "0.20")
        original_hypothetical = self._add_hypothetical(self.scenario)
        ScenarioService.save(
            self.user,
            self.scenario,
            description="Test a higher front percentage",
            assumptions={"market": "growth"},
        )
        ScenarioCalculationService.recalculate(
            self.user,
            self.scenario,
            mode=SandboxRun.MIXED,
            start=self.sale_date,
            end=self.sale_date,
        )
        self.scenario.refresh_from_db()

        copied = ScenarioService.save_as(
            self.user,
            self.scenario,
            "2027 Proposed Pay Plan",
            "Independent proposal",
        )

        self.assertNotEqual(copied.pk, self.scenario.pk)
        self.assertNotEqual(
            copied.draft_version_id,
            self.scenario.draft_version_id,
        )
        self.assertEqual(copied.source_version_id, self.source.pk)
        self.assertEqual(copied.source_scenario_id, self.scenario.pk)
        self.assertEqual(copied.replay_mode, SandboxRun.MIXED)
        self.assertEqual(copied.replay_start_date, self.sale_date)
        self.assertEqual(copied.replay_end_date, self.sale_date)
        self.assertEqual(copied.assumptions, {"market": "growth"})
        self.assertEqual(copied.scenario_notes, "Independent proposal")
        self.assertEqual(
            copied.calculation_summary["scenario_total"],
            self.scenario.calculation_summary["scenario_total"],
        )

        original_rule = self.scenario.draft_version.rules.get(
            semantic_key=self.source_rule.semantic_key,
        )
        copied_rule = copied.draft_version.rules.get(
            semantic_key=self.source_rule.semantic_key,
        )
        self.assertNotEqual(copied_rule.pk, original_rule.pk)
        self.assertEqual(copied_rule.configuration, original_rule.configuration)
        self.assertNotEqual(
            copied_rule.conditions.get().pk,
            original_rule.conditions.get().pk,
        )
        copied_hypothetical = copied.hypothetical_deals.get(
            dealNumber=original_hypothetical.dealNumber,
        )
        self.assertNotEqual(copied_hypothetical.pk, original_hypothetical.pk)

        self._set_rate(copied, "0.30")
        copied_condition = copied.draft_version.rules.get(
            semantic_key=self.source_rule.semantic_key,
        ).conditions.get()
        copied_condition.value = "used"
        copied_condition.save(update_fields=["value"])
        copied_hypothetical.frontEnd = Decimal("9999.00")
        copied_hypothetical.save(update_fields=["frontEnd"])

        original_rule.refresh_from_db()
        original_hypothetical.refresh_from_db()
        self.assertEqual(original_rule.configuration["rate"], "0.20")
        self.assertEqual(original_rule.conditions.get().value, "new")
        self.assertEqual(original_hypothetical.frontEnd, Decimal("2400.00"))
        self.assertEqual(self.assignment.pay_plan_version_id, self.source.pk)

    def test_scenario_names_are_case_insensitive_and_archived_names_are_reusable(self):
        with self.assertRaises(ValidationError):
            ScenarioService.save_as(
                self.user,
                self.scenario,
                "  promotion offer  ",
                "",
            )

        ScenarioService.archive(
            self.user,
            self.scenario,
            confirmed=True,
        )
        reused = ScenarioService.save_as(
            self.user,
            self.scenario,
            "  promotion offer  ",
            "",
        )
        self.assertEqual(reused.scenario_name, "promotion offer")
        self.assertEqual(reused.status, CommissionSandbox.DRAFT)

    def test_duplicate_has_unique_name_and_deeply_isolated_state(self):
        self._add_hypothetical(self.scenario)
        first = ScenarioCloneService.duplicate(self.user, self.scenario)
        second = ScenarioCloneService.duplicate(self.user, self.scenario)

        self.assertEqual(first.scenario_name, "Copy of Promotion Offer")
        self.assertEqual(second.scenario_name, "Copy of Promotion Offer (2)")
        self.assertEqual(first.source_scenario_id, self.scenario.pk)
        self.assertNotEqual(first.draft_version_id, second.draft_version_id)
        self.assertNotEqual(
            first.draft_version.rules.get().pk,
            second.draft_version.rules.get().pk,
        )
        self.assertNotEqual(
            first.hypothetical_deals.get().pk,
            second.hypothetical_deals.get().pk,
        )

        self._set_rate(first, "0.55")
        self.assertEqual(
            self.scenario.draft_version.rules.get().configuration["rate"],
            "0.10",
        )
        self.assertEqual(
            second.draft_version.rules.get().configuration["rate"],
            "0.10",
        )

    def test_owner_isolation_is_enforced_by_services(self):
        other = get_user_model().objects.create_user(
            username="scenario-intruder",
            password=self.password,
        )
        operations = (
            lambda: ScenarioService.get(other, self.scenario),
            lambda: ScenarioService.save(other, self.scenario),
            lambda: ScenarioService.save_as(
                other, self.scenario, "Stolen copy",
            ),
            lambda: ScenarioCloneService.duplicate(other, self.scenario),
            lambda: ScenarioService.rename(
                other, self.scenario, "Stolen name",
            ),
            lambda: ScenarioService.archive(
                other, self.scenario, confirmed=True,
            ),
            lambda: ScenarioService.restore(
                other, self.scenario, confirmed=True,
            ),
            lambda: ScenarioService.delete(
                other, self.scenario, confirmed=True,
            ),
            lambda: ScenarioService.reset(other, self.scenario),
            lambda: ScenarioCalculationService.recalculate(
                other, self.scenario,
            ),
            lambda: ScenarioComparisonService.compare(
                other, [self.scenario],
            ),
            lambda: ScenarioConversionService.convert(
                other, self.scenario, self.sale_date,
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(PermissionDenied):
                    operation()

    def test_owner_isolation_is_enforced_by_scenario_urls(self):
        hypothetical = self._add_hypothetical(self.scenario)
        self.client.force_login(get_user_model().objects.create_user(
            username="scenario-url-intruder",
            password=self.password,
        ))
        scenario_args = [self.scenario.public_id]
        requests = (
            ("get", "commission_sandbox_detail", {}),
            ("post", "commission_sandbox_save", {}),
            ("post", "commission_sandbox_save_as", {
                "name": "Stolen",
                "description": "",
            }),
            ("post", "commission_sandbox_duplicate", {}),
            ("post", "commission_sandbox_rename", {"name": "Stolen"}),
            ("post", "commission_sandbox_archive", {"confirm": "on"}),
            ("post", "commission_sandbox_restore", {"confirm": "on"}),
            ("post", "commission_sandbox_delete", {
                "confirm": "on",
                "confirmation_name": self.scenario.scenario_name,
            }),
            ("post", "commission_sandbox_recalculate", {}),
            ("post", "commission_sandbox_reset", {"confirm": "on"}),
            ("post", "commission_sandbox_convert", {
                "confirm": "on",
                "effective_start_date": self.sale_date,
            }),
        )
        for method, route_name, data in requests:
            with self.subTest(route_name=route_name):
                response = getattr(self.client, method)(
                    reverse(route_name, args=scenario_args),
                    data,
                )
                self.assertEqual(response.status_code, 404)

        for route_name in (
            "commission_sandbox_hypothetical_edit",
            "commission_sandbox_hypothetical_delete",
        ):
            with self.subTest(route_name=route_name):
                response = self.client.post(
                    reverse(
                        route_name,
                        args=[self.scenario.public_id, hypothetical.pk],
                    ),
                )
                self.assertEqual(response.status_code, 404)

        compare = self.client.post(
            reverse("commission_sandbox_compare"),
            {
                "sandboxes": [self.scenario.pk],
                "preset": "custom",
                "start_date": self.sale_date,
                "end_date": self.sale_date,
            },
            follow=True,
        )
        self.assertIn(compare.status_code, {200, 404})
        if compare.status_code == 200:
            self.assertNotContains(compare, self.scenario.scenario_name)

    def test_archive_restore_and_delete_preserve_production_data(self):
        source_id = self.source.pk
        source_rule_id = self.source_rule.pk
        assignment_id = self.assignment.pk
        sale_id = self.sale.pk
        draft_id = self.scenario.draft_version_id

        archived = ScenarioService.archive(
            self.user,
            self.scenario,
            confirmed=True,
        )
        self.assertEqual(archived.status, CommissionSandbox.ARCHIVED)
        with self.assertRaises(ValidationError):
            ScenarioService.save(self.user, archived, description="Blocked")

        restored = ScenarioService.restore(
            self.user,
            archived,
            confirmed=True,
        )
        self.assertEqual(restored.status, CommissionSandbox.DRAFT)
        ScenarioService.archive(self.user, restored, confirmed=True)
        with self.assertRaises(ValidationError):
            ScenarioService.delete(self.user, restored, confirmed=False)
        deleted_id = ScenarioService.delete(
            self.user,
            restored,
            confirmed=True,
        )

        self.assertEqual(deleted_id, self.scenario.pk)
        self.assertFalse(
            CommissionSandbox.objects.filter(pk=self.scenario.pk).exists(),
        )
        self.assertFalse(PayPlanVersion.objects.filter(pk=draft_id).exists())
        self.assertTrue(PayPlanVersion.objects.filter(pk=source_id).exists())
        self.assertTrue(PayPlanRule.objects.filter(pk=source_rule_id).exists())
        self.assertTrue(
            self.user.pay_plan_assignments.filter(pk=assignment_id).exists(),
        )
        self.assertTrue(Sale.objects.filter(pk=sale_id).exists())

    def test_recalculation_is_decimal_exact_and_detects_stale_inputs(self):
        self._set_rate(self.scenario, "0.20")
        self.sale.frontEnd = Decimal("1234.56")
        self.sale.save(update_fields=["frontEnd"])
        run = ScenarioCalculationService.recalculate(
            self.user,
            self.scenario,
            mode=SandboxRun.REPLAY,
            start=self.sale_date,
            end=self.sale_date,
        )
        self.scenario.refresh_from_db()

        self.assertEqual(run.actual_total, Decimal("123.46"))
        self.assertEqual(run.sandbox_total, Decimal("246.91"))
        self.assertEqual(run.difference, Decimal("123.45"))
        self.assertEqual(
            self.scenario.calculation_summary["live_total"],
            "123.46",
        )
        self.assertEqual(
            self.scenario.calculation_summary["scenario_total"],
            "246.91",
        )
        self.assertIsInstance(
            self.scenario.calculation_summary["scenario_total"],
            str,
        )
        self.assertEqual(
            ScenarioCalculationService.stale_reasons(
                self.user, self.scenario,
            ),
            [],
        )

        self.sale.frontEnd = Decimal("2000.00")
        self.sale.save(update_fields=["frontEnd"])
        reasons = ScenarioCalculationService.stale_reasons(
            self.user,
            self.scenario,
        )
        self.assertTrue(
            any("Historical sales" in reason for reason in reasons),
            reasons,
        )
        refreshed = ScenarioCalculationService.recalculate(
            self.user,
            self.scenario,
        )
        self.scenario.refresh_from_db()
        self.assertEqual(refreshed.sandbox_total, Decimal("400.00"))
        self.assertEqual(
            self.scenario.calculation_summary["scenario_total"],
            "400.00",
        )
        self.assertEqual(
            ScenarioCalculationService.stale_reasons(
                self.user, self.scenario,
            ),
            [],
        )

    def test_semantic_rule_comparison_survives_cloned_database_ids(self):
        copied = ScenarioService.save_as(
            self.user,
            self.scenario,
            "Higher Rate",
            "",
        )
        source_rule = self.scenario.draft_version.rules.get()
        copied_rule = copied.draft_version.rules.get()
        self.assertNotEqual(source_rule.pk, copied_rule.pk)
        self.assertEqual(source_rule.semantic_key, copied_rule.semantic_key)

        self._set_rate(copied, "0.25")
        differences = ScenarioComparisonService.compare_rules(
            self.scenario.draft_version,
            copied.draft_version,
        )

        self.assertEqual(differences["added"], [])
        self.assertEqual(differences["removed"], [])
        self.assertEqual(len(differences["modified"]), 1)
        self.assertIn(
            "configuration.rate",
            differences["modified"][0]["changed_fields"],
        )

        original_revision = self.scenario.revision
        original_replay_dates = (
            self.scenario.replay_start_date,
            self.scenario.replay_end_date,
        )
        comparison = ScenarioComparisonService.compare(
            self.user,
            [self.scenario, copied],
            start=self.sale_date,
            end=self.sale_date,
        )
        self.assertEqual(comparison["live"]["total"], "100.00")
        self.assertEqual(len(comparison["scenarios"]), 2)
        totals = {
            item["scenario"].pk: item["summary"]["scenario_total"]
            for item in comparison["scenarios"]
        }
        self.assertEqual(totals[self.scenario.pk], "100.00")
        self.assertEqual(totals[copied.pk], "250.00")
        self.scenario.refresh_from_db()
        self.assertEqual(self.scenario.revision, original_revision)
        self.assertEqual(
            (
                self.scenario.replay_start_date,
                self.scenario.replay_end_date,
            ),
            original_replay_dates,
        )

    def test_conversion_creates_review_draft_without_activation_and_is_idempotent(self):
        self._set_rate(self.scenario, "0.20")
        active_assignment_version = self.assignment.pay_plan_version_id
        active_version_count = PayPlanVersion.objects.filter(
            pay_plan=self.source.pay_plan,
            status=PayPlanVersion.ACTIVE,
            is_sandbox=False,
        ).count()
        production_version_count = PayPlanVersion.objects.filter(
            pay_plan=self.source.pay_plan,
            is_sandbox=False,
        ).count()
        effective_date = self.sale_date + timedelta(days=1)

        converted = ScenarioConversionService.convert(
            self.user,
            self.scenario,
            effective_date,
        )

        self.assertEqual(converted.status, PayPlanVersion.REVIEW_REQUIRED)
        self.assertFalse(converted.is_sandbox)
        self.assertEqual(converted.origin_scenario_id, self.scenario.pk)
        self.assertEqual(converted.previous_version_id, self.source.pk)
        self.assertEqual(converted.effective_start_date, effective_date)
        converted_rule = converted.rules.get(
            semantic_key=self.source_rule.semantic_key,
        )
        self.assertEqual(converted_rule.configuration["rate"], "0.20")
        self.assertEqual(converted_rule.conditions.get().value, "new")

        self.assignment.refresh_from_db()
        self.source.refresh_from_db()
        self.scenario.refresh_from_db()
        self.assertEqual(
            self.assignment.pay_plan_version_id,
            active_assignment_version,
        )
        self.assertEqual(self.source.status, PayPlanVersion.ACTIVE)
        self.assertEqual(self.scenario.status, CommissionSandbox.CONVERTED)
        self.assertEqual(
            PayPlanVersion.objects.filter(
                pay_plan=self.source.pay_plan,
                status=PayPlanVersion.ACTIVE,
                is_sandbox=False,
            ).count(),
            active_version_count,
        )
        self.assertEqual(
            PayPlanVersion.objects.filter(
                pay_plan=self.source.pay_plan,
                is_sandbox=False,
            ).count(),
            production_version_count + 1,
        )

        repeated = ScenarioConversionService.convert(
            self.user,
            self.scenario,
            effective_date + timedelta(days=1),
        )
        self.assertEqual(repeated.pk, converted.pk)
        self.assertEqual(
            PayPlanVersion.objects.filter(
                pay_plan=self.source.pay_plan,
                is_sandbox=False,
            ).count(),
            production_version_count + 1,
        )
        self.assertEqual(
            self.user.pay_plan_assignments.filter(is_active=True).count(),
            1,
        )

    def test_reset_reclones_source_and_respects_retention_choices(self):
        self._set_rate(self.scenario, "0.20")
        hypothetical = self._add_hypothetical(self.scenario)
        self.scenario.replay_mode = SandboxRun.MIXED
        self.scenario.replay_start_date = self.sale_date
        self.scenario.replay_end_date = self.sale_date
        self.scenario.assumptions = {"volume_growth": "2"}
        self.scenario.replay_filters = {"vehicle_condition": "new"}
        self.scenario.save(update_fields=[
            "replay_mode",
            "replay_start_date",
            "replay_end_date",
            "assumptions",
            "replay_filters",
            "updated_at",
        ])
        old_draft_id = self.scenario.draft_version_id

        retained = ScenarioService.reset(
            self.user,
            self.scenario,
            retain_hypothetical_sales=True,
            retain_replay_settings=True,
        )

        self.assertNotEqual(retained.draft_version_id, old_draft_id)
        self.assertFalse(
            PayPlanVersion.objects.filter(pk=old_draft_id).exists(),
        )
        self.assertEqual(
            retained.draft_version.rules.get().configuration["rate"],
            "0.10",
        )
        self.assertTrue(
            retained.hypothetical_deals.filter(pk=hypothetical.pk).exists(),
        )
        self.assertEqual(retained.replay_mode, SandboxRun.MIXED)
        self.assertEqual(retained.replay_start_date, self.sale_date)
        self.assertEqual(retained.assumptions, {"volume_growth": "2"})
        self.assertEqual(
            retained.replay_filters,
            {"vehicle_condition": "new"},
        )
        self.assertTrue(
            retained.history.filter(action="scenario_reset").exists(),
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.pay_plan_version_id, self.source.pk)

        cleared = ScenarioService.reset(
            self.user,
            retained,
            retain_hypothetical_sales=False,
            retain_replay_settings=False,
        )
        self.assertFalse(cleared.hypothetical_deals.exists())
        self.assertEqual(cleared.replay_mode, CommissionSandbox.REPLAY)
        self.assertIsNone(cleared.replay_start_date)
        self.assertIsNone(cleared.replay_end_date)
        self.assertEqual(cleared.assumptions, {})
        self.assertEqual(cleared.replay_filters, {})

    def test_state_changing_endpoints_require_post_and_direct_activation_is_disabled(self):
        hypothetical = self._add_hypothetical(self.scenario)
        self.client.force_login(self.user)
        post_only_routes = (
            ("commission_sandbox_save", [self.scenario.public_id]),
            ("commission_sandbox_duplicate", [self.scenario.public_id]),
            ("commission_sandbox_archive", [self.scenario.public_id]),
            ("commission_sandbox_restore", [self.scenario.public_id]),
            ("commission_sandbox_recalculate", [self.scenario.public_id]),
            (
                "commission_sandbox_hypothetical_delete",
                [self.scenario.public_id, hypothetical.pk],
            ),
        )
        for route_name, args in post_only_routes:
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name, args=args))
                self.assertEqual(response.status_code, 405)

        active_version_id = self.assignment.pay_plan_version_id
        response = self.client.post(
            reverse(
                "commission_sandbox_activate",
                args=[self.scenario.public_id],
            ),
            {
                "effective_start_date": self.sale_date,
                "confirm": "on",
            },
        )
        self.assertRedirects(
            response,
            reverse(
                "commission_sandbox_convert",
                args=[self.scenario.public_id],
            ),
            fetch_redirect_response=False,
        )
        self.assignment.refresh_from_db()
        self.assertEqual(
            self.assignment.pay_plan_version_id,
            active_version_id,
        )
