from __future__ import annotations

import concurrent.futures
import tempfile
import threading
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




class AiGatewayStaleJobPollingTests(unittest.TestCase):
    def _running_job(self, **overrides):
        return {
            "job_id": "d" * 24,
            "status": "running",
            "created_at": 1,
            "updated_at": 1,
            "timeout_seconds": 30,
            "repository": "Synthetic/owner",
            "run_id": "100",
            "run_attempt": "1",
            **overrides,
        }

    def test_stale_snapshot_cannot_overwrite_worker_success_and_review_result(self) -> None:
        snapshot_read = threading.Event()
        worker_completed = threading.Event()
        running = self._running_job()
        completed = {
            **running,
            "status": "succeeded",
            "updated_at": 1000,
            "output": '{"verdict":"approve"}',
            "engineering_review": {
                "purpose": gateway.ENGINEERING_REVIEW_PURPOSE,
                "verdict": "approve",
                "input_digest": "e" * 64,
            },
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            gateway.os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False
        ), patch.object(gateway, "_now", return_value=1000), patch.object(
            gateway, "_record_job_automation_run"
        ) as record_automation_run:
            gateway._write_job(running)

            def poll_after_worker_completes():
                snapshot = gateway._read_job(running["job_id"])
                snapshot_read.set()
                self.assertTrue(worker_completed.wait(timeout=5))
                return gateway._mark_stale_job_failed(snapshot)

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(poll_after_worker_completes)
                try:
                    self.assertTrue(snapshot_read.wait(timeout=5))
                    gateway._write_job(completed)
                    completed_bytes = gateway._job_path(running["job_id"]).read_bytes()
                finally:
                    worker_completed.set()
                result = future.result(timeout=5)

            self.assertEqual(result, completed)
            self.assertEqual(gateway._read_job(running["job_id"]), completed)
            self.assertEqual(gateway._job_path(running["job_id"]).read_bytes(), completed_bytes)
            record_automation_run.assert_not_called()

    def test_concurrent_stale_polls_transition_and_record_only_once(self) -> None:
        ready = threading.Barrier(2)
        running = self._running_job()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            gateway.os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False
        ), patch.object(gateway, "_now", return_value=1000), patch.object(
            gateway, "_record_job_automation_run"
        ) as record_automation_run:
            gateway._write_job(running)

            def poll_same_snapshot():
                snapshot = gateway._read_job(running["job_id"])
                ready.wait(timeout=5)
                return gateway._mark_stale_job_failed(snapshot)

            with patch.object(gateway, "_write_job", wraps=gateway._write_job) as write_job:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(poll_same_snapshot) for _ in range(2)]
                    results = [future.result(timeout=5) for future in futures]
                repeated = gateway._mark_stale_job_failed(running)

            persisted = gateway._read_job(running["job_id"])
            self.assertEqual(persisted["status"], "failed")
            self.assertEqual(persisted["failure_category"], "stale_job_timeout")
            self.assertEqual(results, [persisted, persisted])
            self.assertEqual(repeated, persisted)
            write_job.assert_called_once_with(persisted)
            record_automation_run.assert_called_once_with(persisted)

    def test_current_fresh_or_terminal_state_is_returned_without_mutation(self) -> None:
        snapshot = self._running_job()
        for current in (
            self._running_job(updated_at=999),
            self._running_job(status="queued", updated_at=999),
            self._running_job(status="succeeded", updated_at=2, output="completed"),
            self._running_job(status="failed", updated_at=2, error="existing failure"),
        ):
            with self.subTest(status=current["status"]), tempfile.TemporaryDirectory() as tmp, patch.dict(
                gateway.os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False
            ), patch.object(gateway, "_now", return_value=1000), patch.object(
                gateway, "_record_job_automation_run"
            ) as record_automation_run:
                gateway._write_job(current)
                before = gateway._job_path(snapshot["job_id"]).read_bytes()
                with patch.object(gateway, "_write_job", wraps=gateway._write_job) as write_job:
                    result = gateway._mark_stale_job_failed(snapshot)
                self.assertEqual(result, current)
                self.assertEqual(gateway._job_path(snapshot["job_id"]).read_bytes(), before)
                write_job.assert_not_called()
                record_automation_run.assert_not_called()

    def test_genuine_stale_active_jobs_fail_but_timeout_boundary_is_unchanged(self) -> None:
        for status in ("queued", "running"):
            for now, expected in ((151, status), (152, "failed")):
                with self.subTest(status=status, now=now), tempfile.TemporaryDirectory() as tmp, patch.dict(
                    gateway.os.environ, {"CODEX_AUDIT_SERVICE_JOB_DIR": tmp}, clear=False
                ), patch.object(gateway, "_now", return_value=now), patch.object(
                    gateway, "_record_job_automation_run"
                ) as record_automation_run:
                    job = self._running_job(status=status)
                    gateway._write_job(job)
                    result = gateway._mark_stale_job_failed(job)
                    self.assertEqual(result["status"], expected)
                    self.assertEqual(gateway._read_job(job["job_id"]), result)
                    if expected == "failed":
                        self.assertEqual(result["failure_category"], "stale_job_timeout")
                        record_automation_run.assert_called_once_with(result)
                    else:
                        record_automation_run.assert_not_called()


class AiGatewayJobAdmissionTests(unittest.TestCase):
    def test_research_model_or_effort_upgrade_cannot_reuse_weaker_job(self) -> None:
        payload = {"prompt": "synthetic", "research_stage": "drift_analysis", "model": "gpt-5.6-terra", "reasoning_effort": "medium"}
        def key(value):
            return gateway._job_dedupe_key(value, repository="Synthetic/repo", run_id="1", run_attempt="1")
        for field, value in (("model", "gpt-6-astra"), ("reasoning_effort", "high"), ("research_stage", "promotion_review")):
            with self.subTest(field=field):
                self.assertNotEqual(key(payload), key({**payload, field: value}))
        self.assertEqual(key(payload), key(dict(payload)))

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
