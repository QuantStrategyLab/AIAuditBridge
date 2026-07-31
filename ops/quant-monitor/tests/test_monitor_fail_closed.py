import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HEALTH_CYCLE = _load_script("health_cycle")
DAILY_BRIEFING = _load_script("daily_briefing_builder")


class MonitorFailClosedTests(unittest.TestCase):
    def test_health_cycle_collects_drift_errors_without_aborting(self) -> None:
        def unavailable(_domain):
            raise RuntimeError("sensitive path must not escape")

        results, errors = HEALTH_CYCLE._collect_drift_results(
            unavailable,
            domains=("cn_equity", "us_equity"),
        )

        self.assertEqual(results, {})
        self.assertEqual(
            errors,
            [
                {
                    "domain": "cn_equity",
                    "code": "drift_data_unavailable",
                    "error_type": "RuntimeError",
                },
                {
                    "domain": "us_equity",
                    "code": "drift_data_unavailable",
                    "error_type": "RuntimeError",
                },
            ],
        )
        self.assertNotIn("sensitive path", str(errors))

    def test_health_cycle_refreshes_snapshots_before_drift(self) -> None:
        calls: list[tuple[str, str]] = []

        snapshots, results, errors = HEALTH_CYCLE._refresh_and_collect_drift(
            lambda domain: calls.append(("monitor", domain)) or [object()],
            lambda domain: calls.append(("drift", domain)) or [domain],
            domains=("us_equity", "crypto"),
        )

        self.assertEqual(
            calls,
            [
                ("monitor", "us_equity"),
                ("drift", "us_equity"),
                ("monitor", "crypto"),
                ("drift", "crypto"),
            ],
        )
        self.assertEqual(set(snapshots), {"us_equity", "crypto"})
        self.assertEqual(results, {"us_equity": ["us_equity"], "crypto": ["crypto"]})
        self.assertEqual(errors, [])

    def test_health_cycle_skips_drift_when_snapshot_refresh_fails(self) -> None:
        drift_calls: list[str] = []

        snapshots, results, errors = HEALTH_CYCLE._refresh_and_collect_drift(
            lambda _domain: (_ for _ in ()).throw(RuntimeError("sensitive details")),
            lambda domain: drift_calls.append(domain) or [],
            domains=("hk_equity",),
        )

        self.assertEqual(snapshots, {})
        self.assertEqual(results, {})
        self.assertEqual(drift_calls, [])
        self.assertEqual(
            errors,
            [
                {
                    "domain": "hk_equity",
                    "code": "monitor_data_unavailable",
                    "error_type": "RuntimeError",
                }
            ],
        )
        self.assertNotIn("sensitive details", str(errors))

    def test_health_cycle_only_accepts_ready_artifact_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "data" / "lifecycle-artifacts" / "status.json"
            status_path.parent.mkdir(parents=True)
            status_path.write_text(
                json.dumps(
                    {
                        "schema_version": "quant_monitor_lifecycle_artifact_status.v1",
                        "as_of": "2026-07-30T07:00:00+00:00",
                        "domains": {
                            "us_equity": {
                                "status": "ready",
                                "artifact_id": 1,
                                "run_id": 2,
                                "head_sha": "a" * 40,
                                "profiles": ["global_etf_rotation"],
                            },
                            "crypto": {
                                "status": "error",
                                "code": "trusted_artifact_unavailable",
                                "error_type": "LifecycleArtifactError",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            ready, errors = HEALTH_CYCLE._load_lifecycle_artifact_status(
                root,
                domains=("us_equity", "crypto"),
                now=HEALTH_CYCLE.datetime.fromisoformat(
                    "2026-07-30T07:30:00+00:00"
                ),
            )

        self.assertEqual(ready, ("us_equity",))
        self.assertEqual(
            errors,
            [
                {
                    "domain": "crypto",
                    "code": "trusted_artifact_unavailable",
                    "error_type": "LifecycleArtifactError",
                }
            ],
        )

    def test_daily_briefing_collects_drift_errors_without_aborting(self) -> None:
        def unavailable(_domain):
            raise RuntimeError("sensitive path must not escape")

        results, errors = DAILY_BRIEFING._collect_drift_results(
            unavailable,
            domains=("crypto",),
        )

        self.assertEqual(results, {})
        self.assertEqual(
            errors,
            {
                "crypto": {
                    "code": "drift_data_unavailable",
                    "error_type": "RuntimeError",
                }
            },
        )
        self.assertNotIn("sensitive path", str(errors))

    def test_health_cycle_alert_fingerprint_is_deduplicated_until_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fingerprint = HEALTH_CYCLE._alert_fingerprint(["same failure"])

            self.assertFalse(HEALTH_CYCLE._is_duplicate_alert(root, fingerprint))
            HEALTH_CYCLE._record_alert(root, fingerprint)
            self.assertTrue(HEALTH_CYCLE._is_duplicate_alert(root, fingerprint))

            HEALTH_CYCLE._clear_alert(root)
            self.assertFalse(HEALTH_CYCLE._is_duplicate_alert(root, fingerprint))

    def test_health_cycle_builds_issue_only_monitoring_finding(self) -> None:
        findings = HEALTH_CYCLE._build_monitoring_findings(
            [
                {
                    "domain": "us_equity",
                    "strategy_profile": "global_etf_rotation",
                    "status": "critical",
                    "overall_score": 14.2,
                    "performance_score": 0.0,
                }
            ],
            {},
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].snapshot.repo, "QuantStrategyLab/UsEquityStrategies")
        self.assertEqual(findings[0].finding_type, "monitoring_trigger")
        self.assertEqual(findings[0].severity, "high")

    def test_health_cycle_merges_drift_into_strategy_monitoring_finding(self) -> None:
        drift = types.SimpleNamespace(
            strategy_profile="example",
            drift_score=0.8,
        )

        findings = HEALTH_CYCLE._build_monitoring_findings(
            [
                {
                    "domain": "crypto",
                    "strategy_profile": "example",
                    "status": "review",
                    "overall_score": 45.0,
                }
            ],
            {"crypto": [drift]},
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].snapshot.current_metrics["drift_score"], 0.8)
        self.assertEqual(findings[0].severity, "high")
        self.assertEqual(len(findings[0].signals), 2)

    def test_health_cycle_telegram_body_is_operational_only(self) -> None:
        body = HEALTH_CYCLE._build_alert_body(["[collector] dashboard_data_unavailable"])

        self.assertIn("quant-monitor operational", body)
        self.assertIn("data/evidence or optimization-record delivery", body)
        self.assertNotIn("strategy_lifecycle", body)

    def test_health_cycle_non_object_alert_state_is_a_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = HEALTH_CYCLE._alert_state_path(root)
            state_path.parent.mkdir(parents=True)
            for payload in (None, [], "invalid"):
                state_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(HEALTH_CYCLE._is_duplicate_alert(root, "fingerprint"))

    def test_daily_briefing_marks_missing_dashboard_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qpk = types.ModuleType("quant_platform_kit")
            lifecycle = types.ModuleType("quant_platform_kit.strategy_lifecycle")
            drift_detector = types.ModuleType("quant_platform_kit.strategy_lifecycle.drift_detector")
            health_dashboard = types.ModuleType("quant_platform_kit.strategy_lifecycle.health_dashboard")
            drift_detector.run_drift_detection = lambda _domain: []
            health_dashboard.build_dashboard = lambda **_kwargs: None
            with (
                mock.patch.dict(
                    os.environ,
                    {"QUANT_MONITOR_ROOT": str(root), "DAY": "2026-07-30"},
                ),
                mock.patch.dict(
                    sys.modules,
                    {
                        "quant_platform_kit": qpk,
                        "quant_platform_kit.strategy_lifecycle": lifecycle,
                        "quant_platform_kit.strategy_lifecycle.drift_detector": drift_detector,
                        "quant_platform_kit.strategy_lifecycle.health_dashboard": health_dashboard,
                    },
                ),
            ):
                self.assertEqual(DAILY_BRIEFING.main(), 0)

            for domain in DAILY_BRIEFING.DOMAINS:
                report = json.loads(
                    (root / "data" / "daily-reports" / "2026-07-30" / f"{domain}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertFalse(report["ok"])
                self.assertEqual(report["data_status"], "unavailable")
                self.assertIn(
                    {"code": "dashboard_data_unavailable", "error_type": "FileNotFoundError"},
                    report["errors"],
                )


if __name__ == "__main__":
    unittest.main()
