"""GitHub Actions OIDC token verification.

Extracted from codex_audit_service.py — shared JWT/OIDC validation logic.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = GITHUB_OIDC_ISSUER + "/.well-known/jwks"
_JWKS_CACHE: dict[str, Any] | None = None
_JWKS_CACHE_EXPIRES_AT = 0.0


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _split_csv_env(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {item.strip() for item in re.split(r"[\n,]", raw) if item.strip()}


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


def _require_trusted_strategy_drift_job(payload: dict[str, Any], job_workflow_ref: str) -> None:
    workflow_ref = str(payload.get("workflow_ref") or "")
    if "/.github/workflows/drift-check.yml@" not in workflow_ref:
        return
    trusted_prefix = "QuantStrategyLab/QuantPlatformKit/.github/workflows/reusable-drift-check.yml@"
    allowed_job_refs = _allowed_claim_patterns("CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS")
    trusted_qpk_refs = {
        value for value in allowed_job_refs if re.fullmatch(re.escape(trusted_prefix) + r"[0-9a-f]{40}", value)
    }
    if job_workflow_ref not in trusted_qpk_refs:
        raise PermissionError("OIDC strategy drift caller must use the trusted QPK reusable workflow")


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


def verify_github_oidc(
    token: str,
    *,
    audience: str = "quant-codex-audit",
    clock_skew_seconds: int = 60,
) -> dict[str, Any]:
    """Verify a GitHub Actions OIDC JWT and return its claims.

    Raises PermissionError if the token is invalid, expired, or from an
    un-allowed repository / workflow_ref / ref.
    """
    header, payload, signature, signing_input = _jwt_parts(token)
    if header.get("alg") != "RS256":
        raise PermissionError("OIDC token must use RS256")
    kid = str(header.get("kid") or "")
    keys = _load_jwks().get("keys", [])
    key = next((item for item in keys if isinstance(item, dict) and item.get("kid") == kid), None)
    if not key:
        raise PermissionError("OIDC signing key is unknown")
    _verify_rs256(signing_input, signature, key)

    # --- audience ---
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

    # --- time checks ---
    now = int(time.time())
    skew = int(os.environ.get("CODEX_AUDIT_SERVICE_CLOCK_SKEW_SECONDS", str(clock_skew_seconds)))
    exp = int(payload.get("exp", 0))
    nbf = int(payload.get("nbf", 0) or 0)
    iat = int(payload.get("iat", 0) or 0)
    if exp and now > exp + skew:
        raise PermissionError("OIDC token is expired")
    if nbf and now + skew < nbf:
        raise PermissionError("OIDC token is not active yet")
    if iat and now + skew < iat:
        raise PermissionError("OIDC token issue time is in the future")

    # --- allowlist claims ---
    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES", "repository", "repository")
    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS", "workflow_ref", "workflow_ref")
    _require_allowed_claim(payload, "CODEX_AUDIT_SERVICE_ALLOWED_REFS", "ref", "ref")
    raw_job_workflow_ref = payload.get("job_workflow_ref")
    if raw_job_workflow_ref is not None and not isinstance(raw_job_workflow_ref, str):
        raise PermissionError("OIDC job workflow ref must be a string")
    job_workflow_ref = raw_job_workflow_ref.strip() if isinstance(raw_job_workflow_ref, str) else ""
    if raw_job_workflow_ref is not None and not job_workflow_ref:
        raise PermissionError("OIDC job workflow ref cannot be empty")
    if job_workflow_ref:
        _require_allowed_claim(
            payload,
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS",
            "job_workflow_ref",
            "job workflow ref",
        )
        _require_trusted_strategy_drift_job(payload, job_workflow_ref)
    else:
        direct_repositories = _allowed_claim_patterns("CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES")
        if not direct_repositories:
            raise PermissionError("CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES is required")
        if not _claim_matches(str(payload.get("repository") or ""), direct_repositories):
            raise PermissionError("OIDC job workflow ref is required for this repository")
    _require_optional_allowed_claim(
        payload, "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES", "repository_visibility", "repository visibility"
    )
    return payload


def authenticate(headers: Any, *, audience: str = "quant-codex-audit") -> dict[str, Any]:
    """Authenticate an incoming HTTP request. Returns claims on success.

    Supports three auth modes:
    1. github-oidc — validates RS256 JWT from GitHub Actions (default, production)
    2. static-token — compares against CODEX_AUDIT_SERVICE_TOKEN (dashboard)
    3. none — no auth, requires CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS=true
    """
    mode = os.environ.get("CODEX_AUDIT_SERVICE_AUTH", "github-oidc").strip().lower()
    if mode == "none":
        if os.environ.get("CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS", "").strip().lower() not in {
            "1", "true", "yes", "on",
        }:
            raise PermissionError(
                "CODEX_AUDIT_SERVICE_AUTH=none requires CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS=true"
            )
        return {"repository": "local", "actor": "local", "run_id": "local", "auth_method": "none"}
    authorization = str(headers.get("Authorization") or "")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise PermissionError("missing bearer token")
    token = authorization[len(prefix):].strip()
    if not token:
        raise PermissionError("missing bearer token")

    # Static token check — allows dashboard read-only access
    static_token = os.environ.get("CODEX_AUDIT_SERVICE_TOKEN", "").strip()
    if static_token and token == static_token:
        return {"repository": "dashboard", "actor": "dashboard", "run_id": "dashboard", "auth_method": "static_token"}

    if mode in {"github-oidc", "oidc"}:
        claims = verify_github_oidc(token, audience=audience)
        claims["auth_method"] = "github_oidc"
        return claims
    raise PermissionError(f"unsupported CODEX_AUDIT_SERVICE_AUTH={mode!r}")
