import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


spec = importlib.util.spec_from_file_location(
    "recent_health_cycle", Path(__file__).resolve().parents[1] / "scripts/health_cycle.py"
)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)
NOW = datetime(2026, 9, 11, 4, 40, tzinfo=timezone.utc)
ERROR = {"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}


def save(root, stamp, errors):
    directory = root / "data/health"
    directory.mkdir(parents=True, exist_ok=True)
    date = datetime.fromisoformat(stamp)
    path = directory / f"cycle_{date.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"as_of": stamp, "domains": list(health.DOMAINS), "data_errors": errors}))
    return path


def test_recovered_error_is_diagnosed_with_source_time(tmp_path, monkeypatch):
    save(tmp_path, "2026-09-11T04:00:00+00:00", [{**ERROR, "private_detail": "secret-must-not-leak"}])
    save(tmp_path, "2026-09-11T04:30:00+00:00", [])
    run = Mock(return_value={"status": "succeeded"})
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    result = health.diagnose_recent_cycles(tmp_path, now=NOW)
    assert result["status"] == "succeeded"
    assert run.call_count == 1
    assert run.call_args.args[1] == [ERROR]
    observation = run.call_args.kwargs["observation"]
    assert observation == {
        "observed_at": "2026-09-11T04:00:00+00:00",
        "latest_cycle_at": "2026-09-11T04:30:00+00:00",
        "current_state": "not_observed_in_latest",
    }
    assert run.call_args.kwargs["attempt_date"] == "2026-09-11"
    prompt = health._operational_diagnosis_prompt([ERROR], observation=observation)
    assert "not_observed_in_latest" in prompt and "secret-must-not-leak" not in prompt


@pytest.mark.parametrize("latest_errors", [[ERROR], []])
def test_existing_attempt_is_skipped_without_losing_other_recent_error(tmp_path, monkeypatch, latest_errors):
    other = {**ERROR, "domain": "us_equity"}
    save(tmp_path, "2026-09-11T03:00:00+00:00", [other])
    save(tmp_path, "2026-09-11T04:00:00+00:00", [ERROR])
    save(tmp_path, "2026-09-11T04:30:00+00:00", latest_errors)
    health._record_operational_diagnosis_attempt(tmp_path / "data/diagnosis-consumer", health._operational_diagnosis_fingerprint([ERROR]))
    run = Mock(return_value={"status": "succeeded"})
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    health.diagnose_recent_cycles(tmp_path, now=NOW)
    assert run.call_count == 1 and run.call_args.args[1] == [other]


def test_current_cycle_still_must_be_fresh_even_with_old_error(tmp_path, monkeypatch):
    save(tmp_path, "2026-09-11T00:00:00+00:00", [ERROR])
    run = Mock()
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    assert health.diagnose_recent_cycles(tmp_path, now=NOW)["status"] == "rejected"
    run.assert_not_called()


def test_expired_error_and_invalid_current_cycle_never_trigger(tmp_path, monkeypatch):
    save(tmp_path, "2026-09-10T04:00:00+00:00", [ERROR])
    path = save(tmp_path, "2026-09-11T04:30:00+00:00", [])
    run = Mock()
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    assert health.diagnose_recent_cycles(tmp_path, now=NOW)["status"] == "skipped"
    path.write_text("{}")
    assert health.diagnose_recent_cycles(tmp_path, now=NOW)["status"] == "rejected"
    run.assert_not_called()


@pytest.mark.parametrize("kind", ["future", "symlink", "corrupt_recent", "corrupt_budget", "oversized", "too_many"])
def test_invalid_sources_or_budget_do_not_submit(tmp_path, monkeypatch, kind):
    old = save(tmp_path, "2026-09-11T04:00:00+00:00", [ERROR])
    latest = save(tmp_path, "2026-09-11T04:30:00+00:00", [])
    if kind == "future":
        save(tmp_path, "2026-09-11T05:00:00+00:00", [])
    elif kind == "symlink":
        latest.unlink()
        latest.symlink_to(old)
    elif kind == "corrupt_recent":
        old.write_text("[]")
    elif kind == "corrupt_budget":
        state_root = tmp_path / "data/diagnosis-consumer"
        health._write_alert_state(state_root, {"operational_diagnosis_last_attempt_date": "invalid"})
    elif kind == "oversized":
        old.write_text(" " * (1024 * 1024 + 1))
    else:
        from datetime import timedelta
        for index in range(513):
            save(tmp_path, (NOW - timedelta(seconds=index + 1)).isoformat(), [])
    run = Mock()
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    assert health.diagnose_recent_cycles(tmp_path, now=NOW)["status"] == "rejected"
    run.assert_not_called()


def test_no_gateway_configuration_does_not_spend_daily_attempt(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    root = tmp_path / "data/diagnosis-consumer"
    result = health._run_operational_diagnosis(root, [ERROR], health._operational_diagnosis_fingerprint([ERROR]), attempt_date="2026-09-11")
    assert result == {"status": "deferred", "reason": "ai_gateway_not_configured"}
    assert health._load_operational_diagnosis_state(root) == {}


def test_actual_cli_uses_recent_consumer_without_model_or_recollection(tmp_path):
    import os
    import subprocess
    import sys
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    save(tmp_path, (now - timedelta(minutes=40)).isoformat(), [ERROR])
    save(tmp_path, (now - timedelta(minutes=10)).isoformat(), [])
    env = {**os.environ, "QUANT_MONITOR_ROOT": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("ACTIONS_ID_TOKEN_REQUEST_URL", None)
    env.pop("ACTIONS_ID_TOKEN_REQUEST_TOKEN", None)
    run = subprocess.run([sys.executable, str(spec.origin), "--diagnose-recent"], env=env, capture_output=True, text=True, check=True)
    result = json.loads(run.stdout)
    assert result["reason"] == "ai_gateway_not_configured"
    assert result["observation"]["current_state"] == "not_observed_in_latest"
    assert health._load_operational_diagnosis_state(tmp_path / "data/diagnosis-consumer") == {}


@pytest.mark.parametrize("outcome", ["succeeded", "deferred", "unknown"])
def test_daily_attempt_limit_survives_new_error_and_process_state_reload(tmp_path, monkeypatch, outcome):
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://synthetic.invalid")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "synthetic-only")
    client = Mock()
    if outcome == "unknown":
        client.execute.side_effect = RuntimeError("private-must-not-leak")
    else:
        client.execute.return_value = SimpleNamespace(success=outcome == "succeeded", raw={"status": outcome, "job_id": "synthetic-job"})
    kwargs = dict(config_loader=lambda: object(), client_factory=lambda config: client, attempt_date="2026-09-11")
    root = tmp_path / "data/diagnosis-consumer"
    result = health._run_operational_diagnosis(root, [ERROR], health._operational_diagnosis_fingerprint([ERROR]), **kwargs)
    other = {**ERROR, "domain": "us_equity"}
    second = health._run_operational_diagnosis(root, [other], health._operational_diagnosis_fingerprint([other]), **kwargs)
    assert client.execute.call_count == 1
    assert second == {"status": "skipped", "reason": "daily_attempt_limit"}
    assert "private-must-not-leak" not in json.dumps(result)


def test_next_day_can_diagnose_different_unattempted_error(tmp_path):
    root = tmp_path / "data/diagnosis-consumer"
    health._record_operational_diagnosis_attempt(root, health._operational_diagnosis_fingerprint([ERROR]), attempt_date="2026-09-10")
    other = {**ERROR, "domain": "us_equity"}
    client = Mock()
    client.execute.return_value = SimpleNamespace(success=True, raw={"status": "succeeded", "job_id": "synthetic-job"})
    result = health._run_operational_diagnosis(root, [other], health._operational_diagnosis_fingerprint([other]),
        attempt_date="2026-09-11", config_loader=lambda: object(), client_factory=lambda config: client)
    assert result["status"] == "succeeded" and client.execute.call_count == 1
