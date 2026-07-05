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
import re
import secrets
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from service.auth import authenticate
from service.contracts import (
    MODE_REVIEW_ONLY,
    TASK_ANALYZE,
    TASK_EXECUTE,
    TASK_REVIEW,
    parse_analyze_request,
    parse_execute_request,
    parse_review_request,
)
from service.adapters.llm_adapter import LlmAdapter
from service.adapters.codex_adapter import CodexAdapter
from service.autonomy import (
    load_autonomy_policy,
    recommended_action as compute_recommended_action,
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
from service.task_state import job_task_state
from service.health import get_health_monitor
from service.org_health import read_org_health

# ── constants ───────────────────────────────────────────────────────────

DEFAULT_AUDIENCE = "quant-codex-audit"
DEFAULT_MAX_REQUEST_BYTES = 2_000_000
DEFAULT_JOB_TTL_SECONDS = 86_400
DEFAULT_JOB_MAX_ACTIVE = 10
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
WRITE_AUTH_METHODS = frozenset({"github_oidc", "none"})

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
    _write_job(job)
    return job


def _assert_job_access(job: dict[str, Any], claims: dict[str, Any]) -> None:
    repository = str(claims.get("repository") or "")
    if repository != str(job.get("repository") or ""):
        raise PermissionError("job repository is not allowed")
    request_run_id = str(claims.get("run_id") or "")
    job_run_id = str(job.get("run_id") or "")
    if request_run_id and job_run_id and request_run_id != job_run_id:
        raise PermissionError("job run_id is not allowed")


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


def _job_dedupe_key(payload: dict[str, Any]) -> str:
    issue_number = str(payload.get("issue_number") or "").strip()
    prompt_hash = hashlib.sha256(str(payload.get("prompt") or "").encode("utf-8")).hexdigest()
    parts = [
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
        get_health_monitor().record(
            "/v1/ai/execute/jobs/run",
            time.time() - started,
            job["status"] == "succeeded",
            str(job.get("failure_category") or job.get("error") or ""),
        )
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
        get_health_monitor().record("/v1/ai/execute/jobs/run", time.time() - started, False, type(exc).__name__)
        _audit_log("job_failed", job_id=job_id, error=type(exc).__name__,
                   repository=job.get("repository"))


def _submit_job(claims: dict[str, Any], payload: dict[str, Any]) -> dict[str, object]:
    _cleanup_expired_jobs()
    dedupe_key = _job_dedupe_key(payload)
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
        "actor": str(claims.get("actor") or ""),
        "source_repository": str(payload.get("source_repository") or ""),
        "source_ref": str(payload.get("source_ref") or ""),
        "task": str(payload.get("task") or TASK_EXECUTE),
        "mode": str(payload.get("mode") or MODE_REVIEW_ONLY),
        "timeout_seconds": int(payload.get("timeout_seconds", 2700)),
        "dedupe_key": dedupe_key,
    }
    _write_job(job)
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
            elif self.path in {"/v1/ai/execute", "/v1/codex-audit"}:
                _assert_write_authz(claims, self.path)
                self._handle_execute_sync(claims, payload)
            elif self.path in {"/v1/ai/review"}:
                _assert_write_authz(claims, self.path)
                self._handle_review(payload)
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
        qr = quota.check(source_repo, req.model, req.prompt)
        if not qr["allowed"]:
            _json_response(self, HTTPStatus.TOO_MANY_REQUESTS, {
                "status": "error",
                "error": qr["reason"],
                "recommended_model": qr.get("recommended_model", ""),
                "remaining_usd": qr.get("remaining_usd", 0),
            })
            return

        started = time.time()
        adapter = LlmAdapter()
        result = adapter.complete(
            model=req.model, system=req.system, user=req.prompt,
            max_tokens=req.max_tokens, timeout=req.timeout_seconds,
        )
        latency = time.time() - started

        # Record quota and health
        quota.record(source_repo, req.model, req.prompt, result.output if result.success else "")
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
        qr = quota.check(quota_repo, "codex-cli", req.prompt)
        if not qr["allowed"]:
            _json_response(self, HTTPStatus.TOO_MANY_REQUESTS, {
                "status": "error", "error": qr["reason"],
                "remaining_usd": qr.get("remaining_usd", 0),
            })
            return
        quota.record_execute(quota_repo)

        payload.setdefault("task", TASK_EXECUTE)
        job = _submit_job(claims, payload)
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
        qr = quota.check(quota_repo, "codex-cli", req.prompt)
        if not qr["allowed"]:
            _json_response(self, HTTPStatus.TOO_MANY_REQUESTS, {
                "status": "error", "error": qr["reason"],
                "remaining_usd": qr.get("remaining_usd", 0),
            })
            return
        quota.record_execute(quota_repo)
        adapter = CodexAdapter()
        sandbox = _validate_sandbox(str(payload.get("sandbox") or ""))
        reasoning_effort = _resolve_codex_reasoning_effort(payload, str(payload.get("task") or TASK_EXECUTE))
        result = adapter.execute(
            prompt=req.prompt,
            sandbox=sandbox,
            model=req.model or None,
            reasoning_effort=reasoning_effort,
            timeout=req.timeout_seconds,
        )
        get_health_monitor().record("/v1/ai/execute", time.time() - started, result.success, result.error if not result.success else "")
        if result.success:
            _json_response(self, HTTPStatus.OK, {"status": "ok", "output": result.output})
        else:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "error": result.error})

    def _handle_review(self, payload: dict[str, Any]) -> None:
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

        # Step 1: parallel LLM review
        reviewer_tuples = [
            (r, req.model) if req.model else (r, _default_model_for_reviewer(r))
            for r in req.reviewers
        ]
        _audit_log("review_started", reviewers=req.reviewers, verifier=req.verifier,
                   changed_paths_count=len(changed_paths))
        llm_results = llm.parallel_review(
            reviewers=reviewer_tuples,
            system="You are a careful quantitative strategy reviewer. Return JSON with verdict, confidence (0.0-1.0), and summary.",
            user=req.prompt,
            timeout=req.timeout_seconds,
        )

        # Step 2: optional Codex verification
        codex_result = None
        if req.verifier == "codex":
            codex_result = codex.execute(
                prompt=req.prompt,
                sandbox=_validate_sandbox("read-only"),
                reasoning_effort=_resolve_codex_reasoning_effort(payload, TASK_REVIEW),
                timeout=req.timeout_seconds,
            )

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
        quota_status = get_quota_manager().runtime_status(repo or "unknown").get("status", "ok")
        action = compute_recommended_action(
            results,
            changed_paths,
            repo=repo if repo else None,
            policy=load_autonomy_policy(),
            health_status=get_health_monitor().status,
            quota_status=str(quota_status),
        )
        _audit_log("review_completed", consensus=consensus, all_success=all_ok,
                   action=action["action"], confidence=action["confidence"], risk=action["risk"])

        _json_response(self, HTTPStatus.OK, {
            "status": "ok" if all_ok else "partial",
            "results": results,
            "consensus": consensus,
            "recommended_action": action,
        })

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
    if reviewer == "claude":
        return "claude-sonnet-4-6"
    if reviewer == "gpt":
        return "gpt-5.4-mini"
    return "claude-sonnet-4-6"


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
    if _is_production() and os.environ.get("CODEX_AUDIT_SERVICE_FAKE_OUTPUT") is not None:
        print("[ai-gateway] WARNING: CODEX_AUDIT_SERVICE_FAKE_OUTPUT is set in production!", file=sys.stderr)

    host = os.environ.get("CODEX_AUDIT_SERVICE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("CODEX_AUDIT_SERVICE_PORT", "8797"))
    server = ThreadingHTTPServer((host, port), AiGatewayRequestHandler)

    print(f"[ai-gateway] listening on http://{host}:{port}", file=sys.stderr)
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
