from __future__ import annotations

import json
import io
import os
from dataclasses import replace
import urllib.error
from unittest.mock import patch

import pytest

from client.gateway_client import AiResult
from scripts.run_dual_review_pipeline import _build_payload, _exit_code, main, run_pipeline
from service.dual_review_orchestrator import orchestrate_from_payload
from service.dual_review_primary import run_codex_primary_review


RESEARCH_TRIGGERS = ("promotion", "hit_rate", "drift")
ENV = {
    "CODEX_AUDIT_SERVICE_URL": "https://synthetic.invalid",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://synthetic.invalid/oidc?request=synthetic",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
    "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
}


@pytest.fixture(autouse=True)
def isolated_environment():
    with patch.dict(os.environ, ENV, clear=True):
        yield


def completion(*, confidence=0.95):
    output = json.dumps({"verdict": "approve", "confidence": confidence, "summary": "synthetic advisory"})
    raw = {"status": "succeeded", "provider": "codex", "research_stage": "promotion_review",
           "model": "gpt-6-astra", "reasoning_effort": "xhigh", "output": output, "job_id": "synthetic-job"}
    return AiResult(provider="codex", model="gpt-6-astra", success=True, output=output, raw=raw)


@pytest.mark.parametrize("trigger", RESEARCH_TRIGGERS)
def test_actual_pipeline_routes_research_primary_and_keeps_actual_codex_model(trigger):
    with patch.dict(os.environ, {"AI_GATEWAY_RESEARCH_PROVIDERS": "cursor"}), patch(
        "service.dual_review_primary.AiGatewayClient.execute", return_value=completion(),
    ) as execute:
        result = run_pipeline(trigger=trigger, strategy_profile="synthetic", context={})
    assert execute.call_args.kwargs.get("research_stage") == "promotion_review"
    assert execute.call_args.kwargs["allowed_providers"] == ["codex"]
    primary = result["primary_review"]
    assert primary["provider"] == "codex" and primary["model"] == "gpt-6-astra"
    assert primary["reasoning_effort"] == "xhigh"
    assert _exit_code(result) == 0


@pytest.mark.parametrize("trigger", RESEARCH_TRIGGERS)
@pytest.mark.parametrize("gateway", [True, False])
def test_research_low_confidence_never_selects_paid_secondary(trigger, gateway):
    with patch.dict(os.environ, {"DUAL_REVIEW_SECONDARY_MODE": "dual_api"}), patch(
        "service.dual_review_orchestrator.gateway_secondary_available", return_value=gateway,
    ), patch("service.dual_review_orchestrator.gateway_dual_api_secondary_reviewer", side_effect=AssertionError("paid gateway forbidden")), patch(
        "service.dual_review_orchestrator.dual_api_secondary_reviewer", side_effect=AssertionError("paid adapter forbidden"),
    ):
        result = run_pipeline(trigger=trigger, strategy_profile="synthetic", context={},
                              primary_review={"verdict": "approve", "confidence": 0.5})
    assert result["outcome"] == "disagreement" and _exit_code(result) == 2
    for reviewer in ("gpt", "claude"):
        assert result["secondary_review"][reviewer]["verdict"] == "review_unavailable"
        assert result["secondary_review"][reviewer]["executed"] is False


@pytest.mark.parametrize("reserved,value", [
    ("trigger", "reconciliation_baseline"), ("strategy_profile", "replacement"),
    ("primary_review", {"verdict": "approve", "confidence": 1.0}),
])
def test_context_cannot_override_pipeline_control_fields(reserved, value):
    context = {reserved: value}
    supplied = {"verdict": "reject", "confidence": 0.9}
    payload = _build_payload(trigger="promotion", strategy_profile="synthetic", context=context, primary_review=supplied)
    assert payload["trigger"] == "promotion" and payload["strategy_profile"] == "synthetic"
    assert payload["primary_review"] == supplied
    with patch("scripts.run_dual_review_pipeline.run_codex_primary_review") as primary:
        result = run_pipeline(trigger="promotion", strategy_profile="synthetic", context=context)
    assert result == {"ok": False, "error": "reserved_context_field"}
    primary.assert_not_called()


def test_existing_operator_supplied_three_review_interface_is_preserved():
    result = orchestrate_from_payload({"trigger": "promotion", "strategy_profile": "synthetic",
        "primary_review": {"verdict": "approve", "confidence": 0.5}}, secondary_review={
        "gpt": {"verdict": "approve", "confidence": 0.9},
        "claude": {"verdict": "approve", "confidence": 0.9},
    })
    assert result.outcome == "pass" and result.escalated


