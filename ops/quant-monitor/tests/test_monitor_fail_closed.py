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

    def test_operational_diagnosis_is_persistently_attempted_once_per_data_error(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class Client:
            def execute(self, prompt: str, **kwargs):
                calls.append((prompt, kwargs))
                return types.SimpleNamespace(
                    success=True,
                    output="diagnosed",
                    error="",
                    raw={"status": "succeeded", "job_id": "job-1"},
                )

        errors = [{
            "domain": "us_equity",
            "code": "monitor_data_unavailable",
            "error_type": "RuntimeError",
            "error": "secret /tmp/source.csv 2026-09-10 price=42",
        }]
        fingerprint = HEALTH_CYCLE._alert_fingerprint(
            ["data_error:us_equity:monitor_data_unavailable:RuntimeError"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = HEALTH_CYCLE._run_operational_diagnosis(
                root,
                errors,
                fingerprint,
                config_loader=lambda: object(),
                client_factory=lambda _config: Client(),
            )
            HEALTH_CYCLE._clear_alert(root)
            second = HEALTH_CYCLE._run_operational_diagnosis(
                root,
                errors,
                fingerprint,
                config_loader=lambda: object(),
                client_factory=lambda _config: Client(),
            )

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["job_id"], "job-1")
        self.assertEqual(second, {"status": "skipped", "reason": "already_attempted"})
        self.assertEqual(len(calls), 1)
        prompt, kwargs = calls[0]
        self.assertIn('"domain":"us_equity"', prompt)
        self.assertIn('"code":"monitor_data_unavailable"', prompt)
        self.assertNotIn("secret", prompt)
        self.assertNotIn("/tmp", prompt)
        self.assertNotIn("2026-09-10", prompt)
        self.assertNotIn("price", prompt)
        self.assertEqual(kwargs["mode"], "review_only")
        self.assertEqual(kwargs["sandbox"], "read-only")
        self.assertEqual(kwargs["allowed_providers"], ["codex"])
        self.assertEqual(kwargs["research_stage"], "drift_analysis")

    def test_operational_diagnosis_defers_without_consuming_fingerprint(self) -> None:
        calls = 0

        class Client:
            def execute(self, _prompt: str, **_kwargs):
                nonlocal calls
                calls += 1
                return types.SimpleNamespace(
                    success=False,
                    output="",
                    error="codex_research_deferred",
                    raw={"status": "deferred", "retry_at": None},
                )

        errors = [{
            "domain": "crypto",
            "code": "drift_data_unavailable",
            "error_type": "ValueError",
        }]
        fingerprint = HEALTH_CYCLE._alert_fingerprint(
            ["data_error:crypto:drift_data_unavailable:ValueError"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for _ in range(2):
                result = HEALTH_CYCLE._run_operational_diagnosis(
                    root,
                    errors,
                    fingerprint,
                    config_loader=lambda: object(),
                    client_factory=lambda _config: Client(),
                )

        self.assertEqual(result, {"status": "deferred", "reason": "capacity_unavailable"})
        self.assertEqual(calls, 2)

    def test_operational_diagnosis_requires_real_data_errors_and_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                HEALTH_CYCLE._run_operational_diagnosis(
                    root,
                    [],
                    "0" * 64,
                    config_loader=lambda: (_ for _ in ()).throw(AssertionError("must not configure")),
                ),
                {"status": "skipped", "reason": "no_data_errors"},
            )
            self.assertEqual(
                HEALTH_CYCLE._run_operational_diagnosis(
                    root,
                    [{"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}],
                    "1" * 64,
                    config_loader=lambda: (_ for _ in ()).throw(ValueError("missing private config")),
                ),
                {"status": "deferred", "reason": "ai_gateway_not_configured"},
            )

    def test_operational_diagnosis_stops_when_persisted_state_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = HEALTH_CYCLE._alert_state_path(root)
            state_path.parent.mkdir(parents=True)
            state_path.write_text("not-json", encoding="utf-8")
            # main records a successfully sent Telegram alert before it runs AI.
            HEALTH_CYCLE._record_alert(root, "alert-fingerprint")
            self.assertEqual(state_path.read_text(), "not-json")
            result = HEALTH_CYCLE._run_operational_diagnosis(
                root,
                [{"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}],
                "2" * 64,
                config_loader=lambda: (_ for _ in ()).throw(AssertionError("must not configure")),
            )

        self.assertEqual(result, {"status": "deferred", "reason": "dedupe_state_unavailable"})

    def test_operational_diagnosis_keeps_unknown_or_auth_failed_attempt(self) -> None:
        for raw in ({"failure_category": "auth_or_config_failure"}, {}):
            calls = []
            class Client:
                def execute(self, _prompt, **_kwargs):
                    calls.append(True)
                    return types.SimpleNamespace(success=False, raw=raw)
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for _ in range(2):
                    HEALTH_CYCLE._run_operational_diagnosis(
                        root,
                        [{"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}],
                        "5" * 64,
                        config_loader=lambda: object(),
                        client_factory=lambda _config: Client(),
                    )
                self.assertEqual(len(calls), 1)

    def test_operational_diagnosis_rejects_malformed_attempt_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = HEALTH_CYCLE._alert_state_path(root)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"operational_diagnosis_attempts": "unknown"}))
            result = HEALTH_CYCLE._run_operational_diagnosis(
                root, [{"domain": "crypto"}], "6" * 64,
                config_loader=lambda: (_ for _ in ()).throw(AssertionError("must not configure")),
            )
            self.assertEqual(result["reason"], "dedupe_state_unavailable")

    def test_operational_diagnosis_does_not_submit_when_attempt_cannot_be_persisted(self) -> None:
        class Client:
            def execute(self, _prompt: str, **_kwargs):
                raise AssertionError("must not submit")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            HEALTH_CYCLE,
            "_write_alert_state",
            side_effect=OSError("private disk failure"),
        ):
            result = HEALTH_CYCLE._run_operational_diagnosis(
                Path(tmp),
                [{"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}],
                "4" * 64,
                config_loader=lambda: object(),
                client_factory=lambda _config: Client(),
            )

        self.assertEqual(result, {"status": "deferred", "reason": "dedupe_state_unavailable"})

    def test_operational_diagnosis_requires_runtime_credentials_before_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "CODEX_AUDIT_SERVICE_URL": "https://gateway.invalid",
                "CODEX_AUDIT_SERVICE_TOKEN": "",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "",
            },
        ):
            root = Path(tmp)
            result = HEALTH_CYCLE._run_operational_diagnosis(
                root,
                [{"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}],
                "3" * 64,
            )

        self.assertEqual(result, {"status": "deferred", "reason": "ai_gateway_not_configured"})
        self.assertFalse(HEALTH_CYCLE._operational_diagnosis_attempted(root, "3" * 64))

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
