import os
import tempfile
import unittest
from unittest.mock import patch

from service.feedback import ChangeRecord, EFFECT_DEGRADED, evaluate_change, get_shadow_disagreements, record_shadow_disagreement, read_change, write_change


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
