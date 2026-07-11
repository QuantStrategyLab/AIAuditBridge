#!/usr/bin/env python3
"""AiGateway — unified HTTP service for QuantStrategyLab AI calls.

Three endpoints, two adapters, one service.
Hardened with rate limiting, input validation, audit logging, and sandbox controls.

Endpoints:
    POST /v1/ai/analyze          sync  — LlmAdapter (Claude/GPT API)
    POST /v1/ai/execute/jobs     async — CodexAdapter (codex exec), poll via GET
    POST /v1/ai/review           sync  — LlmAdapter × N + optional CodexAdapter

Backward-compatible aliases:
    POST /v1/codex-audit         → /v1/ai/execute (sync path)
    POST /v1/codex-audit/jobs    → /v1/ai/execute/jobs (async path)
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import posixpath
import re
import secrets
import sys
import tempfile
import threading
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from service.auth import authenticate
from service.contracts import (
    MODE_REVIEW_AND_FIX,
    MODE_REVIEW_ONLY,
    TASK_ANALYZE,
    TASK_EXECUTE,
    TASK_REVIEW,
    parse_analyze_request,
    parse_execute_request,
    parse_review_request,
)
from service.adapters.llm_adapter import LlmAdapter, resolve_model
from service.adapters.codex_adapter import CodexAdapter
from service.autonomy import (
    ACTION_AUTO_PR,
    ACTION_RANK,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    classify_file_risk,
    load_autonomy_policy,
    recommended_action as compute_recommended_action,
)
from service.automation_authority import (
    CLASS_LIVE_EQUIVALENT_OPTIMIZATION,
    CLASS_LIVE_CANDIDATE_PROMOTION,
    CLASS_NEW_OR_RECONSTRUCTED_STRATEGY,
    CLASS_PLUGIN_POSITION_CONTROL,
    LIVE_EQUIVALENT_REQUIRED_EVIDENCE,
    evaluate_automation_authority,
)
from service.automation_run_ledger import (
    CONTROL_CONTINUE,
    CONTROL_ESCALATE,
    CONTROL_PAUSE_AUTO_FIX,
    CONTROL_REVIEW_ONLY,
    get_automation_run_ledger,
    suggest_control_action,
)
from service.automation_decision import EXECUTION_DEFER, EXECUTION_HUMAN_REVIEW, EXECUTION_REVIEW_ONLY, decide_automation_execution, load_execution_policy
from service.strategy_automation_registry import (
    apply_strategy_registry_guard,
    summarize_strategy_registry_context,
)
from service.feedback import (
    write_change,
    read_change,
    list_changes,
    evaluate_change,
    record_shadow_disagreement,
    get_shadow_disagreements,
    effectiveness_report,
    ChangeRecord,
    _new_change_id,
)
from service.quota import get_quota_manager
from service.task_state import TERMINAL_STATES, job_task_state
from service.health import get_health_monitor
from service.org_health import read_org_health
try:
    from quant_platform_kit.strategy_lifecycle.performance_monitor import try_record_platform_execution
except ModuleNotFoundError:  # pragma: no cover - optional in CI
    def try_record_platform_execution(*_args, **_kwargs):
        return None
from service.model_router import default_dual_review_model_for_reviewer

# ── constants ───────────────────────────────────────────────────────────

DEFAULT_AUDIENCE = "quant-codex-audit"
DEFAULT_MAX_REQUEST_BYTES = 2_000_000
DEFAULT_JOB_TTL_SECONDS = 86_400
DEFAULT_JOB_MAX_ACTIVE = 10
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
WRITE_AUTH_METHODS = frozenset({"github_oidc", "none"})
TRUSTED_AUTOMATION_PROOF_PATH_ENV = "CODEX_AUDIT_SERVICE_TRUSTED_AUTOMATION_PROOF_PATH"
DASHBOARD_REPOSITORIES_ENV = "CODEX_AUDIT_SERVICE_DASHBOARD_REPOSITORIES"


def _budget_gate_enabled() -> bool:
    """Keep the explicit unauthenticated local test mode backward compatible."""
    return not (
        os.environ.get("CODEX_AUDIT_SERVICE_AUTH", "").strip().lower() == "none"
        and os.environ.get("CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

# sandbox allowlist — restrict what callers can request
ALLOWED_SANDBOXES: frozenset[str] = frozenset(
    os.environ.get("CODEX_AUDIT_SERVICE_ALLOWED_SANDBOXES", "read-only").replace(" ", "").split(",")
)
DEFAULT_SANDBOX = "read-only"
CODEX_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
TASK_COMPLEXITY_LOW = "low"
TASK_COMPLEXITY_MEDIUM = "medium"
TASK_COMPLEXITY_HIGH = "high"
TASK_COMPLEXITY_LEVELS = (TASK_COMPLEXITY_LOW, TASK_COMPLEXITY_MEDIUM, TASK_COMPLEXITY_HIGH)

# rate limiting for analyze/review (sync endpoints that cost API $$)
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 30
_analyze_timestamps: list[float] = []

SERVICE_FAILURE_CATEGORY_PATTERN = re.compile(r"\[([a-z_]+_failure)\]")

# structured audit logger — writes JSON lines to stderr
_audit = logging.getLogger("ai_gateway.audit")
_audit.setLevel(logging.INFO)
_audit_handler = logging.StreamHandler(sys.stderr)
_audit_handler.setFormatter(logging.Formatter('{"logger":"ai_gateway.audit",%(jsonmsg)s}'))
_audit.addHandler(_audit_handler)
_audit.propagate = False

_JOB_WRITE_LOCK = threading.Lock()

# ── helpers ────────────────────────────────────────────────────────────


def _json_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _resolve_analyze_model(model: str) -> str:
    """Resolve the API-backed model used by the synchronous analyze endpoint."""
    _, resolved_model = resolve_model(model)
    return resolved_model


def _split_csv_env(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {item.strip() for item in re.split(r"[\n,]", raw) if item.strip()}


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _is_production() -> bool:
    return os.environ.get("CODEX_AUDIT_SERVICE_ENV", "").strip().lower() in {"production", "prod"}


def _normalize_complexity(value: str) -> str:
    level = (value or "").strip().lower()
    return level if level in TASK_COMPLEXITY_LEVELS else ""


def _normalize_reasoning_effort(value: str) -> str:
    effort = (value or "").strip().lower()
    if not effort or effort == "auto":
        return effort
    if effort not in CODEX_REASONING_EFFORTS:
        raise ValueError(
            f"reasoning_effort must be one of auto,{','.join(sorted(CODEX_REASONING_EFFORTS))}"
        )
    return effort


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _estimate_codex_complexity(payload: dict[str, Any]) -> str:
    prompt_chars = len(str(payload.get("prompt") or ""))
    changed_files = max(0, _as_int(payload.get("changed_files", 0)))
    changed_lines = max(0, _as_int(payload.get("changed_lines", 0)))
    if (
        changed_files >= _positive_int_env("AI_GATEWAY_COMPLEXITY_HIGH_THRESHOLD_FILES", 12)
        or changed_lines >= _positive_int_env("AI_GATEWAY_COMPLEXITY_HIGH_THRESHOLD_LINES", 1800)
        or prompt_chars >= _positive_int_env("AI_GATEWAY_COMPLEXITY_HIGH_THRESHOLD_PROMPT_CHARS", 18000)
    ):
        return TASK_COMPLEXITY_HIGH
    if (
        changed_files >= _positive_int_env("AI_GATEWAY_COMPLEXITY_MEDIUM_THRESHOLD_FILES", 4)
        or changed_lines >= _positive_int_env("AI_GATEWAY_COMPLEXITY_MEDIUM_THRESHOLD_LINES", 600)
        or prompt_chars >= _positive_int_env("AI_GATEWAY_COMPLEXITY_MEDIUM_THRESHOLD_PROMPT_CHARS", 7000)
    ):
        return TASK_COMPLEXITY_MEDIUM
    return TASK_COMPLEXITY_LOW


def _resolve_codex_reasoning_effort(payload: dict[str, Any], task: str) -> str:
    requested = _normalize_reasoning_effort(str(payload.get("reasoning_effort") or ""))
    if requested and requested != "auto":
        return requested
    configured = _normalize_reasoning_effort(os.environ.get("CODEX_AUDIT_SERVICE_REASONING_EFFORT", ""))
    if configured and configured != "auto":
        return configured
    complexity = _normalize_complexity(str(payload.get("complexity") or "")) or _estimate_codex_complexity(payload)
    task_key = re.sub(r"[^A-Z0-9]+", "_", (task or "").strip().upper()).strip("_")
    env_names: list[str] = []
    if task_key:
        env_names.extend([
            f"CODEX_AUDIT_SERVICE_{task_key}_{complexity.upper()}_REASONING_EFFORT",
            f"CODEX_AUDIT_SERVICE_{task_key}_REASONING_EFFORT",
        ])
    env_names.extend([
        f"CODEX_AUDIT_SERVICE_{complexity.upper()}_COMPLEXITY_REASONING_EFFORT",
        f"AI_GATEWAY_CODEX_{complexity.upper()}_COMPLEXITY_REASONING_EFFORT",
    ])
    for name in env_names:
        effort = _normalize_reasoning_effort(os.environ.get(name, ""))
        if effort and effort != "auto":
            return effort
    return {
        TASK_COMPLEXITY_LOW: "low",
        TASK_COMPLEXITY_MEDIUM: "medium",
        TASK_COMPLEXITY_HIGH: "high",
    }[complexity]


# ── rate limiter (sliding window) ──────────────────────────────────────


def _check_rate_limit(max_per_window: int = _RATE_LIMIT_MAX_REQUESTS, window: float = _RATE_LIMIT_WINDOW_SECONDS) -> None:
    """Sliding-window rate limiter for sync endpoints (analyze, review)."""
    global _analyze_timestamps
    now = time.time()
    _analyze_timestamps = [t for t in _analyze_timestamps if now - t < window]
    if len(_analyze_timestamps) >= max_per_window:
        raise PermissionError(
            f"rate limit exceeded: {max_per_window} requests per {window:.0f}s"
        )
    _analyze_timestamps.append(now)


def _classify_service_failure(message: str) -> str:
    text = message.lower()
    auth_config_signals = (
        "auth",
        "oidc",
        "secret",
        "credential",
        "permission denied",
        "forbidden",
        "not authorized",
        "invalid audience",
        "invalid token",
        "signature verification failed",
        "bearer token is required",
        "service bearer token is required",
        "workflow ref is not allowlisted",
        "repository is not allowlisted",
        "workflow repository is not allowlisted",
        "source repository is not allowlisted",
        "service repo is not allowlisted",
        "service ref is not allowlisted",
        "source repository is missing",
        "trusted automation proof is missing",
        "source_repository is required",
        "service token is missing",
        "service bearer token is missing",
        "secret is missing",
        "secret not configured",
    )
    if any(signal in text for signal in auth_config_signals):
        return "auth_or_config_failure"
    if any(word in text for word in ("quota", "rate limit", "too many active", "budget")):
        return "quota_or_capacity_failure"
    if any(word in text for word in ("timeout", "timed out", "temporarily", "unavailable", "connection", "network")):
        return "transient_service_failure"
    if any(word in text for word in ("json", "contract", "parse", "patch")):
        return "patch_contract_failure"
    return "unknown_failure"


def service_failure_category(message: str) -> str:
    match = SERVICE_FAILURE_CATEGORY_PATTERN.search(message)
    if match:
        return match.group(1)
    return _classify_service_failure(message)


# ── source_repository validation ───────────────────────────────────────


def _validate_source_repo(source_repository: str) -> None:
    """Validate that source_repository is in the service-side allowlist."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repository):
        raise ValueError("source_repository must be an owner/repository string")
    allowed = _split_csv_env("CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES")
    if allowed and source_repository not in allowed:
        raise PermissionError(
            f"source_repository {source_repository!r} is not allowed by "
            "CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES"
        )


