from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import service.ai_gateway_service as gateway
from service.quota import QuotaManager


class AiGatewayExecuteTelemetryTests(unittest.TestCase):
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
