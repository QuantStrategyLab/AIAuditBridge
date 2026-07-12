from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import service.ai_gateway_service as gateway


def test_uncertain_job_failure_is_persisted_and_not_retryable() -> None:
    job: dict[str, object] = {"job_id": "job-1", "status": "queued", "task": "execute"}
    writes: list[dict[str, object]] = []
    result = SimpleNamespace(
        success=False,
        output="",
        error="codex exec timed out",
        dispatch_started=False,
        dispatch_uncertain=True,
    )
    with (
        patch.object(gateway, "_read_job", return_value=job),
        patch.object(gateway, "_write_job", side_effect=lambda payload: writes.append(dict(payload))),
        patch.object(gateway, "_record_job_automation_run"),
        patch.object(gateway, "_audit_log"),
        patch.object(gateway, "_record_platform_execution_telemetry"),
        patch.object(gateway, "get_health_monitor"),
        patch.object(gateway.CodexAdapter, "execute", return_value=result),
    ):
        gateway._run_job("job-1", {"prompt": "review", "task": "execute"})

    completed = writes[-1]
    assert completed["dispatch_state"] == "pending_uncertain"
    assert completed["dispatch_uncertain"] is True
    assert completed["failure_category"] == "dispatch_uncertain"

    control = {
        "effective_action": "continue",
        "action": "continue",
        "auto_fix_allowed": True,
        "requires_human_review": False,
        "execution": {"auto_fix_allowed": True},
    }
    with (
        patch.object(gateway, "_automation_control_snapshot", return_value=control),
        patch.object(gateway, "load_autonomy_policy", return_value={}),
        patch.object(gateway, "get_health_monitor"),
    ):
        triage = gateway._automation_triage_snapshot("QuantStrategyLab/AIAuditBridge", failure_category="dispatch_uncertain")
    assert triage["retry_allowed"] is False
    assert triage["auto_fix_allowed"] is False
    assert triage["next_step"] == "reconcile_dispatch"


def test_pre_dispatch_failure_is_not_dispatched() -> None:
    job: dict[str, object] = {"job_id": "job-2", "status": "queued", "task": "execute"}
    writes: list[dict[str, object]] = []
    with (
        patch.object(gateway, "_read_job", return_value=job),
        patch.object(gateway, "_write_job", side_effect=lambda payload: writes.append(dict(payload))),
        patch.object(gateway, "_record_job_automation_run"),
        patch.object(gateway, "_audit_log"),
        patch.object(gateway, "_record_platform_execution_telemetry"),
        patch.object(gateway, "get_health_monitor"),
        patch.object(gateway, "_validate_sandbox", side_effect=ValueError("invalid sandbox")),
    ):
        gateway._run_job("job-2", {"prompt": "review", "task": "execute"})

    completed = writes[-1]
    assert completed["dispatch_state"] == "not_dispatched"
    assert completed["dispatch_uncertain"] is False


def test_uncertain_review_result_is_returned_and_forces_escalation() -> None:
    result = SimpleNamespace(
        success=False,
        output="",
        error="provider timeout",
        dispatch_started=False,
        dispatch_uncertain=True,
    )
    payload = gateway._review_result_payload("claude", "claude-sonnet-4-6", result, latency_seconds=0.1)
    action = gateway._fail_closed_for_uncertain_dispatch(
        {
            "action": "auto_merge",
            "initial_action": "auto_merge",
            "human_review_required": False,
            "auto_merge_allowed": True,
            "automation_authority": {"final_action": "auto_merge", "human_review_required": False},
        },
        [payload],
    )

    assert payload["dispatch_uncertain"] is True
    assert payload["dispatch_started"] is False
    assert action["action"] == "escalate"
    assert action["auto_merge_allowed"] is False
    assert action["human_review_required"] is True