def _validate_source_repo_org(claims: dict[str, Any], source_repository: str) -> None:
    """Ensure OIDC claims.repository org matches source_repository org.

    Prevents cross-org privilege escalation: a workflow from org-A cannot
    impersonate a source_repository from org-B.
    """
    claims_repo = str(claims.get("repository") or "")
    if not claims_repo:
        return  # local auth — skip check
    claims_org = claims_repo.split("/")[0]
    source_org = source_repository.split("/")[0]
    if source_org != claims_org:
        raise PermissionError(
            f"source_repository org {source_org!r} does not match "
            f"OIDC repository org {claims_org!r}"
        )


def _assert_write_authz(claims: dict[str, Any], path: str) -> None:
    auth_method = str(claims.get("auth_method") or "")
    if auth_method not in WRITE_AUTH_METHODS:
        raise PermissionError(f"{auth_method or 'unknown'} is not allowed to call {path}")


# ── sandbox validation ─────────────────────────────────────────────────


def _validate_sandbox(requested: str) -> str:
    """Validate and normalize sandbox value against service-side allowlist."""
    selected = (requested or os.environ.get("CODEX_AUDIT_SERVICE_SANDBOX", DEFAULT_SANDBOX)).strip()
    if not selected:
        selected = DEFAULT_SANDBOX
    if selected not in ALLOWED_SANDBOXES:
        raise ValueError(
            f"sandbox {selected!r} is not allowed. Allowed: {sorted(ALLOWED_SANDBOXES)}"
        )
    return selected


# ── static token validation (startup check) ────────────────────────────


def _validate_static_token_on_startup() -> None:
    """Enforce minimum complexity for CODEX_AUDIT_SERVICE_TOKEN."""
    token = os.environ.get("CODEX_AUDIT_SERVICE_TOKEN", "").strip()
    if not token:
        return
    if len(token) < 32:
        raise RuntimeError(
            "CODEX_AUDIT_SERVICE_TOKEN must be at least 32 characters. "
            f"Current length: {len(token)}"
        )


# ── audit logging ──────────────────────────────────────────────────────


def _audit_log(event: str, **fields: object) -> None:
    """Emit a structured audit log entry."""
    safe_fields = {k: v for k, v in fields.items() if v is not None}
    safe_fields["event"] = event
    safe_fields["timestamp"] = time.time()
    # Manually build JSON to avoid escaping issues with logging formatter
    msg_parts = ",".join(f'"{k}":{json.dumps(v)}' for k, v in sorted(safe_fields.items()))
    _audit.info("", extra={"jsonmsg": msg_parts})


# ── job store (file-based, for async execute) ──────────────────────────


def _job_dir() -> Path:
    default = Path(tempfile.gettempdir()) / "codex-audit-service-jobs"
    path = Path(os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR", str(default))).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,96}", job_id):
        raise ValueError("job_id is invalid")
    return _job_dir() / f"{job_id}.json"


def _now() -> float:
    return time.time()


def _new_job_id() -> str:
    return secrets.token_urlsafe(24)


def _write_job(job: dict[str, Any]) -> None:
    path = _job_path(str(job["job_id"]))
    payload = json.dumps(job, ensure_ascii=False, sort_keys=True).encode("utf-8")
    tmp = path.with_suffix(".json.tmp")
    with _JOB_WRITE_LOCK:
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def _read_job(job_id: str) -> dict[str, Any]:
    path = _job_path(job_id)
    if not path.exists():
        raise FileNotFoundError(job_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("job state is invalid")
    return payload


def _active_job_count() -> int:
    """Count jobs currently queued or running."""
    count = 0
    for path in _job_dir().glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("status") in {"queued", "running"}:
                count += 1
        except Exception:
            pass
    return count


def _cleanup_expired_jobs() -> None:
    now = _now()
    for path in _job_dir().glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expires_at = float(payload.get("expires_at") or 0)
        except Exception:
            expires_at = 0
        if expires_at and expires_at < now:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _recover_orphaned_jobs() -> int:
    """Fail persisted active jobs whose worker threads were lost on restart."""
    recovered = 0
    for path in _job_dir().glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        previous_status = str(job.get("status") or "")
        if previous_status not in ACTIVE_JOB_STATUSES:
            continue
        job["status"] = "failed"
        job["updated_at"] = _now()
        job["error"] = "codex audit service restarted before job completion"
        job["failure_category"] = "service_restart"
        _write_job(job)
        _record_job_automation_run(job)
        _settle_budget_reservation(job, 0.0)
        _audit_log(
            "job_failed",
            job_id=job.get("job_id"),
            error="service_restart",
            repository=job.get("repository"),
        )
        recovered += 1
    return recovered


def _mark_stale_job_failed(job: dict[str, Any]) -> dict[str, Any]:
    if job.get("status") not in {"queued", "running"}:
        return job
    timeout_seconds = int(job.get("timeout_seconds", 2700))
    updated_at = float(job.get("updated_at") or job.get("created_at") or 0)
    if updated_at and _now() <= updated_at + timeout_seconds + 120:
        return job
    job["status"] = "failed"
    job["updated_at"] = _now()
    job["error"] = "codex audit job became stale before completion"
    job["failure_category"] = "stale_job_timeout"
    _write_job(job)
    _record_job_automation_run(job)
    _settle_budget_reservation(job, 0.0)
    return job


def _settle_budget_reservation(payload: dict[str, Any], actual_cost: float = 0.0) -> None:
    reservation_id = str(payload.get("_budget_reservation_id") or "")
    if not reservation_id:
        return
    from service.ai_budget_guard import get_ai_budget_guard

    get_ai_budget_guard().settle(reservation_id, actual_cost)


def _assert_job_access(job: dict[str, Any], claims: dict[str, Any]) -> None:
    repository = str(claims.get("repository") or "")
    if repository != str(job.get("repository") or ""):
        raise PermissionError("job repository is not allowed")
    request_run_id = str(claims.get("run_id") or "")
    job_run_id = str(job.get("run_id") or "")
    if request_run_id and job_run_id and request_run_id != job_run_id:
        raise PermissionError("job run_id is not allowed")
    request_attempt = str(claims.get("run_attempt") or "")
    job_attempt = str(job.get("run_attempt") or "")
    if request_attempt and job_attempt and request_attempt != job_attempt:
        raise PermissionError("job run_attempt is not allowed")


def _public_job_payload(job: dict[str, Any]) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": str(job.get("status") or "unknown"),
        "job_id": str(job.get("job_id") or ""),
        "created_at": float(job.get("created_at") or 0),
        "updated_at": float(job.get("updated_at") or 0),
        "source_repository": str(job.get("source_repository") or ""),
        "task": str(job.get("task") or ""),
        "task_state": job_task_state(job),
    }
    if job.get("status") == "succeeded":
        payload["output"] = str(job.get("output") or "")
    if job.get("status") == "failed":
        payload["error"] = str(job.get("error") or "")
        payload["failure_category"] = str(job.get("failure_category") or "unknown_failure")
    return payload


