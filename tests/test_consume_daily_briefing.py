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


def test_domain_quiet_route_is_unchanged(tmp_path, capsys) -> None:
    (tmp_path / "us_equity.json").write_text(json.dumps({
        "domain": "us_equity", "ok": True, "data_status": "ready", "as_of": "2026-09-28T08:00:00+00:00",
        "strategies": [{"strategy_profile": "rot", "status": "healthy", "overall_score": 80, "as_of": "2026-09-28"}],
    }), encoding="utf-8")
    assert main(["--report-dir", str(tmp_path)]) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["action"] == "quiet"
    assert "runtime_digest" not in captured
