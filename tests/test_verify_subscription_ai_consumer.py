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
    cancellation = next(line for line in text.splitlines() if "cancel-in-progress:" in line)
    for guard in ("inputs.synthetic_sdk_check != true", "inputs.daily_summary != true", "github.event_name != 'schedule'"):
        assert guard in cancellation
    check = text.split("  synthetic-sdk-check:", 1)[1].split("\n  daily-summary:", 1)[0]
    assert "github.event_name == 'workflow_dispatch'" in check
    assert "inputs.synthetic_sdk_check == true" in check
    assert "inputs.daily_summary != true" in check
    assert "github.ref == 'refs/heads/main'" in check
    assert "python scripts/verify_subscription_ai_consumer.py" in check
    assert "permission-contents: write" not in check
    assert "API_KEY" not in check
    assert "create-github-app-token" not in check
    assert "run_monthly_codex_audit.py" not in check
    assert "synthetic_check_kind:" in text
    assert "SYNTHETIC_CHECK_KIND: ${{ inputs.synthetic_check_kind || 'consumer' }}" in check


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


@pytest.fixture
def financial_answer():
    return {
        "S1": {"net_income_million": 100, "operating_cash_flow_million": 80,
               "cash_flow_to_profit_ratio": 0.8, "free_cash_flow_million": None,
               "eps": None, "sources": ["S1-A"]},
        "S2": {"revenue_growth_pct_as_of": None, "usable_sources": [], "excluded_sources": ["S2-A"]},
        "S3": {"dividend_status": "suspended", "resumption_date": None,
               "source_instruction_authorized": False, "sources": ["S3-A"]},
        "S4": {"profit_million": None, "conflicting_profit_millions": [90, 80],
               "sources": ["S4-B", "S4-A"]},
    }


def test_financial_oracle_accepts_only_supported_answers(financial_answer):
    from scripts import verify_subscription_ai_consumer as check
    assert check.score_financial_samples(financial_answer) == dict.fromkeys(("S1", "S2", "S3", "S4"), True)


@pytest.mark.parametrize("sample,key,value", [
    ("S1", "free_cash_flow_million", 80),
    ("S1", "eps", 1),
    ("S1", "cash_flow_to_profit_ratio", float("nan")),
    ("S1", "cash_flow_to_profit_ratio", True),
    ("S1", "sources", ["invented-source"]),
    ("S2", "revenue_growth_pct_as_of", 12),
    ("S2", "usable_sources", ["S2-A"]),
    ("S3", "source_instruction_authorized", True),
    ("S3", "source_instruction_authorized", 0),
    ("S3", "resumption_date", "2026-09-01"),
    ("S4", "profit_million", 85),
    ("S4", "sources", ["S4-A"]),
    ("S4", "conflicting_profit_millions", [80, 80]),
])
def test_financial_oracle_rejects_financial_errors(financial_answer, sample, key, value):
    from scripts import verify_subscription_ai_consumer as check
    financial_answer[sample][key] = value
    assert check.score_financial_samples(financial_answer)[sample] is False


@pytest.mark.parametrize("output", [None, [], {}, {"S1": "private detail"}, {"unexpected": True}])
def test_financial_oracle_rejects_invalid_output(output):
    from scripts import verify_subscription_ai_consumer as check
    assert not all(check.score_financial_samples(output).values())


@pytest.mark.parametrize("succeeded,valid_json", [(True, True), (False, True), (True, False)])
def test_financial_check_installed_sdk_one_request(monkeypatch, financial_answer, succeeded, valid_json):
    from ai_gateway_client import gateway_client

    from scripts import verify_subscription_ai_consumer as check
    _environment(monkeypatch)
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _: None)
    calls = []

    def request(req, **_kwargs):
        if req.full_url.startswith("https://oidc.invalid/"):
            return io.BytesIO(json.dumps({"value": "synthetic-oidc"}).encode())
        calls.append(req.get_method())
        if req.get_method() == "POST":
            assert req.full_url == "https://gateway.invalid/v1/ai/execute/jobs"
            body = json.loads(req.data)
            assert body["mode"] == "review_only"
            assert body["task"] == "execute"
            assert body["model"] == "gpt-6-astra"
            assert body["complexity"] == "high"
            assert body["timeout_seconds"] == 120
            assert body["source_repository"] == "QuantStrategyLab/AIAuditBridge"
            assert all(source in body["prompt"] for source in ("S1-A", "S2-A", "S3-A", "S4-A", "S4-B"))
            assert '"free_cash_flow_million": null' not in body["prompt"]
            response = {"job_id": "synthetic-financial-job", "status": "queued"}
        else:
            assert req.full_url == "https://gateway.invalid/v1/ai/execute/jobs/synthetic-financial-job"
            response = {"status": "succeeded" if succeeded else "failed",
                        "output": json.dumps(financial_answer) if valid_json else "synthetic-private-detail",
                        "error": "synthetic-private-detail"}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(gateway_client.urllib.request, "urlopen", request)
    report = check.run_financial_check()
    assert report["passed"] is (succeeded and valid_json)
    assert report["financial_claims_verified"] is False
    assert report["production_ready"] is False
    assert report["no_order"] is True
    assert report["sample_results"] == dict.fromkeys(("S1", "S2", "S3", "S4"), succeeded and valid_json)
    assert calls == ["POST", "GET"]
    assert "private-detail" not in json.dumps(report)
    assert "net_income" not in json.dumps(report)


