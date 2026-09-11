from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_watchdog_repair_rehearsal as rehearsal


def _env() -> dict[str, str]:
    return {
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc-request",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
        "GITHUB_RUN_ATTEMPT": "1",
    }


def _result(action: str = "apply_watchdog_489", **changes: object) -> SimpleNamespace:
    output = json.dumps({"action": action})
    raw = {
        "status": "succeeded",
        "job_id": "job-489",
        "provider": "codex",
        "research_stage": "drift_analysis",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "output": output,
        "policy_verdict": "advisory",
    }
    raw.update(changes.pop("raw", {}))
    values = {
        "success": True,
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "output": output,
        "raw": raw,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_offline_check_replays_exact_incident_and_fixed_boundaries(tmp_path: Path) -> None:
    result = rehearsal.offline_check(workspace=tmp_path)

    assert result["status"] == "OBSERVED"
    assert result["evidence_kind"] == "historical_repair_rehearsal"
    assert result["input_kind"] == "synthetic_replay_of_historical_source"
    assert result["ai_request_attempted"] is False
    assert result["production_changed"] is False
    assert result["publish_scope"] == "artifact_only"
    assert result["action"] == "apply_watchdog_489"
    assert result["before"]["status"] == "PARKED"
    assert result["before"]["workflows"][-1]["run"]["conclusion"] == "failure"
    assert result["after"]["status"] == "OBSERVED"
    assert result["after"]["workflows"][-1]["run"]["conclusion"] == "failure"
    assert result["checks"] == {
        "research_failure": "PARKED",
        "watchdog_cancelled": "PARKED",
        "watchdog_timed_out": "PARKED",
        "watchdog_missing": "PARKED",
        "watchdog_success": "OBSERVED",
    }
    target = tmp_path / rehearsal.TARGET_RELATIVE_PATH
    assert target.read_bytes() == rehearsal.APPROVED_FIXTURE.read_bytes()


def test_fixture_hashes_are_fixed_to_reviewed_uesp_commits() -> None:
    assert rehearsal._sha256(rehearsal.PRE_FIX_FIXTURE) == rehearsal.PRE_FIX_SHA256
    assert rehearsal._sha256(rehearsal.APPROVED_FIXTURE) == rehearsal.APPROVED_SHA256
    assert rehearsal.PRE_FIX_COMMIT == "b03ecbe4e0a7a0de22f298499f867a7039e4b60a"
    assert rehearsal.APPROVED_COMMIT == "8f2b0c455833009806ccb63c341f8d0fb36bcfaf"


def test_ai_can_only_select_reviewed_action_and_sdk_route_is_checked(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        def execute(self, prompt: str, **kwargs: object) -> SimpleNamespace:
            calls.append((prompt, kwargs))
            return _result()

    result = rehearsal.run_rehearsal(
        environ=_env(),
        workspace=tmp_path,
        config_loader=lambda: object(),
        client_factory=lambda _config: Client(),
    )

    assert result["status"] == "OBSERVED"
    assert result["ai_request_attempted"] is True
    assert len(calls) == 1
    prompt, kwargs = calls[0]
    assert "historical" in prompt.lower()
    assert "No natural production failure input" in prompt
    assert "apply_watchdog_489" in prompt and "escalate" in prompt
    assert "/Users/" not in prompt and "command" not in prompt.lower()
    assert kwargs == {
        "task": "historical_watchdog_repair_rehearsal",
        "mode": "review_only",
        "sandbox": "read-only",
        "research_stage": "drift_analysis",
        "allowed_providers": ["codex"],
        "source_repository": "QuantStrategyLab/AIAuditBridge",
        "source_ref": "main",
        "timeout": 600,
    }
    assert result["ai_route"] == {
        "job_id": "job-489",
        "provider": "codex",
        "research_stage": "drift_analysis",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
    }


@pytest.mark.parametrize(
    ("ai_result", "reason"),
    [
        (_result("escalate"), "AI_ESCALATED"),
        (_result("write_patch"), "AI_ACTION_INVALID"),
        (_result(raw={"provider": "cursor"}), "AI_ROUTE_INVALID"),
        (_result(success=False), "AI_RESULT_UNAVAILABLE"),
        (_result(raw={"status": "deferred"}), "AI_DEFERRED"),
    ],
)
def test_ai_failure_or_nonapproved_action_parks_once_without_applying(
    tmp_path: Path, ai_result: SimpleNamespace, reason: str
) -> None:
    calls = 0

    class Client:
        def execute(self, _prompt: str, **_kwargs: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            return ai_result

    result = rehearsal.run_rehearsal(
        environ=_env(),
        workspace=tmp_path,
        config_loader=lambda: object(),
        client_factory=lambda _config: Client(),
    )

    assert calls == 1
    assert result["status"] == "PARKED"
    assert result["reason_code"] == reason
    assert result["production_changed"] is False
    assert result["ai_request_attempted"] is True
    assert not (tmp_path / rehearsal.TARGET_RELATIVE_PATH).exists()
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"GITHUB_REF": "refs/heads/feature"}, "INVALID_WORKFLOW_CONTEXT"),
        ({"GITHUB_EVENT_NAME": "schedule"}, "INVALID_WORKFLOW_CONTEXT"),
        ({"GITHUB_RUN_ATTEMPT": "2"}, "INVALID_WORKFLOW_ATTEMPT"),
        ({"ACTIONS_ID_TOKEN_REQUEST_TOKEN": ""}, "GITHUB_OIDC_REQUIRED"),
        ({"CODEX_API_KEY": "static"}, "NON_OIDC_CREDENTIALS_REJECTED"),
    ],
)
def test_live_rehearsal_requires_manual_main_first_attempt_oidc(
    tmp_path: Path, change: dict[str, str], reason: str
) -> None:
    env = {**_env(), **change}
    result = rehearsal.run_rehearsal(
        environ=env,
        workspace=tmp_path,
        config_loader=lambda: (_ for _ in ()).throw(AssertionError("must not configure")),
    )
    assert result["status"] == "PARKED"
    assert result["reason_code"] == reason


def test_gateway_configuration_failure_does_not_claim_ai_request(tmp_path: Path) -> None:
    result = rehearsal.run_rehearsal(
        environ=_env(),
        workspace=tmp_path,
        config_loader=lambda: (_ for _ in ()).throw(ValueError("private config detail")),
    )

    assert result["status"] == "PARKED"
    assert result["reason_code"] == "AI_GATEWAY_NOT_CONFIGURED"
    assert result["ai_request_attempted"] is False
    assert "private" not in json.dumps(result)


def test_tampered_fixture_is_rejected_before_import_or_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tampered = tmp_path / "tampered.py"
    tampered.write_text("raise AssertionError('must not import')\n", encoding="utf-8")
    monkeypatch.setattr(rehearsal, "PRE_FIX_FIXTURE", tampered)

    result = rehearsal.offline_check(workspace=tmp_path / "work")

    assert result["status"] == "PARKED"
    assert result["reason_code"] == "TRUSTED_SOURCE_HASH_MISMATCH"
    assert not (tmp_path / "work" / rehearsal.TARGET_RELATIVE_PATH).exists()


def test_cli_parks_with_nonzero_exit_and_offline_success_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rehearsal, "run_rehearsal", lambda: {"status": "PARKED"})
    monkeypatch.setattr(rehearsal, "offline_check", lambda: {"status": "OBSERVED"})

    assert rehearsal.main([]) == 3
    assert rehearsal.main(["--offline-check"]) == 0
