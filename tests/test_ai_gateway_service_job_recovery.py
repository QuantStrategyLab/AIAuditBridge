from __future__ import annotations

import tempfile
import time
import unittest
from unittest.mock import call, patch

import service.ai_gateway_service as gateway


class AiGatewayJobRecoveryTests(unittest.TestCase):
    def test_restart_marks_orphaned_active_jobs_failed(self) -> None:
        now = time.time()
        jobs = [
            {"job_id": "a" * 24, "status": "queued", "created_at": now, "updated_at": now},
            {"job_id": "b" * 24, "status": "running", "created_at": now, "updated_at": now},
            {"job_id": "c" * 24, "status": "succeeded", "created_at": now, "updated_at": now},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(gateway.os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False):
                for job in jobs:
                    gateway._write_job(job)
                with (
                    patch.object(gateway, "_record_job_automation_run") as record_automation_run,
                    patch.object(gateway, "_audit_log"),
                ):
                    recovered = gateway._recover_orphaned_jobs()

                queued = gateway._read_job("a" * 24)
                running = gateway._read_job("b" * 24)
                completed = gateway._read_job("c" * 24)

        self.assertEqual(recovered, 2)
        self.assertEqual(queued["status"], "failed")
        self.assertEqual(running["status"], "failed")
        self.assertEqual(queued["failure_category"], "service_restart")
        self.assertEqual(running["failure_category"], "service_restart")
        self.assertEqual(completed["status"], "succeeded")
        record_automation_run.assert_has_calls([call(queued), call(running)], any_order=True)
        self.assertEqual(record_automation_run.call_count, 2)

    def test_restart_keeps_ambiguous_dispatch_reservation_pending(self) -> None:
        now = time.time()
        job = {
            "job_id": "d" * 24,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "_budget_reservation_id": "reservation-1",
            "dispatch_state": "pending_uncertain",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(gateway.os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False):
                gateway._write_job(job)
                with (
                    patch.object(gateway, "_record_job_automation_run"),
                    patch.object(gateway, "_audit_log"),
                    patch.object(gateway, "_mark_budget_reservation_uncertain") as mark_uncertain,
                    patch.object(gateway, "_settle_budget_reservation") as settle,
                    patch.object(gateway, "_release_budget_reservation") as release,
                ):
                    self.assertEqual(gateway._recover_orphaned_jobs(), 1)

        mark_uncertain.assert_called_once()
        settle.assert_not_called()
        release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