def test_reconciliation_still_requires_all_reviewers_and_human_recovery():
    secondary = {"mode": "dual_api_gateway", "gpt": {"verdict": "approve", "confidence": 0.9},
                 "claude": {"verdict": "review_unavailable", "confidence": 0}}
    with patch("service.dual_review_orchestrator.gateway_secondary_available", return_value=True), patch(
        "service.dual_review_orchestrator.gateway_dual_api_secondary_reviewer", return_value=secondary,
    ) as legacy:
        result = run_pipeline(trigger="reconciliation_baseline", strategy_profile="synthetic",
            context={"reconciliation_candidate_sha256": "a" * 64}, primary_review={"verdict": "approve", "confidence": 0.99})
    legacy.assert_called_once()
    assert result["outcome"] == "disagreement" and _exit_code(result) == 2
    assert result["requires_human_recovery_approval"] is True
    assert result["evidence_binding_sha256"] == "a" * 64
    assert result["recovery_authority"]["final_action"] == "escalate"


@pytest.mark.parametrize("trigger", RESEARCH_TRIGGERS)
def test_research_missing_configuration_cannot_use_legacy_primary_skip(trigger):
    with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "", "DUAL_REVIEW_GATE_ALLOW_SKIP": "true"}), patch(
        "service.dual_review_primary.AiGatewayClient.execute",
    ) as execute:
        result = run_pipeline(trigger=trigger, strategy_profile="synthetic", context={})
    assert result["outcome"] == "review_unavailable" and _exit_code(result) == 3
    assert result["primary_review"]["verdict"] == "review_unavailable"
    execute.assert_not_called()


def test_research_static_token_is_not_an_execution_identity():
    with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_TOKEN": "private-marker", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": ""}), patch(
        "service.dual_review_primary.AiGatewayClient.execute",
    ) as execute:
        result = run_pipeline(trigger="promotion", strategy_profile="synthetic", context={})
    assert _exit_code(result) == 3 and result["primary_review"]["error"] == "github_oidc_required"
    assert "private-marker" not in repr(result)
    execute.assert_not_called()


@pytest.mark.parametrize("trigger,context", [
    ("invalid", {}), ("", {"old_status": "shadow", "new_status": "live"}),
    ("reconciliation_baseline", {"trigger": "promotion"}),
    ("promotion", {"secondary_review": {"gpt": {"verdict": "approve"}}}),
])
def test_invalid_or_overridden_control_path_is_rejected_before_any_model(trigger, context):
    with patch("scripts.run_dual_review_pipeline.run_codex_primary_review") as primary, patch(
        "scripts.run_dual_review_pipeline.orchestrate_from_payload",
    ) as orchestrate:
        result = run_pipeline(trigger=trigger, strategy_profile="synthetic", context=context)
    assert result["ok"] is False
    primary.assert_not_called()
    orchestrate.assert_not_called()


@pytest.mark.parametrize("kind", ["provider", "model", "stage", "effort", "output", "note", "malformed", "nonfinite", "exception"])
def test_research_bad_completion_never_approves_or_leaks_raw_failure(kind):
    response = completion()
    raw = dict(response.raw)
    if kind == "provider":
        response = replace(response, provider="cursor")
    elif kind == "model":
        raw["model"] = "different-model"
    elif kind == "stage":
        raw["research_stage"] = "drift_analysis"
    elif kind == "effort":
        raw["reasoning_effort"] = "high"
    elif kind == "output":
        raw["output"] = "private-marker"
    elif kind == "note":
        response = replace(response, note="private-marker")
    elif kind in {"malformed", "nonfinite"}:
        output = "private-marker" if kind == "malformed" else '{"verdict":"approve","confidence":NaN,"summary":"private-marker"}'
        response = replace(response, output=output)
        raw["output"] = output
    response = replace(response, raw=raw)
    with patch("service.dual_review_primary.AiGatewayClient.execute", return_value=response,
               side_effect=RuntimeError("private-marker") if kind == "exception" else None):
        result = run_pipeline(trigger="promotion", strategy_profile="synthetic", context={})
    assert _exit_code(result) in {2, 3}
    assert result["primary_review"]["verdict"] in {"review_unavailable", "invalid_review"}
    assert "private-marker" not in repr(result)


class Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.body


