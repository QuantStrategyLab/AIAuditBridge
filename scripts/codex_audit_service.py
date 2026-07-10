#!/usr/bin/env python3
"""Codex audit service — authenticated VPS facade for Codex execution.

The VPS service intentionally runs only Codex. Claude/GPT direct API fallbacks
remain in caller-side GitHub workflows/scripts so provider API keys do not live
in, or pass through, this service.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from abc import ABC, abstractmethod
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


# ── Constants ────────────────────────────────────────────────────────

DEFAULT_AUDIENCE = "quant-codex-audit"
DEFAULT_MAX_REQUEST_BYTES = 2_000_000
DEFAULT_JOB_TTL_SECONDS = 86_400
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = GITHUB_OIDC_ISSUER + "/.well-known/jwks"
_JWKS_CACHE: dict[str, Any] | None = None
_JWKS_CACHE_EXPIRES_AT = 0.0
_JOB_WRITE_LOCK = threading.Lock()

SUPPORTED_TASKS = frozenset({"execute"})
AUDIT_EXECUTE_TASKS = frozenset({"monthly_snapshot_audit", "long_horizon_signal_shadow"})
CODEX_REVIEW_TASKS = frozenset({"pr_review", "review"})
SUPPORTED_MODES = frozenset({"review_only", "review_and_fix"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
TASK_COMPLEXITY_LOW = "low"
TASK_COMPLEXITY_MEDIUM = "medium"
TASK_COMPLEXITY_HIGH = "high"
TASK_COMPLEXITY_LEVELS = (TASK_COMPLEXITY_LOW, TASK_COMPLEXITY_MEDIUM, TASK_COMPLEXITY_HIGH)
AI_GATEWAY_LLM_DEFAULT_MODEL = "gpt-5.4"
AI_GATEWAY_LLM_DEFAULT_MODEL_LOW = os.environ.get(
    "AI_GATEWAY_LLM_LOW_COMPLEXITY_MODEL", "gpt-5.4-mini"
).strip()
AI_GATEWAY_LLM_DEFAULT_MODEL_MEDIUM = os.environ.get(
    "AI_GATEWAY_LLM_MEDIUM_COMPLEXITY_MODEL", AI_GATEWAY_LLM_DEFAULT_MODEL
).strip()
AI_GATEWAY_LLM_DEFAULT_MODEL_HIGH = os.environ.get(
    "AI_GATEWAY_LLM_HIGH_COMPLEXITY_MODEL", "gpt-5.5"
).strip()
CODEX_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_LOW = os.environ.get(
    "AI_GATEWAY_CODEX_LOW_COMPLEXITY_REASONING_EFFORT", "low"
).strip().lower()
AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_MEDIUM = os.environ.get(
    "AI_GATEWAY_CODEX_MEDIUM_COMPLEXITY_REASONING_EFFORT", "medium"
).strip().lower()
AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_HIGH = os.environ.get(
    "AI_GATEWAY_CODEX_HIGH_COMPLEXITY_REASONING_EFFORT", "high"
).strip().lower()


# ── Adapter Protocol ──────────────────────────────────────────────────


class AiAdapter(ABC):
    """Base adapter for AI backends.

    The service currently exposes only the Codex backend. Caller-side scripts
    may still perform Claude/GPT direct API fallback outside the VPS service.

    The adapter receives:
      - prompt: the full instruction text
      - model: which model to use (e.g. "claude-sonnet-4-6")
      - timeout_seconds: max execution time
      - repo_dir: optional path to cloned repo (for execute tasks)
    """

    @abstractmethod
    def run(self, *, prompt: str, model: str, timeout_seconds: int,
            repo_dir: Path | None = None, reasoning_effort: str = "") -> str:
        ...


class LlmAdapter(AiAdapter):
    """Direct API call adapter — calls Anthropic/OpenAI APIs directly.

    Model routing:
      claude-*       → Anthropic API (ANTHROPIC_API_KEY)
      gpt-*          → OpenAI API     (OPENAI_API_KEY)

    No repo cloning needed. Used for: code review, strategy analysis.
    """

    def run(self, *, prompt: str, model: str, timeout_seconds: int,
            repo_dir: Path | None = None, reasoning_effort: str = "") -> str:
        default_model = os.environ.get("AI_GATEWAY_LLM_DEFAULT_MODEL", "claude-sonnet-4-6").strip()
        resolved = model or default_model
        provider = _detect_provider(resolved)
        api_key = self._resolve_api_key(provider)
        if not api_key:
            raise RuntimeError(f"No API key configured for provider={provider}")
        return self._call_api(provider, api_key, resolved, prompt, timeout_seconds)

    def _resolve_api_key(self, provider: str) -> str | None:
        if provider == "anthropic":
            return os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
        return os.environ.get("OPENAI_API_KEY", "").strip() or None

    def _call_api(self, provider: str, api_key: str, model: str,
                  prompt: str, timeout: int) -> str:
        try:
            from quant_strategy_plugins.ai_audit import AiAuditEndpoint, call_ai_audit

            base_url = ("https://api.anthropic.com/v1" if provider == "anthropic"
                        else "https://api.openai.com/v1")
            endpoint = AiAuditEndpoint(
                name="ai_gateway_llm",
                api_key=api_key,
                provider=provider,
                model=model,
                base_url=base_url,
            )
            messages = [{"role": "user", "content": prompt}]
            raw = call_ai_audit(endpoint, messages, timeout=timeout)
            return raw if isinstance(raw, str) else json.dumps(raw)
        except ImportError:
            # Fallback: direct HTTP call without ai_audit plugin
            return self._call_api_direct(provider, api_key, model, prompt, timeout)

    def _call_api_direct(self, provider: str, api_key: str, model: str,
                         prompt: str, timeout: int) -> str:
        if provider == "anthropic":
            return self._call_anthropic(api_key, model, prompt, timeout)
        return self._call_openai(api_key, model, prompt, timeout)

    def _call_anthropic(self, api_key: str, model: str,
                        prompt: str, timeout: int) -> str:
        body = json.dumps({
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body, method="POST",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("content", [{}])[0].get("text", str(data)))

    def _call_openai(self, api_key: str, model: str,
                     prompt: str, timeout: int) -> str:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=body, method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data["choices"][0]["message"]["content"])


class CodexAdapter(AiAdapter):
    """Codex CLI execution adapter — runs codex exec in a repo checkout.

    Used for: bug fixes, code changes, backtest verification.
    Requires source_repository (for repo clone path).
    """

    def run(self, *, prompt: str, model: str, timeout_seconds: int,
            repo_dir: Path | None = None, reasoning_effort: str = "") -> str:
        fake = os.environ.get("CODEX_AUDIT_SERVICE_FAKE_OUTPUT")
        if fake is not None:
            return fake

        if repo_dir is None or not repo_dir.exists():
            raise RuntimeError("CodexAdapter requires a valid repo_dir (execute task)")

        output_path = repo_dir / ".codex-output" / "final-message.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        completed = subprocess.run(
            self._build_command(output_path, model, reasoning_effort),
            input=prompt, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=timeout_seconds,
            env=self._build_env(),
        )
        if completed.returncode != 0:
            detail = (completed.stdout[-4000:] + completed.stderr[-4000:]).strip()
            raise RuntimeError("codex exec failed" + (f":\n{detail}" if detail else ""))

        if output_path.exists() and output_path.read_text(encoding="utf-8").strip():
            return output_path.read_text(encoding="utf-8")
        return completed.stdout

    def _build_command(self, output_path: Path, model: str, reasoning_effort: str = "") -> list[str]:
        codex = shutil.which(os.environ.get("CODEX_AUDIT_SERVICE_CODEX_BIN", "codex"))
        if not codex:
            raise RuntimeError("codex CLI not found on host")
        cmd = [
            codex, "exec", "--skip-git-repo-check",
            "--sandbox", os.environ.get("CODEX_AUDIT_SERVICE_SANDBOX", "read-only").strip() or "read-only",
            "--output-last-message", str(output_path),
        ]
        resolved = model or os.environ.get("CODEX_AUDIT_SERVICE_MODEL", "").strip()
        if resolved:
            cmd.extend(["--model", resolved])
        selected_reasoning_effort = _normalize_reasoning_effort(
            reasoning_effort or os.environ.get("CODEX_AUDIT_SERVICE_REASONING_EFFORT", "")
        )
        if selected_reasoning_effort and selected_reasoning_effort != "auto":
            cmd.extend(["-c", f"model_reasoning_effort={selected_reasoning_effort}"])
        cmd.append("-")
        return cmd

    def _build_env(self) -> dict[str, str]:
        return {
            k: v for k, v in os.environ.items()
            if not k.startswith("CODEX_AUDIT_SERVICE_")
            and not any(m in k.upper() for m in
                        ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "CREDENTIAL", "API_KEY"))
        }


# ── Adapter Registry ─────────────────────────────────────────────────

_ADAPTER_REGISTRY: dict[str, AiAdapter] = {
    "execute": CodexAdapter(),
}

TASK_COMPLEXITY_MEDIUM_LINE_THRESHOLD = int(
    os.environ.get("AI_GATEWAY_COMPLEXITY_MEDIUM_THRESHOLD_LINES", "600")
)
TASK_COMPLEXITY_HIGH_LINE_THRESHOLD = int(os.environ.get("AI_GATEWAY_COMPLEXITY_HIGH_THRESHOLD_LINES", "1800"))
TASK_COMPLEXITY_MEDIUM_PROMPT_THRESHOLD = int(os.environ.get("AI_GATEWAY_COMPLEXITY_MEDIUM_THRESHOLD_PROMPT_CHARS", "7000"))
TASK_COMPLEXITY_HIGH_PROMPT_THRESHOLD = int(os.environ.get("AI_GATEWAY_COMPLEXITY_HIGH_THRESHOLD_PROMPT_CHARS", "18000"))
TASK_COMPLEXITY_FILE_HIGH_THRESHOLD = int(os.environ.get("AI_GATEWAY_COMPLEXITY_HIGH_THRESHOLD_FILES", "12"))
TASK_COMPLEXITY_FILE_MEDIUM_THRESHOLD = int(os.environ.get("AI_GATEWAY_COMPLEXITY_MEDIUM_THRESHOLD_FILES", "4"))


def _canonicalize_task(task: str) -> str:
    task = (task or "").strip().lower()
    if task in {"review", "pr_review", "pr-review", "prreview"}:
        return "pr_review"
    return task


def _adapter_task(task: str, model: str) -> str:
    resolved_task = _canonicalize_task(task or _detect_task_from_model(model))
    if resolved_task == "execute" or resolved_task in AUDIT_EXECUTE_TASKS or resolved_task in CODEX_REVIEW_TASKS:
        return "execute"
    return resolved_task


def resolve_adapter(task: str, model: str) -> AiAdapter:
    """Resolve the adapter for a task."""
    resolved_task = _adapter_task(task, model)
    adapter = _ADAPTER_REGISTRY.get(resolved_task)
    if adapter is None:
        raise ValueError(f"Unsupported task={resolved_task!r}. Supported: {sorted(_ADAPTER_REGISTRY)}")
    return adapter


def _detect_task_from_model(model: str) -> str:
    """The VPS service is Codex-only; API model names do not select API adapters."""
    return "execute"


def _detect_provider(model: str) -> str:
    m = (model or "").lower().strip()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gpt"):
        return "openai"
    return "openai"


def _normalize_complexity(value: str) -> str:
    level = (value or "").strip().lower()
    if level in TASK_COMPLEXITY_LEVELS:
        return level
    return ""


def _normalize_reasoning_effort(value: str) -> str:
    effort = (value or "").strip().lower()
    if not effort or effort == "auto":
        return effort
    if effort not in CODEX_REASONING_EFFORTS:
        raise ValueError(
            f"reasoning_effort must be one of auto,{','.join(sorted(CODEX_REASONING_EFFORTS))}"
        )
    return effort


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _estimate_task_complexity(
    prompt: str,
    *,
    changed_files: int = 0,
    changed_lines: int = 0,
    changed_chars: int = 0,
) -> str:
    changed_files = max(0, _as_int(changed_files, 0))
    changed_lines = max(0, _as_int(changed_lines, 0))
    changed_chars = max(0, _as_int(changed_chars, 0))
    prompt_chars = len(prompt or "")
    complexity_score = (
        changed_files * 2
        + changed_lines * 0.15
        + changed_chars / 200
        + prompt_chars / 2500
    )
    if (
        complexity_score >= TASK_COMPLEXITY_HIGH_LINE_THRESHOLD
        or changed_files >= TASK_COMPLEXITY_FILE_HIGH_THRESHOLD
        or changed_lines >= TASK_COMPLEXITY_HIGH_LINE_THRESHOLD
        or prompt_chars >= TASK_COMPLEXITY_HIGH_PROMPT_THRESHOLD
    ):
        return TASK_COMPLEXITY_HIGH
    if (
        complexity_score >= TASK_COMPLEXITY_MEDIUM_LINE_THRESHOLD
        or changed_files >= TASK_COMPLEXITY_FILE_MEDIUM_THRESHOLD
        or changed_lines >= TASK_COMPLEXITY_MEDIUM_LINE_THRESHOLD
        or prompt_chars >= TASK_COMPLEXITY_MEDIUM_PROMPT_THRESHOLD
    ):
        return TASK_COMPLEXITY_MEDIUM
    return TASK_COMPLEXITY_LOW


def _model_for_complexity(complexity: str) -> str:
    if complexity == TASK_COMPLEXITY_HIGH:
        return AI_GATEWAY_LLM_DEFAULT_MODEL_HIGH or AI_GATEWAY_LLM_DEFAULT_MODEL
    if complexity == TASK_COMPLEXITY_MEDIUM:
        return AI_GATEWAY_LLM_DEFAULT_MODEL_MEDIUM or AI_GATEWAY_LLM_DEFAULT_MODEL
    return AI_GATEWAY_LLM_DEFAULT_MODEL_LOW or AI_GATEWAY_LLM_DEFAULT_MODEL


def _reasoning_effort_for_complexity(complexity: str) -> str:
    if complexity == TASK_COMPLEXITY_HIGH:
        return _normalize_reasoning_effort(AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_HIGH) or "high"
    if complexity == TASK_COMPLEXITY_MEDIUM:
        return _normalize_reasoning_effort(AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_MEDIUM) or "medium"
    return _normalize_reasoning_effort(AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_LOW) or "low"


def _normalize_task_env_key(task: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (task or "").strip().upper()).strip("_")


def _resolve_reasoning_effort(payload: dict[str, Any], task: str) -> str:
    requested = _normalize_reasoning_effort(str(payload.get("reasoning_effort") or ""))
    if requested and requested != "auto":
        return requested

    configured = _normalize_reasoning_effort(os.environ.get("CODEX_AUDIT_SERVICE_REASONING_EFFORT", ""))
    if configured and configured != "auto":
        return configured

    complexity = _normalize_complexity(str(payload.get("complexity", "")))
    if not complexity:
        complexity = _estimate_task_complexity(
            str(payload.get("prompt", "")),
            changed_files=_as_int(payload.get("changed_files", 0), 0),
            changed_lines=_as_int(payload.get("changed_lines", 0), 0),
            changed_chars=_as_int(payload.get("changed_chars", 0), 0),
        )

    env_names: list[str] = []
    task_key = _normalize_task_env_key(task)
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

    return _reasoning_effort_for_complexity(complexity)


def _resolve_model(payload: dict[str, Any], task: str) -> str:
    model = (payload.get("model") or "").strip()
    if task not in CODEX_REVIEW_TASKS and task != "analyze":
        return model
    normalized = _normalize_complexity(model)
    if normalized:
        return _model_for_complexity(normalized)
    if not model or model.lower() == "auto":
        complexity = _normalize_complexity(str(payload.get("complexity", "")))
        if not complexity:
            complexity = _estimate_task_complexity(
                str(payload.get("prompt", "")),
                changed_files=_as_int(payload.get("changed_files", 0), 0),
                changed_lines=_as_int(payload.get("changed_lines", 0), 0),
                changed_chars=_as_int(payload.get("changed_chars", 0), 0),
            )
        return _model_for_complexity(complexity)
    return model


def _task_requires_async(task: str) -> bool:
    canonical = _canonicalize_task(task)
    return canonical == "execute" or canonical in AUDIT_EXECUTE_TASKS or canonical in CODEX_REVIEW_TASKS


# ── Auth ─────────────────────────────────────────────────────────────

def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _split_csv_env(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {i.strip() for i in re.split(r"[\n,]", raw) if i.strip()}


def _claim_matches(value: str, patterns: set[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _require_allowed_claim(payload: dict[str, Any], env_name: str, claim_name: str, label: str) -> None:
    patterns = _split_csv_env(env_name)
    if not patterns:
        raise PermissionError(f"{env_name} is required")
    value = str(payload.get(claim_name) or "")
    if not value:
        raise PermissionError(f"OIDC {label} is missing")
    if not _claim_matches(value, patterns):
        raise PermissionError(f"OIDC {label} is not allowed")


def _require_optional_allowed_claim(payload: dict[str, Any], env_name: str, claim_name: str, label: str) -> None:
    patterns = _split_csv_env(env_name)
    if not patterns:
        return
    value = str(payload.get(claim_name) or "")
    if not _claim_matches(value, patterns):
        raise PermissionError(f"OIDC {label} is not allowed")


def _load_jwks() -> dict[str, Any]:
    global _JWKS_CACHE, _JWKS_CACHE_EXPIRES_AT
    now = time.time()
    if _JWKS_CACHE and now < _JWKS_CACHE_EXPIRES_AT:
        return _JWKS_CACHE
    jwks_file = os.environ.get("CODEX_AUDIT_SERVICE_JWKS_FILE", "").strip()
    if jwks_file:
        payload = json.loads(Path(jwks_file).read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(GITHUB_OIDC_JWKS_URL, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise PermissionError("Invalid JWKS response")
    _JWKS_CACHE = payload
    _JWKS_CACHE_EXPIRES_AT = now + 300
    return payload


def _jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise PermissionError("OIDC token must have 3 JWT segments")
    return (
        json.loads(_b64url_decode(parts[0]).decode("utf-8")),
        json.loads(_b64url_decode(parts[1]).decode("utf-8")),
        _b64url_decode(parts[2]),
        f"{parts[0]}.{parts[1]}".encode("ascii"),
    )


def _verify_rs256(signing_input: bytes, signature: bytes, key: dict[str, Any]) -> None:
    if key.get("kty") != "RSA":
        raise PermissionError("Signing key is not RSA")
    n = int.from_bytes(_b64url_decode(str(key["n"])), "big")
    e = int.from_bytes(_b64url_decode(str(key["e"])), "big")
    key_bytes = (n.bit_length() + 7) // 8
    if len(signature) != key_bytes:
        raise PermissionError("Invalid signature length")
    decoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(key_bytes, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    if not decoded.startswith(b"\x00\x01"):
        raise PermissionError("Invalid signature padding")
    try:
        sep = decoded.index(b"\x00", 2)
    except ValueError:
        raise PermissionError("Missing signature separator")
    if decoded[sep + 1:] != digest_info:
        raise PermissionError("Signature mismatch")


def _verify_github_oidc(token: str) -> dict[str, Any]:
    header, payload, signature, signing_input = _jwt_parts(token)
    if header.get("alg") != "RS256":
        raise PermissionError("Token must use RS256")
    kid = str(header.get("kid") or "")
    keys = _load_jwks().get("keys", [])
    key = next((k for k in keys if isinstance(k, dict) and k.get("kid") == kid), None)
    if not key:
        raise PermissionError("Unknown signing key")
    _verify_rs256(signing_input, signature, key)

    audience = os.environ.get("CODEX_AUDIT_SERVICE_AUDIENCE", DEFAULT_AUDIENCE).strip() or DEFAULT_AUDIENCE
    ta = payload.get("aud")
    audiences = {ta} if isinstance(ta, str) else set(ta) if isinstance(ta, list) else set()
    if audience not in audiences:
        raise PermissionError("OIDC audience mismatch")
    if payload.get("iss") != GITHUB_OIDC_ISSUER:
        raise PermissionError("OIDC issuer mismatch")

    now = int(time.time())
    skew = int(os.environ.get("CODEX_AUDIT_SERVICE_CLOCK_SKEW_SECONDS", "60"))
    if int(payload.get("exp", 0)) and now > int(payload["exp"]) + skew:
        raise PermissionError("Token expired")
    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES", "repository", "repository")
    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS", "workflow_ref", "workflow_ref")
    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_REFS", "ref", "ref")
    if payload.get("job_workflow_ref"):
        _require_allowed_claim(
            payload,
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS",
            "job_workflow_ref",
            "job workflow ref",
        )
    else:
        _require_allowed_claim(
            payload,
            "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES",
            "repository",
            "direct repository",
        )
    _require_optional_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES",
                                    "repository_visibility", "repository visibility")
    return payload


def _authenticate(headers: Any) -> dict[str, Any]:
    mode = os.environ.get("CODEX_AUDIT_SERVICE_AUTH", "github-oidc").strip().lower()
    if mode == "none":
        allow = os.environ.get("CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS", "")
        if allow.strip().lower() not in {"1", "true", "yes", "on"}:
            raise PermissionError("AUTH=none requires ALLOW_NO_AUTH_FOR_LOCAL_TESTS=true")
        return {"repository": "local", "actor": "local", "auth_method": "none"}
    auth = str(headers.get("Authorization") or "")
    if not auth.startswith("Bearer "):
        raise PermissionError("Missing bearer token")
    token = auth[7:].strip()
    if mode in {"github-oidc", "oidc"}:
        claims = _verify_github_oidc(token)
        claims["auth_method"] = "github_oidc"
        return claims
    raise PermissionError(f"Unsupported auth mode: {mode}")


# ── Payload validation ───────────────────────────────────────────────

def _validate_payload(payload: dict[str, Any]) -> None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    task = _canonicalize_task(str(payload.get("task", "") or ""))
    model = payload.get("model", "").strip() or ""
    mode = payload.get("mode", "").strip().lower() or "review_only"

    # If no task specified, auto-detect from model
    if not task:
        task = _detect_task_from_model(model)
    normalized_for_validation = "execute" if task in CODEX_REVIEW_TASKS else task
    if normalized_for_validation not in SUPPORTED_TASKS and normalized_for_validation not in AUDIT_EXECUTE_TASKS:
        raise ValueError(f"Unsupported task: {task!r}")

    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported mode: {mode!r}")

    if _adapter_task(task, payload.get("model", "")) == "execute":
        source_repo = payload.get("source_repository")
        if not isinstance(source_repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repo):
            raise ValueError("source_repository required for execute tasks")
        allowed = _split_csv_env("CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES")
        if allowed and source_repo not in allowed:
            raise PermissionError(f"source_repository {source_repo} not allowed")


def _validate_source_repo_org(claims: dict[str, Any], payload: dict[str, Any]) -> None:
    source_repo = str(payload.get("source_repository") or "")
    claims_repo = str(claims.get("repository") or "")
    if "/" not in source_repo or "/" not in claims_repo:
        return
    source_org = source_repo.split("/", 1)[0]
    claims_org = claims_repo.split("/", 1)[0]
    if source_org != claims_org:
        raise PermissionError(
            f"source_repository org {source_org!r} does not match OIDC repository org {claims_org!r}"
        )


# ── Task execution ───────────────────────────────────────────────────

def _run_task(payload: dict[str, Any], *, repo_dir: Path | None = None) -> str:
    task = _canonicalize_task(str(payload.get("task", "") or _detect_task_from_model(payload.get("model", ""))))
    model = _resolve_model(payload, task)
    reasoning_effort = _resolve_reasoning_effort(payload, task)
    prompt = str(payload["prompt"])
    timeout = int(payload.get("timeout_seconds") or os.environ.get(
        "CODEX_AUDIT_SERVICE_TIMEOUT_SECONDS", "2700"))

    adapter = resolve_adapter(task, model)
    return adapter.run(
        prompt=prompt, model=model,
        timeout_seconds=timeout, repo_dir=repo_dir, reasoning_effort=reasoning_effort,
    )


# ── Job management ───────────────────────────────────────────────────

def _job_dir() -> Path:
    default = Path(tempfile.gettempdir()) / "codex-audit-service-jobs"
    path = Path(os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR", str(default))).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _next_job_id() -> str:
    return secrets.token_hex(12)


def _job_path(job_id: str) -> Path:
    return _job_dir() / f"{job_id}.json"


def _job_dedupe_key(payload: dict[str, Any]) -> str:
    issue_number = str(payload.get("issue_number") or "").strip()
    prompt_hash = hashlib.sha256(str(payload.get("prompt") or "").encode("utf-8")).hexdigest()
    key_parts = [
        str(payload.get("source_repository") or ""),
        str(payload.get("source_ref") or ""),
        str(payload.get("task") or _detect_task_from_model(payload.get("model", ""))),
        str(payload.get("mode") or "review_only"),
        issue_number or prompt_hash,
    ]
    return hashlib.sha256(json.dumps(key_parts, separators=(",", ":")).encode("utf-8")).hexdigest()


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


def _public_job_payload(job_id: str, data: dict[str, Any], *, deduped: bool = False) -> dict[str, object]:
    response: dict[str, object] = {
        "job_id": job_id,
        "status": str(data.get("status") or "unknown"),
        "poll_url": f"/v1/codex-audit/jobs/{job_id}",
    }
    if deduped:
        response["deduped"] = True
    if data.get("output"):
        response["output"] = str(data["output"])
    if data.get("error"):
        response["error"] = str(data["error"])
    if data.get("failure_category"):
        response["failure_category"] = str(data["failure_category"])
    return response


def _write_job(job_id: str, payload: dict[str, Any], dedupe_key: str) -> None:
    with _JOB_WRITE_LOCK:
        now = time.time()
        _job_path(job_id).write_text(json.dumps({
            "job_id": job_id,
            "status": "queued",
            "payload": payload,
            "output": "",
            "error": "",
            "failure_category": "",
            "dedupe_key": dedupe_key,
            "created_at": now,
            "updated_at": now,
        }, ensure_ascii=False), encoding="utf-8")


def _update_job(job_id: str, status: str, output: str = "", error: str = "") -> None:
    with _JOB_WRITE_LOCK:
        path = _job_path(job_id)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            data["status"] = status
            data["updated_at"] = time.time()
            if output:
                data["output"] = output
            if error:
                data["error"] = error
                data["failure_category"] = _classify_failure(error)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_job(job_id: str) -> dict[str, Any] | None:
    path = _job_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _cleanup_expired_jobs() -> None:
    ttl = int(os.environ.get("CODEX_AUDIT_SERVICE_JOB_TTL_SECONDS", str(DEFAULT_JOB_TTL_SECONDS)))
    now = time.time()
    for path in _job_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if now - data.get("created_at", 0) > ttl:
                path.unlink(missing_ok=True)
        except Exception:
            path.unlink(missing_ok=True)


def _find_existing_job(dedupe_key: str) -> tuple[str, dict[str, Any]] | None:
    if os.environ.get("CODEX_AUDIT_SERVICE_DEDUPE_JOBS", "true").strip().lower() in {"0", "false", "no", "off"}:
        return None
    for path in _job_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("dedupe_key") != dedupe_key:
            continue
        if data.get("status") in ACTIVE_JOB_STATUSES:
            return path.stem, data
    return None


def _service_git_token() -> str:
    for env_name in ("CROSS_REPO_GIT_TOKEN", "GH_TOKEN"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return raw
    return ""


def _git_auth_env(token: str) -> dict[str, str] | None:
    if not token:
        return None
    encoded = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
        }
    )
    return env


def _redact_clone_error(text: str, token: str) -> str:
    redacted = text
    if token:
        redacted = redacted.replace(token, "[REDACTED]")
        redacted = redacted.replace(
            base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii"),
            "[REDACTED]",
        )
    redacted = re.sub(
        r"https://x-access-token:[^\s@]+@github\.com/",
        "https://x-access-token:[REDACTED]@github.com/",
        redacted,
    )
    return redacted


def _prepare_repo(repo: str, ref: str, tmp: Path) -> Path:
    """Clone a repository and return the checkout path."""
    repo_dir = tmp / "repo"
    token = _service_git_token()
    url = f"https://github.com/{repo}.git"
    shutil.rmtree(repo_dir, ignore_errors=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", "--branch", ref, url, str(repo_dir)],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
            env=_git_auth_env(token),
        )
    except subprocess.CalledProcessError as exc:
        output = "\n".join(part.strip() for part in (exc.stdout, exc.stderr) if part and part.strip())
        if len(output) > 1200:
            output = f"...\n{output[-1200:]}"
        detail = f"git clone failed for {repo}@{ref} with exit code {exc.returncode}"
        if output:
            detail = f"{detail}:\n{_redact_clone_error(output, token)}"
        raise RuntimeError(detail) from exc
    return repo_dir


# ── HTTP Request Handler ─────────────────────────────────────────────

class AiGatewayHandler(BaseHTTPRequestHandler):
    server_version = "AiGateway/1.0"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            _json_response(self, HTTPStatus.OK, {"status": "ok"})
            return

        m = re.match(r"^/v1/codex-audit/jobs/([a-f0-9]{24})$", self.path)
        if m:
            self._handle_get_job(m.group(1))
            return

        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/v1/codex-audit/jobs":
            self._handle_post_job()
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_post_job(self) -> None:
        try:
            claims = _authenticate(self.headers)
            length = int(self.headers.get("Content-Length", 0))
            if length > int(os.environ.get("CODEX_AUDIT_SERVICE_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES))):
                _json_response(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "payload too large"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            _validate_payload(payload)
            _validate_source_repo_org(claims, payload)
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
            return
        except (json.JSONDecodeError, ValueError) as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        task = _canonicalize_task(str(payload.get("task", "") or _detect_task_from_model(payload.get("model", ""))))

        if _task_requires_async(task):
            # Execute tasks run async (slow, repo clone)
            self._handle_async_job(payload)
        else:
            # Non-Codex tasks are rejected during validation; this is defensive.
            self._handle_sync_task(payload)

    def _handle_sync_task(self, payload: dict[str, Any]) -> None:
        """Defensive path for unsupported sync tasks."""
        try:
            output = _run_task(payload, repo_dir=None)
            _json_response(self, HTTPStatus.OK, {"status": "succeeded", "output": output})
        except (RuntimeError, ValueError) as exc:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "failed", "error": str(exc)})

    def _handle_async_job(self, payload: dict[str, Any]) -> None:
        """Execute tasks: submit job, respond with job_id, run in background."""
        dedupe_key = _job_dedupe_key(payload)
        existing = _find_existing_job(dedupe_key)
        if existing is not None:
            job_id, data = existing
            _json_response(self, HTTPStatus.ACCEPTED, _public_job_payload(job_id, data, deduped=True))
            return
        job_id = _next_job_id()
        _write_job(job_id, payload, dedupe_key)
        _json_response(self, HTTPStatus.ACCEPTED, {
            "job_id": job_id,
            "status": "queued",
            "poll_url": f"/v1/codex-audit/jobs/{job_id}",
        })
        threading.Thread(target=_run_job_background, args=(job_id, payload), daemon=True).start()

    def _handle_get_job(self, job_id: str) -> None:
        job = _read_job(job_id)
        if job is None:
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "job not found"})
            return
        _json_response(self, HTTPStatus.OK, _public_job_payload(job_id, job))

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # Suppress default HTTP server logs


CodexAuditServiceRequestHandler = AiGatewayHandler


def _codex_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEX_AUDIT_SERVICE_")
        and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    }


def _run_job_background(job_id: str, payload: dict[str, Any]) -> None:
    """Background thread for async tasks."""
    _update_job(job_id, "running")
    try:
        task = _canonicalize_task(str(payload.get("task", "") or _detect_task_from_model(payload.get("model", ""))))
        adapter = resolve_adapter(task, str(payload.get("model", "")))
        if os.environ.get("CODEX_AUDIT_SERVICE_FAKE_OUTPUT") is not None:
            output = _run_task(payload, repo_dir=None)
        else:
            with tempfile.TemporaryDirectory(prefix="ai-gateway-") as tmp:
                repo_dir = None
                if isinstance(adapter, CodexAdapter):
                    source_repository = payload.get("source_repository")
                    if not isinstance(source_repository, str) or not source_repository:
                        raise RuntimeError("source_repository required for Codex adapter async job")
                    repo_dir = _prepare_repo(
                        source_repository,
                        payload.get("source_ref", "main"),
                        Path(tmp),
                    )
                output = _run_task(payload, repo_dir=repo_dir)
        _update_job(job_id, "succeeded", output=output)
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        _update_job(job_id, "failed", error=str(exc))


# ── Main ─────────────────────────────────────────────────────────────

def main() -> None:
    port = int(os.environ.get("CODEX_AUDIT_SERVICE_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AiGatewayHandler)
    print(f"AiGateway listening on port {port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
