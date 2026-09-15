"""Tests for automation control-plane execution decisions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from service.ai_gateway_service import _automation_control_snapshot, _automation_triage_snapshot


def _org_health_snapshot(*, status: str = "unhealthy", provider_status: str = "available", repositories=None) -> dict:
    return {
        "status": status,
        "provider": {"status": provider_status},
        "repositories": list(repositories or []),
    }


class TestAutomationControlSnapshot(unittest.TestCase):
    def test_control_snapshot_defaults_to_review_and_fix_for_healthy_repo(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo")

        self.assertTrue(control["execution"]["auto_fix_allowed"])

    def test_manual_platform_bugfix_uses_healthy_target_scope_when_global_org_is_unhealthy(self) -> None:
        repo = "QuantStrategyLab/ScopedTarget"
        target = {
            "repo": repo,
            "status": "healthy",
            "reasons": [],
            "signals": {
                "current_unknown_workflow_runs": 0,
                "current_degraded_workflow_runs": 0,
                "current_failed_workflow_runs": 0,
            },
        }
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()

        with (
            patch(
                "service.ai_gateway_service.read_org_health",
                return_value=_org_health_snapshot(repositories=[target]),
            ),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={}),
        ):
            control = _automation_control_snapshot(
                repo,
                task_name="platform_bugfix",
                requested_mode="review_and_fix",
                manual_approval_valid=True,
                manual_approval_id="approval-1",
                source_ref="refs/heads/main",
                source_sha="a" * 40,
            )

        self.assertEqual(control["org_health_status"], "unhealthy")
        self.assertEqual(control["runtime_action"], "escalate")
        self.assertEqual(control["execution_health_scope"], "scoped")
        self.assertEqual(control["execution_health_status"], "healthy")
        self.assertEqual(control["effective_action"], "continue")
        self.assertTrue(control["auto_fix_allowed"])

    def test_manual_platform_bugfix_scope_requires_complete_healthy_target_snapshot(self) -> None:
        repo = "QuantStrategyLab/ScopedTarget"
        base_target = {
            "repo": repo,
            "status": "healthy",
            "reasons": [],
            "signals": {
                "current_unknown_workflow_runs": 0,
                "current_degraded_workflow_runs": 0,
                "current_failed_workflow_runs": 0,
            },
        }
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()
        variants = {
            "missing_target": [],
            "duplicate_target": [base_target, dict(base_target)],
            "target_unhealthy": [{**base_target, "status": "unhealthy"}],
            "target_with_reason": [{**base_target, "reasons": ["failed workflow"]}],
            "target_with_signal": [{**base_target, "signals": {**base_target["signals"], "current_failed_workflow_runs": 1}}],
            "provider_unavailable": [base_target],
            "global_unknown": [base_target],
            "global_degraded": [base_target],
            "global_healthy_missing_target": [],
        }
        for name, repositories in variants.items():
            with self.subTest(name=name):
                provider_status = "unavailable" if name == "provider_unavailable" else "available"
                global_status = (
                    "unknown"
                    if name == "global_unknown"
                    else "degraded"
                    if name == "global_degraded"
                    else "healthy"
                    if name == "global_healthy_missing_target"
                    else "unhealthy"
                )
                with (
                    patch(
                        "service.ai_gateway_service.read_org_health",
                        return_value=_org_health_snapshot(
                            status=global_status,
                            provider_status=provider_status,
                            repositories=repositories,
                        ),
                    ),
                    patch("service.ai_gateway_service.get_health_monitor", return_value=health),
                    patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
                    patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
                    patch("service.ai_gateway_service.load_execution_policy", return_value={}),
                ):
                    control = _automation_control_snapshot(
                        repo,
                        task_name="platform_bugfix",
                        requested_mode="review_and_fix",
                        manual_approval_valid=True,
                        manual_approval_id="approval-1",
                        source_ref="refs/heads/main",
                        source_sha="a" * 40,
                    )
                self.assertNotEqual(control["effective_action"], "continue")
                self.assertFalse(control["auto_fix_allowed"])

    def test_manual_platform_bugfix_scope_keeps_runtime_safety_gates(self) -> None:
        repo = "QuantStrategyLab/ScopedTarget"
        target = {
            "repo": repo,
            "status": "healthy",
            "reasons": [],
            "signals": {
                "current_unknown_workflow_runs": 0,
                "current_degraded_workflow_runs": 0,
                "current_failed_workflow_runs": 0,
            },
        }
        base_request = {
            "task_name": "platform_bugfix",
            "requested_mode": "review_and_fix",
            "manual_approval_valid": True,
            "manual_approval_id": "approval-1",
            "source_ref": "refs/heads/main",
            "source_sha": "a" * 40,
        }
        ledger_runs = [
            {
                "task_state": "failed",
                "metadata": {"origin": "service_job", "source_repository": repo},
            },
            {
                "task_state": "failed",
                "metadata": {"origin": "service_job", "source_repository": repo},
            },
        ]
        cases = (
            ("service_unhealthy", type("Health", (), {"status": "unhealthy"})(), {"status": "ok"}, []),
            ("quota_exhausted", type("Health", (), {"status": "healthy"})(), {"status": "exhausted"}, []),
            ("failure_threshold", type("Health", (), {"status": "healthy"})(), {"status": "ok"}, ledger_runs),
        )
        for name, health, quota_status, runs in cases:
            with self.subTest(name=name):
                quota = type("Quota", (), {"runtime_status": lambda self, repo, status=quota_status: status})()
                ledger = type("Ledger", (), {"snapshot": lambda self, limit=None, runs=runs: {"runs": runs}})()
                with (
                    patch("service.ai_gateway_service.read_org_health", return_value=_org_health_snapshot(repositories=[target])),
                    patch("service.ai_gateway_service.get_health_monitor", return_value=health),
                    patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
                    patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
                    patch(
                        "service.ai_gateway_service.load_execution_policy",
                        return_value={"default": {"max_consecutive_failures": 2}},
                    ),
                ):
                    control = _automation_control_snapshot(repo, **base_request)
                self.assertEqual(control["execution_health_scope"], "scoped")
                self.assertFalse(control["auto_fix_allowed"])
                self.assertNotEqual(control["effective_action"], "continue")

    def test_manual_platform_bugfix_scope_requires_valid_grant_identity(self) -> None:
        repo = "QuantStrategyLab/ScopedTarget"
        target = {
            "repo": repo,
            "status": "healthy",
            "reasons": [],
            "signals": {
                "current_unknown_workflow_runs": 0,
                "current_degraded_workflow_runs": 0,
                "current_failed_workflow_runs": 0,
            },
        }
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()
        for kwargs in (
            {"manual_approval_valid": False},
            {"task_name": "other_task", "manual_approval_valid": True},
            {"requested_mode": "review_only", "manual_approval_valid": True},
            {"manual_approval_valid": True, "manual_approval_id": ""},
            {"manual_approval_valid": True, "source_ref": "", "source_sha": ""},
        ):
            with self.subTest(kwargs=kwargs):
                request = {
                    "task_name": "platform_bugfix",
                    "requested_mode": "review_and_fix",
                    "manual_approval_valid": True,
                    "manual_approval_id": "approval-1",
                    "source_ref": "refs/heads/main",
                    "source_sha": "a" * 40,
                }
                request.update(kwargs)
                with (
                    patch("service.ai_gateway_service.read_org_health", return_value=_org_health_snapshot(repositories=[target])),
                    patch("service.ai_gateway_service.get_health_monitor", return_value=health),
                    patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
                    patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
                    patch("service.ai_gateway_service.load_execution_policy", return_value={}),
                ):
                    control = _automation_control_snapshot(repo, **request)
                self.assertNotEqual(control["effective_action"], "continue")
                self.assertFalse(control["auto_fix_allowed"])

    def test_control_snapshot_downgrades_legacy_action_for_review_only_mode(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", requested_mode="review_only")

        self.assertEqual(control["effective_action"], "review_only")

    def test_control_snapshot_applies_service_owned_execution_policy(self) -> None:
        with TemporaryDirectory(dir=".") as tmp:
            policy_path = Path(tmp) / "execution_policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "default": {
                            "max_autonomy": "auto_pr",
                            "max_consecutive_failures": 3,
                            "low_cost_model": "gpt-5.4-mini",
                            "low_cost_provider": "openai",
                        },
                        "repositories": {
                            "QuantStrategyLab/TargetRepo": {
                                "max_autonomy": "manual",
                                "max_consecutive_failures": 2,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            health = type("Health", (), {"status": "healthy"})()
            quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
            ledger = type("Ledger", (), {"snapshot": lambda self, limit=20: {"runs": []}})()

            with (
                patch.dict(
                    os.environ,
                    {
                        "CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH": str(policy_path),
                        "CODEX_AUDIT_SERVICE_EXECUTION_POLICY_OWNER": f"{os.getuid()}:{os.getgid()}",
                    },
                    clear=False,
                ),
                patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
                patch("service.ai_gateway_service.get_health_monitor", return_value=health),
                patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
                patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            ):
                control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", requested_mode="review_and_fix")

        self.assertEqual(control["effective_action"], "escalate")

    def test_control_snapshot_scans_full_retained_ledger_for_repo_failure_streak(self) -> None:
        runs = [
            {
                "task_name": f"other-{index}",
                "task_state": "merged",
                "metadata": {"origin": "service_job", "source_repository": f"QuantStrategyLab/Other{index}"},
            }
            for index in range(20)
        ]
        runs.extend(
            [
                {
                    "task_name": "monthly",
                    "task_state": "failed",
                    "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/TargetRepo"},
                },
                {
                    "task_name": "monthly",
                    "task_state": "failed",
                    "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/TargetRepo"},
                },
            ]
        )
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()

        class Ledger:
            requested_limit = object()

            def snapshot(self, limit=100):
                self.requested_limit = limit
                return {"runs": runs}

        ledger = Ledger()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_consecutive_failures": 2}}),
        ):
            _automation_control_snapshot("QuantStrategyLab/TargetRepo", task_name="monthly")

        self.assertIsNone(ledger.requested_limit)

    def test_control_snapshot_counts_pending_run_for_failure_threshold(self) -> None:
        runs = [
            {
                "run_id": "previous-run",
                "task_name": "monthly",
                "task_state": "failed",
                "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/TargetRepo"},
            }
        ]
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": runs}})()
        pending_run = {
            "run_id": "current-run",
            "task_name": "monthly",
            "task_state": "failed",
            "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/TargetRepo"},
        }

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_consecutive_failures": 2}}),
        ):
            control = _automation_control_snapshot(
                "QuantStrategyLab/TargetRepo",
                task_name="monthly",
                requested_mode="review_and_fix",
                pending_run=pending_run,
            )

        self.assertEqual(control["effective_action"], "escalate")

    def test_control_snapshot_fails_closed_after_ledger_eviction(self) -> None:
        runs = [
            {
                "run_id": "failed-1",
                "task_name": "monthly",
                "task_state": "failed",
                "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/TargetRepo"},
            },
        ]
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type(
            "Ledger",
            (),
            {
                "snapshot": lambda self, limit=None: {
                    "runs": runs,
                    "summary": {
                        "retention": {
                            "may_be_truncated": True,
                            "evicted_runs": 1,
                            "evicted_runs_by_repo": {"quantstrategylab/targetrepo": 1},
                        }
                    },
                }
            },
        )()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_consecutive_failures": 2}}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", task_name="monthly")

        self.assertEqual(control["effective_action"], "escalate")

    def test_control_snapshot_allows_known_repo_boundary_when_history_unknown(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type(
            "Ledger",
            (),
            {
                "snapshot": lambda self, limit=None: {
                    "runs": [
                        {"run_id": "merged-1", "task_state": "merged", "metadata": {"origin": "external_workflow", "source_repository": "QuantStrategyLab/TargetRepo"}}
                    ],
                    "summary": {
                        "retention": {
                            "history_completeness_unknown": True,
                            "may_be_truncated": True,
                            "evicted_runs": 1,
                            "evicted_runs_by_repo": {"quantstrategylab/targetrepo": 0},
                        }
                    },
                }
            },
        )()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_consecutive_failures": 2}}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", task_name="monthly", requested_mode="review_and_fix")

        self.assertTrue(control["execution"]["failure_history_complete"])

    def test_control_snapshot_fails_closed_when_ledger_history_is_unknown(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type(
            "Ledger",
            (),
            {
                "snapshot": lambda self, limit=None: {
                    "runs": [],
                    "summary": {"retention": {"history_completeness_unknown": True}},
                }
            },
        )()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_consecutive_failures": 2}}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", task_name="monthly")

        self.assertEqual(control["effective_action"], "escalate")

    def test_control_snapshot_preserves_legacy_continue_when_auto_merge_is_capped(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_autonomy": "auto_pr"}}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", requested_mode="auto_merge")

        self.assertEqual(control["execution"]["effective_autonomy"], "auto_pr")

    def test_triage_omitted_mode_keeps_review_and_fix_default(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={}),
        ):
            triage = _automation_triage_snapshot(
                "QuantStrategyLab/TargetRepo",
                task="monthly",
                changed_paths=["docs/runbook.md"],
            )

        self.assertEqual(triage["control"]["execution"]["requested_mode"], "review_and_fix")

    def test_control_snapshot_deduplicates_pending_run_by_run_id(self) -> None:
        runs = [
            {
                "run_id": "current-run",
                "task_name": "monthly",
                "task_state": "failed",
                "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/TargetRepo"},
            }
        ]
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": runs}})()
        pending_run = {
            "run_id": "current-run",
            "task_name": "monthly",
            "task_state": "failed",
            "metadata": {"origin": "service_job", "source_repository": "QuantStrategyLab/TargetRepo"},
        }

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_consecutive_failures": 2}}),
        ):
            control = _automation_control_snapshot(
                "QuantStrategyLab/TargetRepo",
                task_name="monthly",
                requested_mode="review_and_fix",
                pending_run=pending_run,
            )

        self.assertEqual(control["execution"]["consecutive_failures"], 1)

    def test_control_snapshot_allows_low_cost_auto_fix_for_low_quota_policy(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "low"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"low_cost_model": "gpt-5.4-mini"}}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", requested_mode="review_and_fix")

        self.assertEqual(control["action"], "continue")
        self.assertEqual(control["effective_action"], "continue")

    def test_control_snapshot_fails_closed_when_ledger_is_unavailable(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: (_ for _ in ()).throw(RuntimeError("boom"))})()

        with (
            patch("service.ai_gateway_service.read_org_health", return_value={"status": "ok"}),
            patch("service.ai_gateway_service.get_health_monitor", return_value=health),
            patch("service.ai_gateway_service.get_quota_manager", return_value=quota),
            patch("service.ai_gateway_service.get_automation_run_ledger", return_value=ledger),
            patch("service.ai_gateway_service.load_execution_policy", return_value={"default": {"max_autonomy": "auto_merge"}}),
        ):
            control = _automation_control_snapshot("QuantStrategyLab/TargetRepo", requested_mode="auto_merge")

        self.assertEqual(control["effective_action"], "escalate")


if __name__ == "__main__":
    unittest.main()
