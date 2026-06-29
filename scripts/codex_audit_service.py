#!/usr/bin/env python3
from __future__ import annotations

import base64
import fnmatch
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
import urllib.request


DEFAULT_AUDIENCE = "quant-codex-audit"
DEFAULT_MAX_REQUEST_BYTES = 2_000_000
DEFAULT_JOB_TTL_SECONDS = 86_400
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = GITHUB_OIDC_ISSUER + "/.well-known/jwks"
SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "CREDENTIAL", "API_KEY")
_JWKS_CACHE: dict[str, Any] | None = None
_JWKS_CACHE_EXPIRES_AT = 0.0
_JOB_WRITE_LOCK = threading.Lock()


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


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


def _allowed_claim_patterns(env_name: str) -> set[str]:
    return _split_csv_env(env_name)


def _claim_matches(value: str, patterns: set[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _require_allowed_claim(payload: dict[str, Any], env_name: str, claim_name: str, label: str) -> None:
    patterns = _allowed_claim_patterns(env_name)
    if not patterns:
        raise PermissionError(f"{env_name} is required")
    value = str(payload.get(claim_name) or "")
    if not value:
        raise PermissionError(f"OIDC {label} is missing")
    if not _claim_matches(value, patterns):
        raise PermissionError(f"OIDC {label} is not allowed")


def _require_optional_allowed_claim(payload: dict[str, Any], env_name: str, claim_name: str, label: str) -> None:
    patterns = _allowed_claim_patterns(env_name)
    if not patterns:
        return
    value = str(payload.get(claim_name) or "")
    if not value:
        raise PermissionError(f"OIDC {label} is missing")
    if not _claim_matches(value, patterns):
        raise PermissionError(f"OIDC {label} is not allowed")


def _load_jwks() -> dict[str, Any]:
    global _JWKS_CACHE, _JWKS_CACHE_EXPIRES_AT
    now = time.time()
    if _JWKS_CACHE is not None and now < _JWKS_CACHE_EXPIRES_AT:
        return _JWKS_CACHE
    jwks_file = os.environ.get("CODEX_AUDIT_SERVICE_JWKS_FILE", "").strip()
    if jwks_file:
        payload = json.loads(Path(jwks_file).read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(GITHUB_OIDC_JWKS_URL, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise PermissionError("GitHub OIDC JWKS response is invalid")
    _JWKS_CACHE = payload
    _JWKS_CACHE_EXPIRES_AT = now + 300
    return payload


def _jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise PermissionError("OIDC token must have three JWT segments")
    header_raw = _b64url_decode(parts[0])
    payload_raw = _b64url_decode(parts[1])
    signature = _b64url_decode(parts[2])
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    header = json.loads(header_raw.decode("utf-8"))
    payload = json.loads(payload_raw.decode("utf-8"))
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise PermissionError("OIDC token header or payload is invalid")
    return header, payload, signature, signing_input


def _verify_rs256(signing_input: bytes, signature: bytes, key: dict[str, Any]) -> None:
    if key.get("kty") != "RSA":
        raise PermissionError("OIDC signing key is not RSA")
    try:
        n = int.from_bytes(_b64url_decode(str(key["n"])), "big")
        e = int.from_bytes(_b64url_decode(str(key["e"])), "big")
    except KeyError as exc:
        raise PermissionError("OIDC signing key is missing RSA parameters") from exc
    key_bytes = (n.bit_length() + 7) // 8
    if len(signature) != key_bytes:
        raise PermissionError("OIDC signature length is invalid")
    decoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(key_bytes, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    if not decoded.startswith(b"\x00\x01"):
        raise PermissionError("OIDC signature padding is invalid")
    try:
        separator = decoded.index(b"\x00", 2)
    except ValueError as exc:
        raise PermissionError("OIDC signature padding separator is missing") from exc
    padding = decoded[2:separator]
    if len(padding) < 8 or any(byte != 0xFF for byte in padding):
        raise PermissionError("OIDC signature padding is invalid")
    if decoded[separator + 1 :] != digest_info:
        raise PermissionError("OIDC signature digest does not match")


def _verify_github_oidc(token: str) -> dict[str, Any]:
    header, payload, signature, signing_input = _jwt_parts(token)
    if header.get("alg") != "RS256":
        raise PermissionError("OIDC token must use RS256")
    kid = str(header.get("kid") or "")
    keys = _load_jwks().get("keys", [])
    key = next((item for item in keys if isinstance(item, dict) and item.get("kid") == kid), None)
    if not key:
        raise PermissionError("OIDC signing key is unknown")
    _verify_rs256(signing_input, signature, key)

    audience = os.environ.get("CODEX_AUDIT_SERVICE_AUDIENCE", DEFAULT_AUDIENCE).strip() or DEFAULT_AUDIENCE
    token_audience = payload.get("aud")
    if isinstance(token_audience, str):
        audiences = {token_audience}
    elif isinstance(token_audience, list):
        audiences = {str(item) for item in token_audience}
    else:
        audiences = set()
    if audience not in audiences:
        raise PermissionError("OIDC audience is not allowed")
    if payload.get("iss") != GITHUB_OIDC_ISSUER:
        raise PermissionError("OIDC issuer is not allowed")

    now = int(time.time())
    skew = int(os.environ.get("CODEX_AUDIT_SERVICE_CLOCK_SKEW_SECONDS", "60"))
    exp = int(payload.get("exp", 0))
    nbf = int(payload.get("nbf", 0) or 0)
    iat = int(payload.get("iat", 0) or 0)
    if exp and now > exp + skew:
        raise PermissionError("OIDC token is expired")
    if nbf and now + skew < nbf:
        raise PermissionError("OIDC token is not active yet")
    if iat and now + skew < iat:
        raise PermissionError("OIDC token issue time is in the future")

    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES", "repository", "repository")
    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS", "workflow_ref", "workflow_ref")
    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_REFS", "ref", "ref")
    _require_optional_allowed_claim(
        payload,
        "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES",
        "repository_visibility",
        "repository visibility",
    )
    return payload


def _authenticate(headers: Any) -> dict[str, Any]:
    mode = os.environ.get("CODEX_AUDIT_SERVICE_AUTH", "github-oidc").strip().lower()
    if mode == "none":
        if os.environ.get("CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS", "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise PermissionError(
                "CODEX_AUDIT_SERVICE_AUTH=none requires CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS=true"
            )
        return {"repository": "local", "actor": "local", "run_id": "local"}
    authorization = str(headers.get("Authorization") or "")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise PermissionError("missing bearer token")
    token = authorization[len(prefix) :].strip()
    if not token:
        raise PermissionError("missing bearer token")
    if mode in {"github-oidc", "oidc"}:
        return _verify_github_oidc(token)
    raise PermissionError(f"unsupported CODEX_AUDIT_SERVICE_AUTH={mode!r}")


def _validate_payload(payload: dict[str, Any]) -> None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    source_repository = payload.get("source_repository")
    if not isinstance(source_repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repository):
        raise ValueError("source_repository must be an owner/repository string")
    allowed_sources = _split_csv_env("CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES")
    if allowed_sources and source_repository not in allowed_sources:
        raise PermissionError("source_repository is not allowed")
    mode = payload.get("mode")
    if mode not in {"review_only", "review_and_fix"}:
        raise ValueError("mode must be review_only or review_and_fix")
    # model is optional — defaults to CODEX_AUDIT_SERVICE_MODEL env var


def _codex_command(output_last_message: Path, *, model_override: str | None = None) -> list[str]:
    codex = shutil.which(os.environ.get("CODEX_AUDIT_SERVICE_CODEX_BIN", "codex"))
    if not codex:
        raise RuntimeError("codex CLI was not found on the service host")
    command = [
        codex,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        os.environ.get("CODEX_AUDIT_SERVICE_SANDBOX", "read-only").strip() or "read-only",
        "--output-last-message",
        str(output_last_message),
    ]
    model = model_override or os.environ.get("CODEX_AUDIT_SERVICE_MODEL", "").strip()
    if model:
        command.extend(["--model", model])
    command.append("-")
    return command


def _codex_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEX_AUDIT_SERVICE_")
        and not any(marker in key.upper() for marker in SECRET_ENV_MARKERS)
    }


def _run_codex(payload: dict[str, Any]) -> str:
    fake_output = os.environ.get("CODEX_AUDIT_SERVICE_FAKE_OUTPUT")
    if fake_output is not None:
        return fake_output
    prompt = str(payload["prompt"])
    model = str(payload.get("model") or "").strip() or None
    timeout_seconds = int(payload.get("timeout_seconds") or os.environ.get("CODEX_AUDIT_SERVICE_TIMEOUT_SECONDS", "2700"))
    with tempfile.TemporaryDirectory() as tmp:
        output_last_message = Path(tmp) / "codex-final-message.md"
        completed = subprocess.run(
            _codex_command(output_last_message, model_override=model),
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            env=_codex_env(),
        )
        if completed.returncode != 0:
            detail = (completed.stdout[-4000:] + completed.stderr[-4000:]).strip()
            raise RuntimeError("codex exec failed" + (f":\n{detail}" if detail else ""))
        if output_last_message.exists() and output_last_message.read_text(encoding="utf-8").strip():
            return output_last_message.read_text(encoding="utf-8")
        return completed.stdout


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


def _job_timeout_seconds(job: dict[str, Any]) -> int:
    try:
        return int(job.get("timeout_seconds") or os.environ.get("CODEX_AUDIT_SERVICE_TIMEOUT_SECONDS", "2700"))
    except (TypeError, ValueError):
        return 2700


def _mark_stale_job_failed(job: dict[str, Any]) -> dict[str, Any]:
    if job.get("status") not in {"queued", "running"}:
        return job
    timeout_seconds = _job_timeout_seconds(job)
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
    }
    if job.get("status") == "succeeded":
        payload["output"] = str(job.get("output") or "")
    if job.get("status") == "failed":
        payload["error"] = str(job.get("error") or "")
    return payload


def _run_job(job_id: str, payload: dict[str, Any]) -> None:
    try:
        job = _read_job(job_id)
        job["status"] = "running"
        job["updated_at"] = _now()
        _write_job(job)
        output = _run_codex(payload)
        job = _read_job(job_id)
        job["status"] = "succeeded"
        job["updated_at"] = _now()
        job["output"] = output
        job.pop("error", None)
        _write_job(job)
    except Exception as exc:  # noqa: BLE001 - background job boundary must persist failure.
        try:
            job = _read_job(job_id)
        except Exception:
            job = {"job_id": job_id, "created_at": _now()}
        job["status"] = "failed"
        job["updated_at"] = _now()
        job["error"] = str(exc)[-4000:]
        _write_job(job)
        print(f"[codex-audit-service] async job failed job_id={job_id}: {type(exc).__name__}", file=sys.stderr)


def _submit_job(claims: dict[str, Any], payload: dict[str, Any]) -> dict[str, object]:
    _cleanup_expired_jobs()
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
        "task": str(payload.get("task") or ""),
        "mode": str(payload.get("mode") or ""),
        "timeout_seconds": int(payload.get("timeout_seconds") or os.environ.get("CODEX_AUDIT_SERVICE_TIMEOUT_SECONDS", "2700")),
    }
    _write_job(job)
    thread = threading.Thread(target=_run_job, args=(job_id, payload), name=f"codex-audit-job-{job_id}", daemon=True)
    thread.start()
    return _public_job_payload(job)


def _read_request_payload(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    max_request_bytes = int(os.environ.get("CODEX_AUDIT_SERVICE_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES)))
    if length <= 0:
        raise ValueError("request body is empty")
    if length > max_request_bytes:
        raise ValueError("request body is too large")
    payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    _validate_payload(payload)
    return payload


class CodexAuditServiceRequestHandler(BaseHTTPRequestHandler):
    server_version = "CodexAuditService/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - BaseHTTPRequestHandler API.
        print("[codex-audit-service] " + format % args, file=sys.stderr)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            _json_response(self, HTTPStatus.OK, {"status": "ok"})
            return
        match = re.fullmatch(r"/v1/codex-audit/jobs/([A-Za-z0-9_-]{24,96})", self.path)
        if not match:
            _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "not found"})
            return
        try:
            claims = _authenticate(self.headers)
            job = _mark_stale_job_failed(_read_job(match.group(1)))
            _assert_job_access(job, claims)
            _json_response(self, HTTPStatus.OK, _public_job_payload(job))
        except FileNotFoundError:
            _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "job not found"})
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
        except ValueError as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - service boundary must fail closed with context.
            print(f"[codex-audit-service] {type(exc).__name__}: {exc}", file=sys.stderr)
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "error": str(exc)})

    def do_POST(self) -> None:
        if self.path not in {"/v1/codex-audit", "/v1/codex-audit/jobs"}:
            _json_response(self, HTTPStatus.NOT_FOUND, {"status": "error", "error": "not found"})
            return
        try:
            claims = _authenticate(self.headers)
            payload = _read_request_payload(self)
            repository = str(claims.get("repository") or "")
            run_id = str(claims.get("run_id") or "")
            source_repository = str(payload.get("source_repository") or "")
            task = str(payload.get("task") or "")
            print(
                "[codex-audit-service] accepted request "
                f"repository={repository} run_id={run_id} source_repository={source_repository} task={task}",
                file=sys.stderr,
            )
            if self.path == "/v1/codex-audit/jobs":
                job = _submit_job(claims, payload)
                _json_response(self, HTTPStatus.ACCEPTED, job)
                return
            output = _run_codex(payload)
            _json_response(self, HTTPStatus.OK, {"status": "ok", "output": output})
        except PermissionError as exc:
            _json_response(self, HTTPStatus.UNAUTHORIZED, {"status": "error", "error": str(exc)})
        except ValueError as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"status": "error", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - service boundary must fail closed with context.
            print(f"[codex-audit-service] {type(exc).__name__}: {exc}", file=sys.stderr)
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "error": str(exc)})


def main() -> int:
    os.umask(0o077)
    host = os.environ.get("CODEX_AUDIT_SERVICE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("CODEX_AUDIT_SERVICE_PORT", "8797"))
    server = ThreadingHTTPServer((host, port), CodexAuditServiceRequestHandler)
    print(f"[codex-audit-service] listening on http://{host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
