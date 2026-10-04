"""Format one already-projected LongBridge daily runtime report.

This does not read cloud objects, classify orders, or send notifications.
The projection's original status is translated; it is not recomputed.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

TELEGRAM_TEXT_LIMIT = 4096
_PURPOSE = "runtime_daily_digest"
_PLATFORM = "longbridge"
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:|@+*-]{1,120}$")
_OPTIONAL_TOKEN = re.compile(r"^[A-Za-z0-9_.:|@+*-]{0,120}$")
_TIMEZONE = re.compile(r"^(?:UTC|[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+)$")
_STATUSES = frozenset({
    "no_submission",
    "no_signal",
    "no_rebalance",
    "submitted",
    "broker_acknowledged",
    "partially_filled",
    "filled",
    "reconciliation_required",
    "unknown",
    "failed",
    "blocked",
    "dry_run",
    "shadow",
    "validation",
    "not_due",
    "market_closed",
    "outside_window",
    "within_grace",
    "missing_report",
    "read_incomplete",
    "insufficient",
    "conflict",
})
_SCHEDULE_ONLY = frozenset({"not_due", "market_closed", "outside_window", "within_grace"})
_ANOMALIES = frozenset({
    "reconciliation_required",
    "unknown",
    "failed",
    "blocked",
    "conflict",
    "missing_report",
    "read_incomplete",
    "insufficient",
})
_DRILLS = frozenset({"dry_run", "shadow", "validation"})
# Run activities come from the LongBridge projection, and are not record statuses.
_ACTIVITIES = frozenset({
    "no_submission",
    "no_signal",
    "no_rebalance",
    "submitted",
    "broker_acknowledged",
    "partially_filled",
    "filled",
    "reconciliation_required",
    "unknown",
    "failed",
    "blocked",
    "previewed",
    "insufficient",
})
_STATUS_ZH = {
    "no_submission": "未提交",
    "no_signal": "无信号",
    "no_rebalance": "无调仓",
    "submitted": "已提交",
    "broker_acknowledged": "券商已接受",
    "partially_filled": "部分成交",
    "filled": "已成交",
    "reconciliation_required": "需要对账",
    "unknown": "未知",
    "failed": "失败",
    "blocked": "已阻断",
    "dry_run": "演练 dry-run",
    "shadow": "演练 shadow",
    "validation": "演练 validation",
    "not_due": "未到运行时间",
    "market_closed": "休市",
    "outside_window": "窗口外",
    "within_grace": "宽限期内",
    "missing_report": "到期缺报告",
    "read_incomplete": "读取不完整",
    "insufficient": "资料不足",
    "conflict": "记录冲突",
}
_LANE_ZH = {
    "paper": "模拟",
    "live": "实盘",
    "dry_run": "dry-run 演练",
    "shadow": "shadow 演练",
    "validation": "validation 演练",
    "insufficient": "运行身份未标明",
}


def runtime_digest_event_id(platform: str, business_date: str, target_keys: Sequence[str]) -> str:
    """Stable id for one platform, business date, and explicit target set.

    Target order, message text, observed time, and how many runs were read do
    not change the id. Matching this set to a fixed production target config
    still needs a separate cloud check.
    """
    scope = "\n".join(sorted({str(key) for key in target_keys}))
    raw = f"{_PURPOSE}\n{platform}\n{business_date}\n{scope}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prepare_runtime_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Accept one projection object or return a refusal that is not a closed day."""
    rejected = _reject(payload)
    if rejected is not None:
        return rejected
    records = payload["records"]
    assert isinstance(records, list)
    business_date = str(records[0]["business_date"])
    timezone_name = str(records[0]["timezone"])
    accounts = [_account_view(record) for record in records]
    text = _render(payload, accounts, business_date=business_date)
    if _unsafe_text(text):
        return _refused("unsafe_text")
    target_scope = sorted(account["target_key"] for account in accounts)
    sendable = len(text) <= TELEGRAM_TEXT_LIMIT
    return {
        "ok": True,
        "platform": _PLATFORM,
        "business_date": business_date,
        "timezone": timezone_name,
        "completeness": payload.get("completeness"),
        "target_scope": target_scope,
        "event_id": runtime_digest_event_id(_PLATFORM, business_date, target_scope),
        "text": text,
        "sendable": sendable,
        "reason": None if sendable else "runtime_digest_too_long",
        "accounts": accounts,
    }


