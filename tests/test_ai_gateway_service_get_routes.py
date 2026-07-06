import json
import hashlib
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from service.adapters.llm_adapter import LlmResult
from service.ai_gateway_service import (
    AiGatewayRequestHandler,
    _assert_automation_run_access,
    _assert_source_repository_owner_or_operator,
    _automation_run_access_allowed,
    _automation_run_owner_repository,
    _automation_snapshot_for_claims,
    service_failure_category,
)
from service.automation_run_ledger import get_automation_run_ledger

LIVE_EQUIVALENT_EVIDENCE = {
    "change_class": "live_equivalent_optimization",
    "baseline_profile_runtime_enabled": True,
    "strategy_family_unchanged": True,
    "public_contract_unchanged": True,
    "broker_permission_unchanged": True,
    "risk_limits_not_increased": True,
    "backtest_passed": True,
    "shadow_or_regression_passed": True,
    "rollback_ready": True,
}

STRATEGY_AUTOMATION_REGISTRY = {
    "schema_version": "strategy_automation_registry.v1",
    "summary": {"strategy_profile_count": 1},
    "profiles": [
        {
            "profile": "live",
            "domain": "us_equity",
            "lifecycle_stage": "runtime_enabled",
            "automation_lane": "live_equivalent_optimization",
            "max_autonomy": "auto_pr_or_trusted_live_equivalent",
            "approval_required": False,
            "can_switch_live": True,
            "position_control_sensitive": False,
        }
    ],
}