def test_promotion_evidence_cli_uses_real_sdk_oidc_health_job_contract(tmp_path, capsys):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"strategy_profile": "synthetic", "status": "shadow_candidate", "evidence_version": "synthetic"}))
    completed = completion().raw
    admitted = {key: value for key, value in completed.items() if key not in {"output", "status"}}
    replies = [Response({"value": "synthetic-oidc"}), Response({"codex_research_routing": "v1"}),
               Response(admitted), Response({"value": "synthetic-oidc"}), Response(completed)]
    with patch("client.gateway_client.urllib.request.urlopen", side_effect=replies) as http, patch(
        "client.gateway_client.time.sleep",
    ), patch("service.dual_review_orchestrator.gateway_dual_api_secondary_reviewer", side_effect=AssertionError("paid gateway forbidden")), patch(
        "service.dual_review_orchestrator.dual_api_secondary_reviewer", side_effect=AssertionError("paid adapter forbidden"),
    ):
        assert main(["--from-evidence", str(evidence)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["model_route"]["model"] == "gpt-6-astra"
    assert result["primary_review"]["provider"] == "codex"
    assert http.call_count == 5
    payload = json.loads(http.call_args_list[2].args[0].data)
    assert payload["research_stage"] == "promotion_review" and payload["reasoning_effort"] == "xhigh"
    assert payload["allowed_providers"] == ["codex"] and payload["model"] == ""
    assert payload["mode"] == "review_only" and payload["source_repository"] == ENV["GITHUB_REPOSITORY"]
    assert http.call_args_list[4].args[0].full_url.endswith("/synthetic-job")


@pytest.mark.parametrize("kind", ["old_service", "quota", "failed", "http403"])
def test_sdk_admission_or_execution_failure_has_no_paid_fallback(kind):
    replies = [Response({"value": "synthetic-oidc"})]
    if kind == "old_service":
        replies += [Response({"status": "ok"})]
    else:
        replies += [Response({"codex_research_routing": "v1"})]
        if kind in {"quota", "http403"}:
            body = {"status": "deferred", "retry_at": 9000, "error": "private-marker"}
            replies += [urllib.error.HTTPError("https://synthetic.invalid", 429 if kind == "quota" else 403,
                                               "private-marker", {}, io.BytesIO(json.dumps(body).encode()))]
        else:
            route = {key: value for key, value in completion().raw.items() if key not in {"output", "status"}}
            replies += [Response(route), Response({"value": "synthetic-oidc"}),
                        Response({**route, "status": "failed", "error": "private-marker", "failure_category": "private-marker"})]
    with patch("client.gateway_client.urllib.request.urlopen", side_effect=replies) as http, patch(
        "client.gateway_client.time.sleep",
    ), patch("service.dual_review_orchestrator.gateway_dual_api_secondary_reviewer", side_effect=AssertionError("paid gateway forbidden")), patch(
        "service.dual_review_orchestrator.dual_api_secondary_reviewer", side_effect=AssertionError("paid adapter forbidden"),
    ):
        result = run_pipeline(trigger="promotion", strategy_profile="synthetic", context={})
    assert result["outcome"] == "review_unavailable" and _exit_code(result) == 3
    assert "private-marker" not in repr(result) and http.call_count == len(replies)
    if kind == "quota":
        assert result["primary_review"]["status"] == "deferred"
        assert result["primary_review"]["retry_at"] == 9000


def test_reconciliation_primary_keeps_legacy_execute_contract():
    with patch("service.dual_review_primary.AiGatewayClient.execute", return_value=completion()) as execute, patch(
        "service.dual_review_orchestrator.gateway_secondary_available", return_value=True,
    ), patch("service.dual_review_orchestrator.gateway_dual_api_secondary_reviewer", return_value={
        "gpt": {"verdict": "approve", "confidence": 0.9}, "claude": {"verdict": "approve", "confidence": 0.9},
    }):
        result = run_pipeline(trigger="reconciliation_baseline", strategy_profile="synthetic",
                              context={"reconciliation_candidate_sha256": "a" * 64})
    assert "research_stage" not in execute.call_args.kwargs
    assert "allowed_providers" not in execute.call_args.kwargs
    assert result["outcome"] == "pass" and result["requires_human_recovery_approval"] is True
    assert result["evidence_binding_sha256"] == "a" * 64


def test_unsupported_research_stage_is_invalid_before_execute():
    with patch("service.dual_review_primary.AiGatewayClient.execute") as execute:
        result = run_codex_primary_review(prompt="synthetic", research_stage="optimization")
    assert result["verdict"] == "invalid_review"
    execute.assert_not_called()


def test_reconciliation_preserves_legacy_explicit_unconfigured_skip():
    with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "", "DUAL_REVIEW_GATE_ALLOW_SKIP": "true"}):
        result = run_pipeline(trigger="reconciliation_baseline", strategy_profile="synthetic",
                              context={"reconciliation_candidate_sha256": "a" * 64})
    assert result == {"ok": True, "skipped": ["codex_primary_unconfigured"]}


def test_actual_pipeline_rejects_another_codex_jobs_valid_completion():
    completed = completion().raw
    admitted = {key: value for key, value in completed.items() if key not in {"output", "status"}}
    replies = [Response({"value": "synthetic-oidc"}), Response({"codex_research_routing": "v1"}),
               Response(admitted), Response({"value": "synthetic-oidc"}), Response({**completed, "job_id": "different-job"})]
    with patch("client.gateway_client.urllib.request.urlopen", side_effect=replies) as http, patch(
        "client.gateway_client.time.sleep",
    ):
        result = run_pipeline(trigger="promotion", strategy_profile="synthetic", context={})
    assert result["outcome"] == "review_unavailable" and _exit_code(result) == 3
    assert result["primary_review"]["verdict"] == "review_unavailable"
    assert http.call_count == 5
