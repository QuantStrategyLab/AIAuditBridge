from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from service.strategy_watch import evaluate_strategy_watch, finding_to_research_task


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

    def test_selects_earlier_distinct_cutoff_without_replacing_latest_current(self) -> None:
        current = self._artifact(
            generated_at="2026-08-20T04:00:00Z", as_of="2026-08-18", sharpe=0.8
        )
        repeated_cutoff = self._artifact(
            generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18", sharpe=0.9
        )
        other_identity = self._artifact(
            generated_at="2026-08-18T05:00:00Z", as_of="2026-08-17"
        )
        other_identity["strategy_profile"] = "other"
        later_generation = self._artifact(
            generated_at="2026-08-21T04:00:00Z", as_of="2026-08-17"
        )
        earlier_cutoff = self._artifact(
            generated_at="2026-08-18T04:00:00Z", as_of="2026-08-17", sharpe=1.0
        )

        payload = module.select_strategy_watcher_artifact_payload(
            observations=[
                ("123456", current),
                ("123455", repeated_cutoff),
                ("123454", other_identity),
                ("123453", later_generation),
                ("123452", earlier_cutoff),
            ],
            source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
            workflow_file="soxl-p1-p3-daily-research.yml",
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["current_metrics"]["sharpe"], 0.8)
        self.assertEqual(payload["baseline_metrics"]["sharpe"], 1.0)
        self.assertTrue(payload["source"].endswith(":123452-123456"))

        current["authority"] = {
            "research_only": True,
            "no_order": False,
            "p4_p5_p6_authorized": False,
        }
        with self.assertRaisesRegex(module.StrategyWatcherArtifactError, "research-only"):
            module.select_strategy_watcher_artifact_payload(
                observations=[
                    ("123456", current),
                    ("123455", repeated_cutoff),
                    ("123454", other_identity),
                    ("123453", later_generation),
                    ("123452", earlier_cutoff),
                ],
                source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                workflow_file="soxl-p1-p3-daily-research.yml",
            )

    def test_selection_is_unavailable_without_an_earlier_comparable_cutoff(self) -> None:
        current = self._artifact(
            generated_at="2026-08-20T04:00:00Z", as_of="2026-08-18"
        )
        repeated_cutoff = self._artifact(
            generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18"
        )

        self.assertIsNone(
            module.select_strategy_watcher_artifact_payload(
                observations=[("123456", current), ("123455", repeated_cutoff)],
                source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                workflow_file="soxl-p1-p3-daily-research.yml",
            )
        )

        other_identity = self._artifact(
            generated_at="2026-08-18T04:00:00Z", as_of="2026-08-17"
        )
        other_identity["strategy_profile"] = "other"
        self.assertIsNone(
            module.select_strategy_watcher_artifact_payload(
                observations=[("123456", current), ("123454", other_identity)],
                source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                workflow_file="soxl-p1-p3-daily-research.yml",
            )
        )

        with self.assertRaisesRegex(module.StrategyWatcherArtifactError, "one to five"):
            module.select_strategy_watcher_artifact_payload(
                observations=[("123456", current)] * 6,
                source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                workflow_file="soxl-p1-p3-daily-research.yml",
            )

    def test_each_changed_frozen_evidence_identity_rejects_direct_comparison(self) -> None:
        for key in ("p2_config_digest", "strategy_revision", "producer_revision"):
            with self.subTest(key=key):
                current = self._artifact(
                    generated_at="2026-08-20T04:00:00Z", as_of="2026-08-19", sharpe=0.8
                )
                baseline = self._artifact(
                    generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18"
                )
                baseline["evidence"][key] = "f" * len(baseline["evidence"][key])

                with self.assertRaisesRegex(module.StrategyWatcherArtifactError, "unproven comparison identity"):
                    module.build_strategy_watcher_artifact_payload(
                        current_artifact=current,
                        baseline_artifact=baseline,
                        source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                        workflow_file="tqqq-p1-p3-daily-research.yml",
                        current_run_id="123456",
                        baseline_run_id="123455",
                    )

    def test_selector_skips_unproven_identity_and_uses_nearest_qualified_baseline(self) -> None:
        for key in ("p2_config_digest", "strategy_revision", "producer_revision"):
            with self.subTest(key=key):
                current = self._artifact(
                    generated_at="2026-08-20T04:00:00Z", as_of="2026-08-19", sharpe=0.8
                )
                incompatible = self._artifact(
                    generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18", sharpe=2.0
                )
                incompatible["evidence"][key] = "f" * len(incompatible["evidence"][key])
                nearest_qualified = self._artifact(
                    generated_at="2026-08-18T04:00:00Z", as_of="2026-08-17", sharpe=1.0
                )
                older_qualified = self._artifact(
                    generated_at="2026-08-17T04:00:00Z", as_of="2026-08-16", sharpe=1.5
                )

                payload = module.select_strategy_watcher_artifact_payload(
                    observations=[
                        ("123456", current),
                        ("123455", incompatible),
                        ("123454", nearest_qualified),
                        ("123453", older_qualified),
                    ],
                    source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
                    workflow_file="tqqq-p1-p3-daily-research.yml",
                )

                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(payload["current_metrics"]["sharpe"], 0.8)
                self.assertEqual(payload["baseline_metrics"]["sharpe"], 1.0)
                self.assertTrue(payload["source"].endswith(":123454-123456"))

    def test_all_unproven_baselines_produce_no_payload_or_research_task(self) -> None:
        current = self._artifact(
            generated_at="2026-08-20T04:00:00Z", as_of="2026-08-19", sharpe=0.8
        )
        observations = [("123456", current)]
        for index, key in enumerate(("p2_config_digest", "strategy_revision", "producer_revision")):
            baseline = self._artifact(
                generated_at=f"2026-08-{19 - index:02d}T04:00:00Z",
                as_of=f"2026-08-{18 - index:02d}",
            )
            baseline["evidence"][key] = "f" * len(baseline["evidence"][key])
            observations.append((str(123455 - index), baseline))

        payload = module.select_strategy_watcher_artifact_payload(
            observations=observations,
            source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
            workflow_file="tqqq-p1-p3-daily-research.yml",
        )

        self.assertIsNone(payload)
        tasks = [] if payload is None else [
            finding_to_research_task(finding) for finding in evaluate_strategy_watch(payload)
        ]
        self.assertEqual(tasks, [])

    def test_same_frozen_identity_allows_daily_input_and_evidence_changes(self) -> None:
        current = self._artifact(
            generated_at="2026-08-20T04:00:00Z", as_of="2026-08-19", sharpe=0.8
        )
        baseline = self._artifact(
            generated_at="2026-08-19T04:00:00Z", as_of="2026-08-18"
        )
        baseline["evidence"]["p1_input_digest"] = "f" * 64
        baseline["evidence"]["p3_evidence_id"] = "0" * 64

        payload = module.build_strategy_watcher_artifact_payload(
            current_artifact=current,
            baseline_artifact=baseline,
            source_repository="QuantStrategyLab/UsEquitySnapshotPipelines",
            workflow_file="tqqq-p1-p3-daily-research.yml",
            current_run_id="123456",
            baseline_run_id="123455",
        )

        self.assertEqual(payload["research_task_evidence"], current["evidence"])
        tasks = [finding_to_research_task(finding) for finding in evaluate_strategy_watch(payload)]
        self.assertEqual(len(tasks), 1)
        self.assertIsNotNone(tasks[0])
        self.assertEqual(tasks[0]["authority"], {
            "research_only": True,
            "no_order": True,
            "size_zero_required": True,
            "p4_p5_p6_authorized": False,
        })


if __name__ == "__main__":
    unittest.main()