class AiGatewayGetRoutesTest(unittest.TestCase):
    def test_automation_snapshot_filters_to_calling_repository(self) -> None:
        snapshot = {
            "runs": [
                {
                    "run_id": "run-a",
                    "task_state": "running",
                    "suggested_action": "continue",
                    "metadata": {"repository": "QuantStrategyLab/AIAuditBridge"},
                },
                {
                    "run_id": "run-b",
                    "task_state": "merged",
                    "suggested_action": "continue",
                    "metadata": {
                        "repository": "QuantStrategyLab/Orchestrator",
                        "source_repository": "QuantStrategyLab/OtherRepo",
                        "caller_repository": "QuantStrategyLab/AIAuditBridge",
                    },
                },
            ],
            "summary": {"retention": {"events_included": False}},
        }

        filtered = _automation_snapshot_for_claims(
            snapshot,
            {"repository": "QuantStrategyLab/AIAuditBridge", "auth_method": "github_oidc"},
        )

        self.assertEqual([run["run_id"] for run in filtered["runs"]], ["run-a"])
        self.assertEqual(filtered["summary"]["total_runs"], 1)
        self.assertEqual(filtered["summary"]["active_runs"], 1)

    def test_automation_snapshot_applies_limit_after_repository_filter(self) -> None:
        snapshot = {
            "runs": [
                {
                    "run_id": "other-newer",
                    "task_state": "running",
                    "metadata": {"repository": "QuantStrategyLab/OtherRepo"},
                },
                {
                    "run_id": "mine",
                    "task_state": "running",
                    "metadata": {"repository": "QuantStrategyLab/AIAuditBridge"},
                },
            ],
            "summary": {"retention": {"events_included": False}},
        }

        filtered = _automation_snapshot_for_claims(
            snapshot,
            {"repository": "QuantStrategyLab/AIAuditBridge", "auth_method": "github_oidc"},
            limit=1,
        )

        self.assertEqual([run["run_id"] for run in filtered["runs"]], ["mine"])
        self.assertEqual(filtered["summary"]["total_runs"], 1)
        self.assertEqual(filtered["summary"]["returned_runs"], 1)

    def test_automation_run_owner_prefers_source_repository(self) -> None:
        record = {
            "run_id": "run-a",
            "metadata": {
                "repository": "QuantStrategyLab/Orchestrator",
                "source_repository": "QuantStrategyLab/TargetRepo",
            },
        }

        self.assertEqual(_automation_run_owner_repository(record), "QuantStrategyLab/TargetRepo")
        _assert_automation_run_access(
            record,
            {"repository": "QuantStrategyLab/TargetRepo", "auth_method": "github_oidc"},
        )

    def test_automation_run_access_rejects_cross_repository_update(self) -> None:
        record = {"run_id": "run-a", "metadata": {"repository": "QuantStrategyLab/AIAuditBridge"}}

        with self.assertRaises(PermissionError):
            _assert_automation_run_access(
                record,
                {"repository": "QuantStrategyLab/OtherRepo", "auth_method": "github_oidc"},
            )

    def test_automation_run_access_rejects_original_caller_repository_access(self) -> None:
        record = {
            "run_id": "run-a",
            "metadata": {
                "repository": "QuantStrategyLab/TargetRepo",
                "source_repository": "QuantStrategyLab/TargetRepo",
                "caller_repository": "QuantStrategyLab/Orchestrator",
            },
        }

        with self.assertRaises(PermissionError):
            _assert_automation_run_access(
                record,
                {"repository": "QuantStrategyLab/Orchestrator", "auth_method": "github_oidc"},
            )

    def test_automation_run_access_rejects_unrelated_same_org_update(self) -> None:
        record = {
            "run_id": "run-a",
            "metadata": {
                "repository": "QuantStrategyLab/TargetRepo",
                "source_repository": "QuantStrategyLab/TargetRepo",
                "caller_repository": "QuantStrategyLab/Orchestrator",
            },
        }

        with self.assertRaises(PermissionError):
            _assert_automation_run_access(
                record,
                {"repository": "QuantStrategyLab/OtherRepo", "auth_method": "github_oidc"},
            )

    def test_static_token_dashboard_can_read_allowlisted_repository_runs(self) -> None:
        record = {
            "run_id": "run-a",
            "metadata": {
                "repository": "QuantStrategyLab/TargetRepo",
                "source_repository": "QuantStrategyLab/TargetRepo",
            },
        }
        claims = {"repository": "dashboard", "auth_method": "static_token"}

        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_DASHBOARD_REPOSITORIES": "QuantStrategyLab/TargetRepo"}):
            self.assertTrue(_automation_run_access_allowed(record, claims))
            filtered = _automation_snapshot_for_claims({"runs": [record], "summary": {}}, claims)

        self.assertEqual([run["run_id"] for run in filtered["runs"]], ["run-a"])

    def test_automation_run_update_rejects_owner_mismatch_even_for_operator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    first_payload = {
                        "run_id": "shared-run-id",
                        "task_state": "running",
                        "source_repository": "local/repo-a",
                    }
                    first_request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/runs",
                        data=json.dumps(first_payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(first_request, timeout=5) as response:
                        self.assertEqual(response.status, 200)

                    second_payload = {
                        "run_id": "shared-run-id",
                        "task_state": "running",
                        "source_repository": "local/repo-b",
                    }
                    second_request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/runs",
                        data=json.dumps(second_payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(second_request, timeout=5)
                    self.assertEqual(ctx.exception.code, 401)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_run_record_rejects_blank_run_id_as_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/runs",
                        data=json.dumps({"task_state": "running"}).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(ctx.exception.code, 400)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_external_automation_run_update_cannot_overwrite_service_job_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                get_automation_run_ledger().record(
                    "service-run",
                    "running",
                    metadata={
                        "origin": "service_job",
                        "repository": "local/repo",
                        "source_repository": "local/repo",
                    },
                    owner_repository="local/repo",
                )
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/runs",
                        data=json.dumps(
                            {
                                "run_id": "service-run",
                                "task_state": "merged",
                                "source_repository": "local/repo",
                            }
                        ).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(ctx.exception.code, 401)
                    self.assertEqual(get_automation_run_ledger().get("service-run")["task_state"], "running")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_operator_can_use_real_source_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
                "CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES": "QuantStrategyLab/TargetRepo",
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    run_request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/runs",
                        data=json.dumps(
                            {
                                "run_id": "operator-run",
                                "task_state": "running",
                                "source_repository": "QuantStrategyLab/TargetRepo",
                            }
                        ).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(run_request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        run_body = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(
                        run_body["run"]["metadata"]["source_repository"],
                        "QuantStrategyLab/TargetRepo",
                    )

                    authority_request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(
                            {
                                "source_repository": "QuantStrategyLab/TargetRepo",
                                "changed_paths": ["docs/runbook.md"],
                                "proposed_action": "auto_pr",
                            }
                        ).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(authority_request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_static_token_dashboard_can_query_allowlisted_control_repo(self) -> None:
        token = "t" * 40
        env = {
            "CODEX_AUDIT_SERVICE_AUTH": "github-oidc",
            "CODEX_AUDIT_SERVICE_TOKEN": token,
            "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "dashboard",
            "CODEX_AUDIT_SERVICE_DASHBOARD_REPOSITORIES": "QuantStrategyLab/TargetRepo",
        }
        with patch.dict(os.environ, env, clear=False):
            server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                request = urllib.request.Request(
                    f"{base_url}/v1/ai/automation/control?repo=QuantStrategyLab/TargetRepo",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    control = json.loads(response.read().decode("utf-8"))["control"]
                self.assertIn("execution", control)
                self.assertEqual(control["execution"]["repo"], "QuantStrategyLab/TargetRepo")
                self.assertEqual(control["execution"]["requested_mode"], "review_and_fix")
                self.assertEqual(control["execution"]["effective_mode"], "review_only")
                self.assertFalse(control["execution"]["auto_fix_allowed"])

                for legacy_mode, expected_mode in {"manual": "review_only", "auto_pr": "review_and_fix", "auto_merge": "review_and_fix"}.items():
                    legacy_mode_request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/control?repo=QuantStrategyLab/TargetRepo&mode={legacy_mode}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    with urllib.request.urlopen(legacy_mode_request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        legacy_control = json.loads(response.read().decode("utf-8"))["control"]
                    self.assertEqual(legacy_control["execution"]["requested_mode"], expected_mode)

                invalid_mode_request = urllib.request.Request(
                    f"{base_url}/v1/ai/automation/control?repo=QuantStrategyLab/TargetRepo&mode=bad",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(invalid_mode_request, timeout=5)
                self.assertEqual(ctx.exception.code, 400)

                missing_repo_request = urllib.request.Request(
                    f"{base_url}/v1/ai/automation/control",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(missing_repo_request, timeout=5)
                self.assertEqual(ctx.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()

    def test_source_repository_write_requires_owner_or_operator(self) -> None:
        _assert_source_repository_owner_or_operator(
            {"repository": "QuantStrategyLab/TargetRepo", "auth_method": "github_oidc"},
            "QuantStrategyLab/TargetRepo",
        )
        _assert_source_repository_owner_or_operator(
            {"repository": "QuantStrategyLab/Orchestrator", "auth_method": "static_token", "automation_operator": True},
            "QuantStrategyLab/TargetRepo",
        )
        with self.assertRaises(PermissionError):
            _assert_source_repository_owner_or_operator(
                {"repository": "QuantStrategyLab/Orchestrator", "auth_method": "github_oidc"},
                "QuantStrategyLab/TargetRepo",
            )
        with self.assertRaises(PermissionError):
            _assert_source_repository_owner_or_operator(
                {"repository": "dashboard", "auth_method": "static_token"},
                "QuantStrategyLab/TargetRepo",
            )

    def test_read_routes_accept_query_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    effectiveness_url = f"{base_url}/v1/ai/changes/effectiveness?days=90"
                    with urllib.request.urlopen(effectiveness_url, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        effectiveness = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(effectiveness["status"], "ok")
                    self.assertIn("report", effectiveness)

                    quota_url = f"{base_url}/v1/ai/quota?repo=QuantStrategyLab/AIAuditBridge"
                    with urllib.request.urlopen(quota_url, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        quota = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(quota["status"], "ok")
                    self.assertEqual(quota["quota"]["repo"], "QuantStrategyLab/AIAuditBridge")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_runs_rejects_invalid_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(f"{base_url}/v1/ai/automation/runs?limit=abc", timeout=5)
                    self.assertEqual(ctx.exception.code, 400)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_control_rejects_cross_repository_query_for_oidc_callers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "service.ai_gateway_service.authenticate",
                return_value={"repository": "QuantStrategyLab/AIAuditBridge", "auth_method": "github_oidc"},
            ):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(
                            f"{base_url}/v1/ai/automation/control?repo=QuantStrategyLab/OtherRepo",
                            timeout=5,
                        )
                    self.assertEqual(ctx.exception.code, 401)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_feedback_register_uses_source_repository_for_change_feed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "source_repository": "local/repo",
                        "task": "monthly_snapshot_audit",
                        "action": "auto_pr",
                        "risk": "low",
                        "changed_paths": ["docs/runbook.md"],
                        "external_url": "https://example.test/pr/12",
                        "issue_number": 7,
                        "pr_number": 12,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/feedback/register",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)

                    with urllib.request.urlopen(f"{base_url}/v1/ai/changes?days=7", timeout=5) as response:
                        changes = json.loads(response.read().decode("utf-8"))["changes"]
                    self.assertEqual(changes[0]["repo"], "local/repo")
                    self.assertEqual(changes[0]["external_url"], "https://example.test/pr/12")
                    self.assertEqual(changes[0]["pr_number"], 12)
                    self.assertEqual(changes[0]["state"], "waiting_for_ci")
                    self.assertFalse(changes[0]["human_review_required"])
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_routes_record_and_return_run_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "run_id": "platform-health-run-1",
                        "task_state": "running",
                        "task": "platform-health",
                        "source_repository": "local/repo",
                        "suggested_action": "auto_merge",
                        "service_health": "healthy",
                        "quota_status": "ok",
                        "org_health_status": "ok",
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/runs",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        recorded = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(recorded["run"]["run_id"], "platform-health-run-1")
                    self.assertEqual(recorded["run"]["suggested_action"], recorded["control"]["action"])
                    self.assertEqual(recorded["control"]["execution"]["requested_mode"], "review_and_fix")
                    self.assertEqual(recorded["run"]["service_health"], recorded["control"]["service_health"])
                    self.assertEqual(recorded["run"]["quota_status"], recorded["control"]["quota_status"])
                    self.assertEqual(recorded["run"]["org_health_status"], recorded["control"]["org_health_status"])

                    invalid_mode_payload = {**payload, "run_id": "platform-health-run-invalid", "mode": "bad"}
                    invalid_mode_request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/runs",
                        data=json.dumps(invalid_mode_payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(invalid_mode_request, timeout=5)
                    self.assertEqual(ctx.exception.code, 400)

                    with urllib.request.urlopen(f"{base_url}/v1/ai/automation/runs?include_events=true", timeout=5) as response:
                        ledger = json.loads(response.read().decode("utf-8"))["ledger"]
                    self.assertEqual(ledger["summary"]["total_runs"], 1)
                    self.assertEqual(ledger["runs"][0]["task_name"], "platform-health")
                    self.assertIn("events", ledger["runs"][0])

                    with urllib.request.urlopen(f"{base_url}/v1/ai/automation/runs/platform-health-run-1", timeout=5) as response:
                        fetched = json.loads(response.read().decode("utf-8"))["run"]
                    self.assertEqual(fetched["task_state"], "running")

                    with urllib.request.urlopen(f"{base_url}/v1/ai/automation/control?repo=local/repo", timeout=5) as response:
                        control = json.loads(response.read().decode("utf-8"))["control"]
                    self.assertIn(control["action"], {"continue", "review_only", "pause_auto_fix", "escalate"})
                    self.assertIn("execution", control)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_triage_reports_retryable_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = os.path.join(tmp, "execution_policy.json")
            with open(policy_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "default": {
                            "max_autonomy": "auto_pr",
                            "max_consecutive_failures": 3,
                            "low_cost_model": "gpt-5.4-mini",
                            "low_cost_provider": "openai",
                        },
                        "repositories": {},
                    },
                    handle,
                )
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH": policy_path,
                "CODEX_AUDIT_SERVICE_EXECUTION_POLICY_OWNER": f"{os.getuid()}:{os.getgid()}",
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "source_repository": "local/repo",
                        "task": "runtime-health",
                        "failure_category": "transient_service_failure",
                        "error": "codex service timed out waiting for a background job",
                        "changed_paths": ["docs/runbook.md"],
                        "run_id": "incident-123",
                        "mode": "manual",
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/triage",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        body = json.loads(response.read().decode("utf-8"))

                    triage = body["triage"]
                    self.assertEqual(triage["failure_category"], "transient_service_failure")
                    self.assertEqual(triage["incident_class"], "retryable")
                    self.assertTrue(triage["retry_allowed"])
                    self.assertEqual(triage["recommended_action"], "retry")
                    self.assertEqual(triage["file_risk"], "low")
                    self.assertEqual(triage["control"]["execution"]["action"], "human_review")
                    self.assertIn("run_id=incident-123", triage["summary"])
                finally:
                    server.shutdown()
                    server.server_close()

    def test_service_failure_category_treats_token_rate_limits_as_quota(self) -> None:
        self.assertEqual(
            service_failure_category("OpenAI token-per-minute rate limit exceeded"),
            "quota_or_capacity_failure",
        )

    def test_automation_authority_route_does_not_trust_live_equivalent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": [r"./SRC//US_EQUITY_STRATEGIES\\TQQQ.py"],
                        "proposed_action": "auto_merge",
                        "automation_metadata": {
                            "change_class": "live_equivalent_optimization",
                            **LIVE_EQUIVALENT_EVIDENCE,
                        },
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertEqual(authority["final_action"], "escalate")
                    self.assertTrue(authority["human_review_required"])
                    self.assertTrue(authority["strategy_registry_context"]["profile_required"])
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_rejects_non_repo_relative_changed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(
                            {
                                "changed_paths": ["../src/us_equity_strategies/tqqq.py"],
                                "proposed_action": "auto_merge",
                            }
                        ).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(ctx.exception.code, 400)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_does_not_trust_strategy_registry_from_non_operator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["src/us_equity_strategies/tqqq.py"],
                        "proposed_action": "auto_merge",
                        "strategy_profile": "live",
                        "strategy_automation_registry": STRATEGY_AUTOMATION_REGISTRY,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertEqual(authority["change_class"], "unknown_change")
                    self.assertEqual(authority["strategy_registry_context"]["automation_lane"], "live_equivalent_optimization")
                    self.assertNotEqual(authority["final_action"], "auto_merge")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_does_not_trust_operator_request_body_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["docs/live-optimization.md"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "strategy_profile": "live",
                        "strategy_automation_registry": STRATEGY_AUTOMATION_REGISTRY,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertEqual(authority["change_class"], "routine_low_risk")
                    self.assertEqual(authority["final_action"], "auto_pr")
                    self.assertEqual(authority["strategy_registry_context"]["automation_lane"], "live_equivalent_optimization")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_fails_closed_for_profile_without_explicit_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["docs/live-optimization.md"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "strategy_profile": "live",
                        "platform_health_report": {"automation_registry": STRATEGY_AUTOMATION_REGISTRY},
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertEqual(authority["final_action"], "escalate")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_fails_closed_when_explicit_registry_invalid_even_with_platform_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["docs/live-optimization.md"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "strategy_profile": "live",
                        "strategy_automation_registry": {"schema_version": "stale"},
                        "platform_health_report": {"automation_registry": STRATEGY_AUTOMATION_REGISTRY},
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertEqual(authority["final_action"], "escalate")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_fails_closed_for_registry_without_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["docs/readme.md"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "strategy_automation_registry": STRATEGY_AUTOMATION_REGISTRY,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertTrue(authority["strategy_registry_context"]["profile_required"])
                    self.assertEqual(authority["final_action"], "escalate")
                    self.assertEqual(authority["policy_guard_action"], "escalate")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_fails_closed_for_malformed_registry_without_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["docs/readme.md"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "strategy_automation_registry": {"schema_version": "stale"},
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertTrue(authority["strategy_registry_context"]["profile_required"])
                    self.assertEqual(authority["final_action"], "escalate")
                    self.assertEqual(authority["policy_guard_action"], "escalate")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_fails_closed_for_profile_required_class_without_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["src/quant_strategy_plugins/plugin_policies.py"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertTrue(authority["strategy_registry_context"]["profile_required"])
                    self.assertEqual(authority["final_action"], "escalate")
                    self.assertEqual(authority["policy_guard_action"], "escalate")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_fails_closed_for_existing_high_risk_strategy_path_without_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["src/quant_strategy.py"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertTrue(authority["strategy_registry_context"]["profile_required"])
                    self.assertEqual(authority["final_action"], "escalate")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_does_not_require_profile_for_strategy_profile_example_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["web/strategy-switch-console/strategy-profiles.example.json"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertEqual(authority["change_class"], "live_candidate_promotion")
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertFalse(authority["strategy_registry_context"].get("profile_required", False))
                    self.assertEqual(authority["final_action"], "auto_pr")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_requires_profile_for_example_asset_with_protected_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["web/strategy-switch-console/strategy-profiles.example.json"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "automation_metadata": {"change_class": "plugin_position_control"},
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertEqual(authority["change_class"], "plugin_position_control")
                    self.assertTrue(authority["strategy_registry_context"]["profile_required"])
                    self.assertEqual(authority["final_action"], "escalate")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_requires_profile_for_non_example_promotion_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["src/candidate_strategy.py"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "automation_metadata": {"change_class": "live_candidate_promotion"},
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertEqual(authority["change_class"], "live_candidate_promotion")
                    self.assertTrue(authority["strategy_registry_context"]["profile_required"])
                    self.assertEqual(authority["final_action"], "escalate")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_ignores_metadata_strategy_profile_without_top_level_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["docs/readme.md"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "automation_metadata": {"strategy_profile": "live"},
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertFalse(authority["strategy_registry_context"].get("profile_required", False))
                    self.assertEqual(authority["final_action"], "auto_pr")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_does_not_require_profile_for_docs_strategy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["docs/strategy-notes.md"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertFalse(authority["strategy_registry_context"].get("profile_required", False))
                    self.assertEqual(authority["final_action"], "auto_pr")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_automation_authority_route_ignores_platform_health_registry_without_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
            }
            with patch.dict(os.environ, env, clear=False), patch("service.ai_gateway_service.read_org_health", return_value={"status": "healthy"}):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "changed_paths": ["docs/readme.md"],
                        "proposed_action": "auto_merge",
                        "confidence": 0.99,
                        "platform_health_report": {"automation_registry": STRATEGY_AUTOMATION_REGISTRY},
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/automation/authority",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authority = json.loads(response.read().decode("utf-8"))["automation_authority"]
                    self.assertFalse(authority["strategy_registry_context"]["valid"])
                    self.assertEqual(authority["final_action"], "auto_pr")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_review_route_uses_service_owned_trusted_live_equivalent_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = "review trusted live equivalent optimization"
            proof_path = os.path.join(tmp, "trusted-proof.json")
            with open(proof_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "proofs": [
                            {
                                "proof_id": "proof-1",
                                "source_repository": "QuantStrategyLab/AIAuditBridge",
                                "commit_sha": "abc123def456",
                                "diff_hash": "diffhash123",
                                "base_ref": "main",
                                "base_sha": "base123",
                                "pull_request_number": "21",
                                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                                "changed_paths": ["src/quant_strategy.py"],
                                "trusted_automation_metadata": LIVE_EQUIVALENT_EVIDENCE,
                            }
                        ]
                    },
                    handle,
                )
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                "CODEX_AUDIT_SERVICE_TRUSTED_AUTOMATION_PROOF_PATH": proof_path,
                "CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES": "local",
                "CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "service.ai_gateway_service.LlmAdapter.parallel_review",
                return_value=[
                    LlmResult(
                        provider="claude",
                        model="claude-sonnet-4-6",
                        output='{"verdict":"approve","confidence":0.99,"summary":"ok"}',
                    )
                ],
            ), patch(
                "service.ai_gateway_service.read_org_health",
                return_value={"status": "ok"},
            ):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "prompt": prompt,
                        "reviewers": ["claude"],
                        "verifier": None,
                        "source_repository": "QuantStrategyLab/AIAuditBridge",
                        "trusted_proof_id": "proof-1",
                        "commit_sha": "abc123def456",
                        "diff_hash": "diffhash123",
                        "base_ref": "main",
                        "base_sha": "base123",
                        "pull_request_number": "21",
                        "changed_paths": ["src/quant_strategy.py"],
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/review",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        body = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(body["recommended_action"]["action"], "auto_pr")
                    self.assertTrue(body["recommended_action"]["human_review_required"])

                    missing_source_payload = dict(payload)
                    missing_source_payload.pop("source_repository")
                    missing_source_request = urllib.request.Request(
                        f"{base_url}/v1/ai/review",
                        data=json.dumps(missing_source_payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(missing_source_request, timeout=5)
                    self.assertEqual(ctx.exception.code, 401)
                finally:
                    server.shutdown()
                    server.server_close()