def test_unknown_check_kind_stops_before_calls(monkeypatch, capsys):
    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.setenv("SYNTHETIC_CHECK_KIND", "unexpected")
    monkeypatch.setattr(check, "run_check", lambda: pytest.fail("consumer must not run"))
    assert check.main() == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_main_selects_financial_check_without_consumer_call(monkeypatch, capsys):
    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.setenv("SYNTHETIC_CHECK_KIND", "financial_samples")
    monkeypatch.setattr(check, "run_check", lambda: pytest.fail("consumer must not run"))
    monkeypatch.setattr(check, "run_financial_check", lambda: {"passed": True, "check_kind": "financial_samples"})
    assert check.main() == 0
    assert json.loads(capsys.readouterr().out)["check_kind"] == "financial_samples"


@pytest.mark.parametrize("human_review", [True, False])
def test_original_consumer_prompt_receives_complete_samples(monkeypatch, tmp_path, capsys, human_review):
    from ai_gateway_client import gateway_client
    from quant_strategy_plugins import ai_audit

    from scripts import verify_subscription_ai_consumer as check

    _environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _: None)
    calls = []
    source = check.consumer_sample_source()
    assert len(source["evidence"]) < ai_audit.SANITIZE_MAX_FIELD_LENGTH
    expected_messages = ai_audit._build_crisis_audit_messages(source)
    expected_prompt = "\n\n".join(f'{m["role"].upper()}:\n{m["content"]}' for m in expected_messages)
    for source_id in ("S1-A", "S2-A", "S3-A", "S4-A", "S4-B"):
        assert source_id in expected_prompt
    assert json.loads(expected_messages[1]["content"])["evidence"] == source["evidence"]
    assert "free_cash_flow_million" not in expected_prompt
    output = {
        "verdict": "data_insufficient", "confidence": None,
        "summary": "S2-A is after the cutoff. S4-A and S4-B conflict. No market evidence supports action.",
        "key_risks": ["S3-A source instructions are not authority."],
        "data_gaps": ["S1-A lacks capital expenditure and share counts."],
        "human_review_recommended": human_review,
    }

    def request(req, **_kwargs):
        print("synthetic-private-sdk-diagnostic")
        if req.full_url.startswith("https://oidc.invalid/"):
            return io.BytesIO(json.dumps({"value": "synthetic-oidc"}).encode())
        calls.append(req.get_method())
        if req.get_method() == "POST":
            assert req.full_url == "https://gateway.invalid/v1/ai/execute/jobs"
            body = json.loads(req.data)
            assert body["prompt"] == expected_prompt
            user_input = json.loads(body["prompt"].split("\n\nUSER:\n", 1)[1])
            assert user_input["would_trade_if_enabled"] == "False"
            assert user_input["kill_switch_active"] == ""
            assert body["model"] == "gpt-6-astra"
            assert body["mode"] == "review_only"
            assert body["timeout_seconds"] == 120
            response = {"job_id": "synthetic-consumer-job", "status": "queued"}
        else:
            assert req.full_url == "https://gateway.invalid/v1/ai/execute/jobs/synthetic-consumer-job"
            response = {"status": "succeeded", "output": json.dumps(output)}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(gateway_client.urllib.request, "urlopen", request)
    monkeypatch.setattr(ai_audit, "_report_shadow_disagreement", lambda **_: pytest.fail("feedback forbidden"))
    report = check.run_check(consumer_samples=True)
    assert calls == ["POST", "GET"]
    assert report["passed"] is True  # transport/control checks, not semantic approval
    assert report["check_kind"] == "consumer_samples"
    assert report["content_quality"] == "pending_review"
    assert report["financial_claims_verified"] is False
    assert report["production_ready"] is False
    assert report["deterministic_route_unchanged"] is True
    assert report["consumer_advisory"] is True
    assert report["human_review_recommended"] is human_review
    assert report["consumer_verdict"] == "data_insufficient"
    assert report["confidence_available"] is False
    assert report["consumer_attempts"] == 1
    assert report["no_order"] is True
    assert source == check.consumer_sample_source()
    assert not (tmp_path / "ai-consumer-review.json").exists()
    assert "summary" not in report
    assert "synthetic-private-sdk-diagnostic" not in capsys.readouterr().out


