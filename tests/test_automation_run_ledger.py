"""Tests for service/automation_run_ledger.py."""

from __future__ import annotations

import unittest

from service.automation_run_ledger import (
    AutomationRunLedger,
    CONTROL_CONTINUE,
    CONTROL_ESCALATE,
    CONTROL_PAUSE_AUTO_FIX,
    CONTROL_REVIEW_ONLY,
    suggest_control_action,
)


class TestSuggestControlAction(unittest.TestCase):
    def test_healthy_signals_continue(self) -> None:
        result = suggest_control_action("healthy", {"status": "ok"}, {"status": "ok"})
        self.assertEqual(result["action"], CONTROL_CONTINUE)
        self.assertTrue(result["auto_fix_allowed"])

    def test_unknown_org_health_falls_back_to_review_only(self) -> None:
        result = suggest_control_action("healthy", "ok", {"status": "unavailable"})
        self.assertEqual(result["action"], CONTROL_REVIEW_ONLY)
        self.assertFalse(result["auto_fix_allowed"])
        self.assertTrue(result["requires_human_review"])

    def test_degraded_signals_pause_auto_fix(self) -> None:
        result = suggest_control_action("degraded", "ok", "ok")
        self.assertEqual(result["action"], CONTROL_PAUSE_AUTO_FIX)
        self.assertIn("service health is degraded", result["reasons"])

    def test_low_quota_pauses_auto_fix(self) -> None:
        result = suggest_control_action("healthy", {"status": "low"}, "ok")
        self.assertEqual(result["action"], CONTROL_PAUSE_AUTO_FIX)
        self.assertIn("quota status is low", result["reasons"])

    def test_unhealthy_signals_escalate(self) -> None:
        result = suggest_control_action("healthy", "ok", {"status": "unhealthy"})
        self.assertEqual(result["action"], CONTROL_ESCALATE)
        self.assertIn("org health is unhealthy", result["reasons"])


class TestAutomationRunLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = AutomationRunLedger()

    def test_record_updates_latest_state_and_keeps_history(self) -> None:
        first = self.ledger.record(
            "run-1",
            "queued",
            task_name="monthly-audit",
            suggested_action=CONTROL_REVIEW_ONLY,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
            metadata={"repo": "QuantStrategyLab/AIAuditBridge"},
        )
        second = self.ledger.record(
            "run-1",
            "running",
            suggested_action=CONTROL_CONTINUE,
            service_health="healthy",
            quota_status="ok",
            org_health_status="ok",
        )

        self.assertEqual(first["task_state"], "queued")
        self.assertEqual(second["task_state"], "running")
        self.assertEqual(len(second["events"]), 2)
        self.assertEqual(second["events"][0]["task_state"], "queued")
        self.assertEqual(second["events"][1]["suggested_action"], CONTROL_CONTINUE)

    def test_snapshot_summarizes_terminal_and_active_runs(self) -> None:
        self.ledger.record("run-1", "running", suggested_action=CONTROL_CONTINUE)
        self.ledger.record("run-2", "merged", suggested_action=CONTROL_CONTINUE)

        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["summary"]["total_runs"], 2)
        self.assertEqual(snapshot["summary"]["returned_runs"], 2)
        self.assertEqual(snapshot["summary"]["active_runs"], 1)
        self.assertEqual(snapshot["summary"]["terminal_runs"], 1)
        self.assertEqual(snapshot["summary"]["suggested_actions"][CONTROL_CONTINUE], 2)
        self.assertNotIn("events", snapshot["runs"][0])

    def test_snapshot_can_include_bounded_history(self) -> None:
        ledger = AutomationRunLedger(max_events_per_run=2)
        ledger.record("run-1", "queued")
        ledger.record("run-1", "running")
        ledger.record("run-1", "merged")

        snapshot = ledger.snapshot(include_events=True)
        self.assertEqual(len(snapshot["runs"][0]["events"]), 2)
        self.assertEqual(snapshot["runs"][0]["events"][0]["task_state"], "running")
        self.assertTrue(snapshot["summary"]["retention"]["events_included"])

    def test_ledger_evicts_old_runs_by_count(self) -> None:
        ledger = AutomationRunLedger(max_runs=2)
        ledger.record("run-1", "queued")
        ledger.record("run-2", "queued")
        ledger.record("run-3", "queued")

        snapshot = ledger.snapshot(limit=None)
        self.assertEqual(snapshot["summary"]["total_runs"], 2)
        self.assertEqual({run["run_id"] for run in snapshot["runs"]}, {"run-2", "run-3"})

    def test_record_rejects_blank_run_id(self) -> None:
        with self.assertRaises(ValueError):
            self.ledger.record(" ", "queued")
