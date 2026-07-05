"""Lightweight automation run ledger and runtime health policy runner."""

from __future__ import annotations

import threading
import time
from collections import Counter
from copy import deepcopy
from typing import Any

from service.task_state import TERMINAL_STATES

CONTROL_CONTINUE = "continue"
CONTROL_REVIEW_ONLY = "review_only"
CONTROL_PAUSE_AUTO_FIX = "pause_auto_fix"
CONTROL_ESCALATE = "escalate"

CONTROL_ACTIONS = frozenset(
    {
        CONTROL_CONTINUE,
        CONTROL_REVIEW_ONLY,
        CONTROL_PAUSE_AUTO_FIX,
        CONTROL_ESCALATE,
    }
)


def _normalize_status(value: Any, default: str = "") -> str:
    if isinstance(value, dict):
        value = value.get("status", default)
    return str(value or default).strip().lower()


def suggest_control_action(
    service_health: Any = "healthy",
    quota_status: Any = "ok",
    org_health_status: Any = "ok",
) -> dict[str, Any]:
    """Convert health/quota/org-health signals into a control action."""
    health = _normalize_status(service_health, "healthy")
    quota = _normalize_status(quota_status, "ok")
    org_health = _normalize_status(org_health_status, "ok")

    reasons: list[str] = []
    action = CONTROL_REVIEW_ONLY

    if health == "unhealthy":
        action = CONTROL_ESCALATE
        reasons.append("service health is unhealthy")
    elif quota in {"exhausted", "blocked"}:
        action = CONTROL_ESCALATE
        reasons.append(f"quota status is {quota}")
    elif org_health == "unhealthy":
        action = CONTROL_ESCALATE
        reasons.append("org health is unhealthy")
    elif health == "degraded" or quota in {"low", "constrained"} or org_health == "degraded":
        action = CONTROL_PAUSE_AUTO_FIX
        if health == "degraded":
            reasons.append("service health is degraded")
        if quota in {"low", "constrained"}:
            reasons.append(f"quota status is {quota}")
        if org_health == "degraded":
            reasons.append("org health is degraded")
    elif health == "healthy" and quota == "ok" and org_health in {"ok", "healthy"}:
        action = CONTROL_CONTINUE
        reasons.append("all runtime signals are healthy")
    else:
        reasons.append("runtime signals are incomplete")

    return {
        "action": action,
        "service_health": health,
        "quota_status": quota,
        "org_health_status": org_health,
        "reasons": reasons,
        "requires_human_review": action in {CONTROL_REVIEW_ONLY, CONTROL_PAUSE_AUTO_FIX, CONTROL_ESCALATE},
        "auto_fix_allowed": action == CONTROL_CONTINUE,
    }


class AutomationRunLedger:
    """In-memory ledger of automation runs and their latest task state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    def record(
        self,
        run_id: str,
        task_state: str,
        *,
        task_name: str = "",
        suggested_action: str = "",
        service_health: Any = "",
        quota_status: Any = "",
        org_health_status: Any = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record or update one automation run."""
        if not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        now = time.time()
        entry = {
            "run_id": run_id,
            "task_name": task_name,
            "task_state": str(task_state or "").strip().lower(),
            "suggested_action": str(suggested_action or "").strip().lower(),
            "service_health": _normalize_status(service_health),
            "quota_status": _normalize_status(quota_status, "ok"),
            "org_health_status": _normalize_status(org_health_status, "ok"),
            "metadata": dict(metadata or {}),
            "updated_at": now,
            "events": [],
        }
        with self._lock:
            current = self._runs.get(run_id)
            if current:
                entry["events"] = list(current.get("events", []))
                if not entry["task_name"]:
                    entry["task_name"] = str(current.get("task_name", ""))
                if not entry["metadata"]:
                    entry["metadata"] = dict(current.get("metadata", {}))
            entry["events"].append(
                {
                    "task_state": entry["task_state"],
                    "suggested_action": entry["suggested_action"],
                    "service_health": entry["service_health"],
                    "quota_status": entry["quota_status"],
                    "org_health_status": entry["org_health_status"],
                    "metadata": dict(entry["metadata"]),
                    "recorded_at": now,
                }
            )
            self._runs[run_id] = entry
            return deepcopy(entry)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._runs.get(run_id)
            return deepcopy(entry) if entry else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            runs = [deepcopy(entry) for entry in self._runs.values()]

        task_states = Counter(str(run.get("task_state", "")).strip().lower() for run in runs if run.get("task_state"))
        suggested_actions = Counter(
            str(run.get("suggested_action", "")).strip().lower() for run in runs if run.get("suggested_action")
        )
        terminal_runs = sum(1 for run in runs if str(run.get("task_state", "")).strip().lower() in TERMINAL_STATES)
        return {
            "runs": sorted(runs, key=lambda item: (float(item.get("updated_at", 0.0)), str(item.get("run_id", ""))), reverse=True),
            "summary": {
                "total_runs": len(runs),
                "active_runs": len(runs) - terminal_runs,
                "terminal_runs": terminal_runs,
                "task_states": dict(task_states),
                "suggested_actions": dict(suggested_actions),
            },
        }
