"""Synthetic checks for the optional LongBridge runtime digest pipeline."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_briefing_pipeline.sh"
ZONE = "Pacific/Kiritimati"


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "monitor"
    aab = tmp_path / "aab"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (aab / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "source_telegram_env.sh").write_text("# stub\n", encoding="utf-8")
    (root / "scripts" / "daily_briefing.sh").write_text(
        "#!/bin/bash\nexit \"${DOMAIN_BUILD_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    (aab / "scripts" / "consume_daily_briefing.py").write_text(
        "\n".join([
            "import os, sys",
            "with open(os.environ['CONSUME_LOG'], 'a', encoding='utf-8') as handle:",
            "    handle.write('\\t'.join(sys.argv[1:]) + '\\n')",
            "if '--runtime-projection-gcs' in sys.argv:",
            "    raise SystemExit(int(os.environ.get('RUNTIME_CONSUME_EXIT', '0')))",
            "raise SystemExit(int(os.environ.get('DOMAIN_CONSUME_EXIT', '0')))",
            "",
        ]),
        encoding="utf-8",
    )
    return root, aab, tmp_path / "consume.log"


def _run(tmp_path: Path, *, real_main: bool = False, **env: str) -> subprocess.CompletedProcess[str]:
    root, aab, log = _layout(tmp_path)
    if real_main:
        _install_real_main_fixture(root, aab)
    child_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "QUANT_MONITOR_ROOT": str(root),
        "AIAUDIT_BRIDGE_ROOT": str(aab),
        "CONSUME_LOG": str(log),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    child_env.update(env)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(root),
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )
    result.log = log.read_text(encoding="utf-8") if log.exists() else ""
    return result


def _calls(result: subprocess.CompletedProcess[str]) -> list[list[str]]:
    if not result.log:
        return []
    return [line.split("\t") for line in result.log.splitlines()]


def test_runtime_switch_off_does_not_call_gcs(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="false",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj/runtime_daily",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
    )
    assert result.returncode == 0
    calls = _calls(result)
    assert len(calls) == 1
    assert "--runtime-projection-gcs" not in calls[0]
    assert "--report-dir" in calls[0]
    assert "gs://" not in result.stdout + result.stderr


def test_enabled_pipeline_uses_timezone_day_and_fixed_paper_object(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/runtime_daily",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
    )
    assert result.returncode == 0, result.stderr
    zoned = datetime.now(ZoneInfo(ZONE)).date().isoformat()
    utc = datetime.now(timezone.utc).date().isoformat()
    calls = _calls(result)
    assert len(calls) == 2
    domain, runtime = calls
    assert "--runtime-projection-gcs" not in domain
    assert domain[domain.index("--day") + 1] == utc
    object_uri = runtime[runtime.index("--runtime-projection-gcs") + 1]
    assert object_uri == f"gs://proj-bucket/runtime_daily/longbridge/paper/{zoned}.json"
    assert runtime[runtime.index("--day") + 1] == zoned
    assert runtime[runtime.index("--expected-target-key") + 1] == "lb-paper|rot|paper"
    assert "--dispatch" in runtime
    assert "gs://" not in result.stdout + result.stderr
    assert "lb-paper|rot|paper" not in result.stdout + result.stderr
    if utc != zoned:
        assert domain[domain.index("--day") + 1] != runtime[runtime.index("--day") + 1]


def test_explicit_immutable_object_keeps_exact_day_without_changing_domain_day(tmp_path: Path) -> None:
    uri = "gs://proj-bucket/runtime_daily/longbridge/paper/2026-09-28/20260928T084000000000Z.json"
    result = _run(tmp_path,
                  QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
                  QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/runtime_daily",
                  QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
                  QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
                  QUANT_MONITOR_RUNTIME_PROJECTION_OBJECT=uri,
                  QUANT_MONITOR_RUNTIME_BUSINESS_DAY="2026-09-28")
    assert result.returncode == 0, result.stderr
    domain, runtime = _calls(result)
    assert domain[domain.index("--day") + 1] == datetime.now(timezone.utc).date().isoformat()
    assert runtime[runtime.index("--day") + 1] == "2026-09-28"
    assert runtime[runtime.index("--runtime-projection-gcs") + 1] == uri
    assert uri not in result.stdout + result.stderr


def test_incomplete_or_other_prefix_object_selector_never_starts_runtime(tmp_path: Path) -> None:
    uri = "gs://proj-bucket/runtime_daily/longbridge/paper/2026-09-28/20260928T084000000000Z.json"
    cases = [{"QUANT_MONITOR_RUNTIME_PROJECTION_OBJECT": uri},
             {"QUANT_MONITOR_RUNTIME_BUSINESS_DAY": "2026-09-28"},
             {"QUANT_MONITOR_RUNTIME_PROJECTION_OBJECT": uri.replace("proj-bucket", "other-bucket"),
              "QUANT_MONITOR_RUNTIME_BUSINESS_DAY": "2026-09-28"}]
    for index, selector in enumerate(cases):
        result = _run(tmp_path / str(index),
                      QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
                      QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/runtime_daily",
                      QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
                      QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper", **selector)
        assert result.returncode == 1
        assert len(_calls(result)) == 1
        assert "runtime_projection_config_invalid" in result.stderr
        assert "gs://" not in result.stdout + result.stderr


def test_domain_failure_still_runs_runtime_and_returns_nonzero(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/runtime_daily/",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
        DOMAIN_BUILD_EXIT="4",
    )
    assert result.returncode == 1
    assert len(_calls(result)) == 2
    assert "--runtime-projection-gcs" in _calls(result)[1]
    assert "domain_exit=4 runtime_exit=0" in result.stderr
    assert "gs://" not in result.stderr


def test_runtime_failure_does_not_skip_domain(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/team/runtime_daily",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|paper",
        RUNTIME_CONSUME_EXIT="5",
    )
    assert result.returncode == 1
    calls = _calls(result)
    assert "--report-dir" in calls[0]
    assert "--runtime-projection-gcs" in calls[1]
    assert "domain_exit=0 runtime_exit=5" in result.stderr


def test_invalid_runtime_config_skips_gcs_and_stays_nonzero(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://proj-bucket/other",
        QUANT_MONITOR_RUNTIME_TIMEZONE="Not/AZone",
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="lb-paper|rot|live",
    )
    assert result.returncode == 1
    calls = _calls(result)
    assert len(calls) == 1
    assert "--runtime-projection-gcs" not in calls[0]
    assert "runtime_projection_config_invalid" in result.stderr
    assert "gs://" not in result.stderr


def test_disabled_domain_failure_does_not_start_runtime(tmp_path: Path) -> None:
    result = _run(tmp_path, DOMAIN_BUILD_EXIT="4")
    assert result.returncode == 4
    assert _calls(result) == []
    assert "runtime-projection-gcs" not in result.stdout + result.stderr


def test_pipeline_receipt_separates_builder_and_consumer_first_error(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://private/runtime_daily",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="private-service|private-strategy|paper",
        DOMAIN_BUILD_EXIT="4", DOMAIN_CONSUME_EXIT="2", RUNTIME_CONSUME_EXIT="2",
    )
    assert result.returncode == 1
    assert len(_calls(result)) == 2
    assert "[briefing-pipeline-result:v1] branch=domain builder_exit=4 consumer_exit=2 domain_exit=4" in result.stderr
    assert "domain_exit=4 runtime_exit=2" in result.stderr
    assert "private" not in result.stderr


def test_pipeline_receipt_records_consumer_nonzero_after_successful_builder(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
        QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://private/runtime_daily",
        QUANT_MONITOR_RUNTIME_TIMEZONE=ZONE,
        QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="private-service|private-strategy|paper",
        DOMAIN_CONSUME_EXIT="2", RUNTIME_CONSUME_EXIT="2",
    )
    assert result.returncode == 1
    assert "[briefing-pipeline-result:v1] branch=domain builder_exit=0 consumer_exit=2 domain_exit=2" in result.stderr
    assert "domain_exit=2 runtime_exit=2" in result.stderr
    assert "private" not in result.stderr


def test_disabled_pipeline_receipt_marks_consumer_not_run(tmp_path: Path) -> None:
    result = _run(tmp_path, DOMAIN_BUILD_EXIT="4")
    assert result.returncode == 4
    assert _calls(result) == []
    assert result.stderr.strip() == "[briefing-pipeline-result:v1] branch=domain builder_exit=4 consumer_exit=not_run domain_exit=4"



def _install_real_main_fixture(root: Path, aab: Path) -> None:
    """Run the production CLI with only synthetic input/dispatch ports."""
    import json
    report_dir = root / "data/daily-reports" / datetime.now(timezone.utc).date().isoformat()
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "us_equity.json").write_text(json.dumps({
        "domain": "us_equity", "ok": False, "data_status": "unavailable", "error": "private-body-marker",
    }), encoding="utf-8")
    real_root = SCRIPT.parents[3]
    wrapper = r'''import contextlib, json, os, sys
from unittest.mock import patch
def guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
        raise AssertionError("external side effect forbidden")
def profile(frame, event, arg):
    if event == "call" and frame.f_code.co_filename.endswith(("gateway_client.py", "llm_adapter.py", "codex_adapter.py", "cursor_adapter.py")) and frame.f_code.co_name in {"execute", "analyze", "review", "complete", "parallel_review", "run"}:
        raise AssertionError("model forbidden")
sys.addaudithook(guard)
sys.setprofile(profile)
sys.path.insert(0, REAL_ROOT)
from scripts import consume_daily_briefing as cli
def read_fixture(uri):
    if os.environ.get("RUNTIME_FIXTURE_REJECT") == "true":
        return None, "runtime_projection_unreadable"
    args = sys.argv[1:]
    day = args[args.index("--day") + 1]
    key = args[args.index("--expected-target-key") + 1]
    service, strategy, scope = key.split("|")
    with open(os.environ['CONSUME_LOG'], 'a', encoding='utf-8') as handle:
        handle.write('\t'.join(sys.argv[1:]) + '\n')
    payload = {"platform": "longbridge", "observed_at": os.environ.get("RUNTIME_FIXTURE_OBSERVED_AT", day + "T00:00:00+00:00"), "completeness": "complete", "records": [{
        "target_key": key, "target": {"service": service, "strategy_profile": strategy, "account_scope": scope},
        "business_date": day, "timezone": "UTC", "status": "market_closed", "completeness": "complete",
        "execution_lane": "paper", "runs": [], "conflicts": [], "fills": {"source": "not_connected", "records": [], "count": None},
    }]}
    return json.dumps(payload).encode(), None
def synthetic_send(**kwargs):
    with open(os.environ['CONSUME_LOG'] + '.send', 'a', encoding='utf-8') as handle:
        handle.write('synthetic-send\n')
    return os.environ.get("RUNTIME_FIXTURE_SEND_OUTCOME", "sent")
with contextlib.ExitStack() as stack:
    stack.enter_context(patch.object(cli, "_read_gcs_object", side_effect=read_fixture))
    stack.enter_context(patch.object(cli, "dispatch_briefing_result", return_value={"action": "telegram", "errors": [], "skipped": [], "telegram_sent": True, "github_issue": None, "optimization_watch": None, "operational_fallback_sent": False}))
    if os.environ.get("RUNTIME_REAL_LEDGER") == "true":
        stack.enter_context(patch("service.briefing_dispatch.telegram_target_outcome", side_effect=synthetic_send))
    else:
        stack.enter_context(patch.object(cli, "dispatch_runtime_digest", return_value={"action": "runtime_digest", "errors": [], "skipped": [], "telegram_sent": True, "github_issue": None, "business_date": "synthetic-day", "event_id": "synthetic-event"}))
    raise SystemExit(cli.main())
'''
    wrapper = wrapper.replace("REAL_ROOT", repr(str(real_root)), 1)
    (aab / "scripts/consume_daily_briefing.py").write_text(wrapper, encoding="utf-8")


def test_real_main_pipeline_keeps_telegram_success_and_runtime_stdout_discard(tmp_path: Path) -> None:
    result = _run(tmp_path, real_main=True,
                  QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
                  QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://private/runtime_daily",
                  QUANT_MONITOR_RUNTIME_TIMEZONE="UTC",
                  QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="private-service|private-strategy|paper")
    assert result.returncode == 1, result.stderr
    assert "branch=domain stage=routing reason=telegram_attention action=telegram dispatch_failed=false exit=2" in result.stderr
    assert "branch=runtime stage=dispatch reason=dispatch_completed action=runtime_digest dispatch_failed=false exit=0" in result.stderr
    assert "branch=domain builder_exit=0 consumer_exit=2 domain_exit=2" in result.stderr
    assert "domain_exit=2 runtime_exit=0" in result.stderr
    assert "private" not in result.stderr and "gs://" not in result.stderr
    assert "runtime_digest" not in result.stdout and "accounts" not in result.stdout


def test_real_main_pipeline_keeps_runtime_read_rejection_nonzero(tmp_path: Path) -> None:
    result = _run(tmp_path, real_main=True,
                  QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
                  QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://private/runtime_daily",
                  QUANT_MONITOR_RUNTIME_TIMEZONE="UTC",
                  QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="private-service|private-strategy|paper",
                  RUNTIME_FIXTURE_REJECT="true")
    assert result.returncode == 1, result.stderr
    assert "branch=runtime stage=input_read reason=runtime_projection_unreadable action=none dispatch_failed=unknown exit=2" in result.stderr
    assert "domain_exit=2 runtime_exit=2" in result.stderr
    assert "runtime_projection_rejected" not in result.stdout
    assert "private" not in result.stderr and "gs://" not in result.stderr


def test_immutable_pipeline_uses_real_cli_and_ledger_after_process_restart(tmp_path: Path) -> None:
    import json
    uri = "gs://synthetic/runtime_daily/longbridge/paper/2026-09-28/20260928T084000000000Z.json"
    env = dict(QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
               QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://synthetic/runtime_daily",
               QUANT_MONITOR_RUNTIME_TIMEZONE="UTC",
               QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="synthetic-service|rot|paper",
               QUANT_MONITOR_RUNTIME_PROJECTION_OBJECT=uri,
               QUANT_MONITOR_RUNTIME_BUSINESS_DAY="2026-09-28",
               RUNTIME_FIXTURE_OBSERVED_AT="2026-09-28T08:40:00Z", RUNTIME_REAL_LEDGER="true",
               TELEGRAM_TOKEN="synthetic", GLOBAL_TELEGRAM_CHAT_ID="synthetic-chat")
    first = _run(tmp_path, real_main=True, **env)
    assert "runtime_exit=0" in first.stderr, first.stderr
    state = tmp_path / "monitor/data/alert-state/health_cycle.json"
    before = state.read_bytes()
    assert all(record["status"] == "sent" for targets in json.loads(before)["deliveries"].values() for record in targets.values())
    second = _run(tmp_path, real_main=True, **env)
    assert "runtime_exit=0" in second.stderr, second.stderr
    assert state.read_bytes() == before
    assert (tmp_path / "consume.log.send").read_text().splitlines() == ["synthetic-send"]
    assert all(call[call.index("--runtime-projection-gcs") + 1] == uri for call in _calls(second))
    assert "gs://" not in first.stdout + first.stderr + second.stdout + second.stderr


def test_immutable_pipeline_wrong_body_observation_rejects_before_send(tmp_path: Path) -> None:
    result = _run(tmp_path, real_main=True,
                  QUANT_MONITOR_RUNTIME_DIGEST_ENABLED="true",
                  QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX="gs://synthetic/runtime_daily",
                  QUANT_MONITOR_RUNTIME_TIMEZONE="UTC",
                  QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY="synthetic-service|rot|paper",
                  QUANT_MONITOR_RUNTIME_PROJECTION_OBJECT="gs://synthetic/runtime_daily/longbridge/paper/2026-09-28/20260928T084000000000Z.json",
                  QUANT_MONITOR_RUNTIME_BUSINESS_DAY="2026-09-28",
                  RUNTIME_FIXTURE_OBSERVED_AT="2026-09-28T08:40:01Z", RUNTIME_REAL_LEDGER="true",
                  TELEGRAM_TOKEN="synthetic", GLOBAL_TELEGRAM_CHAT_ID="synthetic-chat")
    assert result.returncode == 1
    assert "reason=runtime_object_observation_mismatch" in result.stderr
    assert not (tmp_path / "consume.log.send").exists()
    assert not (tmp_path / "monitor/data/alert-state/health_cycle.json").exists()


# The one-shot runs the published consumer directly, not the domain/AI pipeline.
ONCE_SCRIPT = SCRIPT.with_name("run_runtime_digest_once.py")
OBJECT = "gs://synthetic/runtime_daily/longbridge/paper/2026-10-08/20261008T000000000000Z.json"
TARGET = "synthetic-lb|synthetic-profile|paper"


def _once():
    spec = importlib.util.spec_from_file_location("runtime_digest_once", ONCE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _once_args(*, mode="preview", uri=OBJECT):
    return ["--source-root", str(SCRIPT.parents[3]), "--mode", mode,
            "--object", uri, "--day", "2026-10-08", "--expected-target-key", TARGET]


def _once_call(module, **kwargs):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = module.main(_once_args(**kwargs))
    return code, json.loads(output.getvalue())


def _once_payload():
    return {"platform": "longbridge", "observed_at": "2026-10-08T00:00:00Z",
            "completeness": "complete", "records": [{
                "target_key": TARGET,
                "target": {"service": "synthetic-lb", "strategy_profile": "synthetic-profile", "account_scope": "paper"},
                "business_date": "2026-10-08", "timezone": "UTC", "status": "market_closed",
                "completeness": "complete", "execution_lane": "paper", "runs": [], "conflicts": [],
                "fills": {"source": "not_connected", "records": [], "count": None}}]}


def _once_ledger(tmp_path):
    root = tmp_path / "monitor"
    state = root / "data/alert-state/health_cycle.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"deliveries":{}}', encoding="utf-8")
    return root, state


def test_once_invalid_object_stops_before_credentials_or_client():
    module = _once()
    for uri in ("https://external.invalid/private", OBJECT.replace("2026-10-08/", "2026-10-07/"),
                OBJECT.replace("000000000000Z", "000000000000+0000"), OBJECT + "/../private"):
        with patch.object(module, "_credentials") as credentials, patch.object(module, "_invoke") as invoke:
            code, result = _once_call(module, uri=uri)
        assert code == 2 and result == {"ok": False, "status": "stopped_without_retry"}
        credentials.assert_not_called()
        invoke.assert_not_called()


def test_once_topic_or_missing_route_stops_before_secret_read():
    module = _once()
    for text in (b'QUANT_SENTINEL_GCP_PROJECT=synthetic-project GLOBAL_TELEGRAM_CHAT_ID=-100123 TELEGRAM_MESSAGE_THREAD_ID=42',
                 b'QUANT_SENTINEL_GCP_PROJECT=synthetic-project',
                 b'QUANT_SENTINEL_GCP_PROJECT=synthetic-project GLOBAL_TELEGRAM_CHAT_ID=-100123 QUANT_SENTINEL_TELEGRAM_SECRET_NAME=other'):
        completed = subprocess.CompletedProcess([], 0, stdout=text)
        with patch.object(module, "_execution_home", return_value="/synthetic-home"), patch.object(module.subprocess, "run", return_value=completed) as process:
            code, result = _once_call(module)
        assert code == 2 and not result["ok"]
        assert process.call_count == 1
        assert process.call_args.args[0] == ["/usr/bin/systemctl", "show", "codex-daily-briefing.service", "--property=Environment", "--value"]


def test_once_uses_original_fixed_secret_in_memory(tmp_path, monkeypatch):
    module = _once()
    root, _ = _once_ledger(tmp_path)
    monkeypatch.setattr(module, "MONITOR_ROOT", root)
    response = io.BytesIO(b'{"ok":true}')
    unit = subprocess.CompletedProcess([], 0, stdout=("QUANT_SENTINEL_GCP_PROJECT=synthetic-project GLOBAL_TELEGRAM_CHAT_ID=-100123 QUANT_MONITOR_ROOT=" + str(root)).encode())
    secret = subprocess.CompletedProcess([], 0, stdout=b"123:synthetic\n")
    with patch.object(module, "_execution_home", return_value=str(tmp_path)), patch.object(module.subprocess, "run", side_effect=[unit, secret]) as process, patch.object(module.urllib.request, "build_opener") as builder:
        builder.return_value.open.return_value = response
        assert module._credentials() == ("123:synthetic", "-100123")
    assert process.call_args.args[0] == ["/usr/bin/gcloud", "secrets", "versions", "access", "latest", "--secret", "quant-sentinel-telegram-bot-token", "--project", "synthetic-project"]
    assert process.call_args.kwargs["env"]["HOME"] == str(tmp_path)
    handler = builder.call_args.args[0]
    assert handler.redirect_request(None, None, 302, "", {}, "https://external.invalid") is None
    assert process.call_count == 2


def test_once_preview_uses_actual_cli_without_state_write(tmp_path, monkeypatch):
    module = _once()
    root, state = _once_ledger(tmp_path)
    monkeypatch.setattr(module, "MONITOR_ROOT", root)
    monkeypatch.setenv("QUANT_MONITOR_ROOT", str(root))
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:synthetic")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "-100123")
    from scripts import consume_daily_briefing as cli
    from quant_monitor_domain import briefing_dispatch as dispatch
    before = state.read_bytes()
    with patch.object(cli, "_read_gcs_object", return_value=(json.dumps(_once_payload()).encode(), None)) as reader, patch.object(dispatch, "telegram_target_outcome") as transport:
        result = module._child(SCRIPT.parents[3], "preview", OBJECT, "2026-10-08", [TARGET])
    assert result == {"ok": True, "approved_route_bound": True, "ledger_readable": True, "sent": False, "duplicate": False}
    assert state.read_bytes() == before
    assert not state.with_suffix(".json.lock").exists()
    reader.assert_called_once_with(OBJECT)
    transport.assert_not_called()


def test_once_wrong_observation_and_missing_ledger_never_send(tmp_path, monkeypatch):
    module = _once()
    root, state = _once_ledger(tmp_path)
    monkeypatch.setattr(module, "MONITOR_ROOT", root)
    monkeypatch.setenv("QUANT_MONITOR_ROOT", str(root))
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:synthetic")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "-100123")
    from scripts import consume_daily_briefing as cli
    from quant_monitor_domain import briefing_dispatch as dispatch
    payload = _once_payload()
    payload["observed_at"] = "2026-10-08T00:00:01Z"
    with patch.object(cli, "_read_gcs_object", return_value=(json.dumps(payload).encode(), None)), patch.object(dispatch, "telegram_target_outcome") as transport:
        result = module._child(SCRIPT.parents[3], "send", OBJECT, "2026-10-08", [TARGET])
    assert not result["ok"] and not result["sent"]
    transport.assert_not_called()
    state.unlink()
    with patch.object(cli, "_read_gcs_object") as reader:
        try:
            module._child(SCRIPT.parents[3], "send", OBJECT, "2026-10-08", [TARGET])
        except ValueError:
            pass
        else:
            raise AssertionError("missing history accepted")
    reader.assert_not_called()


def test_once_unknown_and_return_loss_do_not_start_second_process():
    module = _once()
    for outcome in ({"ok": False, "approved_route_bound": True, "ledger_readable": True, "sent": False, "duplicate": False}, subprocess.TimeoutExpired("synthetic", 90)):
        with patch.object(module, "_credentials", return_value=("123:synthetic", "-100123")), patch.object(module, "_invoke", side_effect=[outcome]) as invoke:
            code, result = _once_call(module, mode="send")
        assert code == 2 and result["status"] == "stopped_without_retry"
        assert invoke.call_count == 1


def test_once_actual_cli_two_processes_reuse_original_ledger(tmp_path, monkeypatch):
    module = _once()
    root, state = _once_ledger(tmp_path)
    wrapper = tmp_path / "once-synthetic-child.py"
    wrapper.write_text('''import importlib.util, json, os, sys
from pathlib import Path
from unittest.mock import patch
def guard(event, args):
    if event.startswith('socket.') or event in {'subprocess.Popen','os.system','os.posix_spawn','os.exec'}:
        raise PermissionError('synthetic child external boundary')
    if event == 'open' and not isinstance(args[0], int):
        path = Path(args[0]).resolve()
        mode, flags = args[1:]
        write = isinstance(mode, str) and any(c in mode for c in 'wax+') or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)
        if write and ROOT not in path.parents:
            raise PermissionError('synthetic child write boundary')
def profile(frame, event, arg):
    if event == 'call' and frame.f_code.co_filename.endswith(('gateway_client.py','llm_adapter.py','codex_adapter.py','cursor_adapter.py')) and frame.f_code.co_name in {'execute','analyze','review','complete','parallel_review','run'}:
        raise PermissionError('synthetic child model boundary')
ROOT = Path(ROOT_VALUE)
sys.addaudithook(guard)
sys.setprofile(profile)
source = Path(SOURCE_VALUE)
sys.path.insert(0,str(source))
spec = importlib.util.spec_from_file_location('once', source/'ops/quant-monitor/scripts/run_runtime_digest_once.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.MONITOR_ROOT = ROOT/'monitor'
os.environ['QUANT_MONITOR_ROOT'] = str(module.MONITOR_ROOT)
from scripts import consume_daily_briefing as cli
from quant_monitor_domain import briefing_dispatch as dispatch
payload = PAYLOAD_VALUE
original_child = module._child
def checked_child(*args):
    try:
        return original_child(*args)
    except Exception:
        import traceback
        with (ROOT/'child-debug.txt').open('w') as handle:
            traceback.print_exc(file=handle)
        raise
module._child = checked_child
def send(**kwargs):
    path=ROOT/'send-count.txt'
    path.write_text(str(int(path.read_text())+1 if path.exists() else 1))
    return 'sent'
with patch.object(cli,'_read_gcs_object',return_value=(json.dumps(payload).encode(),None)), patch.object(dispatch,'telegram_target_outcome',side_effect=send):
    raise SystemExit(module.main(sys.argv[1:]))
'''.replace('ROOT_VALUE', repr(str(tmp_path))).replace('SOURCE_VALUE', repr(str(SCRIPT.parents[3]))).replace('PAYLOAD_VALUE', repr(_once_payload())), encoding="utf-8")
    def arguments(source_root, mode, uri, day, targets):
        return [sys.executable, "-I", "-B", str(wrapper), "--consumer-child", *_once_args(mode=mode)]
    with patch.object(module, "_arguments", side_effect=arguments), patch.object(module, "_execution_home", return_value=str(tmp_path)), patch.object(module, "_credentials", return_value=("123:synthetic", "-100123")):
        code, result = _once_call(module, mode="send")
    assert code == 0 and result["sent"] and result["restart_duplicate"], (tmp_path / "child-debug.txt").read_text() if (tmp_path / "child-debug.txt").exists() else result
    assert not result["already_delivered"]
    assert (tmp_path / "send-count.txt").read_text() == "1"
    ledger = json.loads(state.read_text())
    assert len(ledger["deliveries"]) == 1
    assert next(iter(next(iter(ledger["deliveries"].values())).values())) == {"status": "sent", "attempts": 1}
    assert "123:synthetic" not in state.read_text() and "-100123" not in state.read_text()



def test_once_actual_unknown_and_legacy_suppression_are_not_success(tmp_path, monkeypatch):
    module = _once()
    root, state = _once_ledger(tmp_path)
    monkeypatch.setattr(module, "MONITOR_ROOT", root)
    monkeypatch.setenv("QUANT_MONITOR_ROOT", str(root))
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:synthetic")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "-100123")
    from scripts import consume_daily_briefing as cli
    from quant_monitor_domain import briefing_dispatch as dispatch
    from quant_monitor_domain.runtime_digest import prepare_runtime_digest
    event = prepare_runtime_digest(_once_payload())["event_id"]
    raw = json.dumps(_once_payload()).encode()
    with patch.object(cli, "_read_gcs_object", return_value=(raw, None)), patch.object(dispatch, "telegram_target_outcome", return_value="unknown") as transport:
        first = module._child(SCRIPT.parents[3], "send", OBJECT, "2026-10-08", [TARGET])
        second = module._child(SCRIPT.parents[3], "send", OBJECT, "2026-10-08", [TARGET])
    assert not first["ok"] and not second["ok"] and not second["duplicate"]
    assert transport.call_count == 1
    state.write_text(json.dumps({"fingerprint": event}), encoding="utf-8")
    with patch.object(cli, "_read_gcs_object", return_value=(raw, None)), patch.object(dispatch, "telegram_target_outcome") as transport:
        legacy = module._child(SCRIPT.parents[3], "send", OBJECT, "2026-10-08", [TARGET])
    assert legacy["ok"] and not legacy["sent"] and not legacy["duplicate"]
    transport.assert_not_called()


def test_once_home_and_original_ledger_alias_are_not_replaced(tmp_path, monkeypatch):
    module = _once()
    identity = type("SyntheticUser", (), {"pw_name": "ubuntu", "pw_dir": str(tmp_path)})()
    monkeypatch.setenv("HOME", str(tmp_path / "other"))
    with patch.object(module.pwd, "getpwuid", return_value=identity):
        try:
            module._execution_home()
        except ValueError:
            pass
        else:
            raise AssertionError("HOME silently replaced")
    assert os.environ["HOME"] == str(tmp_path / "other")
    root, state = _once_ledger(tmp_path)
    monkeypatch.setattr(module, "MONITOR_ROOT", root)
    other = tmp_path / "other-monitor"
    (other / "data").mkdir(parents=True)
    unit = subprocess.CompletedProcess([], 0, stdout=("QUANT_SENTINEL_GCP_PROJECT=synthetic-project GLOBAL_TELEGRAM_CHAT_ID=-100123 QUANT_MONITOR_ROOT=" + str(other)).encode())
    before = state.read_bytes()
    with patch.object(module, "_execution_home", return_value=str(tmp_path)), patch.object(module.subprocess, "run", return_value=unit) as process:
        code, result = _once_call(module)
    assert code == 2 and not result["ok"] and process.call_count == 1
    assert state.read_bytes() == before

def test_once_workflow_keeps_fixed35_and_separate_exact_c_source():
    text = (SCRIPT.parents[3] / ".github/workflows/vps_codex_service_ops.yml").read_text()
    assert "ref: 35ac71176127f07e00fe04dbc793777f3c595bc0" in text
    job = text.split("  longbridge-paper-once:\n", 1)[1]
    assert "ref: 823ba856af49d8799506afe476aec9d07ab1c63d" in job
    assert "persist-credentials: false" in job and '"$current_main" = "$RUN_SHA"' in job
    assert "sudo -n -u ubuntu" in job and "/usr/bin/python3 -I -B" in job
    assert "daily_briefing_pipeline.sh" not in job and "systemctl" not in job
    assert "--ai-summary" not in job and "--dual-review" not in job
    dispatch_inputs = text.split("\npermissions:", 1)[0]
    assert "runtime_projection_object:" not in dispatch_inputs
    assert "runtime_expected_target_keys:" not in dispatch_inputs
    assert "inputs.runtime_projection_object" not in text
    assert "inputs.runtime_expected_target_keys" not in text
    assert "QUANT_MONITOR_RUNTIME_PROJECTION_OBJECT: ${{ secrets.RUNTIME_DIGEST_ONCE_PROJECTION_OBJECT }}" in job
    assert "QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY: ${{ secrets.RUNTIME_DIGEST_ONCE_TARGET_KEYS }}" in job
    assert "QUANT_MONITOR_RUNTIME_BUSINESS_DAY: ${{ inputs.runtime_business_day }}" in job
