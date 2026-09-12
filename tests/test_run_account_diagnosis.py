from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts import run_account_diagnosis as runner


REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"
ENV = {
    "GITHUB_REPOSITORY": runner.SOURCE_REPOSITORY,
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_RUN_ID": "123456",
    "GITHUB_RUN_ATTEMPT": "1",
    "QSL_CONTROL_PLANE_SYNC_URL": f"https://{runner.QRT_HOST}",
    "ACCOUNT_DIAGNOSIS_SYNC_TOKEN": "qrt-token-for-test-only",
}


def request_payload(status: str = "queued") -> dict[str, object]:
    return {
        "request_id": REQUEST_ID,
        "platform": "binance",
        "key": "Binance",
        "target_id": "binance.crypto_live_pool_rotation",
        "trigger": "manual_check",
        "observed_at": "2026-09-13T04:00:00Z",
        "checks": {
            "configured_state": "enabled",
            "runtime_enabled": True,
            "scheduler_state": "enabled",
            "runtime_guard": "pass",
            "execution_heartbeat": "pass",
            "freshness": "ready",
        },
        "status": status,
    }


def run_with(responses, client_factory):
    calls = []

    def request_json(method, endpoint, token, payload=None):
        calls.append((method, endpoint, token, payload))
        response = responses[len(calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    with patch.dict(os.environ, ENV, clear=True):
        outcome = runner.run_diagnosis(
            REQUEST_ID,
            request_json=request_json,
            config_loader=lambda: object(),
            client_factory=client_factory,
        )
    return outcome, calls


def test_endpoint_and_payload_reject_untrusted_shapes():
    with pytest.raises(runner.RunnerError, match="control_plane_unavailable"):
        runner.control_plane_endpoint("http://qsl-strategy-switch-console.pigbibi.workers.dev", REQUEST_ID)
    with pytest.raises(runner.RunnerError, match="control_plane_unavailable"):
        runner.control_plane_endpoint("https://other.example", REQUEST_ID)
    with pytest.raises(runner.RunnerError, match="control_plane_unavailable"):
        runner.control_plane_endpoint(f"https://{runner.QRT_HOST}:bad", REQUEST_ID)
    with pytest.raises(runner.RunnerError, match="control_plane_response_invalid"):
        runner.validate_request({**request_payload(), "extra": "reject"}, REQUEST_ID)
    with pytest.raises(runner.RunnerError, match="control_plane_response_invalid"):
        runner.validate_request({**request_payload(), "target_id": "longbridge.other"}, REQUEST_ID)


def test_success_calls_codex_once_with_fixed_read_only_contract_and_writes_result_once():
    client = Mock()
    client.execute.return_value = SimpleNamespace(
        success=True,
        output="事实：当前检查正常。建议继续观察。",
        raw={"status": "succeeded", "job_id": "codex-job-123"},
    )
    outcome, calls = run_with(
        [request_payload(), {"ok": True, "claimed": True}, {"ok": True}],
        lambda _config: client,
    )

    assert outcome == {"status": "succeeded", "reason_code": "diagnosis_ready"}
    assert len(calls) == 3
    assert calls[0][0] == "GET"
    assert calls[1][3] == {
        "request_id": REQUEST_ID,
        "status": "running",
        "workflow_run_id": "123456",
        "workflow_run_attempt": "1",
    }
    callback = calls[2][3]
    assert callback["status"] == "succeeded"
    assert callback["job_id"] == "codex-job-123"
    assert callback["workflow_run_id"] == "123456"
    client.execute.assert_called_once()
    prompt = client.execute.call_args.args[0]
    assert "qrt-token-for-test-only" not in prompt
    assert client.execute.call_args.kwargs == {
        "task": "account_operational_diagnosis",
        "mode": "review_only",
        "sandbox": "read-only",
        "allowed_providers": ["codex"],
        "source_repository": runner.SOURCE_REPOSITORY,
        "source_ref": "main",
        "research_stage": "drift_analysis",
        "complexity": "high",
        "timeout": 600,
    }


def test_duplicate_claim_does_not_call_codex_or_callback():
    client = Mock()
    outcome, calls = run_with(
        [request_payload(), {"ok": True, "claimed": False}],
        lambda _config: client,
    )

    assert outcome == {"status": "unknown", "reason_code": "claim_not_granted"}
    assert len(calls) == 2
    client.execute.assert_not_called()


def test_codex_capacity_failure_is_written_without_model_error_details():
    client = Mock()
    client.execute.return_value = SimpleNamespace(
        success=False,
        output="",
        raw={"status": "deferred", "private": "must-not-leak"},
    )
    outcome, calls = run_with(
        [request_payload(), {"ok": True, "claimed": True}, {"ok": True}],
        lambda _config: client,
    )

    assert outcome == {"status": "failed", "reason_code": "capacity_unavailable"}
    assert calls[2][3]["summary"] == "Codex 当前容量不可用。"
    assert "must-not-leak" not in repr(calls[2][3])


def test_codex_call_exception_is_unknown_and_never_retried():
    client = Mock()
    client.execute.side_effect = TimeoutError("private-timeout")
    outcome, calls = run_with(
        [request_payload(), {"ok": True, "claimed": True}, {"ok": True}],
        lambda _config: client,
    )

    assert outcome == {"status": "unknown", "reason_code": "codex_unavailable"}
    assert calls[2][3]["status"] == "unknown"
    assert calls[2][3]["reason_code"] == "codex_unavailable"
    assert "private-timeout" not in repr(calls[2][3])
    client.execute.assert_called_once()


def test_missing_codex_terminal_status_is_unknown():
    client = Mock()
    client.execute.return_value = SimpleNamespace(
        success=False,
        output="",
        raw={"job_id": "codex-job-123", "status": "running"},
    )
    outcome, calls = run_with(
        [request_payload(), {"ok": True, "claimed": True}, {"ok": True}],
        lambda _config: client,
    )

    assert outcome == {"status": "unknown", "reason_code": "invalid_response"}
    assert calls[2][3]["status"] == "unknown"
    assert "job_id" not in calls[2][3]


def test_success_with_unsafe_job_id_is_unknown():
    client = Mock()
    client.execute.return_value = SimpleNamespace(
        success=True,
        output="事实：当前检查正常。",
        raw={"status": "succeeded", "job_id": "job/with-private-detail"},
    )
    outcome, calls = run_with(
        [request_payload(), {"ok": True, "claimed": True}, {"ok": True}],
        lambda _config: client,
    )

    assert outcome == {"status": "unknown", "reason_code": "invalid_response"}
    assert calls[2][3]["status"] == "unknown"
    assert "job_id" not in calls[2][3]


def test_callback_failure_is_unknown_and_never_retries_ai():
    client = Mock()
    client.execute.return_value = SimpleNamespace(
        success=True,
        output="事实：正常。",
        raw={"status": "succeeded", "job_id": "codex-job-123"},
    )
    outcome, calls = run_with(
        [request_payload(), {"ok": True, "claimed": True}, runner.RunnerError("control_plane_unavailable")],
        lambda _config: client,
    )

    assert outcome == {"status": "unknown", "reason_code": "callback_failed"}
    assert len(calls) == 3
    client.execute.assert_called_once()


def test_workflow_account_branch_is_main_only_and_mutually_exclusive():
    text = (Path(__file__).parents[1] / ".github/workflows/codex_audit.yml").read_text()
    assert "account_diagnosis_request_id" in text
    assert "github.event_name == 'workflow_dispatch'" in text
    assert "inputs.account_diagnosis_request_id != ''" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "ACCOUNT_DIAGNOSIS_SYNC_TOKEN" in text
    assert "QSL_CONTROL_PLANE_SYNC_URL" in text
    assert "id-token: write" in text
    assert "run: python3 -m scripts.run_account_diagnosis" in text
    for job in ("codex-audit:", "synthetic-sdk-check:", "daily-summary:", "operational-diagnosis:",
                "historical-diagnosis-rehearsal:", "watchdog-repair-rehearsal:"):
        start = text.index(f"  {job}")
        next_match = re.search(r"\n  [A-Za-z0-9_-]+:\n", text[start + 3:])
        next_job = -1 if next_match is None else start + 3 + next_match.start()
        section = text[start:] if next_job < 0 else text[start:next_job]
        assert "inputs.account_diagnosis_request_id == ''" in section
