from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import urllib.error
from unittest.mock import patch

import pytest

from client.gateway_client import AiResult
from scripts.consume_daily_briefing import main
from service.briefing_consumer import consume_briefing_dir, summarize_briefing


NOW = datetime(2026, 9, 9, 22, 30, tzinfo=timezone.utc)
ENV = {
    "CODEX_AUDIT_SERVICE_URL": "https://synthetic.invalid",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://synthetic.invalid/oidc?request=synthetic",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
    "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
}


@pytest.fixture
def report(tmp_path):
    payload = {
        "domain": "us_equity", "ok": True, "data_status": "ready",
        "as_of": "2026-09-09T22:00:00+00:00",
        "strategies": [{"status": "critical", "as_of": "2026-09-08"}],
    }
    (tmp_path / "us_equity.json").write_text(json.dumps(payload))
    return tmp_path, payload


@pytest.fixture(autouse=True)
def isolated_environment():
    with patch.dict(os.environ, ENV, clear=True), patch(
        "service.briefing_consumer._summary_now", return_value=NOW,
    ):
        yield


class Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.body


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_cli_real_sdk_oidc_health_submit_poll_and_summary(report, capsys, provider):
    path, _ = report
    route = {"provider": provider, "research_stage": "research_summary",
             "model": "gpt-5.6-luna" if provider == "codex" else "cursor-grok-4.6-low",
             "reasoning_effort": "low", "job_id": "original-summary-job"}
    completed = {**route, "status": "succeeded", "output": "Synthetic advisory only."}
    capability = "codex_research_routing" if provider == "codex" else "subscription_research_routing"
    replies = [Response({"value": "synthetic-oidc"}), Response({capability: "v1"}),
               Response({**route, "status": "queued"}), Response({"value": "synthetic-oidc"}), Response(completed)]
    with patch.dict(os.environ, {"AI_GATEWAY_RESEARCH_PROVIDERS": provider}), patch(
        "client.gateway_client.urllib.request.urlopen", side_effect=replies,
    ) as http, patch("client.gateway_client.time.sleep"), patch(
        "client.gateway_client.AiGatewayClient.analyze", side_effect=AssertionError("paid fallback forbidden"),
    ), patch("scripts.consume_daily_briefing.dispatch_briefing_result", return_value={"errors": []}):
        assert main(["--report-dir", str(path), "--dispatch", "--ai-summary"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "github_issue"
    summary = result["ai_summary"]
    assert summary["status"] == "available" and summary["advisory_only"]
    assert summary["provider"] == provider and summary["model"] == route["model"]
    assert summary["input"]["missing_domains"] == ["cn_equity", "hk_equity", "crypto"]
    assert len(http.call_args_list) == 5
    submit = http.call_args_list[2].args[0]
    payload = json.loads(submit.data)
    assert payload["allowed_providers"] == [provider]
    assert payload["research_stage"] == "research_summary" and payload["model"] == ""
    assert payload["source_repository"] == "QuantStrategyLab/AIAuditBridge"
    assert http.call_args_list[4].args[0].full_url.endswith("/original-summary-job")


def test_summary_requires_source_repository_before_submit(report):
    path, _ = report
    with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}), patch("client.gateway_client.AiGatewayClient.execute") as execute:
        assert summarize_briefing(consume_briefing_dir(path))["reason"] == "source_repository_required"
    execute.assert_not_called()


@pytest.mark.parametrize("kind", ["old_service", "quota", "failed", "wrong_route", "wrong_job"])
def test_real_sdk_unavailable_or_deferred_does_not_fallback_or_leak(report, kind):
    path, _ = report
    route = {"provider": "cursor", "research_stage": "research_summary", "model": "cursor-grok-4.6-low",
             "reasoning_effort": "low", "job_id": "original-summary-job"}
    replies = [Response({"value": "synthetic-oidc"})]
    if kind == "old_service":
        replies += [Response({"status": "ok"})]
    else:
        replies += [Response({"subscription_research_routing": "v1"})]
        if kind == "quota":
            replies += [urllib.error.HTTPError("https://synthetic.invalid", 429, "private-marker", {},
                io.BytesIO(json.dumps({"status": "deferred", "retry_at": 9000, "error": "private-marker"}).encode()))]
        else:
            terminal = {**route, "status": "succeeded", "output": "private-marker"}
            if kind == "failed":
                terminal.update(status="failed", error="private-marker", failure_category="private-marker")
            elif kind == "wrong_route":
                terminal["model"] = "unapproved-alias"
            else:
                terminal["job_id"] = "another-job"
            replies += [Response({**route, "status": "queued"}), Response({"value": "synthetic-oidc"}), Response(terminal)]
    with patch.dict(os.environ, {"AI_GATEWAY_RESEARCH_PROVIDERS": "cursor"}), patch(
        "client.gateway_client.urllib.request.urlopen", side_effect=replies,
    ) as http, patch("client.gateway_client.time.sleep"), patch(
        "client.gateway_client.AiGatewayClient.analyze", side_effect=AssertionError("paid fallback forbidden"),
    ):
        result = summarize_briefing(consume_briefing_dir(path))
    assert result["status"] == ("deferred" if kind == "quota" else "unavailable")
    assert "private-marker" not in repr(result)
    assert "text" not in result and http.call_count == len(replies)
    if kind == "quota":
        assert result["retry_at"] == 9000


