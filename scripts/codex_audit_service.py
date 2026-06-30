#!/usr/bin/env python3
"""AI Gateway — unified service for LLM analysis and Codex execution.

Architecture:

    POST /v1/codex-audit/jobs  ──▶ AiGateway
    {                                │
      "task": "analyze" | "execute", │──▶ LlmAdapter (API call, no repo)
      "model": "claude-sonnet-4-6",  │──▶ CodexAdapter (codex exec, needs repo)
      "prompt": "...",               │──▶ FutureAdapter (extensible)
      ...                            │
    }                                │

Benefits:
  - API keys live on the VPS (one place), not in N repos
  - New AI backends = new adapter class, no service changes
  - Same auth, same job lifecycle, same polling for all tasks
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

SUPPORTED_TASKS = frozenset({"analyze", "execute"})
SUPPORTED_MODES = frozenset({"review_only", "review_and_fix"})


# ── Adapter Protocol ──────────────────────────────────────────────────


class AiAdapter(ABC):
    """Base adapter for AI backends.

    Each adapter implements one AI backend:
      - LlmAdapter: calls Claude/GPT API directly (text-only, no repo)
      - CodexAdapter: runs codex exec (code changes, repo checkout)
      - FutureAdapter: your custom backend

    The adapter receives:
      - prompt: the full instruction text
      - model: which model to use (e.g. "claude-sonnet-4-6")
      - timeout_seconds: max execution time
      - repo_dir: optional path to cloned repo (for execute tasks)
    """

    @abstractmethod
    def run(self, *, prompt: str, model: str, timeout_seconds: int,
            repo_dir: Path | None = None) -> str:
        ...


class LlmAdapter(AiAdapter):
    """Direct API call adapter — calls Anthropic/OpenAI APIs directly.

    Model routing:
      claude-*       → Anthropic API (ANTHROPIC_API_KEY)
      gpt-*          → OpenAI API     (OPENAI_API_KEY)

    No repo cloning needed. Used for: code review, strategy analysis.
    """

    def run(self, *, prompt: str, model: str, timeout_seconds: int,
            repo_dir: Path | None = None) -> str:
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
            repo_dir: Path | None = None) -> str:
        fake = os.environ.get("CODEX_AUDIT_SERVICE_FAKE_OUTPUT")
        if fake is not None:
            return fake

        if repo_dir is None or not repo_dir.exists():
            raise RuntimeError("CodexAdapter requires a valid repo_dir (execute task)")

        output_path = repo_dir / ".codex-output" / "final-message.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        completed = subprocess.run(
            self._build_command(output_path, model),
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

    def _build_command(self, output_path: Path, model: str) -> list[str]:
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
    "analyze": LlmAdapter(),
    "execute": CodexAdapter(),
}


def resolve_adapter(task: str, model: str) -> AiAdapter:
    """Resolve the adapter for a task. Auto-detect analyze vs execute from model if task is empty."""
    resolved_task = task or _detect_task_from_model(model)
    adapter = _ADAPTER_REGISTRY.get(resolved_task)
    if adapter is None:
        raise ValueError(f"Unsupported task={resolved_task!r}. Supported: {sorted(_ADAPTER_REGISTRY)}")
    return adapter


def _detect_task_from_model(model: str) -> str:
    """Determine the task type from the model name.
    Claude/GPT models → analyze (API call).
    Others (codex, empty) → execute (codex CLI).
    """
    m = model.lower().strip()
    if m.startswith("claude") or m.startswith("gpt"):
        return "analyze"
    return "execute"


def _detect_provider(model: str) -> str:
    m = model.lower().strip()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gpt"):
        return "openai"
    return "openai"


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
    _require_optional_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES",
                                    "repository_visibility", "repository visibility")
    return payload


def _authenticate(headers: Any) -> dict[str, Any]:
    mode = os.environ.get("CODEX_AUDIT_SERVICE_AUTH", "github-oidc").strip().lower()
    if mode == "none":
        allow = os.environ.get("CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS", "")
        if allow.strip().lower() not in {"1", "true", "yes", "on"}:
            raise PermissionError("AUTH=none requires ALLOW_NO_AUTH_FOR_LOCAL_TESTS=true")
        return {"repository": "local", "actor": "local"}
    auth = str(headers.get("Authorization") or "")
    if not auth.startswith("Bearer "):
        raise PermissionError("Missing bearer token")
    token = auth[7:].strip()
    if mode in {"github-oidc", "oidc"}:
        return _verify_github_oidc(token)
    raise PermissionError(f"Unsupported auth mode: {mode}")


# ── Payload validation ───────────────────────────────────────────────

def _validate_payload(payload: dict[str, Any]) -> None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    task = payload.get("task", "").strip().lower() or ""
    model = payload.get("model", "").strip() or ""
    mode = payload.get("mode", "").strip().lower() or "review_only"

    # If no task specified, auto-detect from model
    if not task:
        task = _detect_task_from_model(model)
    if task not in SUPPORTED_TASKS:
        raise ValueError(f"Unsupported task: {task!r}")

    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported mode: {mode!r}")

    if task == "execute":
        source_repo = payload.get("source_repository")
        if not isinstance(source_repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repo):
            raise ValueError("source_repository required for execute tasks")
        allowed = _split_csv_env("CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES")
        if allowed and source_repo not in allowed:
            raise PermissionError(f"source_repository {source_repo} not allowed")


# ── Task execution ───────────────────────────────────────────────────

def _run_task(payload: dict[str, Any], *, repo_dir: Path | None = None) -> str:
    task = (payload.get("task", "") or _detect_task_from_model(payload.get("model", ""))).strip().lower()
    model = (payload.get("model") or "").strip()
    prompt = str(payload["prompt"])
    timeout = int(payload.get("timeout_seconds") or os.environ.get(
        "CODEX_AUDIT_SERVICE_TIMEOUT_SECONDS", "2700"))

    adapter = resolve_adapter(task, model)
    return adapter.run(
        prompt=prompt, model=model,
        timeout_seconds=timeout, repo_dir=repo_dir,
    )


# ── Job management ───────────────────────────────────────────────────

def _job_dir() -> Path:
    default = tempfile.gettempdir() / "codex-audit-service-jobs"
    path = Path(os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR", str(default))).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _next_job_id() -> str:
    return secrets.token_hex(12)


def _job_path(job_id: str) -> Path:
    return _job_dir() / f"{job_id}.json"


def _write_job(job_id: str, payload: dict[str, Any]) -> None:
    with _JOB_WRITE_LOCK:
        _job_path(job_id).write_text(json.dumps({
            "status": "queued", "payload": payload, "output": "", "error": "",
            "created_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")


def _update_job(job_id: str, status: str, output: str = "", error: str = "") -> None:
    with _JOB_WRITE_LOCK:
        path = _job_path(job_id)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            data["status"] = status
            if output:
                data["output"] = output
            if error:
                data["error"] = error
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


def _prepare_repo(repo: str, ref: str, tmp: Path) -> Path:
    """Clone a repository and return the checkout path."""
    repo_dir = tmp / "repo"
    token = ""
    for env_name in ("CROSS_REPO_GITHUB_APP_PRIVATE_KEY", "CROSS_REPO_GIT_TOKEN", "GH_TOKEN"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            token = raw
            break
    url = f"https://x-access-token:{token}@github.com/{repo}" if token else f"https://github.com/{repo}"
    shutil.rmtree(repo_dir, ignore_errors=True)
    subprocess.run(
        ["git", "clone", "--depth=1", "--branch", ref, url, str(repo_dir)],
        capture_output=True, text=True, check=True, timeout=120,
    )
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
            _authenticate(self.headers)
            length = int(self.headers.get("Content-Length", 0))
            if length > int(os.environ.get("CODEX_AUDIT_SERVICE_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES))):
                _json_response(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "payload too large"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            _validate_payload(payload)
        except (json.JSONDecodeError, ValueError, PermissionError) as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        task = (payload.get("task") or _detect_task_from_model(payload.get("model", ""))).strip().lower()

        if task == "execute":
            # Execute tasks run async (slow, repo clone)
            self._handle_async_job(payload)
        else:
            # Analyze tasks run sync (fast, API call)
            self._handle_sync_task(payload)

    def _handle_sync_task(self, payload: dict[str, Any]) -> None:
        """Analyze tasks: call LLM API directly, return result inline."""
        try:
            output = _run_task(payload, repo_dir=None)
            _json_response(self, HTTPStatus.OK, {"status": "succeeded", "output": output})
        except (RuntimeError, ValueError) as exc:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "failed", "error": str(exc)})

    def _handle_async_job(self, payload: dict[str, Any]) -> None:
        """Execute tasks: submit job, respond with job_id, run in background."""
        job_id = _next_job_id()
        _write_job(job_id, payload)
        _json_response(self, HTTPStatus.ACCEPTED, {
            "job_id": job_id, "status": "queued",
            "poll_url": f"/v1/codex-audit/jobs/{job_id}",
        })
        threading.Thread(target=_run_job_background, args=(job_id, payload), daemon=True).start()

    def _handle_get_job(self, job_id: str) -> None:
        job = _read_job(job_id)
        if job is None:
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "job not found"})
            return
        resp: dict[str, object] = {"status": str(job["status"])}
        if job.get("output"):
            resp["output"] = job["output"]
        if job.get("error"):
            resp["error"] = job["error"]
        _json_response(self, HTTPStatus.OK, resp)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # Suppress default HTTP server logs


def _run_job_background(job_id: str, payload: dict[str, Any]) -> None:
    """Background thread for execute tasks (repo clone + codex exec)."""
    _update_job(job_id, "running")
    try:
        with tempfile.TemporaryDirectory(prefix="ai-gateway-") as tmp:
            repo_dir = _prepare_repo(
                payload["source_repository"],
                payload.get("source_ref", "main"),
                Path(tmp),
            )
            output = _run_task(payload, repo_dir=repo_dir)
            _update_job(job_id, "succeeded", output=output)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
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
