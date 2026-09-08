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
