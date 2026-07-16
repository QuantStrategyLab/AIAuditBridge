#!/usr/bin/env python3
"""Emit one bounded, non-sensitive PR review completion event."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping
import urllib.error
import urllib.parse
import urllib.request


EVENT_SCHEMA = "qsl.pr_review_event.v1"
EVENT_TASK = "pr_review_completed"
DEFAULT_AUDIENCE = "quant-codex-audit"
MAX_INPUT_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
REPOSITORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
HEAD_SHA_RE = re.compile(r"[0-9a-f]{40}")
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
REVIEW_OUTCOMES = frozenset({"success", "failure", "cancelled", "skipped"})


class ReviewEventEmissionError(ValueError):
    """Raised when the workflow event cannot be safely emitted."""


def _required_string(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if type(value) is not str or not value.strip():
        raise ReviewEventEmissionError(f"{name} is required")
    return value.strip()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReviewEventEmissionError(f"cannot read {path.name}") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise ReviewEventEmissionError(f"{path.name} exceeds size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewEventEmissionError(f"{path.name} is not valid JSON") from exc
    if type(value) is not dict:
        raise ReviewEventEmissionError(f"{path.name} must contain an object")
    return value


def _decision_flags(path_value: str) -> tuple[bool | None, bool | None]:
    if not path_value:
        return None, None
    path = Path(path_value)
    if not path.is_file():
        return None, None
    try:
        decision = _read_json_object(path)
    except ReviewEventEmissionError:
        return None, None
    blocked = decision.get("blocked")
    conflict = decision.get("contract_conflict")
    return (
        blocked if type(blocked) is bool else None,
        conflict if type(conflict) is bool else None,
    )


def build_payload(env: Mapping[str, str]) -> dict[str, Any]:
    repository = _required_string(env, "GITHUB_REPOSITORY")
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise ReviewEventEmissionError("GITHUB_REPOSITORY is invalid")
    workflow_run_id = _required_string(env, "GITHUB_RUN_ID")
    if RUN_ID_RE.fullmatch(workflow_run_id) is None:
        raise ReviewEventEmissionError("GITHUB_RUN_ID is invalid")
    review_outcome = _required_string(env, "CODEX_REVIEW_STEP_OUTCOME").lower()
    if review_outcome not in REVIEW_OUTCOMES:
        raise ReviewEventEmissionError("CODEX_REVIEW_STEP_OUTCOME is invalid")

    pr_number_value = _required_string(env, "CODEX_REVIEW_PR_NUMBER")
    if not pr_number_value.isascii() or not pr_number_value.isdigit():
        raise ReviewEventEmissionError("CODEX_REVIEW_PR_NUMBER is invalid")
    pr_number = int(pr_number_value)
    if pr_number <= 0 or pr_number > 2**53 - 1:
        raise ReviewEventEmissionError("CODEX_REVIEW_PR_NUMBER is invalid")
    head_sha = _required_string(env, "CODEX_REVIEW_HEAD_SHA")
    if HEAD_SHA_RE.fullmatch(head_sha) is None:
        raise ReviewEventEmissionError("CODEX_REVIEW_HEAD_SHA is invalid")

    blocked, contract_conflict = _decision_flags(str(env.get("CODEX_REVIEW_DECISION_PATH") or ""))
    metadata = {
        "schema": EVENT_SCHEMA,
        "repository": repository,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "workflow_run_id": workflow_run_id,
        "review_outcome": review_outcome,
        "blocked": blocked,
        "contract_conflict": contract_conflict,
    }
    return {
        "run_id": f"review:{repository}:{pr_number}:{head_sha}:{workflow_run_id}",
        "task": EVENT_TASK,
        "task_state": "completed",
        "mode": "review_only",
        "source_repository": repository,
        "metadata": metadata,
    }


def normalize_service_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReviewEventEmissionError("CODEX_AUDIT_SERVICE_URL is invalid")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ReviewEventEmissionError("CODEX_AUDIT_SERVICE_URL must use HTTPS")
    return raw


def request_github_oidc_token(env: Mapping[str, str], audience: str) -> str:
    request_url = _required_string(env, "ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = _required_string(env, "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    separator = "&" if "?" in request_url else "?"
    url = f"{request_url}{separator}audience={urllib.parse.quote(audience)}"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {request_token}",
            "Accept": "application/json",
            "User-Agent": "qsl-pr-review-event-bridge",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReviewEventEmissionError("GitHub OIDC response exceeds size limit")
    payload = json.loads(raw.decode("utf-8"))
    token = payload.get("value") if type(payload) is dict else None
    if type(token) is not str or not token:
        raise ReviewEventEmissionError("GitHub OIDC response is invalid")
    return token


def post_event(service_url: str, token: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{service_url}/v1/ai/automation/runs",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "qsl-pr-review-event-bridge",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReviewEventEmissionError("event service response exceeds size limit")
    response_body = json.loads(raw.decode("utf-8"))
    if type(response_body) is not dict or response_body.get("status") != "ok":
        raise ReviewEventEmissionError("event service response is invalid")
    notification = response_body.get("notification")
    if type(notification) is not dict or notification.get("status") not in {"sent", "deduplicated"}:
        raise ReviewEventEmissionError("review notification was not delivered")


def main() -> int:
    service_url_value = str(os.environ.get("CODEX_AUDIT_SERVICE_URL") or "").strip()
    if not service_url_value:
        print("::notice::Review event bridge skipped: CODEX_AUDIT_SERVICE_URL is not configured")
        return 0
    try:
        service_url = normalize_service_url(service_url_value)
        payload = build_payload(os.environ)
        audience = str(os.environ.get("CODEX_AUDIT_SERVICE_AUDIENCE") or DEFAULT_AUDIENCE).strip()
        token = request_github_oidc_token(os.environ, audience)
        for attempt in range(1, 4):
            try:
                post_event(service_url, token, payload)
                print(f"Review completion event recorded for {payload['source_repository']}")
                return 0
            except (OSError, urllib.error.URLError, ReviewEventEmissionError, json.JSONDecodeError):
                if attempt == 3:
                    raise
                time.sleep(attempt)
    except (OSError, urllib.error.URLError, ReviewEventEmissionError, json.JSONDecodeError) as exc:
        print(f"::warning::Review event bridge delivery failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