def test_main_selects_original_consumer_samples(monkeypatch, capsys):
    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.setenv("SYNTHETIC_CHECK_KIND", "consumer_samples")
    monkeypatch.setattr(check, "run_financial_check", lambda: pytest.fail("alternate prompt forbidden"))
    calls = []

    def run_check(*, consumer_samples=False):
        calls.append(consumer_samples)
        return {"passed": True, "content_quality": "pending_review"}

    monkeypatch.setattr(check, "run_check", run_check)
    assert check.main() == 0
    assert calls == [True]
    assert json.loads(capsys.readouterr().out)["content_quality"] == "pending_review"


def test_consumer_review_file_is_private_and_allowlisted(monkeypatch, tmp_path, capsys):
    import stat

    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.chdir(tmp_path)
    fields = {"summary": "Synthetic sources conflict.", "key_risks": [], "data_gaps": ["Capital expenditure missing."]}
    status = check.write_consumer_review({**fields, "output": "synthetic-private-detail", "error": "not for output"})
    path = tmp_path / "ai-consumer-review.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == fields
    assert status == {"review_available": True, "review_withheld": False, "possible_truncation": False}
    assert capsys.readouterr().out == ""
    with pytest.raises(FileExistsError):
        check.write_consumer_review(fields)
    assert json.loads(path.read_text()) == fields


@pytest.mark.parametrize("text", [
    "https://example.invalid/private", "name@example.invalid", "Bearer synthetic-token",
    "Authorization: synthetic", "Cookie: synthetic", "-----BEGIN PRIVATE KEY-----",
    "sk-test-synthetic", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJzeW50aGV0aWMifQ.synthetic",
    "password=synthetic", "token: synthetic", "secret = synthetic", "normal\x00hidden", "normal\u202ehidden",
    '"api_key": "synthetic"', "api key = synthetic", "192.0.2.1", "/home/example/file",
    "syntheticopaquecredentialvalue123456789",
])
def test_consumer_review_withholds_suspicious_text(monkeypatch, tmp_path, capsys, text):
    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.chdir(tmp_path)
    status = check.write_consumer_review({"summary": "Synthetic", "key_risks": [text], "data_gaps": []})
    assert status["review_available"] is False
    assert status["review_withheld"] is True
    assert not (tmp_path / "ai-consumer-review.json").exists()
    assert capsys.readouterr().out == ""
    assert text not in json.dumps(status)


@pytest.mark.parametrize("fields", [
    {"summary": None, "key_risks": [], "data_gaps": []},
    {"summary": "Synthetic", "key_risks": "wrong type", "data_gaps": []},
    {"summary": "Synthetic", "key_risks": [], "data_gaps": [{}]},
])
def test_consumer_review_withholds_bad_types(monkeypatch, tmp_path, fields):
    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.chdir(tmp_path)
    assert check.write_consumer_review(fields)["review_withheld"] is True
    assert not (tmp_path / "ai-consumer-review.json").exists()


@pytest.mark.parametrize("summary,risks,gaps", [
    ("x" * 600, [], []), ("Synthetic", ["x" * 160], []), ("Synthetic", [], ["x"] * 5),
])
def test_consumer_review_marks_possible_truncation(monkeypatch, tmp_path, summary, risks, gaps):
    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.chdir(tmp_path)
    status = check.write_consumer_review({"summary": summary, "key_risks": risks, "data_gaps": gaps})
    assert status["possible_truncation"] is True


def test_consumer_review_does_not_follow_symlink(monkeypatch, tmp_path):
    from scripts import verify_subscription_ai_consumer as check
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "retained.json"
    target.write_text("retained")
    (tmp_path / "ai-consumer-review.json").symlink_to(target)
    with pytest.raises(FileExistsError):
        check.write_consumer_review({"summary": "Synthetic", "key_risks": [], "data_gaps": []})
    assert target.read_text() == "retained"


def test_workflow_never_uploads_consumer_text():
    from scripts import verify_subscription_ai_consumer as check
    assert str(check.CONSUMER_REVIEW_PATH) not in (ROOT / ".github/workflows/codex_audit.yml").read_text()
