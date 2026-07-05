import os
import tempfile
import unittest
from unittest.mock import patch

from service.feedback import ChangeRecord, EFFECT_DEGRADED, effectiveness_report, evaluate_change, get_shadow_disagreements, record_shadow_disagreement, read_change, write_change


class FeedbackPersistenceTest(unittest.TestCase):
    def test_shadow_disagreement_persists_to_job_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False):
                first = record_shadow_disagreement("local/repo", "crisis_response", "disagree", 0.91, "route_a")
                self.assertEqual(first["disagreement_count"], 1)
                record_shadow_disagreement("local/repo", "crisis_response", "disagree", 0.88, "route_a")
            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False):
                disagreements = get_shadow_disagreements()
        self.assertEqual((len(disagreements), disagreements[0]["repo"], disagreements[0]["disagreement_count"]), (1, "local/repo", 2))

    def test_evaluate_change_marks_rollback_intent_for_degraded_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False):
            record = ChangeRecord("abc123abc123abc123abc123", "local/repo", "monthly_snapshot_audit", "auto_pr", 0.8, "medium", before_metrics={"sharpe": 1.0, "max_dd": 0.1})
            write_change(record)
            evaluated = evaluate_change(record.change_id, {"sharpe": 0.7, "max_dd": 0.18})
            stored = read_change(record.change_id)
        self.assertEqual(evaluated.effect, EFFECT_DEGRADED)
        self.assertTrue(stored.rollback_issue_required)
        self.assertEqual(stored.rollback_intent, evaluated.rollback_intent)


class FeedbackStateReportTest(unittest.TestCase):
    def test_change_record_exports_state_and_human_review_flag(self) -> None:
        record = ChangeRecord(
            "abc123abc123abc123abc999",
            "local/repo",
            "monthly_snapshot_audit",
            "auto_merge",
            0.95,
            "low",
            pr_number=12,
            external_url="https://example.test/pr/12",
        )

        payload = record.to_dict()

        self.assertEqual(payload["state"], "auto_merge_requested")
        self.assertFalse(payload["human_review_required"])

    def test_effectiveness_report_persists_operational_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False):
            improved = ChangeRecord(
                "abc123abc123abc123abc124",
                "local/repo",
                "monthly_snapshot_audit",
                "auto_merge",
                0.95,
                "low",
                before_metrics={"sharpe": 1.0},
            )
            degraded = ChangeRecord(
                "abc123abc123abc123abc125",
                "local/repo",
                "monthly_snapshot_audit",
                "auto_pr",
                0.85,
                "medium",
                before_metrics={"sharpe": 1.0},
            )
            write_change(improved)
            write_change(degraded)
            evaluate_change(improved.change_id, {"sharpe": 1.2})
            evaluate_change(degraded.change_id, {"sharpe": 0.7})
            report = effectiveness_report(repo="local/repo", days=7)

        self.assertEqual(report["evaluated"], 2)
        self.assertEqual(report["auto_actions"], 2)
        self.assertEqual(report["human_review_required"], 1)
        self.assertEqual(report["rollback_required"], 1)
        self.assertEqual(report["by_risk"]["medium"]["degraded"], 1)
        self.assertEqual(report["by_state"]["human_review_required"], 1)