def _automation_control_snapshot(
    repo: str,
    *,
    task_name: str = "",
    requested_mode: str = MODE_REVIEW_AND_FIX,
    pending_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        org_health = read_org_health()
    except Exception:
        org_health = {"status": "unavailable"}
    try:
        quota_status = get_quota_manager().runtime_status(repo or "unknown")
    except Exception:
        quota_status = {"status": "unavailable"}
    control = suggest_control_action(get_health_monitor().status, quota_status, org_health)
    try:
        ledger_snapshot = get_automation_run_ledger().snapshot(limit=None)
        recent_runs = ledger_snapshot["runs"]
        ledger_summary = ledger_snapshot.get("summary") if isinstance(ledger_snapshot.get("summary"), dict) else {}
        retention = ledger_summary.get("retention") if isinstance(ledger_summary.get("retention"), dict) else {}
        ledger_unavailable = False
    except Exception:
        recent_runs = []
        retention = {}
        ledger_unavailable = True
    if pending_run is not None:
        pending_run_id = str(pending_run.get("run_id") or "")
        if pending_run_id:
            recent_runs = [run for run in recent_runs if str(run.get("run_id") or "") != pending_run_id]
        recent_runs = [pending_run, *recent_runs]
    evicted_by_repo = (
        retention.get("evicted_runs_by_repo") if isinstance(retention.get("evicted_runs_by_repo"), dict) else {}
    )
    try:
        repo_evictions = int(evicted_by_repo.get(str(repo or "unknown").strip().lower(), 0) or 0)
    except (TypeError, ValueError):
        repo_evictions = 0
    storage_unavailable = bool(retention.get("storage_unavailable"))
    normalized_repo = str(repo or "unknown").strip().lower()
    repo_history_has_terminal_boundary = any(
        str(run.get("task_state") or "").strip().lower() in {"merged", "completed", "succeeded"}
        and _automation_run_owner_repository(run).strip().lower() == normalized_repo
        and str((run.get("metadata") if isinstance(run.get("metadata"), dict) else {}).get("origin") or "") in {"service_job", "external_workflow"}
        for run in recent_runs
        if isinstance(run, dict)
    )
    failure_history_complete = (
        not ledger_unavailable
        and not storage_unavailable
        and (not bool(retention.get("history_completeness_unknown")) or repo_history_has_terminal_boundary)
        and (repo_evictions <= 0 or repo_history_has_terminal_boundary)
    )
    execution = decide_automation_execution(
        repo=repo or "unknown",
        task_name=task_name,
        requested_mode=requested_mode,
        control_action=str(control.get("action") or CONTROL_REVIEW_ONLY),
        service_health=control.get("service_health"),
        quota_status=quota_status,
        org_health_status=control.get("org_health_status"),
        recent_runs=recent_runs,
        failure_history_complete=failure_history_complete,
        policy=load_execution_policy(),
    )
    if ledger_unavailable:
        execution["action"] = EXECUTION_HUMAN_REVIEW
        execution["effective_mode"] = MODE_REVIEW_ONLY
        execution["human_review_required"] = True
        execution["auto_fix_allowed"] = False
        execution["auto_merge_allowed"] = False
        execution["defer"] = False
        reasons = execution.get("reasons") if isinstance(execution.get("reasons"), list) else []
        reasons.append("automation ledger unavailable; forcing human review")
        execution["reasons"] = reasons
    original_action = str(control.get("action") or CONTROL_REVIEW_ONLY)
    strict_action = original_action
    if execution.get("action") == EXECUTION_HUMAN_REVIEW:
        strict_action = CONTROL_ESCALATE
    elif execution.get("action") == EXECUTION_REVIEW_ONLY:
        strict_action = CONTROL_REVIEW_ONLY
    elif execution.get("action") == EXECUTION_DEFER:
        strict_action = CONTROL_PAUSE_AUTO_FIX
    elif execution.get("action") == "run" and execution.get("auto_fix_allowed"):
        strict_action = CONTROL_CONTINUE
    elif execution.get("effective_mode") == MODE_REVIEW_ONLY and strict_action == CONTROL_CONTINUE:
        strict_action = CONTROL_REVIEW_ONLY
    action_rank = {CONTROL_CONTINUE: 0, CONTROL_PAUSE_AUTO_FIX: 1, CONTROL_REVIEW_ONLY: 2, CONTROL_ESCALATE: 3}
    if strict_action != CONTROL_CONTINUE and action_rank.get(strict_action, 2) < action_rank.get(original_action, 2):
        strict_action = original_action
    control["runtime_action"] = original_action
    control["effective_action"] = strict_action
    control["action"] = strict_action
    control["auto_fix_allowed"] = bool(execution.get("auto_fix_allowed")) and strict_action == CONTROL_CONTINUE
    control["auto_merge_allowed"] = bool(execution.get("auto_merge_allowed")) and strict_action == CONTROL_CONTINUE
    control["requires_human_review"] = strict_action != CONTROL_CONTINUE or bool(execution.get("human_review_required"))
    control["execution"] = execution
    return control


def _normalize_control_mode_param(value: str) -> str:
    mode = str(value or "").strip().lower()
    if not mode:
        return MODE_REVIEW_AND_FIX
    if mode in {MODE_REVIEW_ONLY, MODE_REVIEW_AND_FIX, "manual", "auto_pr", "auto_merge"}:
        return mode
    return ""


def _highest_changed_path_risk(changed_paths: list[str], policy: dict[str, Any]) -> str:
    if not changed_paths:
        return RISK_LOW
    rank = {RISK_LOW: 0, RISK_MEDIUM: 1, RISK_HIGH: 2, RISK_CRITICAL: 3}
    highest = RISK_LOW
    for path in changed_paths:
        risk = classify_file_risk(path, policy=policy)
        if rank.get(risk, 1) > rank.get(highest, 0):
            highest = risk
    return highest


def _automation_triage_snapshot(
    repo: str,
    *,
    task: str = "",
    requested_mode: str = MODE_REVIEW_AND_FIX,
    failure_category: str = "",
    error: str = "",
    changed_paths: list[str] | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    control = _automation_control_snapshot(repo, task_name=task, requested_mode=requested_mode)
    policy = load_autonomy_policy()
    normalized_paths = [
        normalized
        for path in (changed_paths or [])
        if isinstance(path, str)
        for normalized in (_normalize_changed_path(path),)
        if normalized
    ]
    path_risk = _highest_changed_path_risk(normalized_paths, policy)
    category = str(failure_category or "").strip().lower()
    if not category and error:
        category = service_failure_category(error)

    control_action = str(control.get("effective_action") or control.get("action") or CONTROL_REVIEW_ONLY)
    execution = control.get("execution") if isinstance(control.get("execution"), dict) else {}
    execution_auto_fix_allowed = bool(control.get("auto_fix_allowed")) and bool(execution.get("auto_fix_allowed"))
    retry_allowed = False
    deploy_allowed = False
    auto_fix_allowed = False
    human_review_required = bool(control.get("requires_human_review"))
    incident_class = "investigate"
    recommended_action = "open_issue"
    next_step = "open_issue"

    if category in {"auth_or_config_failure", "patch_contract_failure"}:
        incident_class = "blocked"
        recommended_action = "open_issue"
        next_step = "fix_config_or_contract"
    elif category == "quota_or_capacity_failure":
        incident_class = "retryable"
        retry_allowed = True
        recommended_action = "retry_later"
        next_step = "retry_after_quota_recovers"
    elif category == "transient_service_failure":
        incident_class = "retryable"
        retry_allowed = True
        recommended_action = "retry"
        next_step = "retry"
    else:
        if path_risk in {RISK_CRITICAL, RISK_HIGH} or control_action == CONTROL_ESCALATE:
            incident_class = "blocked"
            recommended_action = "escalate"
            next_step = "escalate"
        elif control_action == CONTROL_PAUSE_AUTO_FIX:
            incident_class = "degraded"
            recommended_action = "open_issue"
            next_step = "pause_auto_fix"
        elif control_action == CONTROL_CONTINUE and execution_auto_fix_allowed:
            incident_class = "investigate"
            recommended_action = "open_fix_pr" if path_risk in {RISK_LOW, RISK_MEDIUM} else "open_issue"
            next_step = "open_fix_pr" if path_risk in {RISK_LOW, RISK_MEDIUM} else "open_issue"
        else:
            incident_class = "review"
            recommended_action = "open_issue"
            next_step = "open_issue"

    if execution_auto_fix_allowed and category == "" and path_risk in {RISK_LOW, RISK_MEDIUM}:
        auto_fix_allowed = True
        deploy_allowed = True
    if path_risk in {RISK_HIGH, RISK_CRITICAL}:
        human_review_required = True
        deploy_allowed = False
        auto_fix_allowed = False
    if category:
        deploy_allowed = False
        auto_fix_allowed = False
        human_review_required = True

    summary_bits = [
        f"repo={repo or 'unknown'}",
        f"task={task or 'unknown'}",
        f"failure_category={category or 'none'}",
        f"control={control_action}",
        f"file_risk={path_risk}",
    ]
    if run_id:
        summary_bits.append(f"run_id={run_id}")

    error_excerpt = ""
    if error:
        error_excerpt = error.replace("`", "'").replace("\r", " ").replace("\n", " ")[:240]

    return {
        "repo": repo or "unknown",
        "task": task or "",
        "failure_category": category,
        "incident_class": incident_class,
        "file_risk": path_risk,
        "control": control,
        "auto_fix_allowed": auto_fix_allowed,
        "retry_allowed": retry_allowed,
        "deploy_allowed": deploy_allowed,
        "human_review_required": human_review_required,
        "recommended_action": recommended_action,
        "next_step": next_step,
        "summary": "; ".join(summary_bits),
        "evidence": {
            "service_health": str(get_health_monitor().status or ""),
            "quota_status": str(control.get("quota_status") or ""),
            "org_health_status": str(control.get("org_health_status") or ""),
            "error_excerpt": error_excerpt,
            "changed_paths": normalized_paths,
        },
    }


def _record_job_automation_run(job: dict[str, Any]) -> None:
    try:
        repo = str(job.get("source_repository") or job.get("repository") or "unknown")
        task_name = str(job.get("task") or "")
        task_state = job_task_state(job)
        metadata = {
            "origin": "service_job",
            "repository": repo,
            "source_repository": str(job.get("source_repository") or ""),
            "caller_repository": str(job.get("repository") or ""),
            "source_ref": str(job.get("source_ref") or ""),
            "mode": str(job.get("mode") or ""),
            "failure_category": str(job.get("failure_category") or ""),
        }
        control = _automation_control_snapshot(
            repo,
            task_name=task_name,
            requested_mode=str(job.get("mode") or MODE_REVIEW_AND_FIX),
            pending_run={
                "run_id": str(job.get("job_id") or ""),
                "task_name": task_name,
                "task_state": task_state,
                "metadata": metadata,
            },
        )
        get_automation_run_ledger().record(
            str(job.get("job_id") or ""),
            task_state,
            task_name=task_name,
            suggested_action=str(control.get("effective_action") or control.get("action") or ""),
            service_health=str(control.get("service_health") or ""),
            quota_status=str(control.get("quota_status") or ""),
            org_health_status=str(control.get("org_health_status") or ""),
            metadata=metadata,
            owner_repository=repo,
        )
    except Exception as exc:
        _audit_log("automation_ledger_record_failed", job_id=job.get("job_id"), error=type(exc).__name__)


def _automation_operator_claims(claims: dict[str, Any]) -> bool:
    if claims.get("automation_operator") is True or claims.get("operator") is True:
        return True
    if str(claims.get("auth_method") or "") == "static_token":
        return False
    scopes = claims.get("scopes") or claims.get("scope") or ""
    if isinstance(scopes, str):
        scope_values = {item.strip() for item in re.split(r"[\s,]", scopes) if item.strip()}
    elif isinstance(scopes, list):
        scope_values = {str(item).strip() for item in scopes if str(item).strip()}
    else:
        scope_values = set()
    if "automation_operator" in scope_values:
        return True
    allowed_repositories = _split_csv_env("CODEX_AUDIT_SERVICE_AUTOMATION_OPERATOR_REPOSITORIES")
    repository = str(claims.get("repository") or "")
    return bool(repository) and repository in allowed_repositories


def _dashboard_repository_allowed(claims: dict[str, Any], repository: str) -> bool:
    if str(claims.get("auth_method") or "") != "static_token":
        return False
    allowed_repositories = _split_csv_env(DASHBOARD_REPOSITORIES_ENV)
    return bool(repository) and repository in allowed_repositories


def _automation_run_owner_repository(run: dict[str, Any]) -> str:
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    return str(metadata.get("source_repository") or metadata.get("repository") or "")


def _normalize_changed_path(path: str) -> str:
    raw_path = str(path).strip().replace("\\", "/")
    if not raw_path:
        return ""
    if raw_path.startswith("/") or re.match(r"^[A-Za-z]:/", raw_path):
        raise ValueError("changed_paths must be clean repo-relative paths")
    parts = [part for part in raw_path.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError("changed_paths must not contain parent-directory segments")
    normalized = posixpath.normpath("/".join(parts))
    if normalized in {"", "."}:
        return ""
    return normalized


def _strategy_profile_required_for_paths(changed_paths: list[str], policy: dict[str, Any]) -> bool:
    return any(classify_file_risk(path.lower(), policy=policy) == RISK_HIGH for path in changed_paths)


def _strategy_profile_required_for_class(change_class: str, changed_paths: list[str], policy: dict[str, Any]) -> bool:
    profile_optional_paths = {"web/strategy-switch-console/strategy-profiles.example.json"}
    protected_classes = {
        CLASS_LIVE_EQUIVALENT_OPTIMIZATION,
        CLASS_LIVE_CANDIDATE_PROMOTION,
        CLASS_NEW_OR_RECONSTRUCTED_STRATEGY,
        CLASS_PLUGIN_POSITION_CONTROL,
    }
    if change_class in protected_classes:
        if change_class == CLASS_LIVE_CANDIDATE_PROMOTION and changed_paths:
            return not all(path.lower() in profile_optional_paths for path in changed_paths)
        return True
    return _strategy_profile_required_for_paths(changed_paths, policy)


def _assert_source_repository_owner_or_operator(claims: dict[str, Any], source_repository: str) -> None:
    if not source_repository or _automation_operator_claims(claims):
        return
    if source_repository != str(claims.get("repository") or ""):
        raise PermissionError("source_repository must match authenticated repository")


def _automation_run_access_allowed(
    run: dict[str, Any],
    claims: dict[str, Any],
) -> bool:
    if _automation_operator_claims(claims):
        return True
    owner = _automation_run_owner_repository(run)
    caller_repository = str(claims.get("repository") or "")
    return bool(owner) and (owner == caller_repository or _dashboard_repository_allowed(claims, owner))


def _assert_automation_run_access(
    run: dict[str, Any],
    claims: dict[str, Any],
) -> None:
    if not _automation_run_access_allowed(run, claims):
        raise PermissionError("automation run repository is not allowed")


def _automation_snapshot_for_claims(
    snapshot: dict[str, Any],
    claims: dict[str, Any],
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    if _automation_operator_claims(claims):
        return snapshot
    visible_runs = [run for run in snapshot.get("runs", []) if _automation_run_access_allowed(run, claims)]
    runs = visible_runs[: max(0, int(limit))] if limit is not None else visible_runs
    task_states = Counter(str(run.get("task_state", "")).strip().lower() for run in visible_runs if run.get("task_state"))
    suggested_actions = Counter(
        str(run.get("suggested_action", "")).strip().lower()
        for run in visible_runs
        if run.get("suggested_action")
    )
    terminal_runs = sum(1 for run in visible_runs if str(run.get("task_state", "")).strip().lower() in TERMINAL_STATES)
    summary = dict(snapshot.get("summary") or {})
    retention = summary.get("retention")
    if isinstance(retention, dict):
        summary["retention"] = {key: value for key, value in retention.items() if key != "evicted_runs_by_repo"}
    summary.update(
        {
            "total_runs": len(visible_runs),
            "returned_runs": len(runs),
            "active_runs": len(visible_runs) - terminal_runs,
            "terminal_runs": terminal_runs,
            "task_states": dict(task_states),
            "suggested_actions": dict(suggested_actions),
        }
    )
    return {"runs": runs, "summary": summary}


def _trusted_automation_proof_for_review(payload: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    proof_path = os.environ.get(TRUSTED_AUTOMATION_PROOF_PATH_ENV, "").strip()
    proof_id = str(payload.get("trusted_proof_id") or "")
    prompt = str(payload.get("prompt") or "")
    commit_sha = str(payload.get("commit_sha") or payload.get("source_sha") or "")
    diff_hash = str(payload.get("diff_hash") or "")
    base_ref, base_sha = str(payload.get("base_ref") or ""), str(payload.get("base_sha") or "")
    review_context_id = str(payload.get("pull_request_number") or payload.get("pr_number") or payload.get("issue_number") or "")
    changed_paths_raw = payload.get("changed_paths")
    if not proof_path or not proof_id or not prompt or not commit_sha or not diff_hash or not base_ref or not base_sha or not review_context_id:
        return {}
    payload_source_repository = str(payload.get("source_repository") or "")
    if not payload_source_repository:
        raise PermissionError("trusted automation proof requires source_repository")
    _validate_source_repo(payload_source_repository)
    if not _automation_operator_claims(claims):
        _validate_source_repo_org(claims, payload_source_repository)
        _assert_source_repository_owner_or_operator(claims, payload_source_repository)
    if not isinstance(changed_paths_raw, list):
        return {}
    requested_paths = sorted(str(path) for path in changed_paths_raw if isinstance(path, str))
    if not requested_paths:
        return {}
    try:
        proof_payload = json.loads(Path(proof_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    proofs = proof_payload.get("proofs") if isinstance(proof_payload, dict) else None
    if not isinstance(proofs, list):
        return {}
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    for proof in proofs:
        if not isinstance(proof, dict):
            continue
        if str(proof.get("proof_id") or "") != proof_id:
            continue
        if str(proof.get("prompt_sha256") or "") != prompt_sha256:
            continue
        proof_source_repository = str(proof.get("source_repository") or "")
        if not proof_source_repository or payload_source_repository != proof_source_repository:
            continue
        proof_commit_sha = str(proof.get("commit_sha") or proof.get("source_sha") or "")
        if proof_commit_sha != commit_sha or str(proof.get("diff_hash") or "") != diff_hash:
            continue
        proof_context_id = str(proof.get("pull_request_number") or proof.get("pr_number") or proof.get("issue_number") or "")
        if str(proof.get("base_ref") or "") != base_ref or str(proof.get("base_sha") or "") != base_sha or proof_context_id != review_context_id:
            continue
        proof_paths_raw = proof.get("changed_paths")
        if not isinstance(proof_paths_raw, list):
            continue
        proof_paths = sorted(str(path) for path in proof_paths_raw if isinstance(path, str))
        if proof_paths != requested_paths:
            continue
        metadata = proof.get("trusted_automation_metadata")
        if not isinstance(metadata, dict):
            continue
        if metadata.get("change_class") != CLASS_LIVE_EQUIVALENT_OPTIMIZATION:
            continue
        if not all(metadata.get(key) is True for key in LIVE_EQUIVALENT_REQUIRED_EVIDENCE):
            continue
        return {
            "source_repository": proof_source_repository,
            "commit_sha": str(proof.get("commit_sha") or proof.get("source_sha") or ""),
            "diff_hash": str(proof.get("diff_hash") or ""),
            "changed_paths": proof_paths,
            "trusted_automation_metadata": dict(metadata),
        }
    return {}


def _job_dedupe_key(
    payload: dict[str, Any],
    *,
    repository: str,
    run_id: str,
    run_attempt: str,
) -> str:
    issue_number = str(payload.get("issue_number") or "").strip()
    prompt_hash = hashlib.sha256(str(payload.get("prompt") or "").encode("utf-8")).hexdigest()
    parts = [
        repository,
        run_id,
        run_attempt,
        str(payload.get("source_repository") or ""),
        str(payload.get("source_ref") or ""),
        str(payload.get("task") or TASK_EXECUTE),
        str(payload.get("mode") or MODE_REVIEW_ONLY),
        issue_number or prompt_hash,
    ]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("utf-8")).hexdigest()


def _classify_codex_exec_failure(text: str) -> str:
    if any(word in text for word in ("quota", "rate limit", "too many active", "budget")):
        return "quota_or_capacity_failure"
    return "unknown_failure"


def _classify_failure(error: str) -> str:
    text = error.lower()
    if "codex exec failed" in text:
        return _classify_codex_exec_failure(text)
    auth_config_signals = (
        "permission denied",
        "unauthorized",
        "forbidden",
        "oidc",
        "missing bearer",
        "missing token",
        "invalid token",
        "bad credentials",
        "not allowed",
        "allowlist",
        "api key is required",
        "no api key configured",
        "secret is missing",
        "secret not configured",
    )
    if any(signal in text for signal in auth_config_signals):
        return "auth_or_config_failure"
    if any(word in text for word in ("quota", "rate limit", "too many active", "budget")):
        return "quota_or_capacity_failure"
    if any(word in text for word in ("timeout", "timed out", "temporarily", "unavailable", "connection", "network")):
        return "transient_service_failure"
    if any(word in text for word in ("json", "contract", "parse", "patch")):
        return "patch_contract_failure"
    return "unknown_failure"


def _find_active_job_by_dedupe_key(dedupe_key: str) -> dict[str, Any] | None:
    if os.environ.get("CODEX_AUDIT_SERVICE_DEDUPE_JOBS", "true").strip().lower() in {"0", "false", "no", "off"}:
        return None
    for path in _job_dir().glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("dedupe_key") == dedupe_key and job.get("status") in ACTIVE_JOB_STATUSES:
            return _mark_stale_job_failed(job)
    return None


def _run_job(job_id: str, payload: dict[str, Any]) -> None:
    started = time.time()
    try:
        job = _read_job(job_id)
        job["status"] = "running"
        job["updated_at"] = _now()
        _write_job(job)
        _record_job_automation_run(job)
        adapter = CodexAdapter()
        sandbox = _validate_sandbox(str(payload.get("sandbox") or ""))
        reasoning_effort = _resolve_codex_reasoning_effort(payload, str(payload.get("task") or TASK_EXECUTE))
        result = adapter.execute(
            prompt=str(payload["prompt"]),
            sandbox=sandbox,
            model=str(payload.get("model") or "").strip() or None,
            reasoning_effort=reasoning_effort,
            timeout=int(payload.get("timeout_seconds", 2700)),
        )
        job = _read_job(job_id)
        if result.success:
            job["status"] = "succeeded"
            job["output"] = result.output
            job.pop("error", None)
        else:
            job["status"] = "failed"
            job["error"] = result.error
            job["failure_category"] = _classify_failure(result.error)
        job["updated_at"] = _now()
        _write_job(job)
        _record_job_automation_run(job)
        get_health_monitor().record(
            "/v1/ai/execute/jobs/run",
            time.time() - started,
            job["status"] == "succeeded",
            str(job.get("failure_category") or job.get("error") or ""),
        )
        _record_platform_execution_telemetry(
            str(job.get("strategy_profile") or job.get("task") or job.get("source_repository") or job.get("repository") or ""),
            {
                "job_id": job_id,
                "status": job["status"],
                "task": str(job.get("task") or ""),
                "source_repository": str(job.get("source_repository") or job.get("repository") or ""),
                "model": str(payload.get("model") or ""),
                "reasoning_effort": str(reasoning_effort or ""),
                "output": str(job.get("output") or ""),
                "error": str(job.get("error") or ""),
            },
            domain=str(job.get("domain") or ""),
        )
        _settle_budget_reservation(job, 0.10 if job.get("status") == "succeeded" else 0.0)
        _audit_log("job_completed", job_id=job_id, status=job["status"],
                   repository=job.get("repository"), task=job.get("task"))
    except Exception as exc:
        try:
            job = _read_job(job_id)
        except Exception:
            job = {"job_id": job_id, "created_at": _now()}
        job["status"] = "failed"
        job["updated_at"] = _now()
        job["error"] = str(exc)[-4000:]
        job["failure_category"] = _classify_failure(str(exc))
        _write_job(job)
        _record_job_automation_run(job)
        get_health_monitor().record("/v1/ai/execute/jobs/run", time.time() - started, False, type(exc).__name__)
        _record_platform_execution_telemetry(
            str(job.get("strategy_profile") or job.get("task") or job.get("source_repository") or job.get("repository") or ""),
            {
                "job_id": job_id,
                "status": "failed",
                "task": str(job.get("task") or ""),
                "source_repository": str(job.get("source_repository") or job.get("repository") or ""),
                "error": str(job.get("error") or str(exc)),
            },
            domain=str(job.get("domain") or ""),
        )
        _settle_budget_reservation(job, 0.0)
        _audit_log("job_failed", job_id=job_id, error=type(exc).__name__,
                   repository=job.get("repository"))


def _submit_job(claims: dict[str, Any], payload: dict[str, Any]) -> dict[str, object]:
    _cleanup_expired_jobs()
    dedupe_key = _job_dedupe_key(
        payload,
        repository=str(claims.get("repository") or ""),
        run_id=str(claims.get("run_id") or ""),
        run_attempt=str(claims.get("run_attempt") or ""),
    )
    existing_job = _find_active_job_by_dedupe_key(dedupe_key)
    if existing_job is not None and existing_job.get("status") in ACTIVE_JOB_STATUSES:
        public = _public_job_payload(existing_job)
        public["deduped"] = True
        return public

    # check active job cap
    max_active = _positive_int_env("CODEX_AUDIT_SERVICE_MAX_ACTIVE_JOBS", DEFAULT_JOB_MAX_ACTIVE)
    if _active_job_count() >= max_active:
        raise PermissionError(
            f"too many active jobs: max {max_active}. Wait for existing jobs to complete."
        )

    now = _now()
    ttl_seconds = int(os.environ.get("CODEX_AUDIT_SERVICE_JOB_TTL_SECONDS", str(DEFAULT_JOB_TTL_SECONDS)))
    job_id = _new_job_id()
    job = {
        "job_id": job_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + ttl_seconds,
        "repository": str(claims.get("repository") or ""),
        "run_id": str(claims.get("run_id") or ""),
        "run_attempt": str(claims.get("run_attempt") or ""),
        "actor": str(claims.get("actor") or ""),
        "source_repository": str(payload.get("source_repository") or ""),
        "source_ref": str(payload.get("source_ref") or ""),
        "task": str(payload.get("task") or TASK_EXECUTE),
        "mode": str(payload.get("mode") or MODE_REVIEW_ONLY),
        "timeout_seconds": int(payload.get("timeout_seconds", 2700)),
        "dedupe_key": dedupe_key,
        "_budget_reservation_id": str(payload.get("_budget_reservation_id") or ""),
    }
    _write_job(job)
    _record_job_automation_run(job)
    _audit_log("job_submitted", job_id=job_id, repository=job["repository"],
               task=job["task"], source_repository=job["source_repository"])
    thread = threading.Thread(target=_run_job, args=(job_id, payload), name=f"ai-gateway-job-{job_id}", daemon=True)
    thread.start()
    return _public_job_payload(job)


# ── request dispatcher ─────────────────────────────────────────────────


class AiGatewayRequestHandler(BaseHTTPRequestHandler):
    server_version = "AiGateway/2.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Suppress default http.server log — we use structured audit logging instead."""
        pass

    # -- GET --

    def do_GET(self) -> None:
        from urllib.parse import urlparse
        request_path = urlparse(self.path).path

        if request_path == "/healthz":
            health = get_health_monitor()
            _json_response(self, HTTPStatus.OK, {
                "status": health.status,
                "uptime_seconds": health.uptime_seconds,
            })
            return
        if request_path == "/v1/ai/health":
            try:
                claims = authenticate(self.headers, audience=DEFAULT_AUDIENCE)
            except PermissionError as exc:
                _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
                return
            health = get_health_monitor()
            _json_response(self, HTTPStatus.OK, {"status": "ok", **health.snapshot()})
            return
        if request_path == "/v1/ai/org-health":
            try:
                authenticate(self.headers, audience=DEFAULT_AUDIENCE)
            except PermissionError as exc:
                _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
                return
            org_health = read_org_health()
            _json_response(self, HTTPStatus.OK, org_health)
            return
        if request_path == "/v1/ai/quota":
            self._handle_quota_status()
            return
        if request_path == "/v1/ai/automation/control":
            self._handle_automation_control()
            return
        if request_path == "/v1/ai/automation/runs":
            self._handle_list_automation_runs()
            return
        if request_path.startswith("/v1/ai/automation/runs/"):
            run_id = request_path[len("/v1/ai/automation/runs/"):]
            self._handle_get_automation_run(run_id)
            return

        # Feedback: list changes or get effectiveness report
        if request_path == "/v1/ai/changes/effectiveness":
            self._handle_effectiveness()
            return
        if request_path.startswith("/v1/ai/changes/"):
            change_id = request_path[len("/v1/ai/changes/"):]
            self._handle_get_change(change_id)
            return
        if request_path.startswith("/v1/ai/changes"):
            self._handle_list_changes()
            return
        if request_path == "/v1/ai/feedback/shadow":
            self._handle_get_shadow()
            return

        # async job polling: /v1/ai/execute/jobs/{id}  or  /v1/codex-audit/jobs/{id}
        for prefix in ("/v1/ai/execute/jobs/", "/v1/codex-audit/jobs/"):
            if request_path.startswith(prefix):
                job_id = request_path[len(prefix):]
                if not re.fullmatch(r"[A-Za-z0-9_-]{24,96}", job_id):
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": "invalid job_id"})
                    return
                try:
                    claims = authenticate(self.headers, audience=DEFAULT_AUDIENCE)
                    job = _mark_stale_job_failed(_read_job(job_id))
                    _assert_job_access(job, claims)
                    _json_response(self, HTTPStatus.OK, _public_job_payload(job))
                except FileNotFoundError:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "job not found"})
                except PermissionError as exc:
                    _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
                except ValueError as exc:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": str(exc)})
                except Exception as exc:
                    _audit_log("error", path=self.path, error=type(exc).__name__)
                    _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "error": "internal error"})
                return

        _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "not found"})

    # -- POST --

    def do_POST(self) -> None:
        try:
            claims = authenticate(self.headers, audience=DEFAULT_AUDIENCE)
            payload = self._read_payload()

            repository = str(claims.get("repository") or "")
            run_id = str(claims.get("run_id") or "")
            source_repo = str(payload.get("source_repository") or "")
            task = str(payload.get("task") or TASK_ANALYZE)

            _audit_log("request", path=self.path, repository=repository,
                       run_id=run_id, source_repository=source_repo, task=task)

            # Route by path
            if self.path in {"/v1/ai/analyze"}:
                _assert_write_authz(claims, self.path)
                self._handle_analyze(payload)
            elif self.path in {"/v1/ai/feedback/register"}:
                _assert_write_authz(claims, self.path)
                self._handle_feedback_register(claims, payload)
            elif self.path in {"/v1/ai/feedback/evaluate"}:
                _assert_write_authz(claims, self.path)
                self._handle_feedback_evaluate(payload)
            elif self.path in {"/v1/ai/feedback/shadow"}:
                _assert_write_authz(claims, self.path)
                self._handle_feedback_shadow(payload)
            elif self.path in {"/v1/ai/execute/jobs", "/v1/codex-audit/jobs"}:
                _assert_write_authz(claims, self.path)
                self._handle_execute_async(claims, payload)
            elif self.path in {"/v1/ai/automation/runs"}:
                _assert_write_authz(claims, self.path)
                self._handle_record_automation_run(claims, payload)
            elif self.path in {"/v1/ai/automation/authority"}:
                _assert_write_authz(claims, self.path)
                self._handle_automation_authority(claims, payload)
            elif self.path in {"/v1/ai/automation/triage"}:
                _assert_write_authz(claims, self.path)
                self._handle_automation_triage(claims, payload)
            elif self.path in {"/v1/ai/execute", "/v1/codex-audit"}:
                _assert_write_authz(claims, self.path)
                self._handle_execute_sync(claims, payload)
            elif self.path in {"/v1/ai/review"}:
                _assert_write_authz(claims, self.path)
                self._handle_review(claims, payload)
            else:
                _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "not found"})

        except PermissionError as exc:
            _audit_log("auth_error", path=self.path, error=str(exc)[:200])
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
        except ValueError as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": str(exc)})
        except Exception as exc:
            _audit_log("error", path=self.path, error=type(exc).__name__, detail=str(exc)[:500])
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "error": "internal error"})

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        max_bytes = int(os.environ.get("CODEX_AUDIT_SERVICE_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES)))
        if length <= 0:
            raise ValueError("request body is empty")
        if length > max_bytes:
            raise ValueError("request body is too large")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    # -- endpoint handlers --

    def _handle_analyze(self, payload: dict[str, Any]) -> None:
        """POST /v1/ai/analyze — sync LLM completion via LlmAdapter."""
        _check_rate_limit()
        req = parse_analyze_request(payload)
        source_repo = str(payload.get("source_repository") or "unknown")

        # Quota check
        quota = get_quota_manager()
        resolved_model = _resolve_analyze_model(req.model)
        qr = quota.check(
            source_repo, resolved_model, req.prompt, task_class="research",
            budget_guard=_budget_gate_enabled(),
        )
        if not qr["allowed"]:
            _json_response(self, HTTPStatus.TOO_MANY_REQUESTS, {
                "status": "deferred_budget",
                "error": qr["reason"],
                "recommended_model": qr.get("recommended_model", ""),
                "remaining_usd": qr.get("remaining_usd", 0),
                "budget_decision": qr.get("budget_decision", {}),
            })
            return

        started = time.time()
        adapter = LlmAdapter()
        result = adapter.complete(
            model=resolved_model, system=req.system, user=req.prompt,
            max_tokens=req.max_tokens, timeout=req.timeout_seconds,
        )
        latency = time.time() - started

        # Record quota and health
        quota.record(source_repo, resolved_model, req.prompt, result.output if result.success else "")
        reservation_id = str(qr.get("budget_reservation_id") or "")
        if reservation_id:
            from service.ai_budget_guard import get_ai_budget_guard

            get_ai_budget_guard().settle(
                reservation_id,
                float(qr.get("cost_estimate_usd") or 0.0) if result.success else 0.0,
            )
        get_health_monitor().record("/v1/ai/analyze", latency, result.success, result.error if not result.success else "")

        _audit_log("analyze_completed", model=result.model, provider=result.provider,
                   success=result.success, latency=result.latency_seconds)
        if result.success:
            _json_response(self, HTTPStatus.OK, {
                "status": "ok", "output": result.output,
                "model": result.model, "provider": result.provider,
                "latency_seconds": result.latency_seconds,
                "cost_estimate_usd": qr.get("cost_estimate_usd", 0),
            })
        else:
            _json_response(self, HTTPStatus.BAD_GATEWAY, {
                "status": "error", "error": result.error,
                "model": result.model, "provider": result.provider,
            })

    def _handle_execute_async(self, claims: dict[str, Any], payload: dict[str, Any]) -> None:
        """POST /v1/ai/execute/jobs — async Codex execution."""
        started = time.time()
        req = parse_execute_request(payload)

        # Security: validate source_repository against allowlist
        source_repo = str(payload.get("source_repository") or "")
        if source_repo:
            _validate_source_repo(source_repo)
            _validate_source_repo_org(claims, source_repo)
        quota_repo = source_repo or str(claims.get("repository") or "unknown")

        # Quota check
        quota = get_quota_manager()
        qr = quota.check(
            quota_repo, "codex-cli", req.prompt, codex_account=True,
            task_class="auto_fix" if str(payload.get("task") or "").lower() in {"auto_fix", "autofix"} else "maintenance",
        )
        if not qr["allowed"]:
            _json_response(self, HTTPStatus.TOO_MANY_REQUESTS, {
                "status": "deferred_budget", "error": qr["reason"],
                "remaining_usd": qr.get("remaining_usd", 0),
                "budget_decision": qr.get("budget_decision", {}),
            })
            return
        quota.record_execute(quota_repo)

        payload.setdefault("task", TASK_EXECUTE)
        reservation_id = str(qr.get("budget_reservation_id") or "")
        if reservation_id:
            payload["_budget_reservation_id"] = reservation_id
        try:
            job = _submit_job(claims, payload)
        except Exception:
            if reservation_id:
                from service.ai_budget_guard import get_ai_budget_guard

                get_ai_budget_guard().settle(reservation_id, 0.0)
            raise
        get_health_monitor().record("/v1/ai/execute/jobs", time.time() - started, True)
        _json_response(self, HTTPStatus.ACCEPTED, job)

    def _handle_execute_sync(self, claims: dict[str, Any], payload: dict[str, Any]) -> None:
        """POST /v1/ai/execute — sync Codex execution (backward compat)."""
        started = time.time()
        req = parse_execute_request(payload)
        source_repo = str(payload.get("source_repository") or "")
        if source_repo:
            _validate_source_repo(source_repo)
            _validate_source_repo_org(claims, source_repo)
        quota_repo = source_repo or str(claims.get("repository") or "unknown")
        quota = get_quota_manager()
        qr = quota.check(
            quota_repo, "codex-cli", req.prompt, codex_account=True,
            task_class="auto_fix" if str(payload.get("task") or "").lower() in {"auto_fix", "autofix"} else "maintenance",
        )
        if not qr["allowed"]:
            _json_response(self, HTTPStatus.TOO_MANY_REQUESTS, {
                "status": "deferred_budget", "error": qr["reason"],
                "remaining_usd": qr.get("remaining_usd", 0),
                "budget_decision": qr.get("budget_decision", {}),
            })
            return
        quota.record_execute(quota_repo)
        adapter = CodexAdapter()
        sandbox = _validate_sandbox(str(payload.get("sandbox") or ""))
        reasoning_effort = _resolve_codex_reasoning_effort(payload, str(payload.get("task") or TASK_EXECUTE))
        reservation_id = str(qr.get("budget_reservation_id") or "")
        try:
            result = adapter.execute(
                prompt=req.prompt,
                sandbox=sandbox,
                model=req.model or None,
                reasoning_effort=reasoning_effort,
                timeout=req.timeout_seconds,
            )
        except Exception:
            if reservation_id:
                from service.ai_budget_guard import get_ai_budget_guard

                get_ai_budget_guard().release(reservation_id)
            raise
        if reservation_id:
            from service.ai_budget_guard import get_ai_budget_guard

            get_ai_budget_guard().settle(reservation_id, 0.10 if result.success else 0.0)
        get_health_monitor().record("/v1/ai/execute", time.time() - started, result.success, result.error if not result.success else "")
        if result.success:
            _json_response(self, HTTPStatus.OK, {"status": "ok", "output": result.output})
        else:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "error": result.error})

    def _handle_review(self, claims: dict[str, Any], payload: dict[str, Any]) -> None:
        """POST /v1/ai/review — multi-model parallel review + optional Codex verify.

        Returns a ``recommended_action`` based on AI confidence scores and file risk tiers.
        """
        _check_rate_limit()
        req = parse_review_request(payload)
        llm = LlmAdapter()
        codex = CodexAdapter()

        # Collect changed_paths from payload for risk classification
        changed_paths_raw = payload.get("changed_paths")
        changed_paths: list[str] = (
            [str(p) for p in changed_paths_raw if isinstance(p, str)]
            if isinstance(changed_paths_raw, list) else []
        )
        trusted_proof = _trusted_automation_proof_for_review(payload, claims)
        if trusted_proof:
            changed_paths = list(trusted_proof["changed_paths"])

        # Step 1: parallel LLM review
        reviewer_tuples = [
            (r, req.model) if req.model else (r, _default_model_for_reviewer(r))
            for r in req.reviewers
        ]
        _audit_log("review_started", reviewers=req.reviewers, verifier=req.verifier,
                   changed_paths_count=len(changed_paths))
        review_repo = str(payload.get("source_repository") or "unknown")
        review_reservations: list[tuple[str, float]] = []
        for reviewer_name, reviewer_model in reviewer_tuples:
            review_quota = get_quota_manager().check(
                review_repo,
                reviewer_model,
                req.prompt,
                task_class="review",
                budget_guard=_budget_gate_enabled(),
            )
            if not review_quota.get("allowed"):
                if review_reservations:
                    from service.ai_budget_guard import get_ai_budget_guard

                    for reservation_id, _cost in review_reservations:
                        get_ai_budget_guard().release(reservation_id)
                _json_response(self, HTTPStatus.TOO_MANY_REQUESTS, {
                    "status": "deferred_budget",
                    "error": review_quota.get("reason", "AI budget gate denied review"),
                    "reviewer": reviewer_name,
                    "budget_decision": review_quota.get("budget_decision", {}),
                })
                return
            if review_quota.get("budget_reservation_id"):
                review_reservations.append(
                    (str(review_quota["budget_reservation_id"]), float(review_quota.get("cost_estimate_usd") or 0.0))
                )
        try:
            llm_results = llm.parallel_review(
                reviewers=reviewer_tuples,
                system="You are a careful quantitative strategy reviewer. Return JSON with verdict, confidence (0.0-1.0), and summary.",
                user=req.prompt,
                timeout=req.timeout_seconds,
            )
        except Exception:
            if review_reservations:
                from service.ai_budget_guard import get_ai_budget_guard

                for reservation_id, _cost in review_reservations:
                    get_ai_budget_guard().release(reservation_id)
            raise
        if review_reservations:
            from service.ai_budget_guard import get_ai_budget_guard

            for reservation_id, estimated_cost in review_reservations:
                get_ai_budget_guard().settle(reservation_id, estimated_cost)
            review_reservations = []

        # Step 2: optional Codex verification
        codex_result = None
        if req.verifier == "codex":
            quota_repo = str(payload.get("source_repository") or "unknown")
            codex_quota = get_quota_manager().check(
                quota_repo,
                "codex-cli",
                req.prompt,
                codex_account=True,
                task_class="review",
            )
            if not codex_quota.get("allowed"):
                if review_reservations:
                    from service.ai_budget_guard import get_ai_budget_guard

                    for reservation_id, _cost in review_reservations:
                        get_ai_budget_guard().release(reservation_id)
                _json_response(self, HTTPStatus.TOO_MANY_REQUESTS, {
                    "status": "deferred_budget",
                    "error": codex_quota.get("reason", "AI budget gate denied Codex review"),
                    "budget_decision": codex_quota.get("budget_decision", {}),
                })
                return
            get_quota_manager().record_execute(quota_repo)
            codex_reservation_id = str(codex_quota.get("budget_reservation_id") or "")
            try:
                codex_result = codex.execute(
                    prompt=req.prompt,
                    sandbox=_validate_sandbox("read-only"),
                    reasoning_effort=_resolve_codex_reasoning_effort(payload, TASK_REVIEW),
                    timeout=req.timeout_seconds,
                )
            except Exception:
                if codex_reservation_id:
                    from service.ai_budget_guard import get_ai_budget_guard

                    get_ai_budget_guard().release(codex_reservation_id)
                raise
            if codex_reservation_id:
                from service.ai_budget_guard import get_ai_budget_guard

                get_ai_budget_guard().settle(codex_reservation_id, 0.10 if codex_result.success else 0.0)

        # Step 3: build per-reviewer results with extracted confidence
        results: list[dict[str, Any]] = []
        for r in llm_results:
            entry: dict[str, Any] = {
                "reviewer": r.provider,
                "model": r.model,
                "success": r.success,
                "output": r.output if r.success else "",
                "error": r.error if not r.success else "",
                "latency_seconds": r.latency_seconds,
                "confidence": _extract_confidence_from_output(r.output) if r.success else 0.0,
            }
            results.append(entry)
        if codex_result is not None:
            results.append({
                "reviewer": "codex",
                "model": "codex-cli",
                "success": codex_result.success,
                "output": codex_result.output if codex_result.success else "",
                "error": codex_result.error if not codex_result.success else "",
                "confidence": _extract_confidence_from_output(codex_result.output) if codex_result.success else 0.0,
            })

        # Step 4: compute consensus + recommended action
        consensus = _compute_consensus(results)
        all_ok = all(r["success"] for r in results)

        # Autonomy decision: confidence + file risk → recommended action
        repo = str(payload.get("source_repository") or "")
        if trusted_proof and not repo:
            repo = str(trusted_proof.get("source_repository") or "")
        quota_status = get_quota_manager().runtime_status(repo or "unknown").get("status", "ok")
        try:
            org_health_status = str((read_org_health() or {}).get("status") or "unknown")
        except Exception:
            org_health_status = "unavailable"
        action = compute_recommended_action(
            results,
            changed_paths,
            repo=repo if repo else None,
            policy=load_autonomy_policy(),
            automation_metadata=payload.get("automation_metadata") if isinstance(payload.get("automation_metadata"), dict) else {},
            trusted_automation_metadata=trusted_proof.get("trusted_automation_metadata") if trusted_proof else {},
            health_status=get_health_monitor().status,
            quota_status=str(quota_status),
            org_health_status=org_health_status,
        )
        _audit_log("review_completed", consensus=consensus, all_success=all_ok,
                   action=action["action"], confidence=action["confidence"], risk=action["risk"])

        _json_response(self, HTTPStatus.OK, {
            "status": "ok" if all_ok else "partial",
            "results": results,
            "consensus": consensus,
            "recommended_action": action,
        })

    # -- automation control handlers --

    def _handle_automation_control(self) -> None:
        from urllib.parse import parse_qs, urlparse
        try:
            claims = authenticate(self.headers, audience=DEFAULT_AUDIENCE)
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        repo = str(params.get("repo", [""])[0] or "")
        if not _automation_operator_claims(claims):
            claims_repo = str(claims.get("repository") or "")
            if str(claims.get("auth_method") or "") == "static_token":
                if not _dashboard_repository_allowed(claims, repo):
                    _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": "repo is not allowed"})
                    return
            elif repo and _dashboard_repository_allowed(claims, repo):
                pass
            elif repo and repo != claims_repo:
                _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": "repo is not allowed"})
                return
            else:
                repo = claims_repo
        repo = repo or "unknown"
        raw_mode = params["mode"][0] if "mode" in params else MODE_REVIEW_AND_FIX
        mode = _normalize_control_mode_param(str(raw_mode if raw_mode is not None else ""))
        if not mode:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": "invalid mode"})
            return
        _json_response(self, HTTPStatus.OK, {"status": "ok", "control": _automation_control_snapshot(repo, requested_mode=mode)})

    def _handle_automation_triage(self, claims: dict[str, Any], payload: dict[str, Any]) -> None:
        from urllib.parse import parse_qs, urlparse

        source_repo = str(payload.get("source_repository") or "")
        if source_repo:
            _validate_source_repo(source_repo)
            if not _automation_operator_claims(claims):
                _validate_source_repo_org(claims, source_repo)
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        repo = str(payload.get("source_repository") or params.get("repo", [""])[0] or claims.get("repository") or "unknown")
        task = str(payload.get("task") or params.get("task", [""])[0] or "")
        raw_mode = payload.get("mode") if "mode" in payload else params["mode"][0] if "mode" in params else MODE_REVIEW_AND_FIX
        requested_mode = _normalize_control_mode_param(str(raw_mode if raw_mode is not None else ""))
        if not requested_mode:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": "invalid mode"})
            return
        failure_category = str(payload.get("failure_category") or params.get("failure_category", [""])[0] or "")
        error = str(payload.get("error") or params.get("error", [""])[0] or "")
        run_id = str(payload.get("run_id") or params.get("run_id", [""])[0] or "")
        changed_paths_raw = payload.get("changed_paths")
        if not isinstance(changed_paths_raw, list):
            changed_paths_raw = []
        triage = _automation_triage_snapshot(
            repo,
            task=task,
            requested_mode=requested_mode,
            failure_category=failure_category,
            error=error,
            changed_paths=[str(item) for item in changed_paths_raw if isinstance(item, str)],
            run_id=run_id,
        )
        _json_response(self, HTTPStatus.OK, {"status": "ok", "triage": triage})

    def _handle_list_automation_runs(self) -> None:
        from urllib.parse import parse_qs, urlparse
        try:
            claims = authenticate(self.headers, audience=DEFAULT_AUDIENCE)
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            limit = int(params.get("limit", ["100"])[0])
        except (TypeError, ValueError):
            _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": "limit must be an integer"})
            return
        if limit < 0 or limit > 1000:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": "limit out of range"})
            return
        include_events = str(params.get("include_events", ["false"])[0]).lower() in {"1", "true", "yes", "on"}
        snapshot_limit = limit if _automation_operator_claims(claims) else None
        snapshot = get_automation_run_ledger().snapshot(limit=snapshot_limit, include_events=include_events)
        snapshot = _automation_snapshot_for_claims(snapshot, claims, limit=limit)
        _json_response(self, HTTPStatus.OK, {"status": "ok", "ledger": snapshot})

    def _handle_get_automation_run(self, run_id: str) -> None:
        try:
            claims = authenticate(self.headers, audience=DEFAULT_AUDIENCE)
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
            return
        record = get_automation_run_ledger().get(run_id)
        if record is None:
            _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "automation run not found"})
            return
        try:
            _assert_automation_run_access(record, claims)
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
            return
        _json_response(self, HTTPStatus.OK, {"status": "ok", "run": record})

    def _handle_record_automation_run(self, claims: dict[str, Any], payload: dict[str, Any]) -> None:
        source_repo = str(payload.get("source_repository") or "")
        if source_repo:
            _validate_source_repo(source_repo)
            if not _automation_operator_claims(claims):
                _validate_source_repo_org(claims, source_repo)
                _assert_source_repository_owner_or_operator(claims, source_repo)
        repo = source_repo or str(claims.get("repository") or "unknown")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata = {
            key: value
            for key, value in metadata.items()
            if str(key).strip().lower() not in {"requested_mode", "mode"}
        }
        ledger = get_automation_run_ledger()
        run_id = str(payload.get("run_id") or payload.get("job_id") or "")
        if not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        existing = ledger.get(run_id)
        if existing is not None:
            existing_owner = _automation_run_owner_repository(existing)
            if existing_owner and existing_owner != repo:
                raise PermissionError("automation run_id belongs to another repository")
            existing_metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
            if existing_metadata.get("origin") == "service_job":
                raise PermissionError("automation run is service-owned")
            _assert_automation_run_access(existing, claims)
        task_name = str(payload.get("task") or payload.get("task_name") or "")
        existing_state = str(existing.get("task_state") or "") if isinstance(existing, dict) else ""
        task_state = str(payload.get("task_state") or payload.get("state") or existing_state or "running")
        existing_metadata = existing.get("metadata") if isinstance(existing, dict) and isinstance(existing.get("metadata"), dict) else {}
        mode_from_payload = "mode" in payload and str(payload.get("mode") or "").strip() != ""
        raw_mode = (
            payload.get("mode")
            if mode_from_payload
            else existing_metadata.get("requested_mode")
        )
        default_mode = MODE_REVIEW_AND_FIX
        requested_mode = _normalize_control_mode_param(str(raw_mode if raw_mode is not None and raw_mode != "" else ("" if mode_from_payload else default_mode)))
        if mode_from_payload and not requested_mode:
            raise ValueError("invalid mode")
        run_metadata = {
            **metadata,
            "origin": "external_workflow",
            "repository": repo,
            "source_repository": source_repo,
            "caller_repository": str(claims.get("repository") or ""),
        }
        run_metadata["requested_mode"] = requested_mode
        pending_run = {"run_id": run_id, "task_name": task_name, "task_state": task_state, "metadata": run_metadata}
        control = _automation_control_snapshot(repo, task_name=task_name, requested_mode=requested_mode, pending_run=pending_run)
        record = get_automation_run_ledger().record(
            run_id,
            task_state,
            task_name=task_name,
            suggested_action=str(control.get("effective_action") or control.get("action") or ""),
            service_health=str(control.get("service_health") or ""),
            quota_status=control.get("quota_status") or "",
            org_health_status=str(control.get("org_health_status") or ""),
            metadata=run_metadata,
            owner_repository=repo,
        )
        control = _automation_control_snapshot(repo, task_name=task_name, requested_mode=requested_mode, pending_run=record)
        _json_response(self, HTTPStatus.OK, {"status": "ok", "run": record, "control": control})

    def _handle_automation_authority(self, claims: dict[str, Any], payload: dict[str, Any]) -> None:
        source_repo = str(payload.get("source_repository") or "")
        if source_repo:
            _validate_source_repo(source_repo)
            if not _automation_operator_claims(claims):
                _validate_source_repo_org(claims, source_repo)
        changed_paths = [
            normalized
            for path in payload.get("changed_paths", [])
            if isinstance(path, str)
            for normalized in (_normalize_changed_path(path),)
            if normalized
        ]
        metadata = payload.get("automation_metadata") if isinstance(payload.get("automation_metadata"), dict) else {}
        proposed_action = str(payload.get("proposed_action") or ACTION_AUTO_PR)
        strategy_profile = str(payload.get("strategy_profile") or "")
        autonomy_policy = load_autonomy_policy()
        authority = evaluate_automation_authority(
            changed_paths,
            metadata=metadata,
            proposed_action=proposed_action,
        )
        if "strategy_automation_registry" in payload:
            if not strategy_profile:
                registry_context = {
                    "valid": False,
                    "profile_required": True,
                    "reason": "strategy_profile is missing for explicit strategy registry",
                }
            else:
                registry_context = summarize_strategy_registry_context(
                    payload.get("strategy_automation_registry"),
                    strategy_profile,
                )
        elif strategy_profile:
            registry_context = {
                "valid": False,
                "profile": strategy_profile,
                "reason": "strategy_profile requires a trusted server-owned strategy registry",
            }
        elif _strategy_profile_required_for_class(str(authority.get("change_class") or ""), changed_paths, autonomy_policy):
            registry_context = {
                "valid": False,
                "profile_required": True,
                "reason": "strategy_profile is required for strategy-owned changes",
            }
        else:
            registry_context = summarize_strategy_registry_context(None, "")
        repo = source_repo or str(claims.get("repository") or "unknown")
        try:
            org_health_status = str((read_org_health() or {}).get("status") or "unknown")
        except Exception:
            org_health_status = "unavailable"
        guarded = compute_recommended_action(
            [{"confidence": float(payload.get("confidence", 0.5))}],
            changed_paths,
            repo=repo,
            policy=autonomy_policy,
            automation_metadata=metadata,
            health_status=get_health_monitor().status,
            quota_status=str(get_quota_manager().runtime_status(repo).get("status", "ok")),
            org_health_status=org_health_status,
        )
        authority["class_level_final_action"] = authority["final_action"]
        if authority["final_action"] == "auto_merge":
            authority["final_action"] = ACTION_AUTO_PR
            authority["human_review_required"] = True
            authority["reasons"].append("use /v1/ai/review for executable auto_merge decisions")
        if ACTION_RANK.get(str(guarded["action"]), 0) < ACTION_RANK.get(str(authority["final_action"]), 0):
            authority["final_action"] = str(guarded["action"])
            authority["human_review_required"] = True
            authority["reasons"].append("capped by repository policy or runtime guards")
        authority = apply_strategy_registry_guard(authority, registry_context)
        policy_guard_action = str(guarded["action"])
        if ACTION_RANK.get(policy_guard_action, 0) > ACTION_RANK.get(str(authority["final_action"]), 0):
            policy_guard_action = str(authority["final_action"])
        authority["policy_guard_action"] = policy_guard_action
        _json_response(self, HTTPStatus.OK, {"status": "ok", "automation_authority": authority})

    # -- feedback handlers (Phase 3: closed-loop change tracking) --

    def _handle_feedback_register(self, claims: dict[str, Any], payload: dict[str, Any]) -> None:
        """POST /v1/ai/feedback/register — register an autonomous change with pre-metrics."""
        change_id = _new_change_id()
        source_repo = str(payload.get("source_repository") or "")
        if source_repo:
            _validate_source_repo(source_repo)
            _validate_source_repo_org(claims, source_repo)
        record = ChangeRecord(
            change_id=change_id,
            repo=source_repo or str(claims.get("repository", "")),
            task=str(payload.get("task", "")),
            action=str(payload.get("action", "")),
            confidence=float(payload.get("confidence", 0.0)),
            risk=str(payload.get("risk", "")),
            changed_paths=[str(p) for p in payload.get("changed_paths", []) if isinstance(p, str)],
            before_metrics={str(k): float(v) for k, v in payload.get("before_metrics", {}).items()},
            source_repo=source_repo,
            external_url=str(payload.get("external_url", "")),
            issue_number=int(payload["issue_number"]) if payload.get("issue_number") is not None else None,
            pr_number=int(payload["pr_number"]) if payload.get("pr_number") is not None else None,
        )
        write_change(record)
        _audit_log("change_registered", change_id=change_id, repo=record.repo,
                   action=record.action, confidence=record.confidence)
        _json_response(self, HTTPStatus.OK, {"status": "ok", "change_id": change_id})

    def _handle_feedback_evaluate(self, payload: dict[str, Any]) -> None:
        """POST /v1/ai/feedback/evaluate — submit post-change metrics for evaluation."""
        change_id = str(payload.get("change_id", ""))
        after_metrics = {str(k): float(v) for k, v in payload.get("after_metrics", {}).items()}
        try:
            record = evaluate_change(change_id, after_metrics)
            _audit_log("change_evaluated", change_id=change_id, effect=record.effect,
                       detail=record.effect_detail)
            _json_response(self, HTTPStatus.OK, {
                "status": "ok",
                "effect": record.effect,
                "effect_detail": record.effect_detail,
                "rollback_issue_required": record.rollback_issue_required,
                "rollback_intent": record.rollback_intent,
            })
        except FileNotFoundError:
            _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "change_id not found"})

    def _handle_feedback_shadow(self, payload: dict[str, Any]) -> None:
        """POST /v1/ai/feedback/shadow — record AI shadow audit disagreement."""
        result = record_shadow_disagreement(
            repo=str(payload.get("source_repository", "")),
            plugin=str(payload.get("plugin", "")),
            ai_verdict=str(payload.get("ai_verdict", "")),
            ai_confidence=float(payload.get("ai_confidence", 0.0)),
            deterministic_route=str(payload.get("deterministic_route", "")),
        )
        _audit_log("shadow_disagreement", should_escalate=result["should_escalate"],
                   count=result["disagreement_count"])
        _json_response(self, HTTPStatus.OK, {"status": "ok", **result})

    # -- feedback GET handlers --

    def _handle_get_change(self, change_id: str) -> None:
        try:
            authenticate(self.headers, audience=DEFAULT_AUDIENCE)
            record = read_change(change_id)
            _json_response(self, HTTPStatus.OK, {"status": "ok", "change": record.to_dict()})
        except FileNotFoundError:
            _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "change not found"})
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})

    def _handle_list_changes(self) -> None:
        from urllib.parse import urlparse, parse_qs
        try:
            authenticate(self.headers, audience=DEFAULT_AUDIENCE)
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        repo = str(params.get("repo", [""])[0])
        days = int(params.get("days", ["30"])[0])
        records = list_changes(repo=repo, days=days)
        _json_response(self, HTTPStatus.OK, {
            "status": "ok",
            "changes": [r.to_dict() for r in records],
        })

    def _handle_effectiveness(self) -> None:
        from urllib.parse import urlparse, parse_qs
        try:
            authenticate(self.headers, audience=DEFAULT_AUDIENCE)
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        repo = str(params.get("repo", [""])[0])
        days = int(params.get("days", ["90"])[0])
        report = effectiveness_report(repo=repo, days=days)
        _json_response(self, HTTPStatus.OK, {"status": "ok", "report": report})

    def _handle_get_shadow(self) -> None:
        try:
            authenticate(self.headers, audience=DEFAULT_AUDIENCE)
            _json_response(self, HTTPStatus.OK, {
                "status": "ok",
                "disagreements": get_shadow_disagreements(),
            })
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})

    def _handle_quota_status(self) -> None:
        from urllib.parse import urlparse, parse_qs
        try:
            authenticate(self.headers, audience=DEFAULT_AUDIENCE)
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        repo = str(params.get("repo", [""])[0])
        quota = get_quota_manager()
        _json_response(self, HTTPStatus.OK, {"status": "ok", "quota": quota.status(repo)})


