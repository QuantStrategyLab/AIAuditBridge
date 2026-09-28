from __future__ import annotations

import copy

from service.runtime_digest import prepare_runtime_digest, runtime_digest_event_id


def _record(status: str, *, service: str = "lb-svc", scope: str = "paper", day: str = "2026-09-28", lane: str = "paper", completeness: str = "complete", runs: list | None = None, conflicts: list | None = None) -> dict:
    return {
        "platform": "longbridge",
        "target_key": f"{service}|rot|*{scope}",
        "target": {"service": service, "strategy_profile": "rot", "account_scope": scope},
        "business_date": day,
        "timezone": "Asia/Hong_Kong",
        "status": status,
        "kind": "schedule" if status in {"market_closed", "not_due", "outside_window"} else "run",
        "completeness": completeness,
        "execution_lane": lane,
        "runs": runs or [],
        "conflicts": conflicts or [],
        "fills": {"source": "not_connected", "records": [], "count": None},
    }


def _projection(*records: dict, completeness: str = "complete", observed_at: str = "2026-09-28T08:40:00+00:00", read_errors: list | None = None) -> dict:
    return {
        "platform": "longbridge",
        "observed_at": observed_at,
        "completeness": completeness,
        "read_errors": read_errors or [],
        "records": list(records),
        "unmatched_reports": [],
    }


def test_closed_not_due_and_outside_window_stay_distinct() -> None:
    closed = prepare_runtime_digest(_projection(_record("market_closed")))
    waiting = prepare_runtime_digest(_projection(_record("not_due")))
    outside = prepare_runtime_digest(_projection(_record("outside_window")))
    assert closed["ok"] and "休市" in closed["text"] and "未到运行时间" not in closed["text"]
    assert "未到运行时间" in waiting["text"]
    assert "窗口外" in outside["text"]
    assert closed["text"].count("成交明细暂缺") == 1
    assert "0 笔" not in closed["text"]
    assert "complete" not in closed["text"]
    assert "确定性" not in closed["text"]


def test_missing_partial_and_drill_are_not_a_normal_close() -> None:
    missing = prepare_runtime_digest(_projection(
        _record("missing_report", completeness="incomplete"),
        completeness="incomplete",
    ))
    partial = prepare_runtime_digest(_projection(_record("partially_filled", lane="live")))
    drill = prepare_runtime_digest(_projection(_record("dry_run", lane="dry_run")))
    assert "到期缺报告" in missing["text"]
    assert "休市" not in missing["text"]
    assert "部分成交" in partial["text"]
    assert "演练 dry-run" in drill["text"]
    assert "实盘" not in drill["text"]


def test_schedule_status_cannot_hide_unknown_or_cross_day_conflict() -> None:
    hidden = _projection(_record(
        "market_closed",
        runs=[{"activity": "unknown", "execution_lane": "paper", "source_object": "gs://private/secret"}],
    ))
    refused = prepare_runtime_digest(hidden)
    assert refused["ok"] is False
    assert refused["reason"] == "schedule_hides_anomaly"
    assert refused["text"] == ""

    first = _record("market_closed", service="lb-a", scope="paper", day="2026-09-28")
    second = _record("unknown", service="lb-b", scope="live", day="2026-09-29", completeness="incomplete")
    conflict = prepare_runtime_digest(_projection(first, second, completeness="incomplete"))
    assert conflict["ok"] is False
    assert conflict["reason"] == "business_date_conflict"


def test_duplicate_target_invalid_time_and_count_zero_are_refused() -> None:
    duplicate = _projection(_record("market_closed"), _record("no_submission"))
    assert prepare_runtime_digest(duplicate)["reason"] == "duplicate_target"
    naive = _projection(_record("market_closed"), observed_at="2026-09-28T08:40:00")
    assert prepare_runtime_digest(naive)["reason"] == "invalid_observed_at"
    zero = _projection(_record("no_submission"))
    zero["records"][0]["fills"]["count"] = 0
    assert prepare_runtime_digest(zero)["reason"] == "fills_not_connected"