def _reject(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return _refused("malformed")
    if payload.get("platform") != _PLATFORM:
        return _refused("unsupported_platform")
    if _parse_observed_at(payload.get("observed_at")) is None:
        return _refused("invalid_observed_at")
    if payload.get("completeness") not in {"complete", "incomplete"}:
        return _refused("invalid_completeness")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        return _refused("empty_records")
    if not isinstance(payload.get("read_errors", []), list):
        return _refused("malformed")
    if not isinstance(payload.get("unmatched_reports", []), list):
        return _refused("malformed")
    seen: set[str] = set()
    dates: set[str] = set()
    zones: set[str] = set()
    for record in records:
        problem = _record_problem(record, seen)
        if problem is not None:
            return _refused(problem)
        assert isinstance(record, Mapping)
        dates.add(str(record["business_date"]))
        zones.add(str(record["timezone"]))
    if len(dates) != 1:
        return _refused("business_date_conflict")
    if len(zones) != 1:
        return _refused("timezone_conflict")
    return None


def _record_problem(record: Any, seen: set[str]) -> str | None:
    if not isinstance(record, Mapping):
        return "malformed"
    if record.get("platform") not in {None, _PLATFORM}:
        return "unsupported_platform"
    target_key = record.get("target_key")
    if not isinstance(target_key, str) or _SAFE_TOKEN.fullmatch(target_key) is None:
        return "invalid_target"
    if target_key in seen:
        return "duplicate_target"
    seen.add(target_key)
    target = record.get("target")
    if not isinstance(target, Mapping):
        return "invalid_target"
    service = target.get("service")
    if not isinstance(service, str) or _SAFE_TOKEN.fullmatch(service) is None:
        return "invalid_target"
    for key in ("strategy_profile", "account_scope"):
        value = target.get(key)
        if not isinstance(value, str) or _OPTIONAL_TOKEN.fullmatch(value) is None:
            return "invalid_target"
    if _parse_business_date(record.get("business_date")) is None:
        return "invalid_business_date"
    timezone_name = record.get("timezone")
    if not isinstance(timezone_name, str) or _TIMEZONE.fullmatch(timezone_name) is None:
        return "invalid_timezone"
    try:
        ZoneInfo(timezone_name)
    except Exception:
        return "invalid_timezone"
    status = record.get("status")
    if status not in _STATUSES:
        return "invalid_status"
    if record.get("completeness") not in {"complete", "incomplete", "insufficient"}:
        return "invalid_completeness"
    fills = record.get("fills")
    if (
        not isinstance(fills, Mapping)
        or fills.get("source") != "not_connected"
        or fills.get("count") is not None
        or fills.get("records") != []
    ):
        return "fills_not_connected"
    runs = record.get("runs")
    if not isinstance(runs, list):
        return "malformed"
    conflicts = record.get("conflicts")
    if not isinstance(conflicts, list):
        return "malformed"
    if status in _SCHEDULE_ONLY and (_run_anomaly(runs) or conflicts):
        return "schedule_hides_anomaly"
    for run in runs:
        if not isinstance(run, Mapping) or run.get("activity") not in _ACTIVITIES:
            return "invalid_run"
        lane = run.get("execution_lane")
        if lane not in _LANE_ZH:
            return "invalid_run"
        identity_problem = _run_identity_problem(run)
        if identity_problem is not None:
            return identity_problem
    lane = record.get("execution_lane")
    if lane not in _LANE_ZH:
        return "invalid_lane"
    return None


def _run_anomaly(runs: list[Any]) -> bool:
    for run in runs:
        if isinstance(run, Mapping) and run.get("activity") in _ANOMALIES:
            return True
    return False


def _run_identity_problem(run: Mapping[str, Any]) -> str | None:
    run_id = run.get("run_id", None)
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip() or any(ch.isspace() for ch in run_id)):
        return "invalid_run"
    for key in ("started_at", "finished_at", "object_updated_at"):
        if key not in run or run.get(key) is None:
            continue
        if _parse_observed_at(run.get(key)) is None:
            return "invalid_run_time"
    source = run.get("source_object", None)
    if source is not None and not isinstance(source, str):
        return "invalid_run"
    sources = run.get("source_objects", [])
    if sources is None:
        sources = []
    if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
        return "invalid_run"
    return None


def _account_view(record: Mapping[str, Any]) -> dict[str, Any]:
    target = record["target"]
    assert isinstance(target, Mapping)
    status = str(record["status"])
    lane = str(record["execution_lane"])
    runs = []
    for run in record["runs"]:
        assert isinstance(run, Mapping)
        runs.append(_run_view(run))
    return {
        "target_key": record["target_key"],
        "service": target["service"],
        "strategy_profile": target["strategy_profile"],
        "account_scope": target["account_scope"],
        "status": status,
        "status_zh": _STATUS_ZH[status],
        "execution_lane": lane,
        "lane_zh": _LANE_ZH[lane],
        "completeness": record["completeness"],
        "fills_note": "成交明细暂缺",
        "runs": runs,
    }


def _run_view(run: Mapping[str, Any]) -> dict[str, Any]:
    sources = run.get("source_objects") or []
    return {
        "run_id": run.get("run_id"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "object_updated_at": run.get("object_updated_at"),
        "source_object": run.get("source_object"),
        "source_objects": [item for item in sources if isinstance(item, str)],
        "activity": run.get("activity"),
        "execution_lane": run.get("execution_lane"),
    }


def _render(
    payload: Mapping[str, Any],
    accounts: list[dict[str, Any]],
    *,
    business_date: str,
) -> str:
    lines = [f"{business_date} {_PLATFORM}", "成交明细暂缺。"]
    read_errors = payload.get("read_errors") or []
    unmatched = payload.get("unmatched_reports") or []
    if isinstance(read_errors, list) and read_errors:
        lines.append(f"读取失败 {len(read_errors)} 项。")
    if isinstance(unmatched, list) and unmatched:
        lines.append(f"未匹配报告 {len(unmatched)} 项。")
    for account in accounts:
        status = account["status_zh"]
        if account["execution_lane"] in _DRILLS and account["status"] not in _DRILLS:
            status = f"{status}（演练）"
        if account["completeness"] != "complete" and account["status"] not in _ANOMALIES:
            status = f"{status}；资料不完整"
        lines.append(f"• {account['service']} / {account['account_scope']}：{status}")
    return "\n".join(lines)


def _refused(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "text": "",
        "sendable": False,
        "accounts": [],
    }


def _parse_observed_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _parse_business_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if parsed.isoformat() != value:
        return None
    return parsed


def _unsafe_text(text: str) -> bool:
    lowered = text.lower()
    markers = ("secret", "token", "gs://", "/users/", "/home/", "traceback", "\\")
    return any(marker in lowered for marker in markers)
