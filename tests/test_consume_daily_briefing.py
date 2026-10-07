from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from scripts.consume_daily_briefing import main


def _write_critical_report(report_dir: Path) -> None:
    (report_dir / "us_equity.json").write_text(
        json.dumps(
            {
                "domain": "us_equity",
                "ok": True,
                "strategies": [
                    {
                        "strategy_profile": "global_etf_rotation",
                        "status": "critical",
                        "overall_score": 14.2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_successful_optimization_record_exits_zero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        _write_critical_report(report_dir)
        dispatch_summary = {
            "action": "github_issue",
            "optimization_watch": {"status": "ok", "errors": 0},
            "errors": [],
        }

        with patch(
            "scripts.consume_daily_briefing.dispatch_briefing_result",
            return_value=dispatch_summary,
        ):
            assert main(["--report-dir", str(report_dir), "--dispatch"]) == 0


def test_optimization_record_failure_exits_nonzero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        _write_critical_report(report_dir)
        dispatch_summary = {
            "action": "github_issue",
            "optimization_watch": {"status": "partial_error", "errors": 1},
            "errors": ["optimization_record_failed"],
        }

        with patch(
            "scripts.consume_daily_briefing.dispatch_briefing_result",
            return_value=dispatch_summary,
        ):
            assert main(["--report-dir", str(report_dir), "--dispatch"]) == 2


def test_undispatched_optimization_finding_exits_nonzero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        _write_critical_report(report_dir)

        assert main(["--report-dir", str(report_dir)]) == 2


def _write_telegram_report(report_dir: Path) -> None:
    (report_dir / "us_equity.json").write_text(
        json.dumps(
            {
                "domain": "us_equity",
                "ok": False,
                "data_status": "unavailable",
                "error": "synthetic unavailable",
            }
        ),
        encoding="utf-8",
    )


def test_send_dry_run_cli_fail_closed_nonzero_without_side_effects(capsys) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        _write_telegram_report(report_dir)
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("service.briefing_dispatch.urllib.request.urlopen") as urlopen,
            patch("service.briefing_dispatch.subprocess.check_output") as check_output,
            patch("service.briefing_dispatch.create_github_issue") as create_issue,
            patch("service.briefing_dispatch.send_telegram_alert") as send_tg,
            patch("service.automation_run_ledger.get_automation_run_ledger") as get_ledger,
        ):
            code = main(["--report-dir", str(report_dir), "--send-dry-run"])
        payload = json.loads(capsys.readouterr().out)
        dispatch = payload["dispatch"]
        assert code == 2
        assert dispatch["send_dry_run"] is True
        assert "telegram_missing_env" in dispatch["errors"]
        assert dispatch["telegram_sent"] is False
        assert dispatch["github_issue"] is None
        assert dispatch["telegram_dry_run"] == {
            "present": True,
            "safe_summary": "telegram_preview_available",
        }
        urlopen.assert_not_called()
        check_output.assert_not_called()
        create_issue.assert_not_called()
        send_tg.assert_not_called()
        get_ledger.assert_not_called()


def test_send_dry_run_cli_configured_still_skips_send(capsys) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report_dir = Path(tmp)
        _write_telegram_report(report_dir)
        with (
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_TOKEN": "secret-token-value",
                    "GLOBAL_TELEGRAM_CHAT_ID": "123",
                    "QSL_GITHUB_REPO": "QuantStrategyLab/AIAuditBridge",
                    "QUANT_MONITOR_ROOT": str(report_dir),
                },
                clear=True,
            ),
            patch("service.briefing_dispatch.shutil_which", return_value="/usr/bin/gh"),
            patch("service.briefing_dispatch.urllib.request.urlopen") as urlopen,
            patch("service.briefing_dispatch.send_telegram_alert") as send_tg,
        ):
            code = main(["--report-dir", str(report_dir), "--send-dry-run"])
        payload = json.loads(capsys.readouterr().out)
        dispatch = payload["dispatch"]
        # A configuration-check dry-run succeeds when prerequisites are present.
        assert code == 0
        assert dispatch["errors"] == []
        assert dispatch["sender_prerequisites"]["telegram_token_present"] is True
        assert "secret-token-value" not in json.dumps(dispatch)
        assert "123" not in json.dumps(dispatch)
        urlopen.assert_not_called()
        send_tg.assert_not_called()
        assert not (report_dir / "data" / "alert-state" / "health_cycle.json").exists()


def _write_summary_report(report_dir, *, as_of='2026-09-09T22:00:00+00:00'):
    (report_dir / 'us_equity.json').write_text(json.dumps({
        'domain': 'us_equity', 'ok': True, 'data_status': 'ready', 'as_of': as_of,
        'strategies': [{'strategy_profile': 'private-profile', 'status': 'critical', 'as_of': '2026-09-08',
                        'overall_score': 12, 'secret': 'private-marker'}],
        'error': 'private-marker', 'summary': {'critical': 9000},
    }))


def test_summary_report_keeps_source_and_generation_clocks_separate():
    from service.briefing_consumer import _summary_report

    report = _summary_report({
        'domain': 'us_equity', 'ok': True, 'data_status': 'ready',
        'as_of': '2026-09-09T21:45:00+00:00',
        'generated_at': '2026-09-09T22:00:00+00:00',
        'strategies': [{'strategy_profile': 'private', 'status': 'healthy', 'as_of': '2026-09-09'}],
    }, source='us_equity.json')

    assert report['source_as_of'] == '2026-09-09T21:45:00+00:00'
    assert report['report_generated_at'] == '2026-09-09T22:00:00+00:00'
    assert report['data_ready'] is True


def test_summary_report_accepts_only_explicit_not_configured_domain():
    from service.briefing_consumer import _summary_report

    report = _summary_report({
        'domain': 'crypto', 'ok': True, 'data_status': 'not_configured',
        'as_of': '2026-09-09T21:45:00+00:00',
        'generated_at': '2026-09-09T22:00:00+00:00',
        'coverage': {'expected_profiles': [], 'observed_profiles': [], 'missing_profiles': []},
        'strategies': [],
    }, source='crypto.json')
    unstated = _summary_report({
        'domain': 'crypto', 'ok': True, 'data_status': 'not_configured',
        'as_of': '2026-09-09T21:45:00+00:00', 'generated_at': '2026-09-09T22:00:00+00:00',
        'strategies': [],
    }, source='crypto.json')

    assert report['not_configured'] is True
    assert report['data_ready'] is False
    assert unstated['not_configured'] is False


def test_summary_report_rejects_empty_ready_with_missing_profile_coverage():
    from service.briefing_consumer import _summary_report

    report = _summary_report({
        'domain': 'us_equity', 'ok': True, 'data_status': 'ready',
        'as_of': '2026-09-09T21:45:00+00:00',
        'generated_at': '2026-09-09T22:00:00+00:00',
        'coverage': {
            'expected_profiles': ['expected_profile'],
            'observed_profiles': [],
            'missing_profiles': ['expected_profile'],
        },
        'strategies': [],
    }, source='us_equity.json')

    assert report['data_ready'] is False


def test_ai_summary_dry_run_never_authenticates_or_calls_model(tmp_path, capsys):
    _write_summary_report(tmp_path)
    with patch('client.gateway_client.AiGatewayClient.execute') as execute:
        assert main(['--report-dir', str(tmp_path), '--day', '2026-09-09', '--ai-summary', '--dry-run']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['ai_summary']['status'] == 'dry_run'
    execute.assert_not_called()


def test_ai_summary_static_service_token_does_not_authorize_timer_execution(tmp_path, capsys):
    _write_summary_report(tmp_path)
    with patch.dict('os.environ', {'CODEX_AUDIT_SERVICE_URL': 'https://synthetic.invalid', 'CODEX_AUDIT_SERVICE_TOKEN': 'synthetic-private'}, clear=True), patch(
        'client.gateway_client.AiGatewayClient.execute'
    ) as execute:
        assert main(['--report-dir', str(tmp_path), '--day', '2026-09-09', '--ai-summary']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['ai_summary'] == {'status': 'unavailable', 'reason': 'github_oidc_required', 'advisory_only': True}
    execute.assert_not_called()


def test_ai_summary_uses_gateway_contract_after_existing_alert_dispatch(tmp_path, capsys):
    from datetime import datetime, timezone
    from client.gateway_client import AiResult
    _write_summary_report(tmp_path)
    calls = []
    route = {'provider': 'codex', 'research_stage': 'research_summary', 'model': 'gpt-5.6-luna', 'reasoning_effort': 'low', 'status': 'succeeded', 'output': 'synthetic advisory'}
    def execute(_client, prompt, **kwargs):
        calls.append('ai')
        assert kwargs['research_stage'] == 'research_summary'
        assert kwargs['mode'] == 'review_only'
        assert 'private-marker' not in prompt and 'private-profile' not in prompt
        assert '9000' not in prompt
        assert '2026-09-08' in prompt and '2026-09-09T22:00:00+00:00' in prompt
        return AiResult(provider='codex', model='gpt-5.6-luna', success=True, output='synthetic advisory', raw=route)
    def dispatch(*args, **kwargs):
        calls.append('dispatch')
        return {'errors': [], 'optimization_watch': {'errors': 0}}
    with patch.dict('os.environ', {'CODEX_AUDIT_SERVICE_URL': 'https://synthetic.invalid', 'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://synthetic.invalid/oidc', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic', 'GITHUB_REPOSITORY': 'QuantStrategyLab/AIAuditBridge'}, clear=True), patch(
        'client.gateway_client.AiGatewayClient.execute', autospec=True, side_effect=execute
    ), patch('scripts.consume_daily_briefing.dispatch_briefing_result', side_effect=dispatch), patch(
        'service.briefing_consumer._summary_now', return_value=datetime(2026,9,9,22,30,tzinfo=timezone.utc)
    ):
        assert main(['--report-dir', str(tmp_path), '--day', '2026-09-09', '--dispatch', '--ai-summary']) == 0
    result = json.loads(capsys.readouterr().out)
    assert calls == ['dispatch', 'ai']
    assert result['ai_summary']['status'] == 'available'
    assert result['ai_summary']['advisory_only'] is True
    assert result['action'] == 'github_issue'


def _projection_file(tmp_path: Path) -> Path:
    path = tmp_path / "projection.json"
    path.write_text(json.dumps({
        "platform": "longbridge",
        "observed_at": "2026-09-28T08:40:00+00:00",
        "completeness": "complete",
        "read_errors": [],
        "unmatched_reports": [],
        "records": [{
            "platform": "longbridge",
            "target_key": "lb-svc|rot|paper",
            "target": {"service": "lb-svc", "strategy_profile": "rot", "account_scope": "paper"},
            "business_date": "2026-09-28",
            "timezone": "Asia/Hong_Kong",
            "status": "market_closed",
            "completeness": "complete",
            "execution_lane": "paper",
            "runs": [],
            "conflicts": [],
            "fills": {"source": "not_connected", "records": [], "count": None},
        }],
    }), encoding="utf-8")
    return path


def test_runtime_projection_preview_does_not_send_or_call_model(tmp_path, capsys) -> None:
    path = _projection_file(tmp_path)
    with patch("service.briefing_dispatch.telegram_target_outcome") as send_target, patch(
        "client.gateway_client.AiGatewayClient.execute",
    ) as execute:
        code = main(["--runtime-projection", str(path)])
    captured = json.loads(capsys.readouterr().out)
    assert code == 0
    assert captured["kind"] == "runtime_digest"
    assert "休市" in captured["text"]
    assert "dispatch" not in captured
    send_target.assert_not_called()
    execute.assert_not_called()
    assert not (tmp_path / "data" / "alert-state" / "health_cycle.json").exists()


def test_runtime_projection_dry_run_does_not_persist_delivery(tmp_path, capsys) -> None:
    path = _projection_file(tmp_path)
    with patch.dict("os.environ", {"QUANT_MONITOR_ROOT": str(tmp_path), "TELEGRAM_TOKEN": "tok", "GLOBAL_TELEGRAM_CHAT_ID": "1"}, clear=True), patch(
        "service.briefing_dispatch.telegram_target_outcome",
    ) as send_target:
        code = main(["--runtime-projection", str(path), "--dry-run"])
    captured = json.loads(capsys.readouterr().out)
    assert code == 0
    assert captured["dispatch"]["skipped"] == ["dry_run"]
    send_target.assert_not_called()
    assert not (tmp_path / "data" / "alert-state" / "health_cycle.json").exists()


def _multi_target_projection_file(tmp_path):
    path = _projection_file(tmp_path)
    projection = json.loads(path.read_text())
    for profile, status in (("alpha", "unknown"), ("beta", "not_due")):
        record = dict(projection["records"][0])
        record["target"] = dict(record["target"], strategy_profile=profile)
        record["target_key"] = f"lb-svc|{profile}|paper"
        record["status"] = status
        projection["records"].append(record)
    path.write_text(json.dumps(projection), encoding="utf-8")
    return path


def _multi_target_args(path, *, dispatch=False):
    args = ["--runtime-projection", str(path), "--day", "2026-09-28"]
    for profile in ("rot", "alpha", "beta"):
        args.extend(["--expected-target-key", f"lb-svc|{profile}|paper"])
    return args + (["--dispatch"] if dispatch else [])


def _immutable_gcs_args(uri=None, *, dispatch=False):
    uri = uri or "gs://synthetic/runtime_daily/longbridge/paper/2026-09-28/20260928T084000000000Z.json"
    return ["--runtime-projection-gcs", uri, "--day", "2026-09-28",
            "--expected-target-key", "lb-svc|rot|paper"] + (["--dispatch"] if dispatch else [])


def test_current_producer_immutable_object_preview_is_accepted_once(tmp_path, capsys):
    projection = json.loads(_projection_file(tmp_path).read_text())
    # Actual producer _iso/_object_uri output: UTC Z body, six-digit path fraction.
    projection["observed_at"] = "2026-09-28T08:40:00Z"
    uri = _immutable_gcs_args()[1]
    with patch("scripts.consume_daily_briefing._read_gcs_object", return_value=(json.dumps(projection).encode(), None)) as reader, patch(
        "service.briefing_dispatch.telegram_target_outcome", side_effect=AssertionError("preview must not send"),
    ), patch("scripts.consume_daily_briefing.subprocess.Popen", side_effect=AssertionError("external process forbidden")):
        assert main(_immutable_gcs_args()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["observed_at"] == projection["observed_at"]
    assert "dispatch" not in result
    assert result["accounts"][0]["fills"]["count"] is None
    reader.assert_called_once_with(uri)


def test_immutable_object_microseconds_dispatch_and_restart_dedupe(tmp_path, capsys):
    from service import briefing_dispatch

    projection = json.loads(_projection_file(tmp_path).read_text())
    projection["observed_at"] = "2026-09-28T08:40:00.123456Z"
    uri = "gs://synthetic/runtime_daily/longbridge/paper/2026-09-28/20260928T084000123456Z.json"
    with patch.dict("os.environ", {"QUANT_MONITOR_ROOT": str(tmp_path), "TELEGRAM_TOKEN": "synthetic",
                                  "GLOBAL_TELEGRAM_CHAT_ID": "synthetic-chat"}, clear=True), patch(
        "scripts.consume_daily_briefing._read_gcs_object", return_value=(json.dumps(projection).encode(), None),
    ) as reader, patch("service.briefing_dispatch.telegram_target_outcome", return_value="sent") as send, patch(
        "scripts.consume_daily_briefing.subprocess.Popen", side_effect=AssertionError("external process forbidden"),
    ):
        assert main(_immutable_gcs_args(uri, dispatch=True)) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["dispatch"]["telegram_sent"] is True
        state = tmp_path / "data/alert-state/health_cycle.json"
        before = state.read_bytes()
        briefing_dispatch._HEALTH_CYCLE = None
        assert main(_immutable_gcs_args(uri, dispatch=True)) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["dispatch"]["skipped"] == ["duplicate_delivered"]
        assert second["event_id"] == first["event_id"]
        assert state.read_bytes() == before
    assert reader.call_count == 2
    assert all(call.args == (uri,) and call.kwargs == {} for call in reader.call_args_list)
    send.assert_called_once()


def test_immutable_object_body_observation_mismatch_never_dispatches(tmp_path, capsys):
    projection = json.loads(_projection_file(tmp_path).read_text())
    for observed in ("2026-09-28T08:40:01Z", "2026-09-28T08:40:00.000001Z",
                     "2026-09-28T16:40:00+08:00", "2026-09-28T08:40:00+00:00", "2026-09-28T08:40:00"):
        projection["observed_at"] = observed
        with patch("scripts.consume_daily_briefing._read_gcs_object", return_value=(json.dumps(projection).encode(), None)) as reader, patch(
            "scripts.consume_daily_briefing.dispatch_runtime_digest", side_effect=AssertionError("mismatched object cannot dispatch"),
        ) as dispatch:
            assert main(_immutable_gcs_args(dispatch=True)) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["reason"] == "runtime_object_observation_mismatch"
        assert "gs://" not in json.dumps(result)
        reader.assert_called_once_with(_immutable_gcs_args()[1])
        dispatch.assert_not_called()


def test_immutable_object_still_rejects_body_day_and_target_conflicts(tmp_path, capsys):
    for conflict, reason in (("day", "business_date_mismatch"), ("scope", "expected_target_mismatch")):
        projection = json.loads(_projection_file(tmp_path).read_text())
        projection["observed_at"] = "2026-09-28T08:40:00Z"
        record = projection["records"][0]
        if conflict == "day":
            record["business_date"] = "2026-09-27"
        else:
            record["target_key"] = "lb-svc|rot|live"
            record["target"]["account_scope"] = "live"
        with patch("scripts.consume_daily_briefing._read_gcs_object", return_value=(json.dumps(projection).encode(), None)) as reader, patch(
            "scripts.consume_daily_briefing.dispatch_runtime_digest", side_effect=AssertionError("conflicting body cannot dispatch"),
        ) as dispatch:
            assert main(_immutable_gcs_args(dispatch=True)) == 2
        assert json.loads(capsys.readouterr().out)["reason"] == reason
        reader.assert_called_once_with(_immutable_gcs_args()[1])
        dispatch.assert_not_called()


def test_invalid_immutable_paths_and_external_sources_never_read(capsys):
    prefix = "gs://synthetic/runtime_daily/longbridge/paper"
    cases = [
        (prefix + "/2026-09-27/20260928T084000000000Z.json", "runtime_object_day_mismatch"),
        (prefix + "/2026-09-28/20260928T084000Z.json", "invalid_runtime_object"),
        (prefix + "/2026-09-28/20260928T084000000000+0800.json", "invalid_runtime_object"),
        (prefix + "/2026-09-28/20260928T084000000000z.json", "invalid_runtime_object"),
        (prefix + "/2026-09-28/20260931T084000000000Z.json", "invalid_runtime_object"),
        (prefix + "/2026-09-28/20260928T244000000000Z.json", "invalid_runtime_object"),
        (prefix + "/2026-09-28/../20260928T084000000000Z.json", "invalid_runtime_object"),
        (prefix + "/2026-09-28/*.json", "invalid_runtime_object"),
        (prefix + "/2026-09-28/20260928T084000000000Z.json?generation=1", "invalid_runtime_object"),
        (prefix + "/2026-09-28/%32%30%32%36.json", "invalid_runtime_object"),
        ("gs://synthetic/runtime_daily/longbridge/live/2026-09-28/20260928T084000000000Z.json", "invalid_runtime_object"),
        ("https://external.example/runtime_daily/longbridge/paper/2026-09-28/20260928T084000000000Z.json", "invalid_runtime_object"),
    ]
    with patch("scripts.consume_daily_briefing._read_gcs_object", side_effect=AssertionError("invalid path cannot read")) as reader, patch(
        "service.briefing_dispatch.telegram_target_outcome", side_effect=AssertionError("invalid path cannot send"),
    ) as send, patch("scripts.consume_daily_briefing.subprocess.Popen", side_effect=AssertionError("external process forbidden")):
        for uri, reason in cases:
            assert main(_immutable_gcs_args(uri, dispatch=True)) == 2
            result = json.loads(capsys.readouterr().out)
            assert result["reason"] == reason
        live_args = _immutable_gcs_args()
        live_args[-1] = "lb-svc|rot|live"
        assert main(live_args) == 2
        assert json.loads(capsys.readouterr().out)["reason"] == "expected_scope_not_paper"
    reader.assert_not_called()
    send.assert_not_called()


def test_multi_target_cli_preview_then_dispatch_survives_module_restart(tmp_path, capsys):
    from service import briefing_dispatch

    path = _multi_target_projection_file(tmp_path)
    state = tmp_path / "data" / "alert-state" / "health_cycle.json"
    sent = []
    with patch.dict("os.environ", {"QUANT_MONITOR_ROOT": str(tmp_path),
                                  "TELEGRAM_TOKEN": "synthetic", "GLOBAL_TELEGRAM_CHAT_ID": "synthetic-chat"}, clear=True), patch(
        "service.briefing_dispatch.telegram_target_outcome",
        side_effect=lambda **kwargs: sent.append(kwargs["text"]) or "sent",
    ):
        assert main(_multi_target_args(path)) == 0
        preview = json.loads(capsys.readouterr().out)
        assert not state.exists() and sent == []
        assert len(preview["accounts"]) == 3
        assert all(account["fills"]["count"] is None and account["funds"]["cash"] is None
                   for account in preview["accounts"])
        assert main(_multi_target_args(path, dispatch=True)) == 0
        delivered = json.loads(capsys.readouterr().out)
        assert sent == [preview["text"]]
        assert delivered["dispatch"]["telegram_sent"] is True
        original = state.read_bytes()
        # Reload the delivery module from disk, and reverse producer target order.
        briefing_dispatch._HEALTH_CYCLE = None
        projection = json.loads(path.read_text())
        projection["records"].reverse()
        projection["observed_at"] = "2026-09-28T09:00:00+00:00"
        path.write_text(json.dumps(projection))
        assert main(_multi_target_args(path, dispatch=True)) == 0
        recovered = json.loads(capsys.readouterr().out)
        assert recovered["event_id"] == preview["event_id"]
        assert recovered["dispatch"]["skipped"] == ["duplicate_delivered"]
        assert sent == [preview["text"]] and state.read_bytes() == original


def test_multi_target_cli_failed_recipient_only_retries_after_restart(tmp_path, capsys):
    from service import briefing_dispatch

    path = _multi_target_projection_file(tmp_path)
    calls = []
    with patch.dict("os.environ", {"QUANT_MONITOR_ROOT": str(tmp_path),
                                  "TELEGRAM_TOKEN": "synthetic", "GLOBAL_TELEGRAM_CHAT_ID": "synthetic-ok,synthetic-failed"}, clear=True):
        def first(**kwargs):
            calls.append(kwargs["chat_id"])
            return "sent" if kwargs["chat_id"] == "synthetic-ok" else "failed"

        with patch("service.briefing_dispatch.telegram_target_outcome", side_effect=first):
            assert main(_multi_target_args(path, dispatch=True)) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["dispatch"]["errors"] == ["telegram_delivery_failed"]
        calls.clear()
        briefing_dispatch._HEALTH_CYCLE = None
        with patch("service.briefing_dispatch.telegram_target_outcome", side_effect=lambda **kw: calls.append(kw["chat_id"]) or "sent"):
            assert main(_multi_target_args(path, dispatch=True)) == 0
        assert json.loads(capsys.readouterr().out)["dispatch"]["telegram_sent"] is True
        assert calls == ["synthetic-failed"]


def test_multi_target_cli_unknown_is_not_resent_after_restart(tmp_path, capsys):
    from service import briefing_dispatch

    path = _multi_target_projection_file(tmp_path)
    with patch.dict("os.environ", {"QUANT_MONITOR_ROOT": str(tmp_path),
                                  "TELEGRAM_TOKEN": "synthetic", "GLOBAL_TELEGRAM_CHAT_ID": "synthetic-chat"}, clear=True):
        with patch("service.briefing_dispatch.telegram_target_outcome", return_value="unknown") as send:
            assert main(_multi_target_args(path, dispatch=True)) == 2
        assert send.call_count == 1
        assert "telegram_delivery_unknown" in json.loads(capsys.readouterr().out)["dispatch"]["errors"]
        briefing_dispatch._HEALTH_CYCLE = None
        with patch("service.briefing_dispatch.telegram_target_outcome", side_effect=AssertionError("unknown must not resend")) as send:
            assert main(_multi_target_args(path, dispatch=True)) == 2
        assert "telegram_delivery_unknown" in json.loads(capsys.readouterr().out)["dispatch"]["errors"]
        send.assert_not_called()


def test_multi_target_cli_post_send_write_failure_holds_pending_after_restart(tmp_path, capsys):
    from service import briefing_dispatch

    path = _multi_target_projection_file(tmp_path)
    with patch.dict("os.environ", {"QUANT_MONITOR_ROOT": str(tmp_path),
                                  "TELEGRAM_TOKEN": "synthetic", "GLOBAL_TELEGRAM_CHAT_ID": "synthetic-chat"}, clear=True):
        health = briefing_dispatch._health_cycle_module()
        persist = health._persist_delivery_payload

        def fail_after_send(root, payload):
            records = [record for targets in payload.get("deliveries", {}).values() for record in targets.values()]
            if any(record["status"] == "sent" for record in records):
                raise OSError("synthetic post-send write failure")
            return persist(root, payload)

        with patch.object(health, "_persist_delivery_payload", side_effect=fail_after_send), patch(
            "service.briefing_dispatch.telegram_target_outcome", return_value="sent",
        ) as send:
            assert main(_multi_target_args(path, dispatch=True)) == 2
        assert send.call_count == 1
        assert "alert_state_write_failed" in json.loads(capsys.readouterr().out)["dispatch"]["errors"]
        state = tmp_path / "data" / "alert-state" / "health_cycle.json"
        assert '"pending"' in state.read_text()
        briefing_dispatch._HEALTH_CYCLE = None
        with patch("service.briefing_dispatch.telegram_target_outcome", side_effect=AssertionError("pending must not resend")) as send:
            assert main(_multi_target_args(path, dispatch=True)) == 2
        assert "telegram_delivery_unknown" in json.loads(capsys.readouterr().out)["dispatch"]["errors"]
        send.assert_not_called()
        assert '"unknown"' in state.read_text() and '"pending"' not in state.read_text()


def test_runtime_content_preview_preserves_existing_state_and_has_no_ports(tmp_path, capsys) -> None:
    path = _projection_file(tmp_path)
    state = tmp_path / "data" / "alert-state" / "health_cycle.json"
    state.parent.mkdir(parents=True)
    original = b'{"synthetic": "unknown-must-remain"}\n'
    state.write_bytes(original)
    with patch.dict("os.environ", {"QUANT_MONITOR_ROOT": str(tmp_path)}, clear=True), patch(
        "service.briefing_dispatch.telegram_target_outcome",
        side_effect=AssertionError("send forbidden"),
    ), patch("client.gateway_client.AiGatewayClient.execute", side_effect=AssertionError("model forbidden")), patch(
        "scripts.consume_daily_briefing._read_gcs_object", side_effect=AssertionError("fetch forbidden"),
    ):
        assert main(["--runtime-projection", str(path), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["observed_at"] == "2026-09-28T08:40:00+00:00"
    assert result["accounts"][0]["fills"]["count"] is None
    assert result["accounts"][0]["funds"]["currency"] is None
    for section in ("业务日", "Asia/Hong_Kong", "运行", "成交覆盖", "资金覆盖", "人工事项"):
        assert section in result["text"]
    assert state.read_bytes() == original


def test_runtime_unconnected_funds_rejection_is_fixed_and_never_dispatched(tmp_path, capsys) -> None:
    path = _projection_file(tmp_path)
    projection = json.loads(path.read_text())
    projection["records"][0]["cash"] = [{"currency": "USD", "available_cash": 10}]
    path.write_text(json.dumps(projection))
    with patch("scripts.consume_daily_briefing.dispatch_runtime_digest", side_effect=AssertionError("dispatch forbidden")):
        assert main(["--runtime-projection", str(path)]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reason"] == "funds_not_connected"
    assert "reason=funds_not_connected" in captured.err
    assert "USD" not in captured.out + captured.err


def test_domain_quiet_route_is_unchanged(tmp_path, capsys) -> None:
    (tmp_path / "us_equity.json").write_text(json.dumps({
        "domain": "us_equity", "ok": True, "data_status": "ready", "as_of": "2026-09-28T08:00:00+00:00",
        "strategies": [{"strategy_profile": "rot", "status": "healthy", "overall_score": 80, "as_of": "2026-09-28"}],
    }), encoding="utf-8")
    assert main(["--report-dir", str(tmp_path)]) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["action"] == "quiet"
    assert "runtime_digest" not in captured


def _result_receipt(capsys):
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("[briefing-result:v1] ")
    assert len(lines[0]) <= 240
    fields = dict(field.split("=", 1) for field in lines[0].split()[1:])
    assert set(fields) == {"branch", "stage", "reason", "action", "dispatch_failed", "exit"}
    return json.loads(captured.out), fields


def test_domain_result_receipt_keeps_telegram_success_nonzero(tmp_path, capsys):
    _write_telegram_report(tmp_path)
    summary = _complete_dispatch_summary("telegram", telegram_sent=True)
    with patch("scripts.consume_daily_briefing.dispatch_briefing_result", return_value=summary):
        code = main(["--report-dir", str(tmp_path), "--dispatch"])
    payload, receipt = _result_receipt(capsys)
    assert code == 2
    assert payload["dispatch"] == summary
    assert "result_receipt" not in payload
    assert receipt == {"branch": "domain", "stage": "routing", "reason": "telegram_attention",
                       "action": "telegram", "dispatch_failed": "false", "exit": "2"}


def test_domain_result_receipt_keeps_optimization_exit_rules(tmp_path, capsys):
    _write_critical_report(tmp_path)
    for summary, expected_code, reason, failed in [
        (_complete_dispatch_summary("github_issue", optimization_watch={"errors": 0}), 0, "github_issue_recorded", "false"),
        ({"errors": ["optimization_record_failed"], "optimization_watch": {"errors": 1}},
         2, "optimization_record_failed", "true"),
    ]:
        with patch("scripts.consume_daily_briefing.dispatch_briefing_result", return_value=summary):
            assert main(["--report-dir", str(tmp_path), "--dispatch"]) == expected_code
        payload, receipt = _result_receipt(capsys)
        assert payload["dispatch"] == summary
        assert receipt["reason"] == reason
        assert receipt["dispatch_failed"] == failed
        assert receipt["exit"] == str(expected_code)
    assert main(["--report-dir", str(tmp_path)]) == 2
    _, receipt = _result_receipt(capsys)
    assert receipt["reason"] == "dispatch_not_requested"
    assert receipt["dispatch_failed"] == "unknown"


def test_runtime_result_receipt_keeps_rejection_stdout_and_no_send(tmp_path, capsys):
    with patch("scripts.consume_daily_briefing._read_gcs_object") as read, patch(
        "scripts.consume_daily_briefing.dispatch_runtime_digest",
    ) as dispatch:
        assert main(["--runtime-projection-gcs", "gs://private/runtime_daily/longbridge/paper/2026-09-28.json",
                     "--day", "2026-09-28", "--expected-target-key", "private-service|private-strategy|live",
                     "--dispatch"]) == 2
    payload, receipt = _result_receipt(capsys)
    assert payload == {"ok": False, "error": "runtime_projection_rejected", "reason": "expected_scope_not_paper"}
    assert receipt == {"branch": "runtime", "stage": "binding_validation", "reason": "expected_scope_not_paper",
                       "action": "none", "dispatch_failed": "unknown", "exit": "2"}
    read.assert_not_called()
    dispatch.assert_not_called()
    assert "private" not in str(receipt)
    path = _projection_file(tmp_path)
    payload = json.loads(path.read_text())
    payload["records"][0]["fills"]["count"] = 0
    path.write_text(json.dumps(payload))
    assert main(["--runtime-projection", str(path)]) == 2
    payload, receipt = _result_receipt(capsys)
    assert payload["reason"] == "fills_not_connected"
    assert receipt["stage"] == "input_validation"
    assert receipt["reason"] == "fills_not_connected"
    assert receipt["exit"] == "2"


def test_runtime_result_receipt_distinguishes_read_failure(tmp_path, capsys):
    path = tmp_path / "private-unreadable.json"
    assert main(["--runtime-projection", str(path)]) == 2
    payload, receipt = _result_receipt(capsys)
    assert payload == {"ok": False, "error": "runtime_projection_unreadable"}
    assert receipt["stage"] == "input_read"
    assert receipt["reason"] == "runtime_projection_unreadable"
    assert "private" not in str(receipt)


def test_runtime_dispatch_receipt_preserves_unknown_and_legacy_exit(tmp_path, capsys):
    path = _projection_file(tmp_path)
    args = ["--runtime-projection", str(path), "--day", "2026-09-28", "--expected-target-key",
            "lb-svc|rot|paper", "--dispatch"]
    for summary, code, reason, failed in [
        (_complete_dispatch_summary("runtime_digest", telegram_sent=True), 0, "dispatch_completed", "false"),
        ({"errors": ["telegram_missing_env"]}, 2, "telegram_missing_env", "true"),
        ({"errors": ["private\naccount=https://secret.invalid/token"]}, 2, "unknown", "true"),
        ({}, 0, "unknown", "unknown"),
        (None, 0, "unknown", "unknown"),
    ]:
        with patch("scripts.consume_daily_briefing.dispatch_runtime_digest", return_value=summary):
            assert main(args) == code
        payload, receipt = _result_receipt(capsys)
        assert payload["dispatch"] == summary
        assert receipt["reason"] == reason
        assert receipt["dispatch_failed"] == failed
        assert receipt["exit"] == str(code)
        assert "private" not in str(receipt) and "secret" not in str(receipt)
    assert main(["--runtime-projection", str(path)]) == 0
    _, receipt = _result_receipt(capsys)
    assert receipt["stage"] == "routing"
    assert receipt["reason"] == "dispatch_not_requested"
    assert receipt["dispatch_failed"] == "unknown"


def test_runtime_preparation_unknown_reason_is_not_logged(tmp_path, capsys):
    path = _projection_file(tmp_path)
    private_reason = "gs://private/object\naccount=private-token"
    with patch("scripts.consume_daily_briefing.prepare_runtime_digest", return_value={"ok": False, "reason": private_reason}):
        assert main(["--runtime-projection", str(path)]) == 2
    payload, receipt = _result_receipt(capsys)
    assert payload["reason"] == private_reason
    assert receipt["reason"] == "unknown"
    assert receipt["dispatch_failed"] == "unknown"
    assert "private" not in str(receipt)


def _complete_dispatch_summary(action, **overrides):
    summary = {"action": action, "telegram_sent": False, "github_issue": None,
               "errors": [], "skipped": []}
    if action == "runtime_digest":
        summary.update(business_date="2026-09-28", event_id="synthetic-event")
    else:
        summary.update(optimization_watch=None, operational_fallback_sent=False)
    summary.update(overrides)
    return summary


def test_dispatch_receipt_evidence_requires_complete_typed_result():
    from scripts.consume_daily_briefing import _dispatch_failure_evidence

    assert _dispatch_failure_evidence({"errors": []}) == "unknown"
    for action in ["quiet", "github_issue", "telegram", "runtime_digest"]:
        summary = _complete_dispatch_summary(action)
        assert _dispatch_failure_evidence(summary) == "false"
        for field in summary:
            partial = dict(summary)
            del partial[field]
            assert _dispatch_failure_evidence(partial) == "unknown", (action, field)
        for overrides in [
            {"telegram_sent": 0}, {"errors": ()}, {"errors": ""}, {"skipped": ()},
            {"skipped": ""}, {"action": "private\naccount=secret"}, {"github_issue": 0},
        ]:
            assert _dispatch_failure_evidence({**summary, **overrides}) == "unknown", (action, overrides)
        assert _dispatch_failure_evidence({**summary, "errors": ["private-error"]}) == "true"
    for overrides in [{"optimization_watch": {}}, {"optimization_watch": {"errors": False}},
                      {"operational_fallback_sent": 0}]:
        assert _dispatch_failure_evidence(_complete_dispatch_summary("telegram", **overrides)) == "unknown"
    assert _dispatch_failure_evidence({"errors": ["private-error"]}) == "true"
    assert _dispatch_failure_evidence(None) == "unknown"


def test_partial_runtime_dispatch_receipt_keeps_exit_and_stdout(tmp_path, capsys):
    path = _projection_file(tmp_path)
    for summary, failed in [
        ({"errors": []}, "unknown"),
        (_complete_dispatch_summary("runtime_digest"), "false"),
        (_complete_dispatch_summary("runtime_digest", telegram_sent=0), "unknown"),
        (_complete_dispatch_summary("runtime_digest", errors=()), "unknown"),
        (_complete_dispatch_summary("runtime_digest", errors=""), "unknown"),
    ]:
        with patch("scripts.consume_daily_briefing.dispatch_runtime_digest", return_value=summary):
            assert main(["--runtime-projection", str(path), "--day", "2026-09-28",
                         "--expected-target-key", "lb-svc|rot|paper", "--dispatch"]) == 0
        payload, receipt = _result_receipt(capsys)
        assert payload["dispatch"] == json.loads(json.dumps(summary))
        assert receipt["dispatch_failed"] == failed
        assert receipt["exit"] == "0"


def test_quiet_dispatch_receipt_and_no_dispatch_are_distinct(tmp_path, capsys):
    (tmp_path / "us_equity.json").write_text(json.dumps({
        "domain": "us_equity", "ok": True, "strategies": [{"status": "healthy", "overall_score": 80}],
    }))
    with patch("scripts.consume_daily_briefing.dispatch_briefing_result",
               return_value=_complete_dispatch_summary("quiet")):
        assert main(["--report-dir", str(tmp_path), "--dispatch"]) == 0
    _, receipt = _result_receipt(capsys)
    assert receipt["reason"] == "quiet" and receipt["dispatch_failed"] == "false"
    assert main(["--report-dir", str(tmp_path)]) == 0
    payload, receipt = _result_receipt(capsys)
    assert "dispatch" not in payload
    assert receipt["reason"] == "quiet" and receipt["dispatch_failed"] == "unknown"


def test_dispatch_receipt_malformed_error_shapes_stay_unknown(tmp_path, capsys):
    from scripts.consume_daily_briefing import _dispatch_failure_evidence

    path = _projection_file(tmp_path)
    for errors in [{"bad": "synthetic"}, "synthetic", [False], [0], [{"bad": "synthetic"}]]:
        summary = _complete_dispatch_summary("runtime_digest", errors=errors)
        assert _dispatch_failure_evidence(summary) == "unknown"
        with patch("scripts.consume_daily_briefing.dispatch_runtime_digest", return_value=summary):
            assert main(["--runtime-projection", str(path), "--day", "2026-09-28",
                         "--expected-target-key", "lb-svc|rot|paper", "--dispatch"]) == 2
        payload, receipt = _result_receipt(capsys)
        assert payload["dispatch"] == summary
        assert receipt["dispatch_failed"] == "unknown" and receipt["exit"] == "2"
        assert "synthetic" not in str(receipt)
    assert _dispatch_failure_evidence({"errors": ["synthetic"]}) == "true"
    assert _dispatch_failure_evidence({"optimization_watch": {"errors": 1}}) == "true"
