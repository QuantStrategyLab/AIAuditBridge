"""Read OpenAI organization usage/cost snapshots with an Admin API key."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_OPENAI_ADMIN_BASE_URL = "https://api.openai.com/v1"


def _admin_key() -> str:
    return os.environ.get("OPENAI_ADMIN_KEY", "").strip() or os.environ.get("OPENAI_ADMIN_API_KEY", "").strip()


def _int_env(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _usage_window(end_time: int, billing_timezone: str = "UTC") -> tuple[int, int]:
    configured = os.environ.get("CODEX_AUDIT_SERVICE_OPENAI_USAGE_WINDOW_DAYS", "").strip()
    if configured:
        days = _int_env("CODEX_AUDIT_SERVICE_OPENAI_USAGE_WINDOW_DAYS", 1, minimum=1, maximum=31)
        return end_time - days * 86400, days
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(billing_timezone)
    except Exception:  # noqa: BLE001 - invalid configuration must use the guard's UTC fallback.
        zone = UTC
    current = datetime.fromtimestamp(end_time, zone)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), (current.date() - start.date()).days + 1


def _split_csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _request_json(path: str, params: dict[str, Any], admin_key: str, timeout_seconds: float) -> dict[str, Any]:
    base_url = os.environ.get("CODEX_AUDIT_SERVICE_OPENAI_ADMIN_BASE_URL", DEFAULT_OPENAI_ADMIN_BASE_URL).rstrip("/")
    if urlparse(base_url).scheme != "https":
        raise ValueError("OpenAI Admin API base URL must use HTTPS")
    query = urlencode(params, doseq=True)
    request = Request(
        f"{base_url}{path}?{query}",
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("OpenAI Admin API response was not an object")
    return data


def _request_pages(path: str, params: dict[str, Any], admin_key: str, timeout_seconds: float) -> list[dict[str, Any]]:
    pages = []
    page_token = ""
    seen_page_tokens: set[str] = set()
    while True:
        request_params = dict(params)
        if page_token:
            request_params["page"] = page_token
        page = _request_json(path, request_params, admin_key, timeout_seconds)
        pages.append(page)
        next_page = str(page.get("next_page") or "")
        if not page.get("has_more") or not next_page or next_page in seen_page_tokens:
            break
        seen_page_tokens.add(next_page)
        page_token = next_page
    return pages


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sum_usage(payloads: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "input_cached_tokens": 0,
        "input_audio_tokens": 0,
        "output_audio_tokens": 0,
        "num_model_requests": 0,
    }
    for payload in payloads:
        for bucket in payload.get("data", []):
            if not isinstance(bucket, dict):
                continue
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                for key in totals:
                    totals[key] += int(_num(result.get(key)))
    return totals


def _sum_costs(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    by_currency: dict[str, float] = {}
    result_count = 0
    for payload in payloads:
        for bucket in payload.get("data", []):
            if not isinstance(bucket, dict):
                continue
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                amount = result.get("amount")
                if not isinstance(amount, dict):
                    continue
                currency = str(amount.get("currency") or "usd").lower()
                by_currency[currency] = by_currency.get(currency, 0.0) + _num(amount.get("value"))
                result_count += 1
    currency = "usd" if "usd" in by_currency else next(iter(by_currency), "usd")
    return {
        "total_cost": round(by_currency.get(currency, 0.0), 4),
        "currency": currency,
        "result_count": result_count,
    }


def read_openai_admin_usage(
    now: int | None = None,
    timeout_seconds: float | None = None,
    billing_timezone: str = "UTC",
) -> dict[str, Any] | None:
    """Return a sanitized OpenAI completions usage snapshot, or None when unavailable."""
    admin_key = _admin_key()
    if not admin_key:
        return None
    end_time = int(now if now is not None else time.time())
    start_time, days = _usage_window(end_time, billing_timezone)
    timeout = timeout_seconds if timeout_seconds is not None else float(
        _int_env("CODEX_AUDIT_SERVICE_OPENAI_ADMIN_TIMEOUT_SECONDS", 8, minimum=1, maximum=60)
    )
    params: dict[str, Any] = {
        "start_time": start_time,
        "end_time": end_time,
        "bucket_width": "1d",
        "limit": days,
    }
    project_ids = _split_csv_env("CODEX_AUDIT_SERVICE_OPENAI_ADMIN_PROJECT_IDS")
    if project_ids:
        params["project_ids"] = project_ids
    api_key_ids = _split_csv_env("CODEX_AUDIT_SERVICE_OPENAI_ADMIN_API_KEY_IDS")
    usage_params = dict(params)
    if api_key_ids:
        usage_params["api_key_ids"] = api_key_ids
    cost_params = {
        "start_time": start_time,
        "end_time": end_time,
        "bucket_width": "1d",
        "limit": days,
    }

    usage = None
    costs = None
    try:
        usage = _sum_usage(_request_pages("/organization/usage/completions", usage_params, admin_key, timeout))
    except (HTTPError, OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError):
        usage = None
    try:
        costs = _sum_costs(_request_pages("/organization/costs", cost_params, admin_key, timeout))
        costs["scope"] = "organization"
    except (HTTPError, OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError):
        costs = None
    if usage is None and costs is None:
        return None
    return {
        "source": "openai_admin_api",
        "status": "available",
        "usage_surface": "completions",
        "updated_at": int(time.time()),
        "window_days": days,
        "start_time": start_time,
        "end_time": end_time,
        "filtered_project_count": len(project_ids),
        "filtered_api_key_count": len(api_key_ids),
        "completions": usage,
        "organization_costs": costs,
    }
