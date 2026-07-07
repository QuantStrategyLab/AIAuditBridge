"""Lightweight automation run ledger and runtime health policy runner."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from service.task_state import TERMINAL_STATES

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

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

DEFAULT_MAX_RUNS = 500
DEFAULT_MAX_EVENTS_PER_RUN = 50
AUTOMATION_LEDGER_PATH_ENV = "CODEX_AUDIT_SERVICE_AUTOMATION_LEDGER_PATH"
MAX_RUN_METADATA_FIELDS = 20
MAX_RUN_METADATA_VALUE_LENGTH = 500
MAX_EVENT_METADATA_FIELDS = 10
MAX_EVENT_METADATA_VALUE_LENGTH = 200
INTERNAL_ENTRY_KEYS = frozenset({"_ledger_sequence"})

QUOTA_STATUS_SEVERITY = {
    "ok": 0,
    "healthy": 0,
    "unknown": 1,
    "unavailable": 1,
    "low": 2,
    "constrained": 2,
    "exhausted": 3,
    "blocked": 3,
}


def _normalize_status(value: Any, default: str = "") -> str:
    if isinstance(value, dict):
        value = value.get("status", default)
    return str(value or default).strip().lower()


def _normalize_quota_status(value: Any, default: str = "") -> str:
    statuses = [_normalize_status(value)]
    if isinstance(value, dict) and isinstance(value.get("quota"), dict):
        statuses.append(_normalize_status(value["quota"]))
    normalized = [status for status in statuses if status]
    if not normalized:
        return default
    return max(normalized, key=lambda status: QUOTA_STATUS_SEVERITY.get(status, 1))


def _is_omitted(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _metadata_snapshot(
    metadata: dict[str, Any],
    *,
    max_fields: int,
    max_value_length: int,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    omitted = 0
    for key, value in metadata.items():
        if len(snapshot) >= max_fields:
            omitted += 1
            continue
        if not isinstance(value, str | int | float | bool | type(None)):
            omitted += 1
            continue
        if isinstance(value, str) and len(value) > max_value_length:
            value = value[:max_value_length] + "…"
        snapshot[str(key)] = value
    if omitted:
        snapshot["_omitted_fields"] = omitted
    return snapshot


def _run_metadata_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    return _metadata_snapshot(
        metadata,
        max_fields=MAX_RUN_METADATA_FIELDS,
        max_value_length=MAX_RUN_METADATA_VALUE_LENGTH,
    )


def _event_metadata_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    return _metadata_snapshot(
        metadata,
        max_fields=MAX_EVENT_METADATA_FIELDS,
        max_value_length=MAX_EVENT_METADATA_VALUE_LENGTH,
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _entry_order_key(entry: dict[str, Any]) -> tuple[float, int]:
    try:
        updated_at = float(entry.get("updated_at", 0.0))
    except (TypeError, ValueError):
        updated_at = 0.0
    return updated_at, _safe_int(entry.get("_ledger_sequence"), 0)


def _entry_owner_repository(entry: dict[str, Any]) -> str:
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    return str(metadata.get("source_repository") or metadata.get("repository") or "")


def _repo_retention_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _entry_origin(entry: dict[str, Any]) -> str:
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    return str(metadata.get("origin") or "")


def _is_terminal_task_state(value: Any) -> bool:
    return str(value or "").strip().lower() in TERMINAL_STATES


def _can_replace_stale_service_failure(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
    return (
        str(current.get("task_state") or "").strip().lower() == "failed"
        and _entry_origin(current) == "service_job"
        and _entry_origin(candidate) == "service_job"
        and str(metadata.get("failure_category") or "") == "stale_job_timeout"
        and _entry_order_key(candidate) >= _entry_order_key(current)
    )


def suggest_control_action(
    service_health: Any = "",
    quota_status: Any = "",
    org_health_status: Any = "",
) -> dict[str, Any]:
    """Convert health/quota/org-health signals into a control action."""
    health = _normalize_status(service_health, "unknown")
    quota = _normalize_quota_status(quota_status, "unknown")
    org_health = _normalize_status(org_health_status, "unknown")

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
    elif health in {"healthy", "ok"} and quota in {"ok", "healthy"} and org_health in {"ok", "healthy"}:
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

    def __init__(
        self,
        *,
        max_runs: int = DEFAULT_MAX_RUNS,
        max_events_per_run: int = DEFAULT_MAX_EVENTS_PER_RUN,
        storage_path: Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._max_runs = max(1, int(max_runs))
        self._max_events_per_run = max(1, int(max_events_per_run))
        self._sequence = 0
        self._evicted_runs_count = 0
        self._evicted_runs_by_repo: dict[str, int] = {}
        self._history_completeness_unknown = False
        self._storage_file_seen = False
        self._storage_path = storage_path
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if self._storage_path is None:
            return
        if not self._storage_path.exists():
            return
        runs, sequence, evicted_runs, evicted_runs_by_repo, history_unknown = self._read_from_disk_unlocked()
        with self._lock:
            self._storage_file_seen = True
            self._runs = runs
            self._sequence = sequence
            self._evicted_runs_count = evicted_runs
            self._evicted_runs_by_repo = evicted_runs_by_repo
            self._history_completeness_unknown = history_unknown
            self._evict_old_runs_locked()

    def _read_from_disk_unlocked(self) -> tuple[dict[str, dict[str, Any]], int, int, dict[str, int], bool]:
        if self._storage_path is None or not self._storage_path.exists():
            return {}, 0, 0, {}, self._storage_file_seen
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, 0, 0, {}, True
        runs = payload.get("runs") if isinstance(payload, dict) else None
        if not isinstance(runs, dict):
            return {}, 0, 0, {}, True
        clean_runs = {str(key): value for key, value in runs.items() if isinstance(value, dict)}
        sequence = _safe_int(payload.get("sequence"), len(clean_runs))
        history_unknown = "evicted_runs" not in payload or "evicted_runs_by_repo" not in payload
        evicted_runs = _safe_int(payload.get("evicted_runs"), 0)
        raw_evicted_by_repo = payload.get("evicted_runs_by_repo")
        evicted_by_repo = {
            _repo_retention_key(repo): max(0, _safe_int(count))
            for repo, count in (raw_evicted_by_repo.items() if isinstance(raw_evicted_by_repo, dict) else [])
            if _repo_retention_key(repo)
        }
        history_unknown = bool(payload.get("history_completeness_unknown", history_unknown))
        return clean_runs, sequence, max(0, evicted_runs), evicted_by_repo, history_unknown

    def _merge_evicted_runs_by_repo_locked(self, disk_evicted_runs_by_repo: dict[str, int]) -> None:
        for repo, count in disk_evicted_runs_by_repo.items():
            repo_key = _repo_retention_key(repo)
            if not repo_key:
                continue
            self._evicted_runs_by_repo[repo_key] = max(self._evicted_runs_by_repo.get(repo_key, 0), int(count))

    def _drop_runs_evicted_on_disk_locked(self, disk_runs: dict[str, dict[str, Any]], *, preserve_run_id: str = "") -> None:
        if len(disk_runs) < self._max_runs:
            return
        disk_run_ids = set(disk_runs)
        for run_id in list(self._runs):
            if run_id != preserve_run_id and run_id not in disk_run_ids:
                self._runs.pop(run_id, None)

    def _persist_locked(self) -> None:
        self._persist_with_owner_guard_locked()

    def _refresh_from_disk_locked(self) -> None:
        if self._storage_path is None:
            return
        if not self._storage_path.exists():
            if self._storage_file_seen:
                self._history_completeness_unknown = True
            return
        lock_handle = None
        try:
            if fcntl is not None:
                lock_path = self._storage_path.with_suffix(self._storage_path.suffix + ".lock")
                lock_handle = lock_path.open("a+", encoding="utf-8")
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
            (
                disk_runs,
                disk_sequence,
                disk_evicted_runs,
                disk_evicted_runs_by_repo,
                disk_history_unknown,
            ) = self._read_from_disk_unlocked()
            self._storage_file_seen = True
            self._drop_runs_evicted_on_disk_locked(disk_runs)
            for run_id, disk_entry in disk_runs.items():
                current = self._runs.get(run_id)
                if current is not None:
                    self._runs[run_id] = self._merge_entry_locked(current, disk_entry)
                else:
                    self._runs[run_id] = disk_entry
            self._sequence = max(self._sequence, disk_sequence, len(self._runs))
            self._evicted_runs_count = max(self._evicted_runs_count, disk_evicted_runs)
            self._merge_evicted_runs_by_repo_locked(disk_evicted_runs_by_repo)
            self._history_completeness_unknown = self._history_completeness_unknown or disk_history_unknown
            self._evict_old_runs_locked()
        finally:
            if lock_handle is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
                finally:
                    lock_handle.close()

    def _persist_with_owner_guard_locked(
        self,
        *,
        guard_run_id: str = "",
        owner_repository: str = "",
        guard_preexisting: bool = False,
    ) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_handle = None
        try:
            if fcntl is not None:
                lock_path = self._storage_path.with_suffix(self._storage_path.suffix + ".lock")
                lock_handle = lock_path.open("a+", encoding="utf-8")
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            (
                disk_runs,
                disk_sequence,
                disk_evicted_runs,
                disk_evicted_runs_by_repo,
                disk_history_unknown,
            ) = self._read_from_disk_unlocked()
            self._storage_file_seen = True
            if guard_run_id:
                disk_entry = disk_runs.get(guard_run_id)
                disk_owner = _entry_owner_repository(disk_entry) if isinstance(disk_entry, dict) else ""
                if owner_repository and disk_owner and disk_owner != owner_repository:
                    raise PermissionError("automation run_id belongs to another repository")
                if guard_preexisting and disk_entry is None and len(disk_runs) >= self._max_runs:
                    raise ValueError("automation run was evicted from retained ledger")
            self._drop_runs_evicted_on_disk_locked(disk_runs, preserve_run_id=guard_run_id)
            for run_id, disk_entry in disk_runs.items():
                current = self._runs.get(run_id)
                if current is not None:
                    self._runs[run_id] = self._merge_entry_locked(current, disk_entry)
                else:
                    self._runs[run_id] = disk_entry
            self._sequence = max(self._sequence, disk_sequence, len(self._runs))
            self._evicted_runs_count = max(self._evicted_runs_count, disk_evicted_runs)
            self._merge_evicted_runs_by_repo_locked(disk_evicted_runs_by_repo)
            self._history_completeness_unknown = self._history_completeness_unknown or disk_history_unknown
            self._evict_old_runs_locked()
            self._write_to_disk_locked()
        finally:
            if lock_handle is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
                finally:
                    lock_handle.close()

    def _write_to_disk_locked(self) -> None:
        if self._storage_path is None:
            return
        payload = {
            "schema_version": "automation_run_ledger.v1",
            "sequence": self._sequence,
            "evicted_runs": self._evicted_runs_count,
            "evicted_runs_by_repo": self._evicted_runs_by_repo,
            "history_completeness_unknown": self._history_completeness_unknown,
            "runs": self._runs,
        }
        tmp = self._storage_path.with_suffix(self._storage_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._storage_path)
        self._storage_file_seen = True

    def _merge_entry_locked(self, current: dict[str, Any], disk_entry: dict[str, Any]) -> dict[str, Any]:
        current_terminal = _is_terminal_task_state(current.get("task_state"))
        disk_terminal = _is_terminal_task_state(disk_entry.get("task_state"))
        current_state = str(current.get("task_state") or "")
        disk_state = str(disk_entry.get("task_state") or "")
        if _can_replace_stale_service_failure(current, disk_entry):
            base = disk_entry
        elif _can_replace_stale_service_failure(disk_entry, current):
            base = current
        elif current_terminal and disk_terminal and current_state != disk_state:
            base = current if _entry_order_key(current) <= _entry_order_key(disk_entry) else disk_entry
        elif current_terminal != disk_terminal:
            base = current if current_terminal else disk_entry
        else:
            base = current if _entry_order_key(current) >= _entry_order_key(disk_entry) else disk_entry
        merged = deepcopy(base)
        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in (disk_entry, current):
            for event in source.get("events", []):
                if not isinstance(event, dict):
                    continue
                key = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
                if key in seen:
                    continue
                seen.add(key)
                events.append(deepcopy(event))
        if events:
            events.sort(key=lambda event: float(event.get("recorded_at", 0.0) or 0.0))
            merged["events"] = events[-self._max_events_per_run :]
        return merged

    def _evict_old_runs_locked(self) -> None:
        overflow = len(self._runs) - self._max_runs
        if overflow <= 0:
            return
        ordered = sorted(
            self._runs.values(),
            key=lambda item: (
                float(item.get("updated_at", 0.0)),
                int(item.get("_ledger_sequence", 0)),
            ),
        )
        for entry in ordered[:overflow]:
            repo_key = _repo_retention_key(_entry_owner_repository(entry))
            if repo_key:
                self._evicted_runs_by_repo[repo_key] = self._evicted_runs_by_repo.get(repo_key, 0) + 1
            self._runs.pop(str(entry["run_id"]))
        self._evicted_runs_count += overflow

    @staticmethod
    def _public_entry(entry: dict[str, Any], *, include_events: bool = True) -> dict[str, Any]:
        return {
            key: deepcopy(value)
            for key, value in entry.items()
            if key not in INTERNAL_ENTRY_KEYS and (include_events or key != "events")
        }

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
        owner_repository: str = "",
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
            "quota_status": _normalize_quota_status(quota_status),
            "org_health_status": _normalize_status(org_health_status),
            "metadata": _run_metadata_snapshot(metadata or {}),
            "updated_at": now,
            "events": [],
        }
        with self._lock:
            previous_runs = deepcopy(self._runs)
            previous_sequence = self._sequence
            previous_evicted_runs_count = self._evicted_runs_count
            previous_evicted_runs_by_repo = dict(self._evicted_runs_by_repo)
            previous_history_completeness_unknown = self._history_completeness_unknown
            previous_storage_file_seen = self._storage_file_seen
            current = self._runs.get(run_id)
            if current:
                current_owner = _entry_owner_repository(current)
                if owner_repository and current_owner and current_owner != owner_repository:
                    raise PermissionError("automation run_id belongs to another repository")
                current_state = str(current.get("task_state") or "").strip().lower()
                if (
                    _is_terminal_task_state(current_state)
                    and entry["task_state"] != current_state
                    and not _can_replace_stale_service_failure(current, entry)
                ):
                    return self._public_entry(current)
                entry["_ledger_sequence"] = current.get("_ledger_sequence", 0)
                old_events = list(current.get("events", []))
                entry["events"] = (
                    old_events[-(self._max_events_per_run - 1) :] if self._max_events_per_run > 1 else []
                )
                if not entry["task_name"]:
                    entry["task_name"] = str(current.get("task_name", ""))
                if not entry["metadata"]:
                    entry["metadata"] = deepcopy(current.get("metadata", {}))
                if not entry["suggested_action"]:
                    entry["suggested_action"] = str(current.get("suggested_action", ""))
                if _is_omitted(service_health):
                    entry["service_health"] = str(current.get("service_health", ""))
                if _is_omitted(quota_status):
                    entry["quota_status"] = str(current.get("quota_status", ""))
                if _is_omitted(org_health_status):
                    entry["org_health_status"] = str(current.get("org_health_status", ""))
            else:
                self._sequence += 1
                entry["_ledger_sequence"] = self._sequence
            entry["events"].append(
                {
                    "task_state": entry["task_state"],
                    "suggested_action": entry["suggested_action"],
                    "service_health": entry["service_health"],
                    "quota_status": entry["quota_status"],
                    "org_health_status": entry["org_health_status"],
                    "metadata": _event_metadata_snapshot(entry["metadata"]),
                    "recorded_at": now,
                }
            )
            self._runs[run_id] = entry
            if self._storage_path is None:
                self._evict_old_runs_locked()
            try:
                self._persist_with_owner_guard_locked(
                    guard_run_id=run_id,
                    owner_repository=owner_repository,
                    guard_preexisting=current is not None,
                )
            except Exception:
                self._runs = previous_runs
                self._sequence = previous_sequence
                self._evicted_runs_count = previous_evicted_runs_count
                self._evicted_runs_by_repo = previous_evicted_runs_by_repo
                self._history_completeness_unknown = previous_history_completeness_unknown
                self._storage_file_seen = previous_storage_file_seen
                raise
            return self._public_entry(self._runs[run_id])

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._refresh_from_disk_locked()
            entry = self._runs.get(run_id)
            return self._public_entry(entry) if entry else None

    def snapshot(self, *, limit: int | None = 100, include_events: bool = False) -> dict[str, Any]:
        """Return retained runs; ``limit=None`` returns the full retained ledger."""
        with self._lock:
            self._refresh_from_disk_locked()
            retained_runs = [
                self._public_entry(entry, include_events=include_events)
                for entry in self._runs.values()
            ]
            max_runs = self._max_runs
            max_events_per_run = self._max_events_per_run
            evicted_runs_count = self._evicted_runs_count
            evicted_runs_by_repo = dict(self._evicted_runs_by_repo)
            history_completeness_unknown = self._history_completeness_unknown

        task_states = Counter(str(run.get("task_state", "")).strip().lower() for run in retained_runs if run.get("task_state"))
        suggested_actions = Counter(
            str(run.get("suggested_action", "")).strip().lower()
            for run in retained_runs
            if run.get("suggested_action")
        )
        terminal_runs = sum(1 for run in retained_runs if str(run.get("task_state", "")).strip().lower() in TERMINAL_STATES)
        ordered_runs = sorted(
            retained_runs,
            key=lambda item: (float(item.get("updated_at", 0.0)), str(item.get("run_id", ""))),
            reverse=True,
        )
        if limit is not None:
            ordered_runs = ordered_runs[: max(0, int(limit))]
        runs = ordered_runs
        return {
            "runs": runs,
            "summary": {
                "total_runs": len(retained_runs),
                "returned_runs": len(runs),
                "active_runs": len(retained_runs) - terminal_runs,
                "terminal_runs": terminal_runs,
                "task_states": dict(task_states),
                "suggested_actions": dict(suggested_actions),
                "retention": {
                    "max_runs": max_runs,
                    "max_events_per_run": max_events_per_run,
                    "events_included": include_events,
                    "evicted_runs": evicted_runs_count,
                    "evicted_runs_by_repo": evicted_runs_by_repo,
                    "history_completeness_unknown": history_completeness_unknown,
                    "may_be_truncated": evicted_runs_count > 0,
                },
            },
        }


def default_automation_ledger_path() -> Path:
    configured = os.environ.get(AUTOMATION_LEDGER_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    base_dir = Path(
        os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR")
        or os.environ.get("CODEX_AUDIT_SERVICE_STATE_DIR")
        or (Path.home() / ".local/state/codex-audit-service")
    ).expanduser()
    return base_dir / "automation_runs.json"


_automation_run_ledger: AutomationRunLedger | None = None
_automation_run_ledger_path: Path | None = None


def get_automation_run_ledger() -> AutomationRunLedger:
    global _automation_run_ledger, _automation_run_ledger_path
    path = default_automation_ledger_path()
    if _automation_run_ledger is None or _automation_run_ledger_path != path:
        _automation_run_ledger = AutomationRunLedger(storage_path=path)
        _automation_run_ledger_path = path
    return _automation_run_ledger
