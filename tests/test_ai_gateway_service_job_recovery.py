from __future__ import annotations

import tempfile
import time
import unittest
from unittest.mock import patch

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
        record_automation_run.assert_called_once_with(running)


if __name__ == "__main__":
    unittest.main()
