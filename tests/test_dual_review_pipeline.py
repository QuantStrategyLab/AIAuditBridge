from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_dual_review_pipeline import main, run_pipeline


class DualReviewPipelineTests(unittest.TestCase):
    @patch.dict("os.environ", {"DUAL_REVIEW_GATE_SKIP": "1"}, clear=False)
    def test_pipeline_skipped_when_disabled(self) -> None:
        result = run_pipeline(
            trigger="promotion",
            strategy_profile="demo",
            context={"old_status": "shadow_candidate", "new_status": "live_candidate"},
            primary_review={"verdict": "approve", "confidence": 0.4},
        )
        self.assertTrue(result.get("ok"))
        self.assertIn("dual_review_gate_disabled", result.get("skipped", []))

    def test_pipeline_with_injected_primary(self) -> None:
        with patch.dict("os.environ", {"DUAL_REVIEW_SECONDARY_MODE": "stub"}, clear=False):
            result = run_pipeline(
                trigger="drift",
                strategy_profile="demo",
                context={"drift_score": 0.95},
                primary_review={"verdict": "approve", "confidence": 0.4},
            )
        self.assertTrue(result.get("ok"))
        self.assertIn("outcome", result)

    @patch("scripts.run_dual_review_pipeline.orchestrate_from_payload")
    @patch.dict("os.environ", {"DUAL_REVIEW_SECONDARY_MODE": "stub"}, clear=False)
    def test_pipeline_disagreement_exit_shape(self, mock_orchestrate) -> None:
        from service.dual_review import DualReviewTrigger
        from service.dual_review_orchestrator import DualReviewResult

        mock_orchestrate.return_value = DualReviewResult(
            trigger=DualReviewTrigger.DRIFT,
            strategy_profile="demo",
            primary_review={"verdict": "approve", "confidence": 0.4},
            outcome="disagreement",
        )
        result = run_pipeline(
            trigger="drift",
            strategy_profile="demo",
            context={"drift_score": 0.95},
            primary_review={"verdict": "approve", "confidence": 0.4},
        )
        self.assertEqual(result.get("outcome"), "disagreement")

    @patch("scripts.run_dual_review_pipeline.orchestrate_from_payload")
    def test_all_reviewers_unavailable_degrades_without_dispatch(self, mock_orchestrate) -> None:
        from service.dual_review import DualReviewTrigger
        from service.dual_review_orchestrator import DualReviewResult

        mock_orchestrate.return_value = DualReviewResult(
            trigger=DualReviewTrigger.DRIFT,
            strategy_profile="demo",
            primary_review={"verdict": "unavailable", "confidence": 0.0},
            outcome="unavailable",
        )
        result = run_pipeline(
            trigger="drift",
            strategy_profile="demo",
            context={"drift_score": 0.95},
            primary_review={"verdict": "unavailable", "confidence": 0.0},
            dispatch=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped"], ["reviewers_unavailable"])
        self.assertNotIn("dispatch", result)

    @patch.dict("os.environ", {"DUAL_REVIEW_GATE_SKIP": "1"}, clear=False)
    def test_from_evidence_cli_without_trigger_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.json"
            path.write_text(
                json.dumps(
                    {
                        "strategy_profile": "demo",
                        "oos_sharpe": 1.1,
                        "max_drawdown": 0.12,
                        "hit_rate": 0.55,
                    }
                ),
                encoding="utf-8",
            )
            with patch("sys.stdout") as stdout:
                code = main(["--from-evidence", str(path)])
            self.assertEqual(code, 0)
            output = "".join(call.args[0] for call in stdout.write.call_args_list)
            payload = json.loads(output)
            self.assertIn("dual_review_gate_disabled", payload.get("skipped", []))


if __name__ == "__main__":
    unittest.main()