@pytest.mark.parametrize("field,value", [
    ("as_of", None), ("as_of", "invalid"), ("as_of", "2026-09-09T22:00:00"),
    ("as_of", "9999-12-31T23:59:59+00:00"), ("as_of", "2026-09-08T00:00:00+00:00"),
    ("data_status", "unavailable"), ("ok", False), ("strategies", []),
    ("strategies", [{"status": "critical", "as_of": None}]),
    ("strategies", [{"status": "critical", "as_of": "9999-12-31"}]),
    ("strategies", [{"status": "healthy", "as_of": "2026-09-01"}]),
])
def test_bad_or_stale_input_never_calls_model_and_keeps_rule_action(report, field, value):
    path, payload = report
    payload[field] = value
    (path / "us_equity.json").write_text(json.dumps(payload))
    result = consume_briefing_dir(path)
    before = result.to_dict()
    with patch("client.gateway_client.AiGatewayClient.execute") as execute:
        assert summarize_briefing(result)["status"] == "unavailable"
    execute.assert_not_called()
    assert result.to_dict() == before


def test_snapshot_is_not_reread_after_rule_dispatch(report):
    path, _ = report
    result = consume_briefing_dir(path)
    (path / "us_equity.json").write_text("invalid replacement")
    with patch("client.gateway_client.AiGatewayClient.execute", return_value=AiResult.unavailable("codex", "synthetic")) as execute:
        summarize_briefing(result)
    assert execute.call_count == 1
    assert "2026-09-08" in execute.call_args.args[0]


@pytest.mark.parametrize("content", ["invalid JSON", "[]", '{"domain":"untrusted-free-text"}'])
def test_bad_report_is_controlled_unavailable_not_exception(tmp_path, content):
    (tmp_path / "us_equity.json").write_text(content)
    with patch("client.gateway_client.AiGatewayClient.execute") as execute:
        result = summarize_briefing(consume_briefing_dir(tmp_path))
    assert result["status"] == "unavailable"
    execute.assert_not_called()


def test_empty_input_and_missing_configuration_do_not_call_model(report):
    path, _ = report
    with patch("client.gateway_client.AiGatewayClient.execute") as execute:
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": ""}):
            assert summarize_briefing(consume_briefing_dir(path))["reason"] == "ai_gateway_not_configured"
        (path / "us_equity.json").unlink()
        assert summarize_briefing(consume_briefing_dir(path))["reason"] == "briefing_input_unavailable"
    execute.assert_not_called()


def test_cli_default_and_dry_run_make_zero_http_calls(report, capsys):
    path, _ = report
    with patch("client.gateway_client.urllib.request.urlopen", side_effect=AssertionError("network forbidden")) as http:
        assert main(["--report-dir", str(path)]) == 2
        assert "ai_summary" not in json.loads(capsys.readouterr().out)
        assert main(["--report-dir", str(path), "--ai-summary", "--dry-run"]) == 2
        assert json.loads(capsys.readouterr().out)["ai_summary"]["status"] == "dry_run"
    http.assert_not_called()


@pytest.mark.parametrize("change", [
    {"provider": "gpt"}, {"model": "different-model"}, {"note": "private-marker"},
    {"raw": {"status": "succeeded", "output": "private-marker"}},
])
def test_malformed_completion_cannot_become_advisory(report, change):
    path, _ = report
    raw = {"status": "succeeded", "provider": "codex", "model": "gpt-5.6-luna",
           "research_stage": "research_summary", "reasoning_effort": "low", "output": "private-marker"}
    response = AiResult(provider="codex", model="gpt-5.6-luna", success=True, output="private-marker", raw=raw)
    with patch("client.gateway_client.AiGatewayClient.execute", return_value=replace(response, **change)):
        result = summarize_briefing(consume_briefing_dir(path))
    assert result["status"] == "unavailable" and "private-marker" not in repr(result)


def test_summary_failure_does_not_change_successful_rule_dispatch_exit(report, capsys):
    path, _ = report
    with patch("client.gateway_client.AiGatewayClient.execute", side_effect=RuntimeError("private-marker")), patch(
        "scripts.consume_daily_briefing.dispatch_briefing_result", return_value={"errors": []},
    ):
        assert main(["--report-dir", str(path), "--dispatch", "--ai-summary"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "github_issue" and result["ai_summary"]["status"] == "unavailable"
    assert "private-marker" not in repr(result)


@pytest.mark.parametrize("enabled", ["", "false", "true"])
def test_existing_pipeline_only_opts_in_explicitly(tmp_path, enabled):
    root = tmp_path / "monitor"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "source_telegram_env.sh").write_text(":\n")
    (scripts / "daily_briefing.sh").write_text(":\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python3"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_ARGS"\n')
    python.chmod(0o755)
    output = tmp_path / "args"
    pipeline = Path(__file__).resolve().parents[1] / "ops/quant-monitor/scripts/daily_briefing_pipeline.sh"
    env = {"PATH": f"{fake_bin}:{os.defpath}", "QUANT_MONITOR_ROOT": str(root),
           "AIAUDIT_BRIDGE_ROOT": str(tmp_path), "TEST_ARGS": str(output), "QUANT_MONITOR_AI_SUMMARY": enabled}
    run = subprocess.run(["bash", str(pipeline)], env=env, capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    args = output.read_text().splitlines()
    assert "--dispatch" in args
    assert ("--ai-summary" in args) == (enabled == "true")
