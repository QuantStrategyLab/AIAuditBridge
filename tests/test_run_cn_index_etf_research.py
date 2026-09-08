"""Offline job admission; synthetic fixtures are not market evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import hashlib
import importlib
import io
import urllib.error

import pytest

from scripts import run_cn_index_etf_research as job
from service.research_task import build_strategy_diagnosis_task


NOW = datetime(2026, 9, 9, 8, tzinfo=timezone.utc)


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


REVISION = "c" * 40
ENV = {
    "GITHUB_REPOSITORY": "QuantStrategyLab/AIAuditBridge",
    "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/strategy_optimization_watcher.yml@refs/heads/main",
    "GITHUB_RUN_ID": "12345", "GITHUB_SHA": "a" * 40,
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://synthetic.invalid/oidc",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-test-only",
    "CODEX_AUDIT_SERVICE_URL": "https://synthetic.invalid",
}


def write(path, value):
    path.write_text(json.dumps(value))
    return path


@pytest.fixture(autouse=True)
def isolated():
    with patch.dict(os.environ, ENV, clear=True), patch.object(job, "_now", return_value=NOW):
        yield


def task(revision=REVISION):
    return build_strategy_diagnosis_task(
        event_key="a" * 12, created_at="2026-09-09T06:17:00Z", candidate_id=job.PROFILE,
        candidate_kind="individual", domain="cn_equity", strategy_repository=job.STRATEGY_REPOSITORY,
        evidence={"p1_input_digest": "1" * 64, "p2_config_digest": "2" * 64,
                  "p3_evidence_id": "3" * 64, "strategy_revision": revision, "producer_revision": "d" * 40},
    )


def watcher(revision=REVISION):
    return {"research_task_source_snapshot": {"schema_version": "qsl_research_task_source_snapshot.v1",
            "data_status": "ready", "tasks": [task(revision)]}}


def test_missing_or_disabled_policy_never_imports_runner_or_calls_model(tmp_path):
    with patch.object(job, "_load_runtime") as runtime:
        result = job.run_from_watcher(watcher(), policy_path=tmp_path / "absent")
        assert result["reason"] == "cn_research_not_configured"
        with patch.object(job, "_read_policy", return_value={"enabled": False}):
            result = job.run_from_watcher(watcher())
            assert result["reason"] == "cn_research_disabled"
    runtime.assert_not_called()


@pytest.mark.parametrize("change", [{"CODEX_AUDIT_SERVICE_TOKEN": "synthetic-static"},
    {"ACTIONS_ID_TOKEN_REQUEST_URL": ""}, {"ACTIONS_ID_TOKEN_REQUEST_TOKEN": ""},
    {"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_EVENT_NAME": "pull_request"},
    {"GITHUB_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/unapproved.yml@refs/heads/main"}])
def test_workflow_and_oidc_are_required_before_runtime(change):
    with patch.object(job, "_read_policy", return_value={"enabled": True}), \
            patch.dict(os.environ, change), patch.object(job, "_load_runtime") as runtime:
        assert job.run_from_watcher(watcher())["reason"] == "cn_research_workflow_auth_required"
    runtime.assert_not_called()


def test_task_is_verified_and_never_supplies_the_observation_clock():
    verified = job._select_task(watcher(), REVISION, NOW)
    assert verified["created_at"] == "2026-09-09T06:17:00Z"
    invalid = watcher()
    invalid["research_task_source_snapshot"]["tasks"][0]["experiment"]["max_runs"] = 2
    with pytest.raises(ValueError):
        job._select_task(invalid, REVISION, NOW)
    with pytest.raises(ValueError):
        job._select_task(watcher(), "e" * 40, NOW)


@pytest.mark.parametrize("change", [{"as_of": None}, {"as_of": "2026-09-10"}, {"as_of": "2026-09-01"},
    {"drift_score": True}, {"drift_score": float("nan")}, {"status": "healthy"},
    {"source_revision": "wrong"}, {"strategy_profile": "another"}, {"domain": "us_equity"},
    {"alert_suppressed": True}, {"baseline_available": False}])
def test_real_drift_requires_matching_fresh_actionable_source(tmp_path, change):
    raw = {"strategy_profile": job.PROFILE, "domain": "cn_equity", "source_revision": REVISION,
           "as_of": "2026-09-09", "drift_score": .8, "status": "critical", **change}
    source = write(tmp_path / "drift.json", raw)
    with pytest.raises(ValueError):
        job._read_drift({"path": str(source), "source_revision": REVISION}, NOW)


def test_daily_admission_counts_existing_ticket_utc_created_at(tmp_path):
    assert job.admit_one_new_experiment(tmp_path, NOW.isoformat()) is True
    ticket = {"strategy_profile": job.PROFILE, "domain": "cn_equity", "live_authority_granted": False}
    old_id, new_id = "rpt_" + "a" * 64, "rpt_" + "b" * 64
    write(tmp_path / f"{old_id}.json", {**ticket, "ticket_id": old_id, "created_at": "2026-09-08T23:59:59Z"})
    assert job.admit_one_new_experiment(tmp_path, NOW.isoformat()) is True
    write(tmp_path / f"{new_id}.json", {**ticket, "ticket_id": new_id, "created_at": "2026-09-09T00:00:00Z"})
    assert job.admit_one_new_experiment(tmp_path, NOW.isoformat()) is False


@pytest.mark.parametrize("bad", [{}, {"created_at": "2026-09-09T00:00:00"},
    {"created_at": "2026-09-10T00:00:00Z"}, {"created_at": None}])
def test_daily_admission_unknown_count_fails_closed(tmp_path, bad):
    write(tmp_path / "rpt_bad.json", bad)
    assert job.admit_one_new_experiment(tmp_path, NOW.isoformat()) is False


def test_daily_admission_does_not_add_a_ledger_or_follow_symlinks(tmp_path):
    outside = write(tmp_path / "private", {"created_at": NOW.isoformat()})
    (tmp_path / "rpt_link.json").symlink_to(outside)
    assert job.admit_one_new_experiment(tmp_path, NOW.isoformat()) is False
    assert sorted(path.name for path in tmp_path.iterdir()) == ["private", "rpt_link.json"]


def test_policy_rejects_untrusted_owner_mode_and_symlink(tmp_path):
    path = write(tmp_path / "policy.json", {"enabled": True})
    path.chmod(0o666)
    with pytest.raises(ValueError):
        job._read_policy(path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        job._read_policy(link)


@pytest.mark.parametrize("source", [None, {}, {"dir_info": {"editable": True}},
    {"vcs_info": {"commit_id": "e" * 40}},
    {"vcs_info": {"vcs": "git", "commit_id": REVISION}, "dir_info": {"editable": True}},
    {"vcs_info": {"vcs": "svn", "commit_id": REVISION}}])
def test_installed_runtime_requires_noneditable_git_commit(source):
    raw = None if source is None else json.dumps(source)
    with patch.object(job.importlib.metadata, "distribution") as distribution:
        distribution.return_value.read_text.return_value = raw
        with pytest.raises(ValueError, match="installed_revision"):
            job._installed_revision("synthetic-package", REVISION)


def test_installed_runtime_accepts_exact_git_commit():
    with patch.object(job.importlib.metadata, "distribution") as distribution:
        distribution.return_value.read_text.return_value = json.dumps({"vcs_info": {"vcs": "git", "commit_id": REVISION}})
        job._installed_revision("synthetic-package", REVISION)


def test_cli_failure_prints_only_sanitized_summary(tmp_path, capsys):
    path = tmp_path / "sensitive-missing.json"
    assert job.main(["--watcher-result", str(path)]) == 3
    output = capsys.readouterr().out
    assert str(path) not in output and "sensitive" not in output
    assert json.loads(output)["live_authority_granted"] is False


@pytest.fixture
def shadow_inputs(tmp_path):
    importlib.import_module("quant_platform_kit")
    from quant_platform_kit.strategy_lifecycle.forward_observation import ForwardObservationPolicy
    from quant_platform_kit.strategy_lifecycle.forward_observation_receipt import build_forward_observation_receipt
    policy_args = dict(candidate_id="cn-frozen-example", strategy_profile=job.PROFILE, domain="cn_equity",
        benchmark_symbol="510300", required_trading_sessions=2, review_milestones=[1],
        automatic_non_live_modes=["shadow"], auto_resume_clean_sessions=1, observation_calendar="XSHG",
        observation_window_type="fixed", observation_start_session="2026-09-07",
        window_rationale_ref="synthetic-test-policy", non_live_evidence_modes=["shadow_decision"])
    policy = ForwardObservationPolicy(**policy_args)
    calendar = write(tmp_path / "sessions.json", ["2026-09-07", "2026-09-08"])
    dependencies = {key: "a" * 64 for key in ("p1_manifest", "p2_config", "p3_evidence", "risk_policy", "strategy_release", "plugin_bundle")}
    observations = []
    previous = None
    for index, day in enumerate(["2026-09-07", "2026-09-08"], 1):
        receipt = build_forward_observation_receipt(policy=policy, observation_session=day, observation_index=index,
            dependency_digests=dependencies, evidence_modes=["shadow_decision"], previous_receipt=previous)
        leg = {key: {"synthetic": True, "value": 0} for key in ("signal", "hypothetical_order", "position", "cost", "return")}
        observations.append(dict(forward_observation_receipt=receipt, baseline_id="cn-baseline",
            observed_at=day + "T16:00:00+08:00", input_snapshot_sha256="a" * 64, candidate=leg, baseline=leg))
        previous = receipt
    identity = {key: "sha256:" + "f" * 64 for key in job._IDENTITY_FIELDS}
    proposal = SimpleNamespace(strategy_profile=job.PROFILE, domain="cn_equity", current_params={"a": 1},
                               proposed_params={"a": 2}, computed_at="2026-09-06T08:00:00Z")
    payload = dict(strategy_profile=job.PROFILE, domain="cn_equity", source_revision=REVISION,
        research_identity=identity, current_params={"a": 1}, proposed_params={"a": 2}, observations=observations)
    observation_path = write(tmp_path / "observations.json", payload)
    binding = dict(forward_policy=policy_args, observation_path=str(observation_path), calendar_path=str(calendar),
        calendar_sha256=hashlib.sha256(calendar.read_bytes()).hexdigest(), baseline_id="cn-baseline",
        frozen_dependency_digests={key: value for key, value in dependencies.items() if key != "p1_manifest"},
        retry_after_seconds=3600)
    return binding, identity, proposal, payload


def test_shadow_requires_complete_real_calendar_and_receipt_chain(shadow_inputs):
    from quant_platform_kit.strategy_lifecycle.paired_shadow_adapter import collect_paired_shadow_for_promotion
    binding, identity, proposal, _ = shadow_inputs
    result = job._make_shadow_reader(binding, identity, REVISION)(proposal)
    assert result["status"] == "complete"
    record = collect_paired_shadow_for_promotion(result["observation"])
    assert record["passed"] and record["no_order"] and not record["live_authority_granted"]


@pytest.mark.parametrize("missing", [False, True])
def test_incomplete_shadow_is_pending_without_fabricating_success(shadow_inputs, missing):
    binding, identity, proposal, payload = shadow_inputs
    path = Path(binding["observation_path"])
    if missing:
        path.unlink()
    else:
        payload["observations"] = payload["observations"][:1]
        write(path, payload)
    result = job._make_shadow_reader(binding, identity, REVISION)(proposal)
    assert result == {"status": "pending", "passed": False, "no_order": True, "live_authority_granted": False,
                      "retry_at": NOW.timestamp() + 3600}


@pytest.mark.parametrize("change", ["params", "source", "calendar", "chain", "backfill", "future", "missing_session"])
def test_shadow_wrong_binding_or_backfilled_evidence_cannot_complete(shadow_inputs, change):
    binding, identity, proposal, payload = shadow_inputs
    if change == "params":
        payload["proposed_params"] = {"a": 3}
    elif change == "source":
        payload["source_revision"] = "e" * 40
    elif change == "calendar":
        binding["calendar_sha256"] = "b" * 64
    elif change == "chain":
        payload["observations"][1]["forward_observation_receipt"]["previous_receipt_sha256"] = "b" * 64
    elif change == "backfill":
        proposal.computed_at = "2026-09-09T00:00:00Z"
    elif change == "future":
        payload["observations"][1]["observed_at"] = "2026-09-10T00:00:00Z"
    else:
        payload["observations"] = payload["observations"][1:]
    write(Path(binding["observation_path"]), payload)
    if change == "calendar":
        with pytest.raises(ValueError, match="shadow_calendar_mismatch"):
            job._make_shadow_reader(binding, identity, REVISION)
        return
    result = job._make_shadow_reader(binding, identity, REVISION)(proposal)
    assert result["status"] == "failed" and result["passed"] is False
    assert "observation" not in result


def test_actual_entrypoint_calls_cn_runner_with_persistent_callbacks(shadow_inputs, tmp_path):
    # This checks orchestration only; separate source integration runs the CN code.
    from unittest.mock import Mock
    binding, identity, _, _ = shadow_inputs
    runtime = SimpleNamespace(cn=SimpleNamespace(preflight_index_etf_research_job=Mock(return_value=identity),
        run_index_etf_research_job=Mock(return_value={"status": "parked", "reason": "synthetic_gate_rejection",
            "research_key": "f" * 64, "resumed": False, "console_synced": None})),
        read_input=Mock(return_value=object()), fold=lambda **kw: kw,
        execution_config=lambda **kw: kw, cost_model=lambda **kw: kw,
        probe=Mock(return_value={"actionable": True, "status": "critical"}),
        drift=lambda **kw: kw, status=lambda value: value)
    drift = write(tmp_path / "drift.json", dict(strategy_profile=job.PROFILE, domain="cn_equity",
        source_revision=REVISION, as_of="2026-09-09", drift_score=.8, status="critical"))
    policy = dict(enabled=True, code_revision=REVISION, candidate_id=job.PROFILE, domain="cn_equity",
        research_identity=identity, drift={"path": str(drift), "source_revision": REVISION}, shadow=binding, console={},
        inputs={name: {"path": str(tmp_path), "manifest_sha256": "f" * 64} for name in ("development", "validation")},
        plan=dict(development_start="2020-01-01", development_end="2020-12-31", folds=[],
                  locked_oos_start="2024-01-01", locked_oos_end="2025-01-01", purge_days=1, embargo_days=1),
        execution_config={}, cost_model={})
    sync, pull, diagnose = Mock(), Mock(), Mock()
    with patch.object(job, "_read_policy", return_value=policy), patch.object(job, "_load_runtime", return_value=runtime), \
            patch.object(job, "_diagnosis", return_value=diagnose), patch.object(job, "_console_bindings", return_value=(sync, pull)), \
            patch.object(job, "STATE_ROOT", tmp_path / "state"):
        result = job.run_from_watcher(watcher())
    assert result["reason"] == "synthetic_gate_rejection"
    kwargs = runtime.cn.run_index_etf_research_job.call_args.kwargs
    assert callable(kwargs["admit_new_research"])
    assert kwargs["record_shadow"] is kwargs["read_pending_shadow"]
    assert kwargs["sync_console"] is sync and kwargs["pull_console"] is pull
    assert kwargs["diagnose"]() == {"optimization_needed": False, "reason": "forward_window_start_elapsed"}
    diagnose.assert_not_called()
    assert kwargs["as_of"] == "2026-09-09" and kwargs["source_revision"] == REVISION
    assert kwargs["ticket_dir"] == tmp_path / "state" / "research_promotion_tickets"
    assert "ticket" not in result and "trial_records_path" not in result


class Response:
    def __init__(self, value):
        self.body = json.dumps(value).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return None
    def read(self):
        return self.body


def diagnosis_runtime():
    importlib.import_module("quant_platform_kit")
    from client.config import GatewayConfig
    from client.gateway_client import AiGatewayClient
    from quant_platform_kit.strategy_lifecycle.codex_integration import AiOptimizationContext, build_optimization_prompt
    from quant_platform_kit.strategy_lifecycle.contracts import DriftResult, DriftStatus
    drift = DriftResult(strategy_profile=job.PROFILE, domain="cn_equity", as_of=NOW.date(),
                        drift_score=.8, status=DriftStatus.CRITICAL, source_revision=REVISION)
    return SimpleNamespace(config=GatewayConfig, client=AiGatewayClient, context=AiOptimizationContext,
        prompt=build_optimization_prompt, cn=SimpleNamespace(BASELINE_PARAMS={"top_n": 1})), drift


@pytest.mark.parametrize("mismatch", [None, "job_id", "provider", "model", "research_stage", "reasoning_effort"])
def test_real_sdk_binds_optimization_job_and_route_without_paid_fallback(mismatch):
    runtime, drift = diagnosis_runtime()
    route = dict(job_id="original-experiment", provider="codex", model="gpt-5.6-sol",
                 research_stage="optimization", reasoning_effort="high")
    final = {**route, "status": "succeeded", "output": '{"optimization_needed":true,"recommended_method":"grid_search"}'}
    if mismatch:
        final[mismatch] = "different"
    replies = [Response({"value": "synthetic-oidc"}), Response({"codex_research_routing": "v1"}),
               Response({**route, "status": "queued"}), Response({"value": "synthetic-oidc"}), Response(final)]
    with patch("client.gateway_client.urllib.request.urlopen", side_effect=replies) as http, \
            patch("client.gateway_client.time.sleep"), patch.object(runtime.client, "analyze", side_effect=AssertionError("paid fallback")):
        if mismatch:
            with pytest.raises(ValueError, match="research_model_outcome_unavailable"):
                job._diagnosis(runtime, drift, REVISION)()
        else:
            result = job._diagnosis(runtime, drift, REVISION)()
            assert result["optimization_needed"] is True and result["model"] == "gpt-5.6-sol"
            assert result["job_id"] == route["job_id"]
    submitted = json.loads(http.call_args_list[2].args[0].data)
    assert submitted["source_ref"] == REVISION and submitted["source_repository"] == job.STRATEGY_REPOSITORY
    assert submitted["mode"] == "review_only" and submitted["allowed_providers"] == ["codex"]


def test_real_sdk_quota_defers_and_sanitizes_without_polling():
    runtime, drift = diagnosis_runtime()
    error = urllib.error.HTTPError("https://synthetic.invalid", 429, "private-error", {},
        io.BytesIO(json.dumps({"status": "deferred", "retry_at": NOW.timestamp() + 3600, "stderr": "private-error"}).encode()))
    with patch("client.gateway_client.urllib.request.urlopen", side_effect=[
        Response({"value": "synthetic-oidc"}), Response({"codex_research_routing": "v1"}), error,
    ]) as http:
        result = job._diagnosis(runtime, drift, REVISION)()
    assert result == {"optimization_needed": False, "reason": "codex_research_deferred", "retry_at": NOW.timestamp() + 3600}
    assert http.call_count == 3 and "private" not in str(result)


def test_watcher_uses_one_opt_in_vps_owner_with_same_run_artifact():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/strategy_optimization_watcher.yml").read_text()
    research = text.split("  cn-index-etf-research:", 1)[1]
    for required in ("needs: strategy-optimization-watcher", "runs-on: [self-hosted, codex-vps]",
        "vars.CN_INDEX_ETF_RESEARCH_ENABLED == 'true'", "github.ref == 'refs/heads/main'",
        "needs.strategy-optimization-watcher.outputs.source_repo == 'QuantStrategyLab/CnEquitySnapshotPipelines'",
        "needs.strategy-optimization-watcher.outputs.dry_run == 'false'", "group: cn-index-etf-research-vps",
        "cancel-in-progress: false", "id-token: write", "AI_GATEWAY_RESEARCH_PROVIDERS: codex",
        "name: strategy-optimization-watcher-${{ github.run_id }}",
        "/opt/codex-cn-index-etf-research/venv/bin/python", "-m scripts.run_cn_index_etf_research"):
        assert required in research
    assert "CODEX_AUDIT_SERVICE_TOKEN" not in research
    assert "issues: write" not in research and "pip install" not in research
    assert "path: data/output/cn-index-etf-research/result.json" in research


def input_package(root, start, end):
    """Historical *schema* stand-in; all prices/license text are synthetic."""
    import pandas as pd
    from quant_platform_kit.data.research_input import canonical_research_input_manifest_bytes
    days = list(pd.bdate_range(start, end).strftime("%Y-%m-%d"))
    rows = [dict(date=day, symbol=symbol, open=10., high=11., low=9., close=10., volume=100000,
        suspended=False, limit_up=11., limit_down=9., status_known_at=day+"T09:00:00+08:00",
        available_at=day+"T15:01:00+08:00") for day in days for symbol in ("510300", "510500")]
    license_bytes = b"Synthetic test fixture; not a real license or market evidence."
    docs = {"normalized/daily.json": rows,
        "calendar/sessions.json": {"start_date": days[0], "end_date": days[-1], "sessions": days},
        "corporate_actions/events.json": {"complete_from": days[0], "complete_through": days[-1], "events": []},
        "evidence/license_identity.json": {"source_identity": "official:synthetic-fixture", "revision": "test-v1",
            "retention_scope": "private-retention-permitted", "content_sha256": hashlib.sha256(license_bytes).hexdigest()}}
    members = {key: json.dumps(value, separators=(",", ":")).encode() for key, value in docs.items()}
    members["evidence/license.bin"] = license_bytes
    manifest = dict(schema_version="research_input_manifest.v1", manifest_id="synthetic-test",
        research_input_contract_id="qsl.cn_index_etf.execution_input.v1", domain="cn_equity", profile=job.PROFILE,
        artifact_type="cn_index_etf_execution_history", observed_at="2026-09-08T00:00:00Z",
        effective_at=days[-1]+"T15:01:00+08:00", as_of="2026-09-08T00:00:00Z",
        producer={"repository": "QuantStrategyLab/CnEquitySnapshotPipelines", "commit_sha": "a"*40,
                  "tree_sha": "b"*40, "tool": "synthetic.fixture", "tool_version": "1"},
        calendar={"calendar_id": "SSE", "timezone": "Asia/Shanghai", "session_date": days[-1],
                  "source": "official:synthetic-fixture", "source_revision": "sha256:"+hashlib.sha256(members["calendar/sessions.json"]).hexdigest()},
        adjustment={"policy": "raw", "source": "official:synthetic-fixture", "source_revision": "test-v1"},
        sources=[{"source_id": "synthetic-fixture", "revision": "test-v1", "observed_at": "2026-09-08T00:00:00Z",
                  "content_sha256": hashlib.sha256(members["normalized/daily.json"]).hexdigest()}],
        members=[{"path": path, "media_type": "application/json" if path.endswith(".json") else "text/plain",
                  "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()} for path, content in sorted(members.items())])
    encoded = canonical_research_input_manifest_bytes(manifest)
    for name, content in {**members, "research_input_manifest.v1.json": encoded}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return {"path": str(root), "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


@pytest.mark.parametrize(("expired_window", "defer_past_window"), [(False, False), (True, False), (False, True)])
def test_installed_cn_reader_preflight_real_numeric_search_ticket_reuse_and_daily_cap(shadow_inputs, tmp_path, expired_window, defer_past_window):
    # No CN runner, preflight, input reader, optimizer or QPK stage is mocked.
    importlib.import_module("cn_equity_strategies")
    importlib.import_module("ai_gateway_client")
    from unittest.mock import Mock
    from datetime import date
    binding, _, _, _ = shadow_inputs
    if not expired_window:
        # A new candidate can only begin a still-future approved observation window.
        binding["forward_policy"]["observation_start_session"] = "2026-09-10"
        calendar = write(Path(binding["calendar_path"]), ["2026-09-10", "2026-09-11"])
        binding["calendar_sha256"] = hashlib.sha256(calendar.read_bytes()).hexdigest()
    revision = "2a0c5c9aafacfbe6519fb4029ca4ac18e4996a66"
    policy = dict(enabled=True, candidate_id=job.PROFILE, domain="cn_equity", code_revision=revision,
        qpk_revision="b5654244aa5d08bce2b4b4f931436268d57216df", sdk_revision="60bd64a2ae059a082614181eeb845b46df395523", shadow=binding, console={},
        inputs={"development": input_package(tmp_path / "development", "2019-01-02", "2020-12-31"),
                "validation": input_package(tmp_path / "validation", "2020-01-02", "2025-01-08")},
        plan=dict(development_start="2020-01-02", development_end="2020-12-31", folds=[dict(
            train_start=f"{year}-01-04", train_end=f"{year}-11-15", test_start=f"{year}-11-19", test_end=f"{year}-12-31")
            for year in (2021, 2022, 2023)], locked_oos_start="2024-01-08", locked_oos_end="2025-01-08", purge_days=1, embargo_days=1),
        execution_config={}, cost_model={"model_id": "cn_index_etf.next_open.v1", "commission_bps": 3., "slippage_bps": 5.})
    drift_path = write(tmp_path / "drift.json", dict(strategy_profile=job.PROFILE, domain="cn_equity",
        source_revision=revision, as_of="2026-09-09", drift_score=.8, status="critical"))
    policy["drift"] = {"path": str(drift_path), "source_revision": revision}
    # These packages must be noneditable installations from the approved commits.
    runtime = job._load_runtime(policy)
    arguments = dict(development_input=runtime.read_input(Path(policy["inputs"]["development"]["path"]), expected_manifest_sha256=policy["inputs"]["development"]["manifest_sha256"]),
        validation_input=runtime.read_input(Path(policy["inputs"]["validation"]["path"]), expected_manifest_sha256=policy["inputs"]["validation"]["manifest_sha256"]),
        trusted_input_roots={name: value["manifest_sha256"] for name, value in policy["inputs"].items()}, code_revision=revision,
        development_start=date(2020,1,2), development_end=date(2020,12,31),
        folds=tuple(runtime.fold(**{key:date.fromisoformat(value) for key,value in fold.items()}) for fold in policy["plan"]["folds"]),
        locked_oos_start=date(2024,1,8), locked_oos_end=date(2025,1,8), purge_days=1, embargo_days=1)
    policy["research_identity"] = runtime.cn.preflight_index_etf_research_job(**arguments)
    route = dict(job_id="real-caller-synthetic-http", provider="codex", research_stage="optimization",
                 model="gpt-5.6-sol", reasoning_effort="high")
    replies = [Response({"value":"synthetic-oidc"}), Response({"codex_research_routing":"v1"}),
        Response({**route,"status":"queued"}), Response({"value":"synthetic-oidc"}),
        Response({**route,"status":"succeeded","output":'{"optimization_needed":true,"recommended_method":"grid_search"}'})]
    initial_replies = replies
    if defer_past_window:
        quota_error = urllib.error.HTTPError("https://synthetic.invalid", 429, "synthetic quota", {},
            io.BytesIO(json.dumps({"status": "deferred", "retry_at": NOW.timestamp()+3600}).encode()))
        initial_replies = replies[:2] + [quota_error]
    sync, pull = Mock(), Mock()
    with patch.object(job, "_read_policy", return_value=policy), patch.object(job, "_load_runtime", return_value=runtime), \
            patch.object(job, "STATE_ROOT", tmp_path / "state"), patch.object(job, "_console_bindings", return_value=(sync,pull)), \
            patch("quant_platform_kit.strategy_lifecycle.production_drift_health_probe.datetime", FixedClock), \
            patch("quant_platform_kit.strategy_lifecycle.research_promotion_cycle.datetime", FixedClock), \
            patch(runtime.client.__module__ + ".urllib.request.urlopen", side_effect=initial_replies) as http, \
            patch(runtime.client.__module__ + ".time.sleep"):
        first = job.run_from_watcher(watcher(revision))
        if defer_past_window:
            assert http.call_count == 3
            saved = list((tmp_path / "state" / "research_promotion_tickets").glob("*.json"))
            assert len(saved) == 1
            assert json.loads(saved[0].read_text())["research_progress"]["stages"]["diagnose"]["status"] == "deferred"
            later = datetime(2026, 9, 10, 2, tzinfo=timezone.utc)

            class AfterWindow(datetime):
                @classmethod
                def now(cls, tz=None):
                    return later.astimezone(tz)

            http.side_effect = replies
            with patch.object(job, "_now", return_value=later), \
                    patch("quant_platform_kit.strategy_lifecycle.production_drift_health_probe.datetime", AfterWindow), \
                    patch("quant_platform_kit.strategy_lifecycle.research_promotion_cycle.datetime", AfterWindow):
                second = job.run_from_watcher(watcher(revision))
            assert second["research_key"] == first["research_key"]
            assert http.call_count == 3
            assert not list((tmp_path / "state").rglob("trials.json"))
            stage = json.loads(saved[0].read_text())["research_progress"]["stages"]["diagnose"]
            assert stage["status"] == "completed"
            assert stage["result"]["optimization_needed"] is False
            assert stage["result"]["reason"] == "forward_window_start_elapsed"
            sync.assert_not_called()
            return
        if expired_window:
            assert first["reason"] == "new_research_not_admitted"
            assert http.call_count == 0
            assert not list((tmp_path / "state").rglob("trials.json"))
            assert not list((tmp_path / "state" / "research_promotion_tickets").glob("*.json"))
            sync.assert_not_called()
            return
        second = job.run_from_watcher(watcher(revision))
        assert first["status"] == second["status"] == "parked"  # Flat synthetic data correctly rejects improvement.
        assert first["research_key"] == second["research_key"] and second["resumed"] is True
        assert http.call_count == 5
        raw = json.loads(drift_path.read_text())
        raw["drift_score"] = .81
        write(drift_path, raw)
        third = job.run_from_watcher(watcher(revision))
        assert third["reason"] == "new_research_not_admitted" and http.call_count == 5
    tickets = list((tmp_path / "state" / "research_promotion_tickets").glob("*.json"))
    assert len(tickets) == 1
    ticket = json.loads(tickets[0].read_text())
    assert ticket["notes"] == ["recommendation=reject"]
    assert set(ticket["research_progress"]["stages"]) == {"diagnose", "optimize"}
    trials = list((tmp_path / "state" / "experiments").rglob("trials.json"))
    assert len(trials) == 1 and len(json.loads(trials[0].read_text())) == 13
    sync.assert_not_called()


@pytest.mark.parametrize("denied", [None, "repository", "workflow", "ref", "direct", "source", "cross_org", "static"])
def test_sdk_to_actual_auth_and_execute_handler_preserves_aab_caller_cn_source(denied):
    """Real claim/source gates; RSA/JWKS, quota and job execution stay offline."""
    from service import auth
    from service import ai_gateway_service as gateway
    from unittest.mock import Mock
    runtime, drift = diagnosis_runtime()
    claims = dict(aud="quant-codex-audit", iss=auth.GITHUB_OIDC_ISSUER, repository=job.BRIDGE_REPOSITORY,
        workflow_ref=job.WORKFLOW_REF, ref="refs/heads/main", run_id="12345", exp=4102444800)
    service_env = dict(CODEX_AUDIT_SERVICE_AUTH="github-oidc",
        CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=job.BRIDGE_REPOSITORY,
        CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES=job.BRIDGE_REPOSITORY,
        CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS=job.WORKFLOW_REF,
        CODEX_AUDIT_SERVICE_ALLOWED_REFS="refs/heads/main",
        CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES=job.STRATEGY_REPOSITORY)
    if denied == "repository":
        claims["repository"] = job.STRATEGY_REPOSITORY
    elif denied == "workflow":
        claims["workflow_ref"] = job.WORKFLOW_REF.replace("strategy_optimization_watcher", "another")
    elif denied == "ref":
        claims["ref"] = "refs/heads/feature"
    elif denied == "direct":
        service_env["CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES"] = "Another/direct"
    elif denied == "source":
        service_env["CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES"] = job.BRIDGE_REPOSITORY
    elif denied == "cross_org":
        claims["repository"] = "Another/approved"
        service_env["CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES"] = claims["repository"]
        service_env["CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES"] = claims["repository"]
    elif denied == "static":
        service_env["CODEX_AUDIT_SERVICE_TOKEN"] = "synthetic-oidc"
    route = dict(job_id="original-experiment", provider="codex", model="gpt-5.6-sol",
                 research_stage="optimization", reasoning_effort="high")
    quota, submit = Mock(), Mock(return_value={**route, "status": "queued"})
    checked = []

    def admit(_quota, repository, payload):
        assert repository == job.STRATEGY_REPOSITORY
        payload.update({key: route[key] for key in ("provider", "model", "research_stage", "reasoning_effort")})

    def http(request, **_):
        url = request if isinstance(request, str) else request.full_url
        if "/oidc" in url:
            return Response({"value": "synthetic-oidc"})
        if url.endswith("/healthz"):
            return Response({"codex_research_routing": "v1"})
        if url.endswith("/execute/jobs"):
            handler = object.__new__(gateway.AiGatewayRequestHandler)
            handler.path, handler.headers = "/v1/ai/execute/jobs", dict(request.header_items())
            handler.headers["Content-Length"] = str(len(request.data))
            handler.rfile = io.BytesIO(request.data)
            with patch.dict(os.environ, service_env), \
                    patch.object(auth, "_jwt_parts", return_value=({"alg": "RS256", "kid": "test"}, dict(claims), b"x", b"y")), \
                    patch.object(auth, "_load_jwks", return_value={"keys": [{"kid": "test"}]}), \
                    patch.object(auth, "_verify_rs256"), patch.object(gateway, "_audit_log"), \
                    patch.object(gateway, "_json_response") as response:
                gateway.AiGatewayRequestHandler.do_POST(handler)
            code, body = response.call_args.args[1:3]
            checked.append(code)
            if code != 202:
                raise urllib.error.HTTPError(url, code, "synthetic-service-rejection", {}, io.BytesIO(json.dumps(body).encode()))
            return Response(body)
        assert url.endswith("/execute/jobs/original-experiment")
        return Response({**route, "status": "succeeded", "output": '{"optimization_needed":false}'})

    with patch("client.gateway_client.urllib.request.urlopen", side_effect=http), patch("client.gateway_client.time.sleep"), \
            patch.object(gateway, "get_quota_manager", return_value=quota), \
            patch.object(gateway, "_cleanup_expired_jobs"), patch.object(gateway, "_find_active_job_by_dedupe_key", return_value=None), \
            patch.object(gateway, "_active_job_count", return_value=0), patch.object(gateway, "_admit_codex_execute", side_effect=admit), \
            patch.object(gateway, "_submit_job", submit), patch.object(gateway, "get_health_monitor"), \
            patch.object(runtime.client, "analyze", side_effect=AssertionError("paid fallback")):
        if denied:
            with pytest.raises(ValueError, match="research_model_outcome_unavailable"):
                job._diagnosis(runtime, drift, REVISION)()
        else:
            assert job._diagnosis(runtime, drift, REVISION)()["optimization_needed"] is False
    if denied:
        assert checked == [401]
        submit.assert_not_called()
        quota.record_execute.assert_not_called()
    else:
        assert checked == [202]
        actual_claims, payload = submit.call_args.args
        assert actual_claims["repository"] == job.BRIDGE_REPOSITORY
        assert actual_claims["auth_method"] == "github_oidc" and actual_claims["workflow_ref"] == job.WORKFLOW_REF
        assert payload["source_repository"] == job.STRATEGY_REPOSITORY and payload["source_ref"] == REVISION
        quota.record_execute.assert_called_once_with(job.STRATEGY_REPOSITORY, provider="codex")


def test_shadow_cannot_label_future_sessions_as_completed(shadow_inputs):
    binding, identity, proposal, _ = shadow_inputs
    # The second receipt claims Sep 8, but its timestamp is still Sep 7.
    payload = json.loads(Path(binding["observation_path"]).read_text())
    payload["observations"][1]["observed_at"] = "2026-09-07T17:00:00+08:00"
    write(Path(binding["observation_path"]), payload)
    result = job._make_shadow_reader(binding, identity, REVISION)(proposal)
    assert result["status"] == "failed" and result["passed"] is False


@pytest.mark.parametrize("field", ["forward_policy", "calendar_sha256", "frozen_dependency_digests", "baseline_id"])
def test_bad_shadow_configuration_is_rejected_before_constructing_a_callback(shadow_inputs, field):
    binding, identity, _, _ = shadow_inputs
    binding[field] = {} if field.endswith("digests") or field == "forward_policy" else ""
    with pytest.raises(ValueError):
        job._make_shadow_reader(binding, identity, REVISION)


@pytest.mark.parametrize("binding", [None,
    {"sync_url": "http://synthetic.invalid/sync", "pull_url": "https://synthetic.invalid/pull"},
    {"sync_url": "https://synthetic.invalid/sync", "pull_url": "https://different.invalid/pull"}])
def test_console_missing_or_wrong_origin_constructs_no_network_callback(binding):
    importlib.import_module("quant_platform_kit")
    with patch("urllib.request.urlopen", side_effect=AssertionError("unexpected request")) as http:
        with pytest.raises(ValueError):
            job._console_bindings(binding)
    http.assert_not_called()


def test_actual_console_adapter_reads_before_post_and_never_repeats_unknown_write(tmp_path, capsys):
    importlib.import_module("quant_platform_kit")
    from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import ResearchPromotionTicket, ResearchPromotionState
    secret = tmp_path / "synthetic.token"
    secret.write_text("synthetic-test-only")
    binding = dict(sync_url="https://synthetic.invalid/sync", pull_url="https://synthetic.invalid/pull", token_path=str(secret))
    ticket = ResearchPromotionTicket(ticket_id="rpt_"+"f"*64, strategy_profile=job.PROFILE, domain="cn_equity",
        state=ResearchPromotionState.AWAITING_HUMAN, drift_status="critical", drift_score=.8,
        created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    methods = []

    def http(request, **_):
        methods.append(request.method)
        assert request.get_header("Authorization") == "Bearer synthetic-test-only"
        if request.method == "POST":
            assert json.loads(request.data)["live_authority_granted"] is False
            raise OSError("synthetic-private-transport-error")
        raise urllib.error.HTTPError(request.full_url, 404, "synthetic absence", {}, None)

    with patch.object(job, "_protected_file") as protected, patch("urllib.request.urlopen", side_effect=http):
        sync, pull = job._console_bindings(binding)
        assert methods == []
        protected.assert_called_once_with(secret, secret=True)
        assert sync(ticket) is False
        assert sync(ticket) is False
    assert methods == ["GET", "POST", "GET", "GET"]
    assert capsys.readouterr().out == ""


def test_shadow_cannot_relabel_old_sessions_with_postproposal_timestamps(shadow_inputs):
    binding, identity, proposal, payload = shadow_inputs
    proposal.computed_at = "2026-09-09T07:40:00Z"
    payload["observations"][0]["observed_at"] = "2026-09-09T07:50:00Z"
    payload["observations"][1]["observed_at"] = "2026-09-09T07:51:00Z"
    write(Path(binding["observation_path"]), payload)
    result = job._make_shadow_reader(binding, identity, REVISION)(proposal)
    assert result["status"] == "failed" and "observation" not in result


def test_required_ci_runs_the_complete_cn_slice_in_an_isolated_pinned_environment():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert "python3 -m pytest tests ops/quant-monitor/tests -q --ignore=tests/test_run_cn_index_etf_research.py" in text
    assert "  cn-research:\n" not in text
    research = text.split("      - name: Install the isolated CN research dependency set", 1)[1]
    assert "python3 -m venv" in research and "--system-site-packages" not in research
    assert research in text.split("  test:\n", 1)[1]
    assert "cn-equity-strategies[research] @ git+https://github.com/QuantStrategyLab/CnEquityStrategies.git@2a0c5c9aafacfbe6519fb4029ca4ac18e4996a66" in research
    assert '"${RUNNER_TEMP}/aab-cn-research/bin/python" -m pip check' in research
    assert '"tests/test_run_cn_index_etf_research.py"' in research
    assert "socket.socket.connect = blocked" in research
    assert "quant-strategy-plugins" not in research
    assert "continue-on-error" not in research and "if:" not in research
    # Missing optional packages must fail this dedicated job, never skip it.
    assert "importorskip" not in Path(__file__).read_text().split("def test_required_ci_runs_", 1)[0]


@pytest.mark.parametrize("computed_at", ["2026-09-07T01:25:00Z", "2026-09-07T07:30:00Z"])
def test_shadow_candidate_must_exist_before_first_session_auction(shadow_inputs, computed_at):
    binding, identity, proposal, _ = shadow_inputs
    proposal.computed_at = computed_at
    result = job._make_shadow_reader(binding, identity, REVISION)(proposal)
    assert result["status"] == "failed" and "observation" not in result
