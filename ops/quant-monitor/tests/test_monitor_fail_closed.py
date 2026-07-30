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

    def test_health_cycle_non_object_alert_state_is_a_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = HEALTH_CYCLE._alert_state_path(root)
            state_path.parent.mkdir(parents=True)
            for payload in (None, [], "invalid"):
                state_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertFalse(HEALTH_CYCLE._is_duplicate_alert(root, "fingerprint"))

    def test_health_cycle_skips_issue_creation_when_drift_is_unavailable(self) -> None:
        created_for: list[str] = []

        results = HEALTH_CYCLE._create_issues_for_available_domains(
            {"us_equity": []},
            lambda domain: created_for.append(domain) or [],
            domains=("us_equity", "crypto"),
        )

        self.assertEqual(results, [])
        self.assertEqual(created_for, ["us_equity"])

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
