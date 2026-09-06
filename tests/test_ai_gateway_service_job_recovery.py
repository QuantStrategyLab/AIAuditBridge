from __future__ import annotations

import tempfile
from pathlib import Path
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



class AiGatewayJobAdmissionTests(unittest.TestCase):
    def test_concurrent_submit_dedupes_under_admission_mutex(self) -> None:
        import concurrent.futures

        claims = {
            "repository": "QuantStrategyLab/demo",
            "run_id": "run-1",
            "run_attempt": "1",
            "actor": "tester",
        }
        payload = {
            "source_repository": "QuantStrategyLab/demo",
            "source_ref": "main",
            "task": gateway.TASK_EXECUTE,
            "mode": gateway.MODE_REVIEW_ONLY,
            "timeout_seconds": 30,
        }

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                gateway.os.environ,
                {
                    "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
                    "CODEX_AUDIT_SERVICE_MAX_ACTIVE_JOBS": "8",
                },
                clear=False,
            ):
                with (
                    patch.object(gateway, "_run_job"),
                    patch.object(gateway, "_record_job_automation_run"),
                    patch.object(gateway, "_audit_log"),
                ):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                        futures = [
                            pool.submit(gateway._submit_job, claims, payload) for _ in range(8)
                        ]
                        results = [future.result(timeout=5) for future in futures]

                job_files = list(Path(tmp).glob("*.json"))

        unique_ids = {result["job_id"] for result in results}
        self.assertEqual(len(unique_ids), 1)
        self.assertEqual(sum(1 for result in results if result.get("deduped")), 7)
        self.assertEqual(len(job_files), 1)



if __name__ == "__main__":
    unittest.main()
