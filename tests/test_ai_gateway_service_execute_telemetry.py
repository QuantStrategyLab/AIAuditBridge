from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import service.ai_gateway_service as gateway


class AiGatewayExecuteTelemetryTests(unittest.TestCase):
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

        mock_execute.return_value = SimpleNamespace(
            success=True,
            output="done",
            error="",
            dispatch_started=True,
            dispatch_uncertain=False,
        )
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
        self.assertTrue(writes[-1]["dispatch_started"])
        self.assertFalse(writes[-1]["dispatch_uncertain"])
        self.assertEqual(writes[-1]["dispatch_state"], "dispatched")


if __name__ == "__main__":
    unittest.main()
