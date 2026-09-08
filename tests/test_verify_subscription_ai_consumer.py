from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_manual_check_is_separate_from_monthly_audit() -> None:
    text = (ROOT / ".github/workflows/codex_audit.yml").read_text()
    assert "synthetic_sdk_check:" in text
    assert "'codex-audit-synthetic-sdk'" in text
    assert "cancel-in-progress: ${{ inputs.synthetic_sdk_check != true }}" in text
    check = text.split("  synthetic-sdk-check:", 1)[1]
    assert "github.event_name == 'workflow_dispatch'" in check
    assert "inputs.synthetic_sdk_check == true" in check
    assert "github.ref == 'refs/heads/main'" in check
    assert "python scripts/verify_subscription_ai_consumer.py" in check
    assert "permission-contents: write" not in check
    assert "API_KEY" not in check
    assert "create-github-app-token" not in check
    assert "run_monthly_codex_audit.py" not in check


def _environment(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith(("QSP_", "AI_GATEWAY_", "CODEX_AUDIT_", "ACTIONS_ID_TOKEN_")) or "API_KEY" in key:
            monkeypatch.delenv(key)
    for key, value in {
        "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "AI_GATEWAY_SOURCE_REPO": "QuantStrategyLab/AIAuditBridge",
        "CODEX_AUDIT_SERVICE_URL": "https://gateway.invalid",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid/token?api-version=2",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc-request",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("key,value", [
    ("GITHUB_REF", "refs/heads/unapproved"),
    ("GITHUB_EVENT_NAME", "push"),
    ("GITHUB_REPOSITORY", "example/unapproved"),
    ("AI_GATEWAY_SOURCE_REPO", "example/unapproved"),
    ("CODEX_AUDIT_SERVICE_URL", "http://gateway.invalid"),
    ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
    ("OPENAI_API_KEY", "synthetic-forbidden-key"),
])
def test_unapproved_environment_stops_before_consumer(monkeypatch, key, value) -> None:
    from scripts import verify_subscription_ai_consumer as check
    _environment(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="verification environment rejected"):
        check.validate_environment()


@pytest.mark.parametrize("output,expected", [
    ({"verdict": "data_insufficient", "confidence": 0.2, "summary": "Synthetic data has no market evidence."}, True),
    ({}, False),
    ({"verdict": "data_insufficient", "confidence": "low", "summary": "Synthetic"}, True),
    ({"verdict": "data_insufficient", "confidence": None, "summary": "Synthetic"}, True),
    ({"verdict": "agree", "confidence": "nan", "summary": "Synthetic"}, True),
    ({"verdict": "agree", "confidence": "inf", "summary": "Synthetic"}, True),
])
def test_installed_consumer_uses_one_codex_job(monkeypatch, output, expected) -> None:
    from ai_gateway_client import gateway_client
    from quant_strategy_plugins import ai_audit

    from scripts import verify_subscription_ai_consumer as check

    _environment(monkeypatch)
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _: None)
    calls = []

    def request(req, **_kwargs):
        if req.full_url.startswith("https://oidc.invalid/"):
            return io.BytesIO(json.dumps({"value": "synthetic-oidc"}).encode())
        calls.append((req.get_method(), req.full_url))
        if req.get_method() == "POST":
            assert req.full_url == "https://gateway.invalid/v1/ai/execute/jobs"
            body = json.loads(req.data)
            assert body["mode"] == "review_only"
            assert body["model"] == "gpt-6-astra"
            assert body["timeout_seconds"] == 120
            assert body["source_repository"] == "QuantStrategyLab/AIAuditBridge"
            assert "synthetic" in body["prompt"].lower()
            response = {"job_id": "synthetic-job", "status": "queued"}
        else:
            assert req.full_url == "https://gateway.invalid/v1/ai/execute/jobs/synthetic-job"
            response = {"status": "succeeded", "output": json.dumps(output)}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(gateway_client.urllib.request, "urlopen", request)
    monkeypatch.setattr(ai_audit, "_report_shadow_disagreement", lambda **_: pytest.fail("feedback forbidden"))
    report = check.run_check()
    assert report["passed"] is expected
    assert report["confidence_available"] is isinstance(output.get("confidence"), (int, float))
    assert report["financial_claims_verified"] is False
    assert report["learning_only"] is True
    assert report["live_ready"] is False
    assert report["no_order"] is True
    assert [method for method, _url in calls] == ["POST", "GET"]
    assert "summary" not in report
    assert "output" not in report
    assert "token" not in json.dumps(report)


def test_failure_output_is_sanitized(monkeypatch, capsys) -> None:
    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.setattr(check, "run_check", lambda: (_ for _ in ()).throw(RuntimeError("synthetic-private-detail")))
    assert check.main() == 1
    result = capsys.readouterr()
    assert "synthetic-private-detail" not in result.out + result.err
    assert json.loads(result.out)["failure_category"] == "verification_failed"
