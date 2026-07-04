"""Read Anthropic organization usage/cost snapshots with an Admin API key."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_ANTHROPIC_ADMIN_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


def _admin_key() -> str:
    return os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip() or os.environ.get("ANTHROPIC_ADMIN_API_KEY", "").strip()


def _int_env(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _iso_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _request_json(path: str, params: dict[str, Any], admin_key: str, timeout_seconds: float) -> dict[str, Any]:
    base_url = os.environ.get("CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_BASE_URL", DEFAULT_ANTHROPIC_ADMIN_BASE_URL).rstrip("/")
    if urlparse(base_url).scheme != "https":
        raise ValueError("Anthropic Admin API base URL must use HTTPS")
    query = urlencode(params, doseq=True)
    request = Request(
        f"{base_url}{path}?{query}",
        headers={
            "anthropic-version": os.environ.get("ANTHROPIC_VERSION", DEFAULT_ANTHROPIC_VERSION),
            "x-api-key": admin_key,
            "User-Agent": "AIAuditBridge/1.0",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Anthropic Admin API response was not an object")
    return data


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _result_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for bucket in payload.get("data", []):
        if not isinstance(bucket, dict):
            continue
        results = bucket.get("results")
        if isinstance(results, list):
            items.extend(item for item in results if isinstance(item, dict))
        else:
            items.append(bucket)
    return items


def _sum_usage(payload: dict[str, Any]) -> dict[str, int]:
    totals = {
        "uncached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "num_model_requests": 0,
    }
    for result in _result_items(payload):
        totals["uncached_input_tokens"] += int(_num(result.get("uncached_input_tokens", result.get("input_tokens"))))
        totals["cache_creation_input_tokens"] += int(_num(result.get("cache_creation_input_tokens")))
        totals["cache_read_input_tokens"] += int(_num(result.get("cache_read_input_tokens", result.get("cached_input_tokens"))))
        totals["output_tokens"] += int(_num(result.get("output_tokens")))
        totals["num_model_requests"] += int(_num(result.get("num_model_requests", result.get("request_count"))))
    totals["input_tokens"] = (
        totals["uncached_input_tokens"] + totals["cache_creation_input_tokens"] + totals["cache_read_input_tokens"]
    )
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


def _sum_costs(payload: dict[str, Any]) -> dict[str, Any]:
    total_cents = 0.0
    result_count = 0
    for result in _result_items(payload):
        amount = result.get("amount", result.get("cost"))
        if isinstance(amount, dict):
            total_cents += _num(amount.get("value"))
        else:
            total_cents += _num(amount)
        result_count += 1
    return {
        "total_cost": round(total_cents / 100, 4),
        "currency": "usd",
        "result_count": result_count,
    }


def read_anthropic_admin_usage(now: int | None = None, timeout_seconds: float | None = None) -> dict[str, Any] | None:
    """Return a sanitized Anthropic org usage/cost snapshot, or None when unavailable."""
    admin_key = _admin_key()
    if not admin_key:
        return None
    days = _int_env("CODEX_AUDIT_SERVICE_ANTHROPIC_USAGE_WINDOW_DAYS", 7, minimum=1, maximum=31)
    end_time = int(now if now is not None else time.time())
    start_time = end_time - days * 86400
    timeout = timeout_seconds if timeout_seconds is not None else float(
        _int_env("CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_TIMEOUT_SECONDS", 8, minimum=1, maximum=60)
    )
    params: dict[str, Any] = {
        "starting_at": _iso_utc(start_time),
        "ending_at": _iso_utc(end_time),
        "limit": days,
    }
    usage_params = {**params, "bucket_width": "1d"}
    api_key_ids = _split_csv_env("CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_API_KEY_IDS")
    if api_key_ids:
        usage_params["api_key_ids[]"] = api_key_ids
    workspace_ids = _split_csv_env("CODEX_AUDIT_SERVICE_ANTHROPIC_ADMIN_WORKSPACE_IDS")
    if workspace_ids:
        usage_params["workspace_ids[]"] = workspace_ids

    usage = None
    costs = None
    try:
        usage = _sum_usage(_request_json("/organizations/usage_report/messages", usage_params, admin_key, timeout))
    except (HTTPError, OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError):
        usage = None
    if not api_key_ids and not workspace_ids:
        try:
            costs = _sum_costs(_request_json("/organizations/cost_report", params, admin_key, timeout))
        except (HTTPError, OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError):
            costs = None
    if usage is None and costs is None:
        return None
    return {
        "source": "anthropic_admin_api",
        "status": "available",
        "updated_at": int(time.time()),
        "window_days": days,
        "start_time": start_time,
        "end_time": end_time,
        "filtered_api_key_count": len(api_key_ids),
        "filtered_workspace_count": len(workspace_ids),
        "messages": usage,
        "costs": costs,
    }
