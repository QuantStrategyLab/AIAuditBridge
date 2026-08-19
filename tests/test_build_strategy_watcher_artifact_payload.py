from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from service.strategy_watch import evaluate_strategy_watch


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "build_strategy_watcher_artifact_payload.py"
SPEC = importlib.util.spec_from_file_location("build_strategy_watcher_artifact_payload", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class StrategyWatcherArtifactPayloadTest(unittest.TestCase):
    def _artifact(
        self, *, generated_at: str, as_of: str, sharpe: float = 1.0
    ) -> dict[str, object]:
        return {
            "schema_version": "strategy_performance.v2",
            "metrics_kind": "performance",
            "repository": "QuantStrategyLab/UsEquitySnapshotPipelines",
            "strategy_profile": "tqqq_core_only_p2_v5",
            "candidate_kind": "individual",
            "domain": "us_equity",
            "generated_at": generated_at,
            "as_of": as_of,
            "current_metrics": {"sharpe": sharpe, "cagr": 0.20, "calmar": 1.2, "win_rate": 0.55, "max_dd": 0.12},
            "evidence": {
                "p1_input_digest": "a" * 64,
                "p2_config_digest": "b" * 64,
                "p3_evidence_id": "c" * 64,
                "strategy_revision": "d" * 40,
                "producer_revision": "e" * 40,
            },
            "lifecycle": {"stage": "P3", "status": "verified"},
            "authority": {"research_only": True, "no_order": True, "p4_p5_p6_authorized": False},
        }

    def test_two_bound_completed_artifacts_form_one_comparable_watcher_payload(self) -> None:
        payload = module.build_strategy_watcher_artifact_payload(
            current_artifact=self._artifact(
                generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18", sharpe=0.8
            ),
            baseline_artifact=self._artifact(
                generated_at="2026-08-18T04:00:00Z", as_of="2026-08-17", sharpe=1.0
            ),
            source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
            workflow_file="tqqq-p1-p3-daily-research.yml",
            current_run_id="123456",
            baseline_run_id="123455",
        )

        self.assertEqual(payload["current_metrics"]["sharpe"], 0.8)
        self.assertEqual(payload["baseline_metrics"]["sharpe"], 1.0)
        self.assertEqual(
            payload["source"],
            "github_actions:QuantStrategyLab/UsEquitySnapshotPipelines:tqqq-p1-p3-daily-research.yml:123455-123456",
        )
        self.assertNotIn("evidence", payload)
        self.assertEqual(
            payload["research_task_evidence"],
            {
                "p1_input_digest": "a" * 64,
                "p2_config_digest": "b" * 64,
                "p3_evidence_id": "c" * 64,
                "strategy_revision": "d" * 40,
                "producer_revision": "e" * 40,
            },
        )
        findings = evaluate_strategy_watch(payload)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_type, "metric_degradation")

    def test_misbound_or_execution_capable_artifacts_fail_closed(self) -> None:
        current = self._artifact(generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18")
        baseline = self._artifact(generated_at="2026-08-18T04:00:00Z", as_of="2026-08-17")
        current["authority"] = {"research_only": True, "no_order": False, "p4_p5_p6_authorized": False}
        with self.assertRaisesRegex(module.StrategyWatcherArtifactError, "research-only"):
            module.build_strategy_watcher_artifact_payload(
                current_artifact=current,
                baseline_artifact=baseline,
                source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                workflow_file="tqqq-p1-p3-daily-research.yml",
                current_run_id="123456",
                baseline_run_id="123455",
            )

        current = self._artifact(generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18")
        current["strategy_profile"] = "other"
        with self.assertRaisesRegex(module.StrategyWatcherArtifactError, "different research candidates"):
            module.build_strategy_watcher_artifact_payload(
                current_artifact=current,
                baseline_artifact=baseline,
                source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                workflow_file="tqqq-p1-p3-daily-research.yml",
                current_run_id="123456",
                baseline_run_id="123455",
            )

    def test_unsafe_fields_and_nonincreasing_observations_fail_closed(self) -> None:
        current = self._artifact(generated_at="2026-08-18T04:00:00Z", as_of="2026-08-18")
        baseline = self._artifact(generated_at="2026-08-18T04:00:00Z", as_of="2026-08-17")
        with self.assertRaisesRegex(module.StrategyWatcherArtifactError, "baseline must precede"):
            module.build_strategy_watcher_artifact_payload(
                current_artifact=current,
                baseline_artifact=baseline,
                source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                workflow_file="tqqq-p1-p3-daily-research.yml",
                current_run_id="123456",
                baseline_run_id="123455",
            )

        current = self._artifact(generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18")
        current["evidence"] = dict(current["evidence"], raw_path="not-allowed")
        with self.assertRaisesRegex(module.StrategyWatcherArtifactError, "unsafe artifact field"):
            module.build_strategy_watcher_artifact_payload(
                current_artifact=current,
                baseline_artifact=baseline,
                source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                workflow_file="tqqq-p1-p3-daily-research.yml",
                current_run_id="123456",
                baseline_run_id="123455",
            )


if __name__ == "__main__":
    unittest.main()
