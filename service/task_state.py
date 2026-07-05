"""Shared task state vocabulary for dashboard and automation decisions."""

from __future__ import annotations

from typing import Any

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_REVIEWED = "reviewed"
STATE_PR_OPENED = "pr_opened"
STATE_WAITING_FOR_CI = "waiting_for_ci"
STATE_AUTO_MERGE_REQUESTED = "auto_merge_requested"
STATE_HUMAN_REVIEW_REQUIRED = "human_review_required"
STATE_MERGED = "merged"
STATE_FAILED = "failed"
STATE_BLOCKED = "blocked"

TERMINAL_STATES = frozenset({STATE_MERGED, STATE_FAILED, STATE_BLOCKED, STATE_HUMAN_REVIEW_REQUIRED})


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def job_task_state(job: dict[str, Any]) -> str:
    """Map low-level async job status to the shared task-state vocabulary."""
    status = str(job.get("status") or "").strip().lower()
    if status in {"queued", "pending"}:
        return STATE_QUEUED
    if status == "running":
        return STATE_RUNNING
    if status == "succeeded":
        return STATE_REVIEWED
    if status in {"failed", "error"}:
        failure = str(job.get("failure_category") or "").strip().lower()
        if failure in {"auth_or_config_failure", "patch_contract_failure"}:
            return STATE_BLOCKED
        return STATE_FAILED
    return STATE_BLOCKED


def change_requires_human_review(change: Any) -> bool:
    """Whether a feedback/change record should appear in the human queue."""
    action = str(_get(change, "action", "") or "").strip().lower()
    risk = str(_get(change, "risk", "") or "").strip().lower()
    effect = str(_get(change, "effect", "") or "").strip().lower()
    return (
        action in {"escalate", "manual"}
        or risk in {"critical", "high"}
        or effect == "degraded"
        or bool(_get(change, "rollback_issue_required", False))
        or bool(_get(change, "rollback_issue_url", ""))
    )


def change_task_state(change: Any) -> str:
    """Derive the shared task state for a registered autonomous change."""
    merged_at = _get(change, "merged_at", "")
    if merged_at:
        return STATE_MERGED
    if change_requires_human_review(change):
        return STATE_HUMAN_REVIEW_REQUIRED
    action = str(_get(change, "action", "") or "").strip().lower()
    pr_number = _get(change, "pr_number", None)
    external_url = str(_get(change, "external_url", "") or "")
    effect = str(_get(change, "effect", "") or "").strip().lower()
    if action == "auto_merge" and pr_number:
        return STATE_AUTO_MERGE_REQUESTED
    if pr_number:
        return STATE_WAITING_FOR_CI
    if external_url:
        return STATE_PR_OPENED
    if effect and effect != "pending":
        return STATE_REVIEWED
    return STATE_REVIEWED
