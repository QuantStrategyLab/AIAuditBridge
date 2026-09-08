"""Read Codex account rate-limit snapshots through the local Codex app-server."""

from __future__ import annotations

import json
import math
import os
import select
import shutil
import subprocess
import time
from typing import Any

from service.adapters.codex_adapter import _codex_env


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int else None


def _remaining_percent(used_percent: float | None) -> float | None:
    if used_percent is None:
        return None
    return max(0, min(100, 100 - used_percent))


def _window(raw: Any) -> dict[str, int | float | None] | None:
    if not isinstance(raw, dict):
        return None
    used_percent = raw.get("usedPercent")
    if type(used_percent) not in (int, float) or not math.isfinite(used_percent) or not 0 <= used_percent <= 100:
        used_percent = None
    return {
        "used_percent": used_percent,
        "remaining_percent": _remaining_percent(used_percent),
        "window_duration_mins": _int_or_none(raw.get("windowDurationMins")),
        "resets_at": _int_or_none(raw.get("resetsAt")),
    }


def _credits(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "has_credits": bool(raw.get("hasCredits")),
        "unlimited": bool(raw.get("unlimited")),
        "balance": str(raw.get("balance")) if raw.get("balance") is not None else None,
    }


def _rate_limit(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "limit_id": raw.get("limitId"),
        "limit_name": raw.get("limitName"),
        "plan_type": raw.get("planType"),
        "primary": _window(raw.get("primary")),
        "secondary": _window(raw.get("secondary")),
        "credits": _credits(raw.get("credits")),
        "rate_limit_reached_type": raw.get("rateLimitReachedType"),
    }


def _send(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("codex app-server stdin unavailable")
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen[str], response_id: int, deadline: float) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("codex app-server stdout unavailable")
    while time.monotonic() < deadline:
        timeout = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            break
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == response_id:
            if "error" in message:
                error = message.get("error") or {}
                raise RuntimeError(str(error.get("message") or "codex app-server request failed"))
            return message
    raise TimeoutError(f"codex app-server response {response_id} timed out")


def _read_models(proc: subprocess.Popen[str], deadline: float) -> list[dict[str, Any]] | None:
    """Read only model metadata, with bounded pagination; never generate text."""
    models = []
    cursor = None
    seen = set()
    for request_id in range(3, 13):
        params: dict[str, Any] = {"limit": 100, "includeHidden": False}
        if cursor:
            params["cursor"] = cursor
        _send(proc, {"method": "model/list", "id": request_id, "params": params})
        result = _read_response(proc, request_id, deadline).get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            return None
        for item in result["data"]:
            if not isinstance(item, dict) or item.get("hidden") is True:
                continue
            model, efforts = item.get("model"), item.get("supportedReasoningEfforts")
            if not isinstance(model, str) or not model or not isinstance(efforts, list):
                continue
            models.append({"model": model, "supported_reasoning_efforts": [
                effort["reasoningEffort"] for effort in efforts
                if isinstance(effort, dict) and isinstance(effort.get("reasoningEffort"), str)
            ]})
        cursor = result.get("nextCursor")
        if cursor is None:
            return models
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            return None
        seen.add(cursor)
    return None


def read_codex_rate_limits(timeout_seconds: float | None = None, *, include_models: bool = False) -> dict[str, Any] | None:
    """Return a sanitized Codex account rate-limit snapshot, or None when disabled/unavailable."""
    if not _bool_env("CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE", False):
        return None
    codex_bin = shutil.which(os.environ.get("CODEX_AUDIT_SERVICE_CODEX_BIN", "codex"))
    if not codex_bin:
        return None
    proc: subprocess.Popen[str] | None = None
    try:
        timeout = timeout_seconds if timeout_seconds is not None else float(os.environ.get("CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_TIMEOUT_SECONDS", "8"))
        deadline = time.monotonic() + max(1.0, timeout)
        proc = subprocess.Popen(
            [codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=_codex_env(),
        )
        _send(
            proc,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "aiauditbridge_dashboard",
                        "title": "AIAuditBridge Dashboard",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        _read_response(proc, 1, deadline)
        _send(proc, {"method": "initialized", "params": {}})
        _send(proc, {"method": "account/rateLimits/read", "id": 2, "params": {}})
        response = _read_response(proc, 2, deadline)
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            return None
        raw_limits_by_id = result.get("rateLimitsByLimitId") if isinstance(result.get("rateLimitsByLimitId"), dict) else {}
        limits_by_id = {str(key): value for key, value in ((key, _rate_limit(value)) for key, value in raw_limits_by_id.items()) if value}
        primary = limits_by_id.get("codex") or _rate_limit(result.get("rateLimits"))
        if not primary:
            return None
        snapshot = {
            "source": "codex_app_server",
            "status": "available",
            "updated_at": int(time.time()),
            "rate_limits": primary,
            "rate_limits_by_limit_id": limits_by_id or None,
        }
        if include_models:
            try:
                snapshot["available_models"] = _read_models(proc, deadline)
            except (OSError, RuntimeError, TimeoutError, TypeError, ValueError):
                snapshot["available_models"] = None
        return snapshot
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError):
        return None
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()
