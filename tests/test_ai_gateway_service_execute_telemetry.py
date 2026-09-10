from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import service.ai_gateway_service as gateway
from service.quota import QuotaManager


class AiGatewayExecuteTelemetryTests(unittest.TestCase):
    def test_diagnosis_ledger_exposes_only_bounded_success_summary(self):
        base = {"job_id": "a" * 32, "task": "operational_data_diagnosis",
                "source_repository": "QuantStrategyLab/AIAuditBridge", "mode": "review_only",
                "provider": "codex", "research_stage": "drift_analysis",
                "status": "succeeded", "output": "建议" * 400, "error": "private-error"}
        cases = [({}, True), ({"status": "failed"}, False), ({"status": "running"}, False),
                 ({"status": "surprise"}, False), ({"output": None}, False),
                 ({"task": "execute"}, False), ({"provider": "cursor"}, False),
                 ({"source_repository": "Other/repo"}, False), ({"mode": "review_and_fix"}, False),
                 ({"research_stage": "optimization"}, False)]
        for change, visible in cases:
            with self.subTest(change=change), patch.object(gateway, "_automation_control_snapshot", return_value={}), patch.object(gateway, "get_automation_run_ledger") as ledger:
                gateway._record_job_automation_run({**base, **change})
                metadata = ledger.return_value.record.call_args.kwargs["metadata"]
                self.assertEqual("diagnosis_summary" in metadata, visible)
                self.assertNotIn("private-error", repr(metadata))
                if visible:
                    self.assertEqual(metadata["diagnosis_summary"], base["output"][:500])
                    self.assertEqual(metadata["diagnosis_status"], "succeeded")
                if change.get("status") == "surprise":
                    self.assertEqual(metadata["diagnosis_status"], "unknown")

    def test_research_refreshes_partial_or_stale_dashboard_snapshot_once(self):
        for updated, models in ((1000, None), (819, [{"model": "gpt-5.6-sol"}])):
            quota = QuotaManager()
            quota._codex_account_cache = {"status": "available", "updated_at": updated, "available_models": models}
            quota._codex_account_cache_ts = updated
            quota._codex_account_attempt_ts = updated
            fresh = {"status": "available", "updated_at": 1000, "available_models": [{"model": "gpt-5.6-sol"}]}
            with self.subTest(updated=updated), patch.dict(gateway.os.environ, {"CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_CACHE_SECONDS": "3600"}), patch(
                "service.quota.time.time", return_value=1000
            ), patch("service.quota.read_codex_rate_limits", return_value=fresh) as read:
                self.assertEqual(quota._codex_account_snapshot(require_models=True), fresh)
                self.assertEqual(quota._codex_account_snapshot(require_models=True), fresh)
                read.assert_called_once()

    def test_research_routes_select_supported_model_and_defer_before_provider(self) -> None:
        account = {"status": "available", "updated_at": 1000,
            "rate_limits": {"primary": {"used_percent": 10, "window_duration_mins": 10080, "resets_at": 9000}},
            "available_models": [{"model": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]}]}
        for method in ("_handle_execute_async", "_handle_execute_sync"):
            for used in (10, 65):
                quota = QuotaManager()
                account["rate_limits"]["primary"]["used_percent"] = used
                payload = {"prompt": "synthetic", "mode": "review_only", "research_stage": "optimization"}
                with (
                    self.subTest(method=method, used=used),
                    patch.dict(gateway.os.environ, {}, clear=True),
                    patch.object(gateway.time, "time", return_value=1000),
                    patch.object(quota, "_codex_account_snapshot", return_value=account),
                    patch.object(quota, "record_execute") as record,
                    patch.object(gateway, "get_quota_manager", return_value=quota),
                    patch.object(gateway, "get_health_monitor"),
                    patch.object(gateway, "_json_response") as response,
                    patch.object(gateway, "_submit_job", return_value={"job_id": "synthetic"}) as submit,
                    patch.object(gateway, "CodexAdapter") as adapter,
                ):
                    adapter.return_value.execute.return_value = SimpleNamespace(success=True, output="synthetic", error="")
                    getattr(gateway.AiGatewayRequestHandler, method)(object(), {"repository": "Synthetic/caller"}, payload)
                    if used == 65:
                        self.assertEqual(response.call_args.args[1], 429)
                        self.assertEqual(response.call_args.args[2]["status"], "deferred")
                        self.assertEqual(response.call_args.args[2]["retry_at"], 9000)
                        submit.assert_not_called()
                        adapter.assert_not_called()
                        record.assert_not_called()
                    else:
                        self.assertIn(response.call_args.args[1], (200, 202))
                        selected = submit.call_args.args[1] if method.endswith("async") else adapter.return_value.execute.call_args.kwargs
                        self.assertEqual(selected["model"], "gpt-5.6-sol")
                        self.assertEqual(selected["reasoning_effort"], "high")
                        record.assert_called_once()

    def test_exhausted_subscription_rejects_both_execute_routes_before_submission(self) -> None:
        quota = QuotaManager()
        account = {"status": "available", "rate_limits": {"primary": {"used_percent": 100}}}
        for method in ("_handle_execute_async", "_handle_execute_sync"):
            with (
                self.subTest(method=method),
                patch.object(quota, "_codex_account_snapshot", return_value=account),
                patch.object(gateway, "get_quota_manager", return_value=quota),
                patch.object(gateway, "_json_response") as response,
                patch.object(gateway, "_submit_job") as submit,
                patch.object(gateway, "CodexAdapter") as adapter,
            ):
                getattr(gateway.AiGatewayRequestHandler, method)(
                    object(), {"repository": "Synthetic/caller"}, {"prompt": "synthetic", "mode": "review_only"},
                )
                self.assertEqual(response.call_args.args[1], 429)
                submit.assert_not_called()
                adapter.assert_not_called()
                self.assertNotIn("Synthetic/caller", quota._records)

    def test_failed_codex_diagnostics_never_reach_job_or_telemetry(self) -> None:
        marker = "synthetic-private-diagnostic"
        failures = [
            (SimpleNamespace(returncode=1, stdout=marker, stderr="quota exceeded " + marker), None, "quota_or_capacity_failure"),
            (None, subprocess.TimeoutExpired([marker], 1), "transient_service_failure"),
            (None, FileNotFoundError(marker), "unknown_failure"),
        ]
        for completed, error, category in failures:
            job = {"job_id": "synthetic", "status": "queued", "task": "execute"}
            writes = []
            with (
                self.subTest(category=category),
                patch("service.adapters.codex_adapter._codex_command", return_value=["synthetic"]),
                patch("service.adapters.codex_adapter.subprocess.run", return_value=completed, side_effect=error),
                patch.dict(gateway.os.environ, {"CODEX_AUDIT_SERVICE_ENV": "production"}),
                patch.object(gateway, "_read_job", return_value=job),
                patch.object(gateway, "_write_job", side_effect=lambda payload: writes.append(dict(payload))),
                patch.object(gateway, "_record_job_automation_run"),
                patch.object(gateway, "_audit_log"),
                patch.object(gateway, "get_health_monitor"),
                patch.object(gateway, "_record_platform_execution_telemetry") as telemetry,
            ):
                gateway._run_job("synthetic", {"prompt": "synthetic", "task": "execute"})
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["failure_category"], category)
            self.assertNotIn(marker, repr(writes))
            self.assertNotIn(marker, repr(telemetry.call_args))

    @patch("service.ai_gateway_service.try_record_platform_execution")
    @patch("service.ai_gateway_service.get_health_monitor")
    @patch("service.ai_gateway_service._record_job_automation_run")
    @patch("service.ai_gateway_service._audit_log")
    @patch("service.ai_gateway_service.CodexAdapter.execute")
    def test_run_job_records_execution_telemetry(
        self,
        mock_execute,
        _mock_audit_log,
        _mock_record_job_automation_run,
        mock_health_monitor,
        mock_try_record,
    ) -> None:
        job = {
            "job_id": "job-1",
            "status": "queued",
            "task": "execute",
            "source_repository": "QuantStrategyLab/CnEquityStrategies",
            "repository": "QuantStrategyLab/CnEquityStrategies",
            "domain": "cn_equity",
        }
        writes: list[dict[str, object]] = []

        def _read_job(_job_id: str) -> dict[str, object]:
            return job

        def _write_job(payload: dict[str, object]) -> None:
            writes.append(dict(payload))

        mock_execute.return_value = SimpleNamespace(success=True, output="done", error="")
        health = mock_health_monitor.return_value
        health.record.return_value = None

        with (
            patch.object(gateway, "_read_job", side_effect=_read_job),
            patch.object(gateway, "_write_job", side_effect=_write_job),
            patch.object(gateway, "_classify_failure", return_value=""),
            patch.dict(gateway.os.environ, {"CODEX_AUDIT_SERVICE_DEDUPE_JOBS": "true"}, clear=False),
        ):
            gateway._run_job("job-1", {"prompt": "hello", "task": "execute", "model": "gpt-5.4-mini"})

        self.assertGreaterEqual(len(writes), 2)
        mock_try_record.assert_called()
        profile_id, execution_result = mock_try_record.call_args.args[:2]
        call_domain = mock_try_record.call_args.kwargs["domain"]
        self.assertEqual(profile_id, "execute")
        self.assertEqual(execution_result["status"], "succeeded")
        self.assertEqual(execution_result["model"], "gpt-5.4-mini")
        self.assertEqual(call_domain, "cn_equity")


if __name__ == "__main__":
    unittest.main()