def _default_model_for_reviewer(reviewer: str) -> str:
    return default_dual_review_model_for_reviewer(reviewer)


def _record_platform_execution_telemetry(
    profile: str,
    payload: dict[str, Any],
    *,
    domain: str,
) -> None:
    try:
        try_record_platform_execution(profile, payload, domain=domain)
    except Exception as exc:  # pragma: no cover - telemetry best effort
        logging.getLogger(__name__).warning("platform execution telemetry failed: %s", exc)


def _extract_confidence_from_output(output: str) -> float:
    """Extract confidence score from a reviewer's JSON output.

    Looks for a ``confidence`` field (0.0–1.0) in the first JSON block found.
    Returns 0.5 (neutral) if no confidence data is found.
    """
    if not output:
        return 0.0
    try:
        match = re.search(r"\{[\s\S]*\}", output)
        if match:
            obj = json.loads(match.group(0))
            c = float(obj.get("confidence", 0.5))
            return max(0.0, min(1.0, c))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return 0.5


def _compute_consensus(results: list[dict[str, Any]]) -> str:
    """Simple consensus from review results — extracts approve/reject/escalate from JSON outputs."""
    verdicts: list[str] = []
    for r in results:
        if not r.get("success") or not r.get("output"):
            continue
        try:
            text = r["output"]
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                obj = json.loads(match.group(0))
                verdict = str(obj.get("verdict", "")).lower()
                if verdict in {"approve", "reject", "escalate", "verified", "mismatch", "agree", "review", "data_insufficient"}:
                    verdicts.append(verdict)
        except (json.JSONDecodeError, KeyError):
            continue

    if not verdicts:
        return "unknown"
    if all(v == verdicts[0] for v in verdicts):
        return verdicts[0]
    if any(v in {"reject", "mismatch"} for v in verdicts):
        return "escalate"
    if any(v == "escalate" for v in verdicts):
        return "escalate"
    return "approve"


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    os.umask(0o077)

    # Startup security checks
    _validate_static_token_on_startup()
    recovered_jobs = _recover_orphaned_jobs()
    if _is_production() and os.environ.get("CODEX_AUDIT_SERVICE_FAKE_OUTPUT") is not None:
        print("[ai-gateway] WARNING: CODEX_AUDIT_SERVICE_FAKE_OUTPUT is set in production!", file=sys.stderr)

    host = os.environ.get("CODEX_AUDIT_SERVICE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("CODEX_AUDIT_SERVICE_PORT", "8797"))
    server = ThreadingHTTPServer((host, port), AiGatewayRequestHandler)

    print(f"[ai-gateway] listening on http://{host}:{port}", file=sys.stderr)
    if recovered_jobs:
        print(f"[ai-gateway] failed {recovered_jobs} orphaned jobs after restart", file=sys.stderr)
    print("[ai-gateway] security:", file=sys.stderr)
    print(f"  auth_mode: {os.environ.get('CODEX_AUDIT_SERVICE_AUTH', 'github-oidc')}", file=sys.stderr)
    print(f"  production: {_is_production()}", file=sys.stderr)
    print(f"  sandbox_allowlist: {sorted(ALLOWED_SANDBOXES)}", file=sys.stderr)
    print(f"  rate_limit: {_RATE_LIMIT_MAX_REQUESTS}/{_RATE_LIMIT_WINDOW_SECONDS:.0f}s (analyze/review)", file=sys.stderr)
    print(f"  max_active_jobs: {_positive_int_env('CODEX_AUDIT_SERVICE_MAX_ACTIVE_JOBS', DEFAULT_JOB_MAX_ACTIVE)}", file=sys.stderr)
    print("[ai-gateway] endpoints:", file=sys.stderr)
    print("  POST /v1/ai/analyze          (sync, rate-limited, LlmAdapter)", file=sys.stderr)
    print("  POST /v1/ai/execute/jobs     (async, source_repo validated, CodexAdapter)", file=sys.stderr)
    print("  POST /v1/ai/review           (sync, rate-limited, multi-model)", file=sys.stderr)
    print("  POST /v1/codex-audit*        (backward compat)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