def test_producer_dry_run_previewed_run_is_accepted() -> None:
    projected = _projection(_record(
        "dry_run",
        service="lb-us",
        scope="us",
        lane="dry_run",
        runs=[{
            "run_id": "run-dry-1",
            "source_object": "gs://runtime-reports/longbridge/2026-09/run-dry-1.json",
            "source_objects": ["gs://runtime-reports/longbridge/2026-09/run-dry-1.json"],
            "started_at": "2026-09-28T08:06:00Z",
            "finished_at": "2026-09-28T08:07:00Z",
            "object_updated_at": "2026-09-28T08:08:00Z",
            "report_status": "ok",
            "execution_lane": "dry_run",
            "activity": "previewed",
            "run_time_known": True,
            "evidence": {"execution_status": "previewed"},
        }],
    ))
    prepared = prepare_runtime_digest(projected)
    assert prepared["ok"] is True
    assert prepared["accounts"][0]["status"] == "dry_run"
    assert prepared["accounts"][0]["runs"][0]["activity"] == "previewed"
    assert "演练 dry-run" in prepared["text"]
    assert "gs://" not in prepared["text"]


def test_cross_day_unknown_keeps_run_identity_and_time() -> None:
    carried = _record(
        "unknown",
        completeness="incomplete",
        runs=[{
            "run_id": "run-yday",
            "started_at": "2026-09-27T08:10:00Z",
            "finished_at": "2026-09-27T08:12:00Z",
            "source_object": "gs://runtime-reports/longbridge/2026-09/run-yday.json",
            "source_objects": ["gs://runtime-reports/longbridge/2026-09/run-yday.json"],
            "execution_lane": "paper",
            "activity": "unknown",
        }, {
            "run_id": "run-today",
            "activity": "unknown",
            "execution_lane": "paper",
        }],
    )
    prepared = prepare_runtime_digest(_projection(carried, completeness="incomplete"))
    runs = prepared["accounts"][0]["runs"]
    assert [(run["run_id"], run["started_at"], run["finished_at"]) for run in runs] == [
        ("run-yday", "2026-09-27T08:10:00Z", "2026-09-27T08:12:00Z"),
        ("run-today", None, None),
    ]
    assert runs[0]["source_object"].endswith("run-yday.json")
    assert "gs://" not in prepared["text"]
    assert "未知" in prepared["text"]
    naive = _projection(_record("unknown", runs=[{
        "run_id": "run-bad",
        "activity": "unknown",
        "execution_lane": "paper",
        "started_at": "2026-09-27T08:10:00",
    }]))
    assert prepare_runtime_digest(naive)["reason"] == "invalid_run_time"


def test_event_id_uses_target_scope_not_order_or_observed_at() -> None:
    first = _record("no_submission", service="lb-a", scope="paper")
    second = _record("market_closed", service="lb-b", scope="live")
    forward = prepare_runtime_digest(_projection(first, second))
    reverse = prepare_runtime_digest(_projection(second, first, observed_at="2026-09-28T18:40:00+00:00"))
    keys = ["lb-a|rot|*paper", "lb-b|rot|*live"]
    assert forward["ok"] and reverse["ok"]
    assert forward["event_id"] == reverse["event_id"]
    assert forward["event_id"] == runtime_digest_event_id("longbridge", "2026-09-28", keys)
    assert forward["target_scope"] == sorted(keys)
    with_runs = prepare_runtime_digest(_projection(
        _record("no_submission", service="lb-a", scope="paper", runs=[{
            "run_id": "run-1",
            "activity": "no_submission",
            "execution_lane": "paper",
            "source_object": "/Users/private/report.json",
        }]),
        second,
    ))
    assert with_runs["event_id"] == forward["event_id"]
    assert with_runs["accounts"][0]["runs"][0]["source_object"].endswith("report.json")
    assert "/Users/" not in with_runs["text"]
    other_scope = prepare_runtime_digest(_projection(first))
    assert other_scope["event_id"] != forward["event_id"]
    changed = copy.deepcopy(forward)
    assert "observed_at" not in changed["event_id"]
