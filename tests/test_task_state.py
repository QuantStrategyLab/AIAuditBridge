import unittest

from service.task_state import change_task_state, job_task_state


class TaskStateTest(unittest.TestCase):
    def test_job_task_state_maps_core_statuses(self) -> None:
        self.assertEqual(job_task_state({"status": "queued"}), "queued")
        self.assertEqual(job_task_state({"status": "pending"}), "queued")
        self.assertEqual(job_task_state({"status": "running"}), "running")
        self.assertEqual(job_task_state({"status": "succeeded"}), "reviewed")
        self.assertEqual(job_task_state({"status": "failed", "failure_category": "transient_service_failure"}), "failed")
        self.assertEqual(job_task_state({"status": "error"}), "failed")
        self.assertEqual(job_task_state({"status": "failed", "failure_category": "auth_or_config_failure"}), "blocked")
        self.assertEqual(job_task_state({"status": "failed", "failure_category": "patch_contract_failure"}), "blocked")
        self.assertEqual(job_task_state({"status": "mystery"}), "blocked")

    def test_change_task_state_priority(self) -> None:
        self.assertEqual(change_task_state({"merged_at": "2026-07-05", "risk": "high"}), "merged")
        self.assertEqual(change_task_state({"rollback_issue_required": True}), "human_review_required")
        self.assertEqual(change_task_state({"rollback_issue_url": "https://example.test/issue/1"}), "human_review_required")
        self.assertEqual(change_task_state({"risk": "critical"}), "human_review_required")
        self.assertEqual(change_task_state({"action": "auto_merge", "pr_number": 7}), "auto_merge_requested")
        self.assertEqual(change_task_state({"action": "auto_pr", "pr_number": 7}), "waiting_for_ci")
        self.assertEqual(change_task_state({"external_url": "https://example.test/pr/7"}), "pr_opened")
        self.assertEqual(change_task_state({"effect": "improved"}), "reviewed")
