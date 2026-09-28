import importlib.util
import json
import os
import sys
import tempfile
import threading
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


def _write_fresh_lifecycle_status(root: Path, *, profiles_by_domain=None):
    domains = profiles_by_domain or {domain: [f"{domain}_profile"] for domain in DAILY_BRIEFING.DOMAINS}
    now = HEALTH_CYCLE.datetime.now(HEALTH_CYCLE.timezone.utc).isoformat()
    payload = {
        "schema_version": "quant_monitor_lifecycle_artifact_status.v1",
        "as_of": now,
        "domains": {
            domain: {
                "status": "ready", "artifact_id": index + 1, "run_id": index + 11,
                "head_sha": f"{index + 1:040x}", "profiles": profiles,
            }
            for index, (domain, profiles) in enumerate(domains.items())
        },
    }
    status_path = root / "data/lifecycle-artifacts/status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    return now


HEALTH_CYCLE = _load_script("health_cycle")
DAILY_BRIEFING = _load_script("daily_briefing_builder")


class MonitorFailClosedTests(unittest.TestCase):
    def test_historical_auth_guard_rehearsal_is_fixed_read_only_codex_only(self):
        calls: list[tuple[str, dict[str, object]]] = []

        class Client:
            def execute(self, prompt: str, **kwargs):
                calls.append((prompt, kwargs))
                return types.SimpleNamespace(
                    success=True,
                    raw={"status": "succeeded", "job_id": "R" * 32},
                )

        safe_env = {
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc-request",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        with mock.patch.dict(os.environ, safe_env, clear=True):
            result = HEALTH_CYCLE.run_historical_diagnosis_rehearsal(
                config_loader=lambda: object(),
                client_factory=lambda _config: Client(),
            )

        self.assertEqual(result, {"status": "succeeded", "job_id": "R" * 32})
        self.assertEqual(len(calls), 1)
        prompt, kwargs = calls[0]
        self.assertIn("static_token_write_guard_source_audit_v1", prompt)
        self.assertIn('"observed_request":false', prompt)
        self.assertIn("No real HTTP 403 request was observed", prompt)
        self.assertIn("Simplified Chinese", prompt)
        self.assertIn("300 Chinese characters or fewer", prompt)
        self.assertNotIn("/Users/", prompt)
        self.assertNotIn("token=", prompt)
        self.assertEqual(kwargs["task"], "historical_operational_diagnosis_rehearsal")
        self.assertEqual(kwargs["mode"], "review_only")
        self.assertEqual(kwargs["sandbox"], "read-only")
        self.assertEqual(kwargs["research_stage"], "drift_analysis")
        self.assertEqual(kwargs["allowed_providers"], ["codex"])
        self.assertEqual(kwargs["timeout"], 600)

    def test_historical_rehearsal_requires_first_attempt_oidc_and_rejects_keys(self):
        base = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
        }
        cases = [
            ({**base, "GITHUB_RUN_ATTEMPT": "2", "ACTIONS_ID_TOKEN_REQUEST_URL": "x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "y"}, "invalid_workflow_attempt"),
            ({**base, "GITHUB_RUN_ATTEMPT": "1"}, "github_oidc_required"),
            ({**base, "GITHUB_RUN_ATTEMPT": "1", "ACTIONS_ID_TOKEN_REQUEST_URL": "x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "y", "CODEX_AUDIT_SERVICE_TOKEN": "static"}, "non_oidc_credentials_rejected"),
            ({**base, "GITHUB_RUN_ATTEMPT": "1", "ACTIONS_ID_TOKEN_REQUEST_URL": "x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "y", "OPENAI_API_KEY": "paid"}, "non_oidc_credentials_rejected"),
            ({**base, "GITHUB_RUN_ATTEMPT": "1", "ACTIONS_ID_TOKEN_REQUEST_URL": "x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "y", "CURSOR_API_KEY": "paid"}, "non_oidc_credentials_rejected"),
            ({**base, "GITHUB_REPOSITORY": "Other/repo", "GITHUB_RUN_ATTEMPT": "1", "ACTIONS_ID_TOKEN_REQUEST_URL": "x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "y"}, "invalid_workflow_context"),
            ({**base, "GITHUB_REF": "refs/heads/feature", "GITHUB_RUN_ATTEMPT": "1", "ACTIONS_ID_TOKEN_REQUEST_URL": "x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "y"}, "invalid_workflow_context"),
            ({**base, "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ATTEMPT": "1", "ACTIONS_ID_TOKEN_REQUEST_URL": "x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "y"}, "invalid_workflow_context"),
        ]
        for env, reason in cases:
            with self.subTest(reason=reason), mock.patch.dict(os.environ, env, clear=True):
                result = HEALTH_CYCLE.run_historical_diagnosis_rehearsal(
                    config_loader=lambda: (_ for _ in ()).throw(AssertionError("must not configure")),
                )
            self.assertEqual(result, {"status": "rejected", "reason": reason})

    def test_historical_rehearsal_never_retries_or_leaks_gateway_failure(self):
        calls = 0

        class Client:
            def execute(self, _prompt: str, **_kwargs):
                nonlocal calls
                calls += 1
                return types.SimpleNamespace(
                    success=False,
                    error="private response /tmp/token",
                    raw={"status": "deferred", "retry_at": 9999},
                )

        with mock.patch.dict(os.environ, {
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc-request",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
            "GITHUB_RUN_ATTEMPT": "1",
        }, clear=True):
            result = HEALTH_CYCLE.run_historical_diagnosis_rehearsal(
                config_loader=lambda: object(),
                client_factory=lambda _config: Client(),
            )
        self.assertEqual(result, {"status": "deferred", "reason": "capacity_unavailable"})
        self.assertEqual(calls, 1)
        self.assertNotIn("private", repr(result))

    def test_latest_cycle_diagnosis_rejects_invalid_or_stale_without_fallback(self):
        now = HEALTH_CYCLE.datetime(2026, 9, 10, 4, tzinfo=HEALTH_CYCLE.timezone.utc)
        valid = {"as_of": "2026-09-10T03:50:00+00:00", "domains": list(HEALTH_CYCLE.DOMAINS),
                 "data_errors": [{"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}]}
        variants = ["invalid", {**valid, "as_of": "2026-09-09T00:00:00+00:00"},
                    {**valid, "as_of": "2026-09-10T05:00:00+00:00"},
                    {**valid, "as_of": "2026-09-10T03:50:00"}, {**valid, "data_errors": None},
                    {**valid, "data_errors": [{"domain": "unknown"}]}, {**valid, "domains": []}]
        for latest in variants:
            with self.subTest(latest=latest), tempfile.TemporaryDirectory() as tmp, mock.patch.object(HEALTH_CYCLE, "_run_operational_diagnosis") as run:
                root = Path(tmp)
                health = root / "data/health"
                health.mkdir(parents=True)
                (health / "cycle_20260910T035000Z.json").write_text(json.dumps(valid))
                (health / "cycle_20260910T035100Z.json").write_text(json.dumps(latest))
                result = HEALTH_CYCLE.diagnose_latest_cycle(root, now=now)
                self.assertEqual(result["status"], "rejected")
                run.assert_not_called()

    def test_latest_cycle_consumer_uses_isolated_state_and_existing_diagnosis(self):
        now = HEALTH_CYCLE.datetime(2026, 9, 10, 4, tzinfo=HEALTH_CYCLE.timezone.utc)
        errors = [{"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(HEALTH_CYCLE, "_run_operational_diagnosis", return_value={"status": "succeeded"}) as run:
            root = Path(tmp)
            health = root / "data/health"
            health.mkdir(parents=True)
            path = health / "cycle_20260910T035000Z.json"
            payload = {"as_of": "2026-09-10T03:50:00+00:00", "domains": list(HEALTH_CYCLE.DOMAINS), "data_errors": errors}
            path.write_text(json.dumps(payload))
            self.assertEqual(HEALTH_CYCLE.diagnose_latest_cycle(root, now=now)["status"], "succeeded")
            self.assertEqual(run.call_args.args, (root / "data/diagnosis-consumer", errors, HEALTH_CYCLE._operational_diagnosis_fingerprint(errors)))
            run.reset_mock()
            payload["data_errors"] = []
            path.write_text(json.dumps(payload))
            self.assertEqual(HEALTH_CYCLE.diagnose_latest_cycle(root, now=now), {"status": "skipped", "reason": "no_data_errors"})
            run.assert_not_called()

    def test_static_dashboard_token_cannot_submit_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_TOKEN": "synthetic-readonly", "CODEX_AUDIT_SERVICE_URL": "https://invalid"}, clear=True), mock.patch.object(HEALTH_CYCLE, "_record_operational_diagnosis_attempt") as record:
            result = HEALTH_CYCLE._run_operational_diagnosis(Path(tmp), [{"domain": "crypto"}], "3" * 64)
            self.assertEqual(result, {"status": "deferred", "reason": "ai_gateway_not_configured"})
            record.assert_not_called()

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

            ready, source_revisions, errors = HEALTH_CYCLE._load_lifecycle_artifact_status(
                root,
                domains=("us_equity", "crypto"),
                now=HEALTH_CYCLE.datetime.fromisoformat(
                    "2026-07-30T07:30:00+00:00"
                ),
            )

        self.assertEqual(ready, ("us_equity",))
        self.assertEqual(source_revisions, {"us_equity": "a" * 40})
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

    def test_health_cycle_passes_through_safe_reason_code(self) -> None:
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
                            "crypto": {
                                "status": "error",
                                "code": "artifact_sync_status_unavailable",
                                "error_type": "RuntimeError",
                                "source": "binance_live_runs",
                                "reason_code": "github_api_unavailable",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            ready, source_revisions, errors = HEALTH_CYCLE._load_lifecycle_artifact_status(
                root,
                domains=("crypto",),
                now=HEALTH_CYCLE.datetime.fromisoformat("2026-07-30T07:30:00+00:00"),
            )

        self.assertEqual(ready, ())
        self.assertEqual(source_revisions, {})
        self.assertEqual(
            errors,
            [
                {
                    "domain": "crypto",
                    "code": "artifact_sync_status_unavailable",
                    "error_type": "RuntimeError",
                    "reason_code": "github_api_unavailable",
                }
            ],
        )
        self.assertNotIn("token", json.dumps(errors))

    def test_health_cycle_compresses_shared_github_upstream_alerts(self) -> None:
        errors = [
            {
                "domain": domain,
                "code": "github_api_rate_limit",
                "error_type": "LifecycleArtifactError",
                "reason_code": "github_api_rate_limit",
                "shared_root_cause": "github_api_rate_limit",
                "rate_limit_reset_at": "2023-09-22T16:00:00+00:00",
            }
            for domain in ("cn_equity", "hk_equity", "us_equity", "crypto")
        ]
        lines, identities = HEALTH_CYCLE._format_data_error_alerts(errors)
        self.assertEqual(len(lines), 1)
        self.assertIn("github_api_upstream", lines[0])
        self.assertIn("github_api_rate_limit", lines[0])
        self.assertIn("domains=cn_equity,crypto,hk_equity,us_equity", lines[0])
        self.assertIn("not a strategy/trading signal", lines[0])
        self.assertIn("rate_limit_reset_at=2023-09-22T16:00:00+00:00", lines[0])
        self.assertEqual(len(identities), 1)
        self.assertTrue(identities[0].startswith("data_error:github_api_upstream:"))
        body = HEALTH_CYCLE._build_alert_body(lines)
        self.assertIn("github_api_upstream", body)
        self.assertNotIn("ghs_", body)

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
            _write_fresh_lifecycle_status(root)
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

    def test_daily_briefing_marks_missing_profile_and_drift_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_as_of = _write_fresh_lifecycle_status(root, profiles_by_domain={
                "us_equity": ["expected_profile"],
            })
            qpk = types.ModuleType("quant_platform_kit")
            lifecycle = types.ModuleType("quant_platform_kit.strategy_lifecycle")
            drift_detector = types.ModuleType("quant_platform_kit.strategy_lifecycle.drift_detector")
            health_dashboard = types.ModuleType("quant_platform_kit.strategy_lifecycle.health_dashboard")
            drift_detector.run_drift_detection = lambda _domain: []
            health_dashboard.build_dashboard = lambda **kwargs: Path(kwargs["output_dir"], "strategy_health_dashboard.json").write_text(
                json.dumps({"strategies": [{
                    "domain": "us_equity", "strategy_profile": "expected_profile", "status": "healthy",
                    "as_of": HEALTH_CYCLE.datetime.now(HEALTH_CYCLE.timezone.utc).date().isoformat(),
                }]}), encoding="utf-8"
            )
            with (
                mock.patch.dict(os.environ, {"QUANT_MONITOR_ROOT": str(root), "DAY": "2026-09-29"}),
                mock.patch.dict(sys.modules, {
                    "quant_platform_kit": qpk,
                    "quant_platform_kit.strategy_lifecycle": lifecycle,
                    "quant_platform_kit.strategy_lifecycle.drift_detector": drift_detector,
                    "quant_platform_kit.strategy_lifecycle.health_dashboard": health_dashboard,
                }),
            ):
                self.assertEqual(DAILY_BRIEFING.main(), 0)

            report = json.loads((root / "data/daily-reports/2026-09-29/us_equity.json").read_text())
            self.assertFalse(report["ok"])
            self.assertEqual(report["data_status"], "unavailable")
            self.assertEqual(report["as_of"], source_as_of)
            self.assertEqual(report["coverage"]["expected_profiles"], ["expected_profile"])
            self.assertIn("drift_data_unavailable", {error["code"] for error in report["errors"]})

    def test_daily_briefing_does_not_call_empty_ready_domain_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fresh_lifecycle_status(root, profiles_by_domain={"us_equity": ["expected_profile"]})
            qpk = types.ModuleType("quant_platform_kit")
            lifecycle = types.ModuleType("quant_platform_kit.strategy_lifecycle")
            drift_detector = types.ModuleType("quant_platform_kit.strategy_lifecycle.drift_detector")
            health_dashboard = types.ModuleType("quant_platform_kit.strategy_lifecycle.health_dashboard")
            drift_detector.run_drift_detection = lambda _domain: [types.SimpleNamespace(strategy_profile="expected_profile", drift_score=0.0)]
            health_dashboard.build_dashboard = lambda **kwargs: Path(kwargs["output_dir"], "strategy_health_dashboard.json").write_text(
                json.dumps({"strategies": []}), encoding="utf-8"
            )
            with (
                mock.patch.dict(os.environ, {"QUANT_MONITOR_ROOT": str(root), "DAY": "2026-09-29"}),
                mock.patch.dict(sys.modules, {
                    "quant_platform_kit": qpk,
                    "quant_platform_kit.strategy_lifecycle": lifecycle,
                    "quant_platform_kit.strategy_lifecycle.drift_detector": drift_detector,
                    "quant_platform_kit.strategy_lifecycle.health_dashboard": health_dashboard,
                }),
            ):
                self.assertEqual(DAILY_BRIEFING.main(), 0)

            report = json.loads((root / "data/daily-reports/2026-09-29/us_equity.json").read_text())
            self.assertFalse(report["ok"])
            self.assertEqual(report["data_status"], "unavailable")
            self.assertIn("dashboard_coverage_incomplete", {error["code"] for error in report["errors"]})

    def test_daily_briefing_distinguishes_explicit_unconfigured_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = HEALTH_CYCLE.datetime.now(HEALTH_CYCLE.timezone.utc).isoformat()
            status_path = root / "data/lifecycle-artifacts/status.json"
            status_path.parent.mkdir(parents=True)
            domains = {
                domain: {
                    "status": "ready", "artifact_id": index + 1, "run_id": index + 11,
                    "head_sha": f"{index + 1:040x}", "profiles": [f"{domain}_profile"],
                }
                for index, domain in enumerate(DAILY_BRIEFING.DOMAINS)
                if domain != "crypto"
            }
            domains["crypto"] = {"status": "not_configured", "profiles": []}
            status_path.write_text(json.dumps({
                "schema_version": "quant_monitor_lifecycle_artifact_status.v1",
                "as_of": now,
                "domains": domains,
            }), encoding="utf-8")
            qpk = types.ModuleType("quant_platform_kit")
            lifecycle = types.ModuleType("quant_platform_kit.strategy_lifecycle")
            drift_detector = types.ModuleType("quant_platform_kit.strategy_lifecycle.drift_detector")
            health_dashboard = types.ModuleType("quant_platform_kit.strategy_lifecycle.health_dashboard")
            drift_detector.run_drift_detection = lambda domain: [types.SimpleNamespace(
                strategy_profile=f"{domain}_profile", drift_score=0.0,
            )]
            def write_dashboard(**kwargs):
                rows = [{"domain": domain, "strategy_profile": f"{domain}_profile", "status": "healthy",
                         "as_of": HEALTH_CYCLE.datetime.now(HEALTH_CYCLE.timezone.utc).date().isoformat()}
                        for domain in DAILY_BRIEFING.DOMAINS if domain != "crypto"]
                Path(kwargs["output_dir"], "strategy_health_dashboard.json").write_text(
                    json.dumps({"strategies": rows}), encoding="utf-8",
                )
            health_dashboard.build_dashboard = write_dashboard
            with (
                mock.patch.dict(os.environ, {"QUANT_MONITOR_ROOT": str(root), "DAY": "2026-09-29"}),
                mock.patch.dict(sys.modules, {
                    "quant_platform_kit": qpk,
                    "quant_platform_kit.strategy_lifecycle": lifecycle,
                    "quant_platform_kit.strategy_lifecycle.drift_detector": drift_detector,
                    "quant_platform_kit.strategy_lifecycle.health_dashboard": health_dashboard,
                }),
            ):
                self.assertEqual(DAILY_BRIEFING.main(), 0)

            report = json.loads((root / "data/daily-reports/2026-09-29/crypto.json").read_text())
            self.assertTrue(report["ok"])
            self.assertEqual(report["data_status"], "not_configured")
            self.assertEqual(report["coverage"]["expected_profiles"], [])
            self.assertEqual(report["errors"], [])

    def test_daily_briefing_preserves_complete_profile_coverage_and_zero_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_as_of = _write_fresh_lifecycle_status(root, profiles_by_domain={
                "us_equity": ["expected_profile"],
            })
            qpk = types.ModuleType("quant_platform_kit")
            lifecycle = types.ModuleType("quant_platform_kit.strategy_lifecycle")
            drift_detector = types.ModuleType("quant_platform_kit.strategy_lifecycle.drift_detector")
            health_dashboard = types.ModuleType("quant_platform_kit.strategy_lifecycle.health_dashboard")
            drift_detector.run_drift_detection = lambda domain: (
                [types.SimpleNamespace(strategy_profile="expected_profile", drift_score=0.0)]
                if domain == "us_equity" else []
            )
            def write_dashboard(**kwargs):
                Path(kwargs["output_dir"], "strategy_health_dashboard.json").write_text(json.dumps({
                    "strategies": [{
                        "domain": "us_equity", "strategy_profile": "expected_profile", "status": "healthy",
                        "as_of": HEALTH_CYCLE.datetime.now(HEALTH_CYCLE.timezone.utc).date().isoformat(),
                    }],
                }), encoding="utf-8")
            health_dashboard.build_dashboard = write_dashboard
            with (
                mock.patch.dict(os.environ, {"QUANT_MONITOR_ROOT": str(root), "DAY": "2026-09-29"}),
                mock.patch.dict(sys.modules, {
                    "quant_platform_kit": qpk,
                    "quant_platform_kit.strategy_lifecycle": lifecycle,
                    "quant_platform_kit.strategy_lifecycle.drift_detector": drift_detector,
                    "quant_platform_kit.strategy_lifecycle.health_dashboard": health_dashboard,
                }),
            ):
                self.assertEqual(DAILY_BRIEFING.main(), 0)

            report = json.loads((root / "data/daily-reports/2026-09-29/us_equity.json").read_text())
            self.assertTrue(report["ok"])
            self.assertEqual(report["data_status"], "ready")
            self.assertEqual(report["as_of"], source_as_of)
            self.assertEqual(report["coverage"]["expected_profiles"], ["expected_profile"])
            self.assertEqual(report["coverage"]["observed_profiles"], ["expected_profile"])
            self.assertEqual(report["strategies"][0]["drift_score"], 0.0)

    def test_daily_briefing_rejects_stale_observation_before_consumer_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fresh_lifecycle_status(root, profiles_by_domain={"us_equity": ["expected_profile"]})
            qpk = types.ModuleType("quant_platform_kit")
            lifecycle = types.ModuleType("quant_platform_kit.strategy_lifecycle")
            drift_detector = types.ModuleType("quant_platform_kit.strategy_lifecycle.drift_detector")
            health_dashboard = types.ModuleType("quant_platform_kit.strategy_lifecycle.health_dashboard")
            drift_detector.run_drift_detection = lambda _domain: [types.SimpleNamespace(
                strategy_profile="expected_profile", drift_score=0.0,
            )]
            health_dashboard.build_dashboard = lambda **kwargs: Path(
                kwargs["output_dir"], "strategy_health_dashboard.json",
            ).write_text(json.dumps({"strategies": [{
                "domain": "us_equity", "strategy_profile": "expected_profile",
                "status": "healthy", "as_of": "2000-01-01",
            }]}), encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"QUANT_MONITOR_ROOT": str(root), "DAY": "2026-09-29"}),
                mock.patch.dict(sys.modules, {
                    "quant_platform_kit": qpk,
                    "quant_platform_kit.strategy_lifecycle": lifecycle,
                    "quant_platform_kit.strategy_lifecycle.drift_detector": drift_detector,
                    "quant_platform_kit.strategy_lifecycle.health_dashboard": health_dashboard,
                }),
            ):
                self.assertEqual(DAILY_BRIEFING.main(), 0)

            report = json.loads((root / "data/daily-reports/2026-09-29/us_equity.json").read_text())
            self.assertFalse(report["ok"])
            self.assertEqual(report["data_status"], "unavailable")
            from service.briefing_consumer import BriefingAction, consume_briefing_report
            findings = consume_briefing_report(report)
            self.assertTrue(findings)
            self.assertEqual(findings[0].level, BriefingAction.TELEGRAM)

    def test_daily_briefing_rejects_stale_and_bad_lifecycle_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "data/lifecycle-artifacts/status.json"
            status_path.parent.mkdir(parents=True)
            valid = {
                "schema_version": "quant_monitor_lifecycle_artifact_status.v1",
                "as_of": "2000-01-01T00:00:00+00:00",
                "domains": {"us_equity": {
                    "status": "ready", "artifact_id": 1, "run_id": 2,
                    "head_sha": "a" * 40, "profiles": ["expected_profile"],
                }},
            }
            for payload in (valid, {**valid, "schema_version": "unknown"}):
                status_path.write_text(json.dumps(payload), encoding="utf-8")
                expected, revisions, _as_of, errors, not_configured = DAILY_BRIEFING._load_expected_coverage(root)
                self.assertEqual(expected, {})
                self.assertEqual(revisions, {})
                self.assertEqual(not_configured, set())
                self.assertTrue(errors)

    def test_delivery_retries_only_failed_target_and_hides_chat_id(self) -> None:
        event = "a" * 64
        calls: list[str] = []

        def send_one(chat_id: str) -> str:
            calls.append(chat_id)
            return "sent" if chat_id == "ok-chat" else "failed"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = HEALTH_CYCLE.deliver_telegram_targets(
                root, event, ("ok-chat", "bad-chat"), send_one,
            )
            self.assertFalse(first["all_sent"])
            self.assertEqual(calls, ["ok-chat", "bad-chat"])
            self.assertIn("telegram_delivery_failed", first["errors"])
            retry: list[str] = []

            def send_retry(chat_id: str) -> str:
                retry.append(chat_id)
                return "sent"

            second = HEALTH_CYCLE.deliver_telegram_targets(
                root, event, ("ok-chat", "bad-chat"), send_retry,
            )
            self.assertEqual(retry, ["bad-chat"])
            self.assertTrue(second["all_sent"])
            raw = (root / "data" / "alert-state" / "health_cycle.json").read_text(encoding="utf-8")
            self.assertNotIn("ok-chat", raw)
            self.assertNotIn("bad-chat", raw)
            state = json.loads(raw)
            self.assertEqual(
                state["operational_diagnosis_attempts"] if "operational_diagnosis_attempts" in state else [],
                [],
            )

    def test_pending_or_timeout_reentry_does_not_send_and_is_not_success(self) -> None:
        event = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls: list[str] = []

            def explode(chat_id: str) -> str:
                calls.append(chat_id)
                raise TimeoutError("timed out")

            first = HEALTH_CYCLE.deliver_telegram_targets(root, event, ("chat-1",), explode)
            self.assertFalse(first["all_sent"])
            self.assertIn("telegram_delivery_unknown", first["errors"])
            self.assertEqual(calls, ["chat-1"])
            second = HEALTH_CYCLE.deliver_telegram_targets(
                root, event, ("chat-1",), lambda chat_id: (_ for _ in ()).throw(AssertionError("resent")),
            )
            self.assertFalse(second["all_sent"])
            self.assertTrue(second["suppressed"] is False)
            self.assertIn("telegram_delivery_unknown", second["errors"])
            target = HEALTH_CYCLE._delivery_target_hash("chat-1")
            state = json.loads((root / "data" / "alert-state" / "health_cycle.json").read_text())
            self.assertEqual(state["deliveries"][event][target]["status"], "unknown")

    def test_concurrent_same_event_sends_once(self) -> None:
        event = "c" * 64
        calls: list[str] = []
        gate = threading.Barrier(2)

        def send_one(chat_id: str) -> str:
            calls.append(chat_id)
            return "sent"

        def run(root: str) -> None:
            gate.wait()
            HEALTH_CYCLE.deliver_telegram_targets(Path(root), event, ("same-chat",), send_one)

        with tempfile.TemporaryDirectory() as tmp:
            threads = [threading.Thread(target=run, args=(tmp,)) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(calls, ["same-chat"])

    def test_state_write_failure_sends_nothing(self) -> None:
        event = "d" * 64
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            HEALTH_CYCLE, "_write_alert_state", side_effect=OSError("disk"),
        ):
            result = HEALTH_CYCLE.deliver_telegram_targets(
                Path(tmp), event, ("chat",), lambda chat_id: (_ for _ in ()).throw(AssertionError("sent")),
            )
        self.assertEqual(result["errors"], ["alert_state_write_failed"])
        self.assertFalse(result["all_sent"])

    def test_malformed_or_missing_root_does_not_count_as_delivered(self) -> None:
        event = "e" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = HEALTH_CYCLE._alert_state_path(root)
            state.parent.mkdir(parents=True)
            state.write_text("not-json", encoding="utf-8")
            bad = HEALTH_CYCLE.deliver_telegram_targets(
                root, event, ("chat",), lambda chat_id: (_ for _ in ()).throw(AssertionError("sent")),
            )
            self.assertEqual(bad["errors"], ["alert_state_unreadable"])
            self.assertFalse(bad["all_sent"])
            missing_root = root / "missing"
            missing = HEALTH_CYCLE.deliver_telegram_targets(
                missing_root, event, ("chat",), lambda chat_id: "sent",
            )
            self.assertEqual(missing["errors"], ["alert_state_root_unavailable"])
            self.assertFalse(missing["all_sent"])
            self.assertFalse(missing_root.exists())

    def test_diagnosis_attempt_creates_consumer_directory_and_keeps_delivery_locked(self) -> None:
        event = "a" * 64
        fingerprint = "ab" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            consumer = root / "data/diagnosis-consumer"
            self.assertFalse(consumer.exists())
            HEALTH_CYCLE._record_operational_diagnosis_attempt(
                consumer, fingerprint, attempt_date="2026-09-28",
            )
            self.assertTrue(consumer.is_dir())
            self.assertEqual(
                HEALTH_CYCLE._load_operational_diagnosis_state(consumer)["operational_diagnosis_attempts"],
                [fingerprint],
            )
            missing_root = root / "missing-monitor"
            result = HEALTH_CYCLE.deliver_telegram_targets(
                missing_root, event, ("chat",),
                lambda chat_id: (_ for _ in ()).throw(AssertionError("sent")),
            )
            self.assertEqual(result["errors"], ["alert_state_root_unavailable"])
            self.assertFalse(result["all_sent"])
            self.assertFalse(missing_root.exists())

    def test_recovery_keeps_unknown_and_diagnosis_but_drops_sent_fingerprint(self) -> None:
        event = "f" * 64
        other = "9" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            HEALTH_CYCLE._record_operational_diagnosis_attempt(
                root, other, attempt_date="2026-09-28",
            )
            kept = "1" * 64
            HEALTH_CYCLE.deliver_telegram_targets(
                root, kept, ("other-chat",), lambda chat_id: "sent",
            )
            HEALTH_CYCLE.deliver_telegram_targets(
                root, event, ("chat",), lambda chat_id: (_ for _ in ()).throw(TimeoutError("lost")),
            )
            target = HEALTH_CYCLE._delivery_target_hash("chat")
            kept_target = HEALTH_CYCLE._delivery_target_hash("other-chat")
            HEALTH_CYCLE._record_alert(root, event)
            HEALTH_CYCLE._clear_alert(root)
            state = json.loads(HEALTH_CYCLE._alert_state_path(root).read_text(encoding="utf-8"))
            self.assertNotIn("fingerprint", state)
            self.assertEqual(state["deliveries"][event][target]["status"], "unknown")
            self.assertEqual(state["deliveries"][kept][kept_target]["status"], "sent")
            self.assertEqual(state["operational_diagnosis_attempts"], [other])

    def test_legacy_fingerprint_suppresses_without_inventing_target_delivery(self) -> None:
        event = HEALTH_CYCLE._alert_fingerprint(["same failure"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            HEALTH_CYCLE._record_alert(root, event)
            result = HEALTH_CYCLE.deliver_telegram_targets(
                root, event, ("new-chat",), lambda chat_id: (_ for _ in ()).throw(AssertionError("sent")),
            )
            self.assertTrue(result["suppressed"])
            self.assertFalse(result["all_sent"])
            state = json.loads(HEALTH_CYCLE._alert_state_path(root).read_text(encoding="utf-8"))
            self.assertNotIn("deliveries", state)
            self.assertEqual(state["fingerprint"], event)

    def _state(self, root: Path) -> dict:
        return json.loads(HEALTH_CYCLE._alert_state_path(root).read_text(encoding="utf-8"))

    def test_diagnosis_and_clear_keep_delivery_written_during_their_read(self) -> None:
        unknown_event = "a" * 64
        failed_event = "b" * 64
        sent_event = "c" * 64
        cleared_event = "d" * 64
        diagnosis = "e" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            HEALTH_CYCLE.deliver_telegram_targets(
                root, unknown_event, ("unknown-chat",),
                lambda chat_id: (_ for _ in ()).throw(TimeoutError("lost")),
            )
            HEALTH_CYCLE.deliver_telegram_targets(
                root, failed_event, ("failed-chat",), lambda chat_id: "failed",
            )
            unknown_target = HEALTH_CYCLE._delivery_target_hash("unknown-chat")
            failed_target = HEALTH_CYCLE._delivery_target_hash("failed-chat")
            sent_target = HEALTH_CYCLE._delivery_target_hash("sent-chat")
            cleared_target = HEALTH_CYCLE._delivery_target_hash("clear-chat")
            original = HEALTH_CYCLE._load_operational_diagnosis_state

            def race(mutator, event_id: str, chat_id: str) -> list[str]:
                loaded = threading.Event()
                release_load = threading.Event()
                saw_pending: list[str] = []
                errors: list[BaseException] = []

                def slow_load(root_path: Path) -> dict:
                    payload = original(root_path)
                    loaded.set()
                    if not release_load.wait(5):
                        raise TimeoutError("load was not released")
                    return payload

                def mutate() -> None:
                    try:
                        with mock.patch.object(
                            HEALTH_CYCLE, "_load_operational_diagnosis_state", slow_load,
                        ):
                            mutator()
                    except BaseException as exc:
                        errors.append(exc)

                def deliver() -> None:
                    try:
                        if not loaded.wait(5):
                            raise TimeoutError("mutation did not load state")

                        def send_one(target: str) -> str:
                            state = self._state(root)
                            status = state["deliveries"][event_id][HEALTH_CYCLE._delivery_target_hash(target)]["status"]
                            saw_pending.append(status)
                            return "sent"

                        HEALTH_CYCLE.deliver_telegram_targets(root, event_id, (chat_id,), send_one)
                    except BaseException as exc:
                        errors.append(exc)

                mutator_thread = threading.Thread(target=mutate)
                delivery_thread = threading.Thread(target=deliver)
                mutator_thread.start()
                self.assertTrue(loaded.wait(5))
                delivery_thread.start()
                delivery_thread.join(1)
                release_load.set()
                mutator_thread.join(5)
                delivery_thread.join(5)
                self.assertFalse(mutator_thread.is_alive())
                self.assertFalse(delivery_thread.is_alive())
                self.assertEqual(errors, [])
                return saw_pending

            pending_during_diagnosis = race(
                lambda: HEALTH_CYCLE._record_operational_diagnosis_attempt(
                    root, diagnosis, attempt_date="2026-09-28",
                ),
                sent_event,
                "sent-chat",
            )
            state = self._state(root)
            self.assertEqual(pending_during_diagnosis, ["pending"])
            self.assertEqual(state["deliveries"][unknown_event][unknown_target]["status"], "unknown")
            self.assertEqual(state["deliveries"][failed_event][failed_target]["status"], "failed")
            self.assertEqual(state["deliveries"][sent_event][sent_target]["status"], "sent")
            self.assertEqual(state["operational_diagnosis_attempts"], [diagnosis])

            pending_during_clear = race(
                lambda: HEALTH_CYCLE._clear_alert(root),
                cleared_event,
                "clear-chat",
            )
            state = self._state(root)
            self.assertEqual(pending_during_clear, ["pending"])
            self.assertEqual(state["deliveries"][unknown_event][unknown_target]["status"], "unknown")
            self.assertEqual(state["deliveries"][failed_event][failed_target]["status"], "failed")
            self.assertEqual(state["deliveries"][sent_event][sent_target]["status"], "sent")
            self.assertEqual(state["deliveries"][cleared_event][cleared_target]["status"], "sent")
            self.assertEqual(state["operational_diagnosis_attempts"], [diagnosis])
            self.assertEqual(state["operational_diagnosis_last_attempt_date"], "2026-09-28")

    def test_recovery_clears_sent_health_events_and_lets_both_recur(self) -> None:
        event_a = "a" * 64
        event_b = "b" * 64
        daily = "c" * 64
        unknown = "d" * 64
        failed = "e" * 64
        diagnosis = "f" * 64
        calls: list[str] = []

        def send(chat_id: str) -> str:
            calls.append(chat_id)
            return "sent"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            HEALTH_CYCLE._record_operational_diagnosis_attempt(
                root, diagnosis, attempt_date="2026-09-28",
            )
            first_a = HEALTH_CYCLE.deliver_telegram_targets(
                root, event_a, ("chat-a",), send, confirm_legacy=True,
            )
            first_b = HEALTH_CYCLE.deliver_telegram_targets(
                root, event_b, ("chat-b",), send, confirm_legacy=True,
            )
            daily_sent = HEALTH_CYCLE.deliver_telegram_targets(
                root, daily, ("chat-daily",), send,
            )
            unknown_result = HEALTH_CYCLE.deliver_telegram_targets(
                root, unknown, ("chat-unknown",),
                lambda chat_id: (_ for _ in ()).throw(TimeoutError("lost")),
                confirm_legacy=True,
            )
            failed_result = HEALTH_CYCLE.deliver_telegram_targets(
                root, failed, ("chat-failed",), lambda chat_id: "failed", confirm_legacy=True,
            )
            self.assertTrue(first_a["all_sent"])
            self.assertTrue(first_b["all_sent"])
            self.assertTrue(daily_sent["all_sent"])
            self.assertIn("telegram_delivery_unknown", unknown_result["errors"])
            self.assertIn("telegram_delivery_failed", failed_result["errors"])
            before = self._state(root)
            self.assertEqual(before["fingerprint"], event_a)
            self.assertEqual(before["health_sent_events"], [event_a, event_b, unknown, failed])

            HEALTH_CYCLE._clear_alert(root)
            cleared = self._state(root)
            self.assertNotIn("fingerprint", cleared)
            self.assertNotIn("health_sent_events", cleared)
            self.assertNotIn(event_a, cleared["deliveries"])
            self.assertNotIn(event_b, cleared["deliveries"])
            self.assertEqual(
                cleared["deliveries"][daily][HEALTH_CYCLE._delivery_target_hash("chat-daily")]["status"],
                "sent",
            )
            self.assertEqual(
                cleared["deliveries"][unknown][HEALTH_CYCLE._delivery_target_hash("chat-unknown")]["status"],
                "unknown",
            )
            self.assertEqual(
                cleared["deliveries"][failed][HEALTH_CYCLE._delivery_target_hash("chat-failed")]["status"],
                "failed",
            )
            self.assertEqual(cleared["operational_diagnosis_attempts"], [diagnosis])

            calls.clear()
            again_a = HEALTH_CYCLE.deliver_telegram_targets(
                root, event_a, ("chat-a",), send, confirm_legacy=True,
            )
            again_b = HEALTH_CYCLE.deliver_telegram_targets(
                root, event_b, ("chat-b",), send, confirm_legacy=True,
            )
            self.assertTrue(again_a["all_sent"])
            self.assertTrue(again_b["all_sent"])
            self.assertEqual(calls, ["chat-a", "chat-b"])
            daily_again = HEALTH_CYCLE.deliver_telegram_targets(
                root, daily, ("chat-daily",),
                lambda chat_id: (_ for _ in ()).throw(AssertionError("daily resent")),
            )
            unknown_again = HEALTH_CYCLE.deliver_telegram_targets(
                root, unknown, ("chat-unknown",),
                lambda chat_id: (_ for _ in ()).throw(AssertionError("unknown resent")),
                confirm_legacy=True,
            )
            self.assertTrue(daily_again["suppressed"])
            self.assertFalse(unknown_again["all_sent"])
            self.assertIn("telegram_delivery_unknown", unknown_again["errors"])

    def test_interrupt_after_sent_keeps_health_ownership_for_later_recurrence(self) -> None:
        event = "a" * 64
        changed = "b" * 64
        daily = "c" * 64
        unknown = "d" * 64
        diagnosis = "e" * 64

        class _Interrupted(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            HEALTH_CYCLE._record_operational_diagnosis_attempt(
                root, diagnosis, attempt_date="2026-09-28",
            )
            HEALTH_CYCLE.deliver_telegram_targets(root, daily, ("chat-daily",), lambda chat_id: "sent")
            HEALTH_CYCLE.deliver_telegram_targets(
                root, unknown, ("chat-unknown",),
                lambda chat_id: (_ for _ in ()).throw(TimeoutError("lost")),
            )
            sent_hash = HEALTH_CYCLE._delivery_target_hash("chat-sent")
            pending_hash = HEALTH_CYCLE._delivery_target_hash("chat-pending")
            observed: dict[str, dict] = {}

            def send_one(chat_id: str) -> str:
                if chat_id == "chat-sent":
                    observed["pending"] = self._state(root)
                    return "sent"
                raise _Interrupted()

            with self.assertRaises(_Interrupted):
                HEALTH_CYCLE.deliver_telegram_targets(
                    root, event, ("chat-sent", "chat-pending"), send_one, confirm_legacy=True,
                )

            self.assertEqual(observed["pending"]["deliveries"][event][sent_hash]["status"], "pending")
            self.assertEqual(observed["pending"]["health_sent_events"], [event])
            self.assertNotIn("fingerprint", observed["pending"])
            interrupted = self._state(root)
            self.assertEqual(interrupted["deliveries"][event][sent_hash]["status"], "sent")
            self.assertEqual(interrupted["deliveries"][event][pending_hash]["status"], "pending")
            self.assertEqual(interrupted["health_sent_events"], [event])
            self.assertNotIn("fingerprint", interrupted)

            HEALTH_CYCLE._clear_alert(root)
            recovered = self._state(root)
            self.assertNotIn(sent_hash, recovered["deliveries"][event])
            self.assertEqual(recovered["deliveries"][event][pending_hash]["status"], "pending")
            self.assertEqual(
                recovered["deliveries"][unknown][HEALTH_CYCLE._delivery_target_hash("chat-unknown")]["status"],
                "unknown",
            )
            self.assertEqual(
                recovered["deliveries"][daily][HEALTH_CYCLE._delivery_target_hash("chat-daily")]["status"],
                "sent",
            )
            self.assertEqual(recovered["operational_diagnosis_attempts"], [diagnosis])
            self.assertNotIn("health_sent_events", recovered)
            self.assertNotIn("fingerprint", recovered)

            calls: list[str] = []

            def send(chat_id: str) -> str:
                calls.append(chat_id)
                return "sent"

            recurred = HEALTH_CYCLE.deliver_telegram_targets(
                root, event, ("chat-sent", "chat-pending"), send, confirm_legacy=True,
            )
            self.assertEqual(calls, ["chat-sent"])
            self.assertFalse(recurred["all_sent"])
            self.assertIn("telegram_delivery_unknown", recurred["errors"])
            held = self._state(root)
            self.assertEqual(held["deliveries"][event][pending_hash]["status"], "unknown")

            calls.clear()
            changed_result = HEALTH_CYCLE.deliver_telegram_targets(
                root, changed, ("chat-changed",), send, confirm_legacy=True,
            )
            self.assertTrue(changed_result["all_sent"])
            self.assertEqual(calls, ["chat-changed"])
            HEALTH_CYCLE.deliver_telegram_targets(
                root, daily, ("chat-daily",),
                lambda chat_id: (_ for _ in ()).throw(AssertionError("daily resent")),
            )
            HEALTH_CYCLE.deliver_telegram_targets(
                root, unknown, ("chat-unknown",),
                lambda chat_id: (_ for _ in ()).throw(AssertionError("unknown resent")),
            )


if __name__ == "__main__":
    unittest.main()
