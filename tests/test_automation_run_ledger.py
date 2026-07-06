"""Tests for service/automation_run_ledger.py."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_ok_service_health_is_healthy(self) -> None:
        result = suggest_control_action({"status": "ok"}, {"status": "ok"}, {"status": "ok"})
        self.assertEqual(result["action"], CONTROL_CONTINUE)
        self.assertTrue(result["auto_fix_allowed"])

    def test_healthy_quota_status_is_healthy(self) -> None:
        result = suggest_control_action({"status": "ok"}, {"status": "healthy"}, {"status": "ok"})
        self.assertEqual(result["action"], CONTROL_CONTINUE)
        self.assertTrue(result["auto_fix_allowed"])

    def test_unknown_org_health_falls_back_to_review_only(self) -> None:
        result = suggest_control_action("healthy", "ok", {"status": "unavailable"})
        self.assertEqual(result["action"], CONTROL_REVIEW_ONLY)
        self.assertFalse(result["auto_fix_allowed"])
        self.assertTrue(result["requires_human_review"])

    def test_missing_signals_fall_back_to_review_only(self) -> None:
        result = suggest_control_action()
        self.assertEqual(result["action"], CONTROL_REVIEW_ONLY)
        self.assertFalse(result["auto_fix_allowed"])
        self.assertIn("runtime signals are incomplete", result["reasons"])

    def test_degraded_signals_pause_auto_fix(self) -> None:
        result = suggest_control_action("degraded", "ok", "ok")
        self.assertEqual(result["action"], CONTROL_PAUSE_AUTO_FIX)
        self.assertIn("service health is degraded", result["reasons"])

    def test_low_quota_pauses_auto_fix(self) -> None:
        result = suggest_control_action("healthy", {"status": "low"}, "ok")
        self.assertEqual(result["action"], CONTROL_PAUSE_AUTO_FIX)
        self.assertIn("quota status is low", result["reasons"])

    def test_nested_quota_snapshot_controls_action(self) -> None:
        result = suggest_control_action(
            "healthy",
            {"status": "ok", "quota": {"status": "exhausted"}},
            "ok",
        )
        self.assertEqual(result["action"], CONTROL_ESCALATE)
        self.assertFalse(result["auto_fix_allowed"])

    def test_quota_snapshot_keeps_most_severe_status(self) -> None:
        result = suggest_control_action(
            "healthy",
            {"status": "blocked", "quota": {"status": "ok"}},
            "ok",
        )
        self.assertEqual(result["action"], CONTROL_ESCALATE)
        self.assertEqual(result["quota_status"], "blocked")

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
        self.assertNotIn("_ledger_sequence", second)
        self.assertEqual(len(second["events"]), 2)
        self.assertEqual(second["events"][0]["task_state"], "queued")
        self.assertEqual(second["events"][1]["suggested_action"], CONTROL_CONTINUE)

    def test_record_does_not_regress_terminal_state(self) -> None:
        self.ledger.record("run-1", "merged")
        result = self.ledger.record("run-1", "running")

        self.assertEqual(result["task_state"], "merged")
        self.assertEqual([event["task_state"] for event in result["events"]], ["merged"])

    def test_record_does_not_replace_existing_terminal_state(self) -> None:
        self.ledger.record("run-1", "failed")
        result = self.ledger.record("run-1", "merged")

        self.assertEqual(result["task_state"], "failed")
        self.assertEqual([event["task_state"] for event in result["events"]], ["failed"])

    def test_service_job_can_replace_stale_failed_state(self) -> None:
        metadata = {"origin": "service_job", "repository": "QuantStrategyLab/RepoA", "failure_category": "stale_job_timeout"}
        self.ledger.record("run-1", "failed", metadata=metadata, owner_repository="QuantStrategyLab/RepoA")
        result = self.ledger.record("run-1", "reviewed", metadata=metadata, owner_repository="QuantStrategyLab/RepoA")

        self.assertEqual(result["task_state"], "reviewed")
        self.assertEqual([event["task_state"] for event in result["events"]], ["failed", "reviewed"])

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

    def test_snapshot_none_limit_returns_all_retained_runs(self) -> None:
        self.ledger.record("run-1", "running")
        self.ledger.record("run-2", "running")
        self.ledger.record("run-3", "running")

        snapshot = self.ledger.snapshot(limit=None)

        self.assertEqual(snapshot["summary"]["total_runs"], 3)
        self.assertEqual(snapshot["summary"]["returned_runs"], 3)
        self.assertEqual({run["run_id"] for run in snapshot["runs"]}, {"run-1", "run-2", "run-3"})

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
        ledger.record("run-1", "queued", metadata={"source_repository": "QuantStrategyLab/RepoA"})
        ledger.record("run-2", "queued", metadata={"source_repository": "QuantStrategyLab/RepoA"})
        ledger.record("run-3", "queued", metadata={"source_repository": "QuantStrategyLab/RepoB"})

        snapshot = ledger.snapshot(limit=None)
        self.assertEqual(snapshot["summary"]["total_runs"], 2)
        self.assertEqual(snapshot["summary"]["retention"]["evicted_runs"], 1)
        self.assertEqual(snapshot["summary"]["retention"]["evicted_runs_by_repo"], {"quantstrategylab/repoa": 1})
        self.assertTrue(snapshot["summary"]["retention"]["may_be_truncated"])
        self.assertEqual({run["run_id"] for run in snapshot["runs"]}, {"run-2", "run-3"})

    def test_ledger_eviction_keeps_new_run_when_timestamps_match(self) -> None:
        ledger = AutomationRunLedger(max_runs=1)
        with patch("service.automation_run_ledger.time.time", return_value=123.0):
            ledger.record("run-1", "queued")
            ledger.record("run-2", "queued")

        snapshot = ledger.snapshot(limit=None)
        self.assertEqual([run["run_id"] for run in snapshot["runs"]], ["run-2"])

    def test_persisted_ledger_eviction_count_is_not_double_counted(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            ledger = AutomationRunLedger(max_runs=2, storage_path=path)
            ledger.record("run-1", "queued", metadata={"source_repository": "QuantStrategyLab/RepoA"})
            ledger.record("run-2", "queued", metadata={"source_repository": "QuantStrategyLab/RepoA"})
            ledger.record("run-3", "queued", metadata={"source_repository": "QuantStrategyLab/RepoB"})

            reloaded = AutomationRunLedger(max_runs=2, storage_path=path)
            snapshot = reloaded.snapshot(limit=None)

        self.assertEqual(snapshot["summary"]["retention"]["evicted_runs"], 1)
        self.assertEqual(snapshot["summary"]["retention"]["evicted_runs_by_repo"], {"quantstrategylab/repoa": 1})
        self.assertTrue(snapshot["summary"]["retention"]["may_be_truncated"])
        self.assertEqual({run["run_id"] for run in snapshot["runs"]}, {"run-2", "run-3"})

    def test_persist_merge_does_not_recount_disk_evicted_local_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            ledger_a = AutomationRunLedger(max_runs=2, storage_path=path)
            with patch("service.automation_run_ledger.time.time", side_effect=[1.0, 2.0, 3.0]):
                ledger_a.record("run-1", "queued", metadata={"source_repository": "QuantStrategyLab/RepoA"})
                ledger_a.record("run-2", "queued", metadata={"source_repository": "QuantStrategyLab/RepoA"})
                ledger_b = AutomationRunLedger(max_runs=2, storage_path=path)
                ledger_a.record("run-3", "queued", metadata={"source_repository": "QuantStrategyLab/RepoB"})
            with patch("service.automation_run_ledger.time.time", return_value=4.0):
                ledger_b.record("run-4", "queued", metadata={"source_repository": "QuantStrategyLab/RepoB"})
            snapshot = AutomationRunLedger(max_runs=2, storage_path=path).snapshot(limit=None)

        self.assertEqual(snapshot["summary"]["retention"]["evicted_runs"], 2)
        self.assertEqual(snapshot["summary"]["retention"]["evicted_runs_by_repo"], {"quantstrategylab/repoa": 2})
        self.assertEqual({run["run_id"] for run in snapshot["runs"]}, {"run-3", "run-4"})

    def test_pre_migration_ledger_marks_history_completeness_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "automation_run_ledger.v1",
                        "sequence": 1,
                        "runs": {
                            "run-1": {
                                "run_id": "run-1",
                                "task_state": "failed",
                                "updated_at": 1.0,
                                "metadata": {"source_repository": "QuantStrategyLab/RepoA"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            ledger = AutomationRunLedger(max_runs=2, storage_path=path)
            snapshot = ledger.snapshot(limit=None)

        self.assertTrue(snapshot["summary"]["retention"]["history_completeness_unknown"])

    def test_fresh_missing_persisted_ledger_starts_as_complete_empty_history(self) -> None:
        with TemporaryDirectory() as tmp:
            ledger = AutomationRunLedger(max_runs=2, storage_path=Path(tmp) / "missing.json")
            snapshot = ledger.snapshot(limit=None)

        self.assertFalse(snapshot["summary"]["retention"]["history_completeness_unknown"])

    def test_disappeared_persisted_ledger_marks_history_completeness_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            ledger = AutomationRunLedger(max_runs=2, storage_path=path)
            ledger.record("run-1", "queued", metadata={"source_repository": "QuantStrategyLab/RepoA"})
            path.unlink()
            snapshot = ledger.snapshot(limit=None)

        self.assertTrue(snapshot["summary"]["retention"]["history_completeness_unknown"])

    def test_corrupt_persisted_ledger_marks_history_completeness_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            path.write_text("{not-json", encoding="utf-8")
            ledger = AutomationRunLedger(max_runs=2, storage_path=path)
            snapshot = ledger.snapshot(limit=None)

        self.assertTrue(snapshot["summary"]["retention"]["history_completeness_unknown"])

    def test_update_preserves_control_fields_when_omitted(self) -> None:
        self.ledger.record(
            "run-1",
            "queued",
            suggested_action=CONTROL_PAUSE_AUTO_FIX,
            service_health="degraded",
            quota_status="low",
            org_health_status="ok",
        )

        updated = self.ledger.record("run-1", "running")

        self.assertEqual(updated["suggested_action"], CONTROL_PAUSE_AUTO_FIX)
        self.assertEqual(updated["service_health"], "degraded")
        self.assertEqual(updated["quota_status"], "low")
        self.assertEqual(updated["events"][-1]["suggested_action"], CONTROL_PAUSE_AUTO_FIX)

    def test_record_sanitizes_metadata(self) -> None:
        metadata = {
            "repo": "QuantStrategyLab/AIAuditBridge",
            "repos": ["QuantStrategyLab/AIAuditBridge"],
            "note": "x" * 600,
        }
        recorded = self.ledger.record("run-1", "queued", metadata=metadata)
        metadata["repos"].append("mutated")

        stored = self.ledger.get("run-1")

        self.assertEqual(recorded["metadata"]["repo"], "QuantStrategyLab/AIAuditBridge")
        self.assertNotIn("repos", recorded["metadata"])
        self.assertEqual(recorded["metadata"]["_omitted_fields"], 1)
        self.assertTrue(recorded["metadata"]["note"].endswith("…"))
        self.assertEqual(stored["metadata"], recorded["metadata"])
        self.assertEqual(stored["events"][0]["metadata"]["repo"], "QuantStrategyLab/AIAuditBridge")
        self.assertNotIn("repos", stored["events"][0]["metadata"])
        self.assertEqual(stored["events"][0]["metadata"]["_omitted_fields"], 1)

    def test_record_rejects_blank_run_id(self) -> None:
        with self.assertRaises(ValueError):
            self.ledger.record(" ", "queued")

    def test_persist_merges_existing_disk_runs_before_write(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            ledger_a = AutomationRunLedger(storage_path=path)
            ledger_b = AutomationRunLedger(storage_path=path)
            reader = AutomationRunLedger(storage_path=path)

            ledger_a.record("run-a", "queued", task_name="repo-a")
            ledger_b.record("run-b", "queued", task_name="repo-b")

            reloaded = AutomationRunLedger(storage_path=path)
            snapshot = reloaded.snapshot(limit=None)
            self.assertEqual(reader.get("run-a")["task_state"], "queued")

        self.assertEqual({run["run_id"] for run in snapshot["runs"]}, {"run-a", "run-b"})

    def test_owner_guard_rejects_cross_repository_run_id_reuse(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            ledger_a = AutomationRunLedger(storage_path=path)
            ledger_b = AutomationRunLedger(storage_path=path)

            ledger_a.record(
                "shared-run",
                "queued",
                metadata={"repository": "QuantStrategyLab/RepoA"},
                owner_repository="QuantStrategyLab/RepoA",
            )

            with self.assertRaises(PermissionError):
                ledger_b.record(
                    "shared-run",
                    "queued",
                    metadata={"repository": "QuantStrategyLab/RepoB"},
                    owner_repository="QuantStrategyLab/RepoB",
                )

    def test_persist_merges_same_run_events_before_write(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            ledger_a = AutomationRunLedger(storage_path=path)
            ledger_b = AutomationRunLedger(storage_path=path)

            ledger_a.record(
                "shared-run",
                "queued",
                metadata={"repository": "QuantStrategyLab/RepoA"},
                owner_repository="QuantStrategyLab/RepoA",
            )
            ledger_b.record(
                "shared-run",
                "running",
                metadata={"repository": "QuantStrategyLab/RepoA"},
                owner_repository="QuantStrategyLab/RepoA",
            )

            reloaded = AutomationRunLedger(storage_path=path)
            stored = reloaded.get("shared-run")

        self.assertEqual(stored["task_state"], "running")
        self.assertEqual([event["task_state"] for event in stored["events"]], ["queued", "running"])

    def test_disk_merge_keeps_terminal_state_over_newer_nonterminal(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            delayed = AutomationRunLedger(storage_path=path)
            terminal = AutomationRunLedger(storage_path=path)

            terminal.record("shared-run", "merged")
            result = delayed.record("shared-run", "running")

            reloaded = AutomationRunLedger(storage_path=path)
            stored = reloaded.get("shared-run")

        self.assertEqual(result["task_state"], "merged")
        self.assertEqual(stored["task_state"], "merged")

    def test_disk_merge_keeps_first_terminal_state(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            delayed = AutomationRunLedger(storage_path=path)
            first_terminal = AutomationRunLedger(storage_path=path)

            first_terminal.record("shared-run", "failed")
            result = delayed.record("shared-run", "merged")

            reloaded = AutomationRunLedger(storage_path=path)
            stored = reloaded.get("shared-run")

        self.assertEqual(result["task_state"], "failed")
        self.assertEqual(stored["task_state"], "failed")

    def test_disk_merge_allows_service_job_stale_failure_recovery(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            delayed = AutomationRunLedger(storage_path=path)
            stale_failure = AutomationRunLedger(storage_path=path)
            metadata = {"origin": "service_job", "repository": "QuantStrategyLab/RepoA", "failure_category": "stale_job_timeout"}

            stale_failure.record("shared-run", "failed", metadata=metadata, owner_repository="QuantStrategyLab/RepoA")
            result = delayed.record("shared-run", "reviewed", metadata=metadata, owner_repository="QuantStrategyLab/RepoA")

            reloaded = AutomationRunLedger(storage_path=path)
            stored = reloaded.get("shared-run")

        self.assertEqual(result["task_state"], "reviewed")
        self.assertEqual(stored["task_state"], "reviewed")

    def test_record_restores_full_state_when_persist_fails(self) -> None:
        ledger = AutomationRunLedger(max_runs=1)
        ledger.record("run-a", "queued")
        before = ledger.snapshot(limit=None)

        with patch.object(
            ledger,
            "_persist_with_owner_guard_locked",
            side_effect=OSError("disk unavailable"),
        ):
            with self.assertRaises(OSError):
                ledger.record("run-b", "running")

        after = ledger.snapshot(limit=None)
        self.assertEqual([run["run_id"] for run in after["runs"]], [run["run_id"] for run in before["runs"]])
        self.assertEqual(after["runs"][0]["task_state"], "queued")

    def test_invalid_persisted_sequence_is_recoverable(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation_runs.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "automation_run_ledger.v1",
                        "sequence": "not-an-int",
                        "runs": {
                            "run-a": {
                                "run_id": "run-a",
                                "task_name": "repo-a",
                                "task_state": "queued",
                                "updated_at": 1.0,
                                "events": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            reloaded = AutomationRunLedger(storage_path=path)
            stored = reloaded.get("run-a")

        self.assertIsNotNone(stored)
        self.assertEqual(stored["task_state"], "queued")
