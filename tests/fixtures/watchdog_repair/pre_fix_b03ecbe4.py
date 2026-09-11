"""Assess whether scheduled daily research workflows actually ran.

This is deliberately an Actions-control-plane check, not market-data evidence.
It never infers that P1 was accepted or that P3 produced a promotable result.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence


SCHEDULE_WATCHDOG_SCHEMA = "qsl.daily-research-schedule-watchdog.v1"
WATCHDOG_OBSERVED = "OBSERVED"
WATCHDOG_PARKED = "PARKED"


class DailyResearchScheduleWatchdogError(ValueError):
    """Raised when the sanitized GitHub Actions response is not usable."""


def _fail(reason: str) -> None:
    raise DailyResearchScheduleWatchdogError(reason)


def _utc_date(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"invalid_{field}")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        raise DailyResearchScheduleWatchdogError(f"invalid_{field}") from exc


def _expected_date(value: object) -> str:
    if not isinstance(value, str) or not value:
        _fail("invalid_expected_utc_date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise DailyResearchScheduleWatchdogError("invalid_expected_utc_date") from exc


def _run_summary(value: Mapping[str, Any], *, expected_utc_date: str) -> dict[str, object] | None:
    if value.get("event") != "schedule":
        return None
    created_at = value.get("created_at")
    if _utc_date(created_at, field="created_at") != expected_utc_date:
        return None
    run_id = value.get("id")
    if not isinstance(run_id, int) or run_id <= 0:
        _fail("invalid_run_id")
    status = value.get("status")
    conclusion = value.get("conclusion")
    if not isinstance(status, str) or not status:
        _fail("invalid_run_status")
    if conclusion is not None and not isinstance(conclusion, str):
        _fail("invalid_run_conclusion")
    return {
        "run_id": run_id,
        "created_at": created_at,
        "status": status,
        "conclusion": conclusion,
    }


def assess_scheduled_research_workflow(
    *,
    workflow_id: str,
    workflow_runs_response: object,
    expected_utc_date: str,
) -> dict[str, object]:
    """Return a sanitized, fail-closed availability record for one workflow."""

    if not isinstance(workflow_id, str) or not workflow_id:
        _fail("invalid_workflow_id")
    expected = _expected_date(expected_utc_date)
    if not isinstance(workflow_runs_response, Mapping):
        _fail("invalid_workflow_runs_response")
    raw_runs = workflow_runs_response.get("workflow_runs")
    if not isinstance(raw_runs, Sequence) or isinstance(raw_runs, (str, bytes)):
        _fail("invalid_workflow_runs")

    matching: list[dict[str, object]] = []
    for raw_run in raw_runs:
        if not isinstance(raw_run, Mapping):
            _fail("invalid_workflow_run")
        summary = _run_summary(raw_run, expected_utc_date=expected)
        if summary is not None:
            matching.append(summary)

    result: dict[str, object] = {
        "schema_version": SCHEDULE_WATCHDOG_SCHEMA,
        "workflow_id": workflow_id,
        "expected_utc_date": expected,
    }
    if not matching:
        return {
            **result,
            "status": WATCHDOG_PARKED,
            "reason_code": "SCHEDULED_RUN_MISSING",
        }

    latest = max(matching, key=lambda item: (str(item["created_at"]), int(item["run_id"])))
    if latest["status"] != "completed":
        reason_code = "SCHEDULED_RUN_NOT_TERMINAL"
    elif latest["conclusion"] != "success":
        reason_code = "SCHEDULED_RUN_NOT_SUCCESSFUL"
    else:
        return {
            **result,
            "status": WATCHDOG_OBSERVED,
            "reason_code": "SCHEDULED_RUN_SUCCEEDED",
            "run": latest,
        }
    return {
        **result,
        "status": WATCHDOG_PARKED,
        "reason_code": reason_code,
        "run": latest,
    }


def build_daily_research_schedule_watchdog_summary(
    *,
    expected_utc_date: str,
    tqqq_workflow_runs_response: object,
    soxl_workflow_runs_response: object,
    additional_workflow_checks: Mapping[str, tuple[str, object]] | None = None,
) -> dict[str, object]:
    """Assess scheduled workflow availability without mixing in P1/P3 state.

    Additional checks are intentionally supplied as explicit workflow-id,
    expected-date and Actions-response tuples.  This keeps the watchdog a
    read-only control-plane observer while allowing it to cover non-live
    forward/calibration workflows and the previous watchdog window itself.
    """

    expected = _expected_date(expected_utc_date)
    workflows = [
        assess_scheduled_research_workflow(
            workflow_id="tqqq-p1-p3-daily-research",
            workflow_runs_response=tqqq_workflow_runs_response,
            expected_utc_date=expected,
        ),
        assess_scheduled_research_workflow(
            workflow_id="soxl-p1-p3-daily-research",
            workflow_runs_response=soxl_workflow_runs_response,
            expected_utc_date=expected,
        ),
    ]
    if additional_workflow_checks is not None:
        if not isinstance(additional_workflow_checks, Mapping):
            _fail("invalid_additional_workflow_checks")
        for workflow_id, check in additional_workflow_checks.items():
            if not isinstance(check, tuple) or len(check) != 2:
                _fail("invalid_additional_workflow_check")
            additional_expected_date, workflow_runs_response = check
            workflows.append(
                assess_scheduled_research_workflow(
                    workflow_id=workflow_id,
                    workflow_runs_response=workflow_runs_response,
                    expected_utc_date=additional_expected_date,
                )
            )
    reason_codes = sorted(
        {
            str(workflow["reason_code"])
            for workflow in workflows
            if workflow["status"] == WATCHDOG_PARKED
        }
    )
    return {
        "schema_version": SCHEDULE_WATCHDOG_SCHEMA,
        "expected_utc_date": expected,
        "status": WATCHDOG_OBSERVED if not reason_codes else WATCHDOG_PARKED,
        "reason_codes": reason_codes,
        "workflows": workflows,
    }
