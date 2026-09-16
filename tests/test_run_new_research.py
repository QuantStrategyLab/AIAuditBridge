from __future__ import annotations

from datetime import datetime, timezone
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from quant_platform_kit.research_factory import ResearchSourceReceipt, ResearchWorkerManifest, ResearchWorkerRole
from quant_platform_kit.strategy_lifecycle.contracts import OptimizationProposal
from scripts.run_new_research import (
    NewResearchInputError, SOXL_RSI2_CODEGEN_SOURCE_URLS, _LOCAL_STRATEGY_FACTS,
    fetch_soxl_rsi2_codegen_sources, run_request, soxl_rsi2_codegen,
    validate_soxl_rsi2_codegen_change, _codex_codegen_execute,
    run_trusted_soxl_rsi2_dual_window, run_soxl_rsi2_codegen_case, main,
    _codegen_prompt, _project_validated_research_artifact,
)


def _receipt() -> dict:
    value = ResearchSourceReceipt(
        schema_version="research_source_receipt.v1", source_id="soxl-bars",
        source_url="https://example.test/soxl.csv", publisher="example",
        retrieved_at=datetime(2026, 9, 1, tzinfo=timezone.utc), content_sha256="a" * 64,
        declared_license="CC-BY-4.0", usage_scope="candidate_copy", license_review_id="review-1",
    )
    return value.to_dict()


def _codegen_core() -> str:
    return (
        "from typing import Any\n\n"
        "def _rsi2_research_target(*, held: bool, lagged_rsi: float | None, entry_threshold: float, prior_closes: tuple[float, ...]) -> bool:\n"
        "    if not held:\n"
        "        return lagged_rsi is not None and lagged_rsi <= entry_threshold\n"
        "    if lagged_rsi is not None and lagged_rsi >= 70.0:\n"
        "        return False\n"
        "    return True\n"
        "\nTAIL = 1\n"
    )


def _init_codegen_fixture_git(root: Path) -> None:
    (root / "src/us_equity_strategies/__init__.py").write_text("", encoding="utf-8")
    (root / "src/us_equity_strategies/research/__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.test",
         "commit", "-qm", "base"], cwd=root, check=True,
    )


def test_codegen_sources_are_fixed_bounded_and_citation_only(monkeypatch):
    class Response:
        def __init__(self, url, body): self.url, self.body = url, body
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def geturl(self): return self.url
        def read(self, _limit): return self.body
    bodies = {url: f"fixture:{url}".encode() for url in SOXL_RSI2_CODEGEN_SOURCE_URLS}
    class Opener:
        def open(self, request, timeout):
            assert timeout == 15
            return Response(request.full_url, bodies[request.full_url])
    monkeypatch.setattr("urllib.request.build_opener", lambda _handler: Opener())
    receipts = fetch_soxl_rsi2_codegen_sources(retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert [item["source_url"] for item in receipts] == list(SOXL_RSI2_CODEGEN_SOURCE_URLS)
    assert all(item["usage_scope"] == "citation_or_summary" for item in receipts)


def test_codegen_prompt_requires_bounded_objective_or_no_changes():
    prompt = _codegen_prompt({"fixed.py": "return True"}, [], "只评估 RSI2 候选的已有证据")
    assert "RESEARCH_OBJECTIVE" in prompt
    assert "只评估 RSI2 候选的已有证据" in prompt
    assert "不得发明策略假说" in prompt
    assert "changes=[]" in _codegen_prompt({"fixed.py": "return True"}, [])


def test_codegen_missing_objective_never_fetches_or_executes(tmp_path, monkeypatch):
    root = tmp_path / "ues"
    (root / "src/us_equity_strategies/research").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/us_equity_strategies/research/soxl_core_optimization.py").write_text(_codegen_core(), encoding="utf-8")
    (root / "tests/test_soxl_rsi2_mean_reversion.py").write_text("# test\n", encoding="utf-8")
    _init_codegen_fixture_git(root)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    monkeypatch.setattr("scripts.run_new_research.fetch_soxl_rsi2_codegen_sources", lambda **_: pytest.fail("must not fetch"))
    result = soxl_rsi2_codegen(
        ues_repo_root=root, approved_commit=commit, source_ref="a" * 40,
        run_root=tmp_path / "run", execute=lambda _: pytest.fail("must not execute"),
    )
    assert result["reason"] == "research_objective_missing"


def test_codegen_case_missing_objective_returns_before_materialize_or_source_read(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.run_new_research.read_soxl_ues_provenance", lambda *_args, **_kwargs: pytest.fail("must not read source"))
    result = run_soxl_rsi2_codegen_case(
        p1_root=tmp_path / "missing-p1", ues_repo_root=tmp_path / "missing-ues",
        run_root=tmp_path / "run", source_ref="a" * 40, research_objective="  ",
    )
    assert result["reason"] == "research_objective_missing"
    assert not (tmp_path / "run").exists()


def test_codegen_case_missing_objective_preserves_existing_run_identity(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "codegen_case_input.json").write_text(
        json.dumps({"research_objective": "objective A"}), encoding="utf-8",
    )
    with pytest.raises(NewResearchInputError, match="research_objective_mismatch"):
        run_soxl_rsi2_codegen_case(
            p1_root=tmp_path / "missing-p1", ues_repo_root=tmp_path / "missing-ues",
            run_root=run_root, source_ref="a" * 40,
        )


def test_codegen_objective_resume_and_saved_input_binding(tmp_path, monkeypatch):
    root = tmp_path / "ues"
    (root / "src/us_equity_strategies/research").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/us_equity_strategies/research/soxl_core_optimization.py").write_text(_codegen_core(), encoding="utf-8")
    (root / "tests/test_soxl_rsi2_mean_reversion.py").write_text("# test\n", encoding="utf-8")
    _init_codegen_fixture_git(root)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    monkeypatch.setattr("scripts.run_new_research.fetch_soxl_rsi2_codegen_sources", lambda **_: [])
    response = types.SimpleNamespace(success=True, provider="codex", raw={"status": "succeeded", "provider": "codex", "research_stage": "optimization"}, output=json.dumps({"final_message": "none", "changes": []}))
    run_root = tmp_path / "run"
    soxl_rsi2_codegen(ues_repo_root=root, approved_commit=commit, source_ref="a" * 40,
                      run_root=run_root, research_objective="A", execute=lambda _prompt: response)
    with pytest.raises(NewResearchInputError, match="research_objective_mismatch"):
        soxl_rsi2_codegen(ues_repo_root=root, approved_commit=commit, source_ref="a" * 40,
                          run_root=run_root, research_objective="B")
    (run_root / "codegen_input.json").unlink()
    with pytest.raises(NewResearchInputError, match="codegen_saved_input_missing"):
        soxl_rsi2_codegen(ues_repo_root=root, approved_commit=commit, source_ref="a" * 40,
                          run_root=run_root, research_objective="A")


def test_codegen_projects_only_verified_rsi2_artifact_fields(tmp_path, monkeypatch):
    import us_equity_strategies.research.soxl_core_optimization as core
    output = tmp_path / "output"
    output.mkdir()
    source_commit = "a" * 40
    source_blobs = {path: value * 40 for path, value in zip(core._REQUIRED_SOURCE_PATHS, ("b", "c", "d"))}
    metric = {"cagr": (0.12).hex(), "cumulative_return": (0.35).hex(), "max_drawdown": (-0.2).hex(), "expected_shortfall_95": (-0.1).hex(), "turnover": (1.2).hex()}
    artifact_value = {
        "schema": "qsl.research.soxl_rsi2_mean_reversion.v1",
        "outcome": "CHARACTERIZATION_CANDIDATE_FOUND", "evidence_valid": True,
        "evidence_gates": {"no_lookahead": True}, "locked_winner": "RSI2_ENTRY_5_EXIT_70",
        "soxx_drawdown_comparison": {"final_holdout_max_drawdown": -0.2},
        "validation_metrics_c2_5": {"private": "must not leak"},
        "notes": "must not leak",
        "post_lock_metrics": {"RSI2_ENTRY_5_EXIT_70": {"FINAL_HOLDOUT": {"C2_5": metric, "C5_10_STRESS": metric}}, "UNSCALED_SMA200": {"FINAL_HOLDOUT": {"C2_5": metric, "C5_10_STRESS": metric}}},
    }
    bundle = core._canonical_bytes(artifact_value)
    (output / "soxl_rsi2_mean_reversion_v1.json").write_bytes(bundle)
    (output / "soxl_rsi2_mean_reversion_v1.sha256").write_text(hashlib.sha256(bundle).hexdigest() + "\n", encoding="utf-8")
    (output / "soxl_rsi2_mean_reversion_v1.readback.json").write_text(json.dumps({
        "schema": core.RSI2_MEAN_REVERSION_READBACK_SCHEMA, "bundle_sha256": hashlib.sha256(bundle).hexdigest(), "bundle_bytes": len(bundle), "source_commit": source_commit, "source_blobs": source_blobs, "result_digest": core._digest(artifact_value),
    }), encoding="utf-8")
    projected = _project_validated_research_artifact(output, source_commit=source_commit, source_blobs=source_blobs)
    assert projected["outcome"] == "CHARACTERIZATION_CANDIDATE_FOUND"
    assert projected["financial_comparison"]["candidate_final_holdout"]["C2_5"]["cagr"] == 0.12
    (output / "soxl_rsi2_mean_reversion_v1.json").write_bytes(b"tampered")
    with pytest.raises(NewResearchInputError):
        _project_validated_research_artifact(output, source_commit=source_commit, source_blobs=source_blobs)
    assert "validation_metrics_c2_5" not in projected
    assert "notes" not in projected


def test_codegen_helper_validator_rejects_signature_and_outside_bytes_changes():
    original = _codegen_core()
    updated = original.replace("return True", "return bool(True)")
    with pytest.raises(NewResearchInputError, match="codegen_helper_expression_invalid"):
        validate_soxl_rsi2_codegen_change(
            "src/us_equity_strategies/research/soxl_core_optimization.py", original, updated,
        )
    malicious = original.replace("entry_threshold: float", "entry_threshold: float = evil()")
    with pytest.raises(NewResearchInputError, match="codegen_helper_signature_changed"):
        validate_soxl_rsi2_codegen_change(
            "src/us_equity_strategies/research/soxl_core_optimization.py", original, malicious,
        )
    outside = original.replace("TAIL = 1", "TAIL = 2")
    with pytest.raises(NewResearchInputError, match="codegen_core_bytes_outside_helper_changed"):
        validate_soxl_rsi2_codegen_change(
            "src/us_equity_strategies/research/soxl_core_optimization.py", original, outside,
        )


def test_soxl_rsi2_codegen_uses_fixed_gateway_contract_and_can_stop_empty(tmp_path, monkeypatch):
    root = tmp_path / "ues"
    (root / "src/us_equity_strategies/research").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/us_equity_strategies/research/soxl_core_optimization.py").write_text(_codegen_core(), encoding="utf-8")
    (root / "tests/test_soxl_rsi2_mean_reversion.py").write_text("# fixed test\n", encoding="utf-8")
    for relative in (
        "src/us_equity_strategies/research/soxl_soxx_offline_input_contract.py",
        "src/us_equity_strategies/research/soxl_soxx_typed_baseline_result.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# provenance\n", encoding="utf-8")
    _init_codegen_fixture_git(root)
    approved_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    bodies = {url: f"UNTRUSTED_BODY:{url}".encode() for url in SOXL_RSI2_CODEGEN_SOURCE_URLS}
    class Response:
        def __init__(self, url): self.url = url
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def geturl(self): return self.url
        def read(self, _limit): return bodies[self.url]
    class Opener:
        def open(self, request, timeout): return Response(request.full_url)
    monkeypatch.setattr("urllib.request.build_opener", lambda _handler: Opener())
    captured = {}
    response = types.SimpleNamespace(
        success=True, provider="codex", raw={"status": "succeeded", "provider": "codex", "research_stage": "optimization"},
        output=json.dumps({"final_message": "no patch", "changes": []}),
    )
    def execute(prompt):
        captured["prompt"] = prompt
        return response
    result = soxl_rsi2_codegen(
        ues_repo_root=root, source_ref="a" * 40,
        approved_commit=approved_commit,
        retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        execute=execute, research_objective="bounded RSI2 objective",
    )
    assert result["status"] == "no_changes"
    assert "UNTRUSTED_BODY" not in captured["prompt"]
    assert hashlib.sha256(
        _codegen_core().encode("utf-8")
    ).hexdigest() in captured["prompt"]
    assert result["source_receipts"][0]["usage_scope"] == "citation_or_summary"


def test_default_codegen_callback_uses_codex_medium_research_contract(monkeypatch):
    calls = []
    config = types.SimpleNamespace(research_providers=("codex",))
    class FakeClient:
        def __init__(self, received):
            assert received is config
        def execute(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return "response"
    fake_module = types.ModuleType("ai_gateway_client")
    fake_module.GatewayConfig = types.SimpleNamespace(from_env=lambda: config)
    fake_module.AiGatewayClient = FakeClient
    monkeypatch.setitem(sys.modules, "ai_gateway_client", fake_module)
    callback = _codex_codegen_execute(source_ref="a" * 40, research_objective="bounded objective")
    assert callback("bounded prompt") == "response"
    prompt, kwargs = calls[0]
    assert prompt == "bounded prompt"
    assert kwargs == {
        "task": "soxl_rsi2_research_codegen", "mode": "review_only",
        "model": "gpt-5.6-luna", "complexity": "medium",
        "research_stage": "optimization", "reasoning_effort": "medium",
        "sandbox": "read-only", "allowed_providers": ["codex"],
        "source_repository": "QuantStrategyLab/AIAuditBridge",
        "source_ref": "a" * 40, "research_objective": "bounded objective", "timeout": 1800,
    }


def test_fixed_codegen_case_builds_optimization_only_payload_and_reuses_run_root(tmp_path, monkeypatch):
    import us_equity_strategies.research.soxl_alpaca_input_adapter as alpaca
    import us_equity_strategies.research.soxl_rsi2_research_adapter as adapter
    p1_root = tmp_path / "p1"
    ues_root = tmp_path / "ues"
    p1_root.mkdir()
    ues_root.mkdir()
    paths = types.SimpleNamespace(
        manifest=tmp_path / "manifest.json", artifact=tmp_path / "artifact.json", readback=tmp_path / "readback.json",
    )
    for path in (paths.manifest, paths.artifact, paths.readback):
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "scripts.run_new_research.read_soxl_ues_provenance",
        lambda _root, expected_commit: (expected_commit, {"a": "a" * 40, "b": "b" * 40, "c": "c" * 40}),
    )
    monkeypatch.setattr(alpaca, "materialize_soxl_alpaca_input", lambda *_args, **_kwargs: paths)
    monkeypatch.setattr(adapter, "load_rsi2_offline_input", lambda _paths: types.SimpleNamespace(
        source_revision="source-v1", input_digest="input-v1",
    ))
    captured = []
    monkeypatch.setattr(
        "scripts.run_new_research.soxl_rsi2_codegen",
        lambda **kwargs: captured.append(kwargs) or {"status": "parked"},
    )
    result = run_soxl_rsi2_codegen_case(
        p1_root=p1_root, ues_repo_root=ues_root, run_root=tmp_path / "run", source_ref="a" * 40,
        research_objective="bounded RSI2 objective",
    )
    assert result == {"status": "parked"}
    payload = captured[0]["research_payload"]
    assert payload["research_identity"]["code_revision"] == "86aa4e03c30eb2fb561748d6e9c22e68d3267cfa"
    assert payload["request"]["research_intent"] == "bounded_template_selection"
    assert payload["input_paths"]["manifest"].endswith("manifest.json")
    assert captured[0]["run_root"] == tmp_path / "run"
    run_soxl_rsi2_codegen_case(
        p1_root=p1_root, ues_repo_root=ues_root, run_root=tmp_path / "run", source_ref="a" * 40,
        research_objective="bounded RSI2 objective",
    )
    assert captured[1]["research_payload"]["request"] == payload["request"]
    assert captured[1]["research_payload"]["source_receipts"] == payload["source_receipts"]


def test_codegen_cli_requires_fixed_case_roots(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["--soxl-rsi2-codegen", "--output", str(tmp_path / "result.json")])
    assert error.value.code == 2


def test_codegen_cli_persists_sanitized_gateway_failure(tmp_path, monkeypatch):
    from scripts import run_new_research as module
    monkeypatch.setattr(module, "run_soxl_rsi2_codegen_case", lambda **_kwargs: (_ for _ in ()).throw(
        NewResearchInputError("codegen_gateway_deferred")
    ))
    output = tmp_path / "result.json"
    assert main([
        "--soxl-rsi2-codegen", "--p1-root", str(tmp_path),
        "--ues-repo-root", str(tmp_path), "--run-root", str(tmp_path / "run"),
        "--output", str(output),
    ]) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved == {
        "status": "deferred", "reason": "codegen_gateway_deferred",
        "research_executed": False, "learning_only": True,
        "no_order": True, "promotion_eligible": False,
        "live_authority_granted": False,
    }


def test_soxl_rsi2_codegen_validates_patch_in_git_archive_candidate(tmp_path, monkeypatch):
    root = tmp_path / "ues"
    (root / "src/us_equity_strategies/research").mkdir(parents=True)
    (root / "tests").mkdir()
    core = _codegen_core()
    (root / "src/us_equity_strategies/research/soxl_core_optimization.py").write_text(core, encoding="utf-8")
    (root / "tests/test_soxl_rsi2_mean_reversion.py").write_text("# fixed test\n", encoding="utf-8")
    for relative in (
        "src/us_equity_strategies/research/soxl_soxx_offline_input_contract.py",
        "src/us_equity_strategies/research/soxl_soxx_typed_baseline_result.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# provenance\n", encoding="utf-8")
    _init_codegen_fixture_git(root)
    approved_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    opener = type("Opener", (), {"open": lambda self, request, timeout: type("Response", (), {
        "__enter__": lambda self: self, "__exit__": lambda self, *_args: None,
        "geturl": lambda self: request.full_url, "read": lambda self, _limit: b"fixture",
    })()})()
    monkeypatch.setattr("urllib.request.build_opener", lambda _handler: opener)
    helper_start = core.index("def _rsi2_research_target")
    helper_end = core.index("\nTAIL = 1")
    old = core[helper_start:helper_end]
    new = old.replace("return True", "return not False")
    response = types.SimpleNamespace(
        success=True, provider="codex", raw={"status": "succeeded", "provider": "codex", "research_stage": "optimization"},
        output=json.dumps({"final_message": "bounded patch", "changes": [{
            "path": "src/us_equity_strategies/research/soxl_core_optimization.py",
            "base_sha256": hashlib.sha256(core.encode()).hexdigest(),
            "edits": [{"old": old, "new": new}],
        }]}),
    )
    input_paths = {}
    for key in ("manifest", "artifact", "readback"):
        path = tmp_path / f"{key}.json"
        path.write_text("{}", encoding="utf-8")
        input_paths[key] = str(path)
    research_payload = {"input_paths": input_paths, "request": {"as_of": "2026-09-15"},
                        "research_identity": {"input_revision": "input-v1"}}
    captured_research = {}
    def research_runner(_candidate, **kwargs):
        captured_research.update(kwargs)
        return {"status": "parked", "live_authority_granted": False}
    result = soxl_rsi2_codegen(
        ues_repo_root=root, source_ref="a" * 40,
        approved_commit=approved_commit,
        retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        execute=lambda _prompt: response,
        candidate_test_runner=lambda _candidate, **_kwargs: {"status": "passed", "execution_isolation": "mock"},
        research_payload=research_payload, run_root=tmp_path / "run",
        candidate_research_runner=research_runner, research_objective="bounded RSI2 objective",
    )
    assert result["status"] == "patch_validated"
    assert result["integration_status"] == "research_completed"
    assert result["live_authority_granted"] is False
    assert len(result["source_commit"]) == 40
    assert captured_research["payload"]["source_commit"] == result["source_commit"]
    assert captured_research["payload"]["source_blobs"] == result["source_blobs"]
    assert captured_research["payload"]["research_identity"]["code_revision"] == result["source_commit"]


def test_codegen_candidate_tests_runs_two_locked_down_docker_containers(tmp_path, monkeypatch):
    from scripts import run_new_research as module
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root in (baseline, candidate):
        (root / "tests").mkdir(parents=True)
        (root / "src").mkdir()
    (baseline / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n", encoding="utf-8")
    (baseline / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    calls = []
    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: (calls.append(command) or Completed()))
    result = module._run_codegen_candidate_tests(candidate, baseline_root=baseline)
    runs = [call for call in calls if call[1] == "run"]
    assert result["execution_isolation"] == "docker"
    assert len(runs) == 2
    for command in runs:
        assert "--network=none" in command
        assert "--read-only" in command
        assert "--cap-drop=ALL" in command
        assert "--security-opt=no-new-privileges" in command
        assert "-v" in command and any(item.endswith(":/workspace:ro") for item in command)
    assert f"{candidate}:/workspace:ro" in runs[0]
    assert f"{candidate}:/workspace:ro" in runs[1]
    assert any(item.endswith(":/trusted/test_soxl_rsi2_mean_reversion.py:ro") for item in runs[0])
    assert not any(item.endswith(":/trusted/test_soxl_rsi2_mean_reversion.py:ro") for item in runs[1])


def test_codegen_candidate_tests_stops_after_baseline_failure(tmp_path, monkeypatch):
    from scripts import run_new_research as module
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root in (baseline, candidate):
        (root / "tests").mkdir(parents=True)
        (root / "src").mkdir()
    (baseline / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n", encoding="utf-8")
    (baseline / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    calls = []
    class Completed:
        stdout = ""
        stderr = ""
        def __init__(self, returncode): self.returncode = returncode
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/docker")
    def fake_run(command, **kwargs):
        calls.append(command)
        return Completed(1 if command[1] == "run" and sum(item[1] == "run" for item in calls) == 1 else 0)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(NewResearchInputError, match="codegen_trusted_tests_failed"):
        module._run_codegen_candidate_tests(candidate, baseline_root=baseline)
    assert sum(command[1] == "run" for command in calls) == 1


def test_codegen_research_runner_is_docker_only_and_reentrant(tmp_path, monkeypatch):
    from scripts import run_new_research as module
    monkeypatch.setattr(module, "_project_validated_research_artifact", lambda *_args, **_kwargs: {})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n", encoding="utf-8")
    (candidate / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    inputs = {}
    for key in ("manifest", "artifact", "readback"):
        path = tmp_path / f"{key}.json"
        path.write_text("{}", encoding="utf-8")
        inputs[key] = str(path)
    payload = {
        "source_commit": "a" * 40,
        "source_blobs": {"core": "b" * 40, "input": "c" * 40, "baseline": "d" * 40},
        "input_paths": inputs,
    }
    calls = []
    class Completed:
        returncode = 0
        stdout = json.dumps({"status": "parked", "live_authority_granted": False})
        stderr = ""
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/docker")
    run_root = tmp_path / "run"
    def fake_run(command, **kwargs):
        calls.append(command)
        if "run" in command:
            output = run_root / "codegen-research-output"
            output.mkdir(parents=True, exist_ok=True)
            for name in (
                "soxl_rsi2_mean_reversion_v1.json",
                "soxl_rsi2_mean_reversion_v1.sha256",
                "soxl_rsi2_mean_reversion_v1.readback.json",
            ):
                (output / name).write_text("{}", encoding="utf-8")
        return Completed()
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module._run_codegen_candidate_research(
        candidate, payload=payload, run_root=run_root, source_commit="a" * 40,
    )
    assert result["execution_isolation"] == "docker"
    assert any(command[1] == "build" for command in calls)
    run_command = next(command for command in calls if command[1] == "run")
    assert "--network=none" in run_command
    assert "--read-only" in run_command
    assert "--cap-drop=ALL" in run_command
    assert "--security-opt=no-new-privileges" in run_command
    assert f"{candidate}:/workspace:ro" in run_command
    assert any(item.endswith(":/aab:ro") for item in run_command)
    assert "/Users/lisiyi/Projects/.worktrees/aab-rsi2-codegen-20260915:/aab:ro" not in run_command
    call_count = len(calls)
    assert module._run_codegen_candidate_research(
        candidate, payload=payload, run_root=run_root, source_commit="a" * 40,
    ) == result
    assert len(calls) == call_count
    changed = dict(payload)
    changed["source_blobs"] = {**payload["source_blobs"], "core": "e" * 40}
    with pytest.raises(NewResearchInputError, match="saved_input_mismatch"):
        module._run_codegen_candidate_research(
            candidate, payload=changed, run_root=run_root, source_commit="a" * 40,
        )


def test_codegen_research_failed_cache_rethrows_without_retry(tmp_path, monkeypatch):
    from scripts import run_new_research as module
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n", encoding="utf-8")
    (candidate / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    inputs = {}
    for key in ("manifest", "artifact", "readback"):
        path = tmp_path / f"{key}.json"
        path.write_text("{}", encoding="utf-8")
        inputs[key] = str(path)
    payload = {
        "source_commit": "a" * 40,
        "source_blobs": {"core": "b" * 40, "input": "c" * 40, "baseline": "d" * 40},
        "input_paths": inputs,
    }
    run_root = tmp_path / "run"
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(NewResearchInputError, match="codegen_docker_unavailable"):
        module._run_codegen_candidate_research(
            candidate, payload=payload, run_root=run_root, source_commit="a" * 40,
        )
    saved = json.loads((run_root / "codegen_research_result.json").read_text(encoding="utf-8"))
    assert saved == {
        "status": "failed", "reason": "codegen_docker_unavailable",
        "source_commit": "a" * 40, "execution_isolation": "docker",
    }
    assert not (run_root / "codegen_result.json").exists()

    with pytest.raises(NewResearchInputError, match="codegen_docker_unavailable"):
        module._run_codegen_candidate_research(
            candidate, payload=payload, run_root=run_root, source_commit="a" * 40,
        )
    assert calls == []

    saved["reason"] = []
    (run_root / "codegen_research_result.json").write_text(
        json.dumps(saved, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    with pytest.raises(NewResearchInputError, match="codegen_research_saved_result_invalid"):
        module._run_codegen_candidate_research(
            candidate, payload=payload, run_root=run_root, source_commit="a" * 40,
        )
    assert calls == []


def test_codegen_docker_integration_fixture(tmp_path, monkeypatch):
    """Opt-in CI fixture: real Docker, UES optimizer and QPK cycle, no model/network."""
    if os.environ.get("AAB_RUN_DOCKER_INTEGRATION") != "1":
        pytest.skip("Docker integration is opt-in and runs only in the dedicated CI step")
    from scripts import run_new_research as module
    def synthetic_receipts(**_kwargs):
        value = ResearchSourceReceipt(
            schema_version="research_source_receipt.v1",
            source_id="synthetic-soxl-codegen",
            source_url=module.SOXL_RSI2_CODEGEN_SOURCE_URLS[0],
            publisher="synthetic-fixture",
            retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
            content_sha256=hashlib.sha256(b"synthetic-fixture").hexdigest(),
            declared_license=None, usage_scope="citation_or_summary",
            license_review_id=None, untrusted=True,
        )
        return [value.to_dict()]
    monkeypatch.setattr(module, "fetch_soxl_rsi2_codegen_sources", synthetic_receipts)
    approved_root = Path(os.environ["AAB_SOXL_APPROVED_REPO"]).resolve()
    approved_commit = os.environ["AAB_SOXL_APPROVED_COMMIT"]
    assert subprocess.check_output(
        ["git", "-C", str(approved_root), "rev-parse", "HEAD"], text=True,
    ).strip() == approved_commit
    from us_equity_strategies.research.soxl_alpaca_input_adapter import materialize_soxl_alpaca_input
    from us_equity_strategies.research.soxl_rsi2_research_adapter import (
        _COST_MODEL_REVISION, _PARAM_SPACE_REVISION, _VALIDATOR_REVISION,
        load_rsi2_offline_input, Rsi2OfflineInputPaths,
    )
    source_root = _alpaca_source_root(tmp_path)
    paths = materialize_soxl_alpaca_input(
        source_root, tmp_path / "alpaca-pack", start="2022-01-03",
        end_exclusive="2024-01-26", expected_sessions=753,
    )
    source = load_rsi2_offline_input(Rsi2OfflineInputPaths(
        paths.manifest, paths.artifact, paths.readback,
    ))
    core = (approved_root / "src/us_equity_strategies/research/soxl_core_optimization.py").read_text(encoding="utf-8")
    start = core.index("def _rsi2_research_target")
    end = core.index("\ndef ", start + 1)
    old = core[start:end]
    new = old.replace("return True", "return not False")
    response = types.SimpleNamespace(
        success=True, provider="codex",
        raw={"status": "succeeded", "provider": "codex", "research_stage": "optimization"},
        output=json.dumps({"final_message": "synthetic Docker fixture", "changes": [{
            "path": "src/us_equity_strategies/research/soxl_core_optimization.py",
            "base_sha256": hashlib.sha256(core.encode()).hexdigest(),
            "edits": [{"old": old, "new": new}],
        }]}),
    )
    blobs = {
        path: subprocess.check_output(
            ["git", "-C", str(approved_root), "rev-parse", f"HEAD:{path}"], text=True,
        ).strip()
        for path in module._SOXL_PROVENANCE_PATHS
    }
    payload = _payload(tmp_path)
    payload.update({
        "source_commit": approved_commit, "source_blobs": blobs,
        "request": {
            **payload["request"],
            "as_of": datetime.now(timezone.utc).date().isoformat(),
            "source_revision": source.source_revision,
        },
        "research_identity": {
            "code_revision": approved_commit, "input_revision": source.input_digest,
            "param_space_revision": _PARAM_SPACE_REVISION,
            "cost_model_revision": _COST_MODEL_REVISION,
            "validator_revision": _VALIDATOR_REVISION,
        },
        "input_paths": {"manifest": str(paths.manifest), "artifact": str(paths.artifact), "readback": str(paths.readback)},
        "output_root": str(tmp_path / "output"), "ticket_dir": str(tmp_path / "tickets"),
        "ues_repo_root": str(approved_root),
    })
    run_root = tmp_path / "codegen-run"
    first = module.soxl_rsi2_codegen(
        ues_repo_root=approved_root, approved_commit=approved_commit,
        source_ref="e" * 40, execute=lambda _prompt: response,
        research_payload={**payload, "research_objective": "bounded RSI2 objective"}, run_root=run_root,
        research_objective="bounded RSI2 objective",
    )
    assert first["status"] == "patch_validated"
    assert first["research_result"]["live_authority_granted"] is False
    assert first["source_commit"] != approved_commit
    assert len(first["source_blobs"]) == 3
    output_root = run_root / "codegen-research-output"
    artifact = output_root / "soxl_rsi2_mean_reversion_v1.json"
    readback = output_root / "soxl_rsi2_mean_reversion_v1.readback.json"
    sidecar = output_root / "soxl_rsi2_mean_reversion_v1.sha256"
    assert artifact.is_file()
    assert readback.is_file()
    assert sidecar.is_file()
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    persisted_readback = json.loads(readback.read_text(encoding="utf-8"))
    assert isinstance(persisted, dict)
    assert persisted_readback["source_commit"] == first["source_commit"]
    assert persisted_readback["source_blobs"] == first["source_blobs"]
    output_bytes = {
        path.name: path.read_bytes() for path in (artifact, sidecar, readback)
    }
    monkeypatch.setattr(module, "_run_codegen_candidate_tests", lambda *_args, **_kwargs: pytest.fail("re-entry must not start Docker tests"))
    monkeypatch.setattr(module, "_run_codegen_candidate_research", lambda *_args, **_kwargs: pytest.fail("re-entry must not start Docker research"))
    second = module.soxl_rsi2_codegen(
        ues_repo_root=approved_root, approved_commit=approved_commit,
        source_ref="e" * 40, execute=lambda _prompt: pytest.fail("re-entry must not call model"),
        research_payload={**payload, "research_objective": "bounded RSI2 objective"}, run_root=run_root,
        research_objective="bounded RSI2 objective",
    )
    assert second == first
    assert {path.name: path.read_bytes() for path in (artifact, sidecar, readback)} == output_bytes


def _payload(tmp_path):
    worker = ResearchWorkerManifest.expected(worker_id="worker-1", role=ResearchWorkerRole.PLANNER_BUILDER)
    return {
        "request": {"strategy_profile": "soxl_rsi2_mean_reversion", "domain": "us_equity",
                    "as_of": "2026-09-08", "source_revision": "source-v1",
                    "research_intent": "bounded_template_selection"},
        "worker_manifest": {"schema_version": worker.schema_version, "worker_id": worker.worker_id,
                            "role": worker.role.value, "capabilities": sorted(worker.capabilities),
                            "secret_access": False, "broker_access": False,
                            "cloud_runtime_access": False, "deployment_write_access": False},
        "source_receipts": [_receipt()],
        "research_identity": {key: f"{key}-v1" for key in ("code_revision", "input_revision", "param_space_revision", "cost_model_revision", "validator_revision")},
        "source_commit": "a" * 40,
        "source_blobs": {"study.py": "b" * 40},
        "strategy_facts": _LOCAL_STRATEGY_FACTS,
        "caller_ref": "c" * 40,
        "input_paths": {"manifest": "manifest.json", "artifact": "artifact.csv", "readback": "readback.json"},
        "output_root": str(tmp_path / "output"), "ticket_dir": str(tmp_path / "tickets"),
    }


def _install_adapter(monkeypatch, *, prepare=None):
    package = types.ModuleType("us_equity_strategies")
    research = types.ModuleType("us_equity_strategies.research")
    adapter = types.ModuleType("us_equity_strategies.research.soxl_rsi2_research_adapter")
    class Paths:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    def factory(**_kwargs):
        def optimize(request, _budget):
            assert request.strategy_profile == "soxl_rsi2_mean_reversion"
            return OptimizationProposal(strategy_profile=request.strategy_profile, domain=request.domain,
                                        proposed_params={"candidate_id": "UNSCALED_SMA200"}, recommendation="research_candidate")
        return optimize
    adapter.Rsi2OfflineInputPaths = Paths
    adapter.load_rsi2_offline_input = lambda _paths: object()
    adapter._validate_research_identity = lambda _source, _commit, _identity: None
    adapter.make_soxl_rsi2_optimize = factory
    adapter.prepare_soxl_rsi2_promotion = prepare or (
        lambda *, optimization_source, source_commit, research_identity,
        promotion_binding, promotion_store, promotion_shadow_recorder: (
            dict(research_identity),
            lambda _proposal: None,
            lambda _proposal: {"status": "pending", "passed": False, "no_order": True,
                               "live_authority_granted": False},
        )
    )
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, research.__name__, research)
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)


def _freeze_qpk_clock(monkeypatch):
    import quant_platform_kit.strategy_lifecycle.research_promotion_cycle as qpk_cycle

    monkeypatch.setattr(
        qpk_cycle, "_clock_now",
        lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
    )


def test_new_request_runs_existing_qpk_cycle_and_parks_without_promotion(tmp_path, monkeypatch):
    _install_adapter(monkeypatch)
    _freeze_qpk_clock(monkeypatch)
    result = run_request(_payload(tmp_path), diagnose=lambda _context, _budget: {
        "optimization_needed": True, "design": "固定模板研究设计"
    })
    assert result["status"] == "parked"
    assert result["notes"] == ["promotion_backtest_gate_failed", "missing_promotion_backtest_evidence"]
    assert result["drift_score"] is None and result["drift_status"] == "new_research"
    assert result["live_authority_granted"] is False


def test_invalid_worker_or_receipt_fails_before_adapter(tmp_path):
    payload = _payload(tmp_path)
    payload["worker_manifest"]["secret_access"] = True
    with pytest.raises(NewResearchInputError, match="worker_manifest_invalid"):
        run_request(payload)
    payload = _payload(tmp_path)
    payload["source_receipts"][0]["receipt_sha256"] = "c" * 64
    with pytest.raises(NewResearchInputError, match="source_receipt_invalid"):
        run_request(payload)


def test_default_entry_requires_verified_adapter_facts_and_caller_ref(tmp_path, monkeypatch):
    _install_adapter(monkeypatch)
    payload = _payload(tmp_path)
    payload.pop("strategy_facts")
    payload.pop("caller_ref")
    with pytest.raises(NewResearchInputError, match="strategy_facts_or_caller_ref_invalid"):
        run_request(payload)


def test_formal_binding_is_passed_to_ues_prepare_and_identity_is_used(tmp_path, monkeypatch):
    calls = []
    _freeze_qpk_clock(monkeypatch)

    def prepare(**kwargs):
        calls.append(kwargs)
        return ({**kwargs["research_identity"], "input_revision": "formal-root"},
                lambda _proposal: {"status": "PASS", "evidence_kind": "formal_backtest"},
                lambda _proposal: {"status": "pending", "passed": False, "no_order": True,
                                   "live_authority_granted": False})

    _install_adapter(monkeypatch, prepare=prepare)
    binding = object()
    store = object()
    def recorder(_proposal):
        return {"status": "pending", "passed": False, "no_order": True,
                "live_authority_granted": False}
    result = run_request(_payload(tmp_path), diagnose=lambda _context, _budget: {
        "optimization_needed": True, "design": "固定模板研究设计"
    }, promotion_binding=binding, promotion_store=store,
        promotion_shadow_recorder=recorder)
    assert result["status"] == "parked"
    assert calls and calls[0]["optimization_source"] is not None
    assert calls[0]["promotion_binding"] is binding
    assert calls[0]["promotion_store"] is store
    assert calls[0]["promotion_shadow_recorder"] is recorder
    assert result["reason"] == "promotion_cycle_completed"
    assert result["drift_status"] == "new_research"


def test_frozen_proposal_skips_second_optimizer_run(tmp_path, monkeypatch):
    _install_adapter(monkeypatch)
    import us_equity_strategies.research.soxl_rsi2_research_adapter as adapter
    proposal = OptimizationProposal(
        strategy_profile="soxl_rsi2_mean_reversion", domain="us_equity",
        proposed_params={"candidate_id": "UNSCALED_SMA200"}, recommendation="research_candidate",
    )
    monkeypatch.setattr(adapter, "make_soxl_rsi2_optimize", lambda **_: (_ for _ in ()).throw(
        AssertionError("optimizer must not rerun after candidate freeze")
    ))
    result = run_request(
        _payload(tmp_path),
        diagnose=lambda _context, _budget: {"optimization_needed": True, "design": "固定模板研究设计"},
        frozen_proposal=proposal,
    )
    assert result["status"] == "parked"


def test_installed_ues_exposes_formal_promotion_binding_helper():
    from us_equity_strategies.research import soxl_rsi2_research_adapter as adapter

    assert callable(adapter.prepare_soxl_rsi2_promotion)


def _alpaca_source_root(tmp_path: Path, count: int = 753, sessions: list[str] | None = None) -> Path:
    root = tmp_path / "alpaca-p1"
    root.mkdir()
    start = date(2022, 1, 3)
    sessions = sessions or [(start + timedelta(days=index)).isoformat() for index in range(count)]
    cutoff = sessions[-1]

    def canonical(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()

    binding = {
        "schema_version": "qsl.soxl_soxx_core_only_p1_data_binding.v2",
        "data_identity": {
            "provider": "ALPACA_MARKET_DATA", "feed": "SIP",
            "calendar": {"calendar_id": "XNYS", "timezone": "America/New_York", "source": "exchange_calendars:4.13.2:XNYS"},
            "adjustment": {"policy": "total_return_adjusted", "source": "ALPACA_MARKET_DATA adjustment=all(split,dividend,spin-off)"},
            "universe": ["SOXL", "SOXX", "BOXX"], "date_cutoff": cutoff,
        },
    }
    binding_bytes = canonical(binding)
    binding_digest = hashlib.sha256(binding_bytes).hexdigest()
    series = {}
    for symbol, base in (("SOXL", 30.0), ("SOXX", 100.0), ("BOXX", 10.0)):
        rows = []
        for index, session in enumerate(sessions):
            close = base + (index % 7) * 0.2
            rows.append({"session_date": session, "bar": {"open": close, "high": close + 1.0, "low": close - 1.0, "close": close, "volume": 100.0}})
        series[symbol] = rows
    bars = {"schema_version": "qsl.soxl-soxx-core-only-adjusted-ohlcv.v2", "series": series}
    bars_bytes = canonical(bars)
    manifest = {
        "schema_version": "research_input_manifest.v1", "profile": "soxl_soxx_core_only_p2_v3",
        "artifact_type": "immutable_adjusted_ohlcv_etf_only", "observed_at": "2026-01-08T00:00:00Z",
        "calendar": {"calendar_id": "XNYS", "timezone": "America/New_York", "session_date": cutoff, "source_revision": binding_digest},
        "adjustment": {"policy": "total_return_adjusted", "source": "ALPACA_MARKET_DATA adjustment=all(split,dividend,spin-off)", "source_revision": binding_digest},
        "members": [{"path": "bars.json", "media_type": "application/json", "size_bytes": len(bars_bytes), "sha256": hashlib.sha256(bars_bytes).hexdigest()}],
        "sources": [{"source_id": f"alpaca_sip_1day_adjustment_all:{symbol}", "content_sha256": hashlib.sha256(canonical({"schema_version": "qsl.soxl-soxx-core-only-adjusted-ohlcv-source.v1", "symbol": symbol, "sessions": series[symbol]})).hexdigest()} for symbol in ("SOXL", "SOXX", "BOXX")],
    }
    (root / "binding.json").write_bytes(binding_bytes)
    (root / "bars.json").write_bytes(bars_bytes)
    (root / "manifest.json").write_bytes(canonical(manifest))
    return root


def _ues_provenance_repo(tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    repo = tmp_path / "ues-source"
    required = (
        "src/us_equity_strategies/research/soxl_core_optimization.py",
        "src/us_equity_strategies/research/soxl_soxx_offline_input_contract.py",
        "src/us_equity_strategies/research/soxl_soxx_typed_baseline_result.py",
    )
    for relative in required:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic provenance fixture\n", encoding="utf-8")
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "fixture")
    git("add", ".")
    git("commit", "-qm", "synthetic UES provenance")
    commit = git("rev-parse", "HEAD")
    return repo, commit, {path: git("rev-parse", f"HEAD:{path}") for path in required}


def test_installed_ues_alpaca_path_runs_aab_request(tmp_path, monkeypatch):
    _freeze_qpk_clock(monkeypatch)
    from us_equity_strategies.research.soxl_alpaca_input_adapter import (
        load_soxl_alpaca_input, materialize_soxl_alpaca_input,
    )
    from us_equity_strategies.research.soxl_rsi2_research_adapter import (
        _COST_MODEL_REVISION, _PARAM_SPACE_REVISION, _VALIDATOR_REVISION,
        Rsi2OfflineInputPaths, load_rsi2_offline_input,
    )

    paths = materialize_soxl_alpaca_input(
        _alpaca_source_root(tmp_path), tmp_path / "alpaca-pack",
        start="2022-01-03", end_exclusive="2024-01-26", expected_sessions=753,
    )
    source = load_soxl_alpaca_input(paths.manifest, paths.artifact, paths.readback)
    routed = load_rsi2_offline_input(Rsi2OfflineInputPaths(paths.manifest, paths.artifact, paths.readback))
    assert source.input_digest == routed.input_digest
    assert source.source_revision == routed.source_revision
    assert source.source_revision.startswith("alpaca_sip:total_return_adjusted:adjustment=all:")

    provenance_repo, source_commit, source_blobs = _ues_provenance_repo(tmp_path)
    payload = _payload(tmp_path)
    payload["source_commit"] = source_commit
    payload["source_blobs"] = source_blobs
    payload["ues_repo_root"] = str(provenance_repo)
    payload["request"]["source_revision"] = source.source_revision
    payload["input_paths"] = {"manifest": str(paths.manifest), "artifact": str(paths.artifact), "readback": str(paths.readback)}
    payload["research_identity"] = {
        "code_revision": source_commit, "input_revision": source.input_digest,
        "param_space_revision": _PARAM_SPACE_REVISION, "cost_model_revision": _COST_MODEL_REVISION,
        "validator_revision": _VALIDATOR_REVISION,
    }
    result = run_request(payload, diagnose=lambda _context, _budget: {
        "optimization_needed": True, "design": "固定模板研究设计",
    })
    assert result["status"] == "parked"
    assert result["reason"] == "promotion_cycle_completed"
    assert result["notes"] == ["recommendation=reject"]
    assert (Path(payload["output_root"]) / "soxl_rsi2_mean_reversion_v1.json").is_file()
    assert result["live_authority_granted"] is False


def test_trusted_dual_window_materializes_both_windows_before_request(tmp_path, monkeypatch):
    import scripts.run_new_research as runner
    from us_equity_strategies.research import soxl_rsi2_research_adapter as adapter
    from quant_platform_kit.strategy_lifecycle.contracts import PromotionCostModel, PurgedWalkForwardFold
    from us_equity_strategies.research.soxl_rsi2_research_adapter import (
        _COST_MODEL_REVISION, _PARAM_SPACE_REVISION, _VALIDATOR_REVISION,
        load_rsi2_offline_input, Rsi2OfflineInputPaths,
    )
    from us_equity_strategies.research.soxl_alpaca_input_adapter import materialize_soxl_alpaca_input

    def weekdays(start: date, end: date) -> list[str]:
        values = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                values.append(current.isoformat())
            current += timedelta(days=1)
        return values

    # Fixed XNYS sessions from the verified 2022-2026 NYSE calendar; prices remain synthetic.
    holidays = {
        "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
        "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2025-01-09",
    }
    first_window = [x for x in weekdays(date(2022, 1, 3), date(2024, 12, 31)) if x not in holidays]
    second_window = [x for x in weekdays(date(2023, 9, 12), date(2026, 9, 11)) if x not in holidays]
    sessions = sorted(set(first_window) | set(second_window))
    root = _alpaca_source_root(tmp_path, sessions=sessions)
    optimization_paths = materialize_soxl_alpaca_input(
        root, tmp_path / "optimization-check",
        start="2022-01-03", end_exclusive="2025-01-01", expected_sessions=753,
    )
    optimization = load_rsi2_offline_input(Rsi2OfflineInputPaths(
        optimization_paths.manifest, optimization_paths.artifact, optimization_paths.readback,
    ))
    promotion_paths = materialize_soxl_alpaca_input(
        root, tmp_path / "promotion-check",
        start="2023-09-12", end_exclusive="2026-09-12", expected_sessions=753,
    )
    promotion = load_rsi2_offline_input(Rsi2OfflineInputPaths(
        promotion_paths.manifest, promotion_paths.artifact, promotion_paths.readback,
    ))
    promotion_dates = sorted({date.fromisoformat(row.as_of) for row in promotion.rows})
    folds = (
        # Fixed before OOS; QPK validates 20-day purge and 20-day embargo.
        PurgedWalkForwardFold(date(2023, 9, 12), date(2024, 12, 31), date(2025, 1, 27), date(2025, 2, 7)),
        PurgedWalkForwardFold(date(2025, 3, 3), date(2025, 3, 14), date(2025, 4, 7), date(2025, 4, 18)),
        PurgedWalkForwardFold(date(2025, 5, 12), date(2025, 5, 23), date(2025, 6, 16), date(2025, 6, 27)),
    )
    payload = _payload(tmp_path)
    payload["request"]["source_revision"] = optimization.source_revision
    payload["research_identity"] = {
        "code_revision": "a" * 40, "input_revision": optimization.input_digest,
        "param_space_revision": _PARAM_SPACE_REVISION, "cost_model_revision": _COST_MODEL_REVISION,
        "validator_revision": _VALIDATOR_REVISION,
    }
    captured = {}
    original_run_request = runner.run_request
    monkeypatch.setattr(
        "scripts.run_new_research.run_request",
        lambda request_payload, **kwargs: captured.update(payload=request_payload, kwargs=kwargs) or {"status": "parked"},
    )
    binding_source = promotion
    result = run_trusted_soxl_rsi2_dual_window(
        payload, p1_root=root, optimization_root=tmp_path / "optimization",
        promotion_root=tmp_path / "promotion",
        expected_p1_manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
        candidate_id="UNSCALED_SMA200",
        folds=folds, locked_oos_start=date(2025, 8, 1), locked_oos_end=date(2026, 9, 11),
        purge_days=20, embargo_days=20, source_revision="a" * 40,
        cost_model=PromotionCostModel("C2_5", 2.0, 5.0),
    )
    assert result == {"status": "parked"}
    assert captured["payload"]["research_identity"]["input_revision"] == optimization.input_digest
    assert captured["payload"]["input_paths"]["manifest"].endswith("optimization/prices.csv.manifest.json")
    binding = captured["kwargs"]["promotion_binding"]
    assert binding.source.input_digest == binding_source.input_digest
    assert binding.locked_oos_start == date(2025, 8, 1)
    assert promotion_dates[-1] >= date(2026, 9, 11)

    # Full fixed caller path: real materializer, real optimizer, and real AAB/QPK
    # request cycle; only the AI diagnosis is deterministic in this synthetic test.
    monkeypatch.setattr(runner, "run_request", original_run_request)
    monkeypatch.setattr(runner, "SOXL_P1_MANIFEST_SHA256", hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest())
    synthetic_ues, synthetic_commit, synthetic_blobs = _ues_provenance_repo(tmp_path)
    monkeypatch.setattr(runner, "SOXL_UES_COMMIT", synthetic_commit)
    monkeypatch.setattr(runner, "read_soxl_ues_provenance", lambda _root: (synthetic_commit, synthetic_blobs))
    original_make_optimizer = adapter.make_soxl_rsi2_optimize
    optimizer_calls = {"count": 0}

    def counted_make_optimizer(**kwargs):
        optimize = original_make_optimizer(**kwargs)

        def counted_optimize(*args, **inner_kwargs):
            optimizer_calls["count"] += 1
            return optimize(*args, **inner_kwargs)

        return counted_optimize

    monkeypatch.setattr(adapter, "make_soxl_rsi2_optimize", counted_make_optimizer)
    run_root = tmp_path / "fixed-case"
    case_result = runner.run_fixed_soxl_rsi2_case(
        p1_root=root, ues_repo_root=synthetic_ues,
        run_root=run_root, diagnose=lambda _context, _budget: {
            "optimization_needed": True, "design": "固定模板研究设计",
        },
    )
    assert case_result["status"] == "parked"
    assert case_result["live_authority_granted"] is False
    assert case_result["optimizer_executions"] == 1
    assert optimizer_calls["count"] == 1
    saved_request = json.loads((run_root / "request.json").read_text(encoding="utf-8"))
    second_result = runner.run_fixed_soxl_rsi2_case(
        p1_root=root, ues_repo_root=synthetic_ues, run_root=run_root,
        diagnose=lambda _context, _budget: pytest.fail("saved request must skip diagnosis"),
    )
    assert second_result["optimizer_executions"] == 0
    assert optimizer_calls["count"] == 1
    assert json.loads((run_root / "request.json").read_text(encoding="utf-8")) == saved_request

    tampered_request = json.loads(json.dumps(saved_request))
    tampered_request["request"]["source_revision"] = "tampered-source"
    (run_root / "request.json").write_text(
        json.dumps(tampered_request, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    with pytest.raises(NewResearchInputError, match="fixed_research_input_mismatch"):
        runner.run_fixed_soxl_rsi2_case(
            p1_root=root, ues_repo_root=synthetic_ues, run_root=run_root,
        )
    (run_root / "request.json").write_text(
        json.dumps(saved_request, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )

    ticket_path = next((run_root / "tickets").glob("*.json"))
    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    ticket["research_progress"]["stages"]["optimize"] = {"status": "unknown"}
    ticket_path.write_text(json.dumps(ticket, sort_keys=True), encoding="utf-8")
    unknown_result = runner.run_fixed_soxl_rsi2_case(
        p1_root=root, ues_repo_root=synthetic_ues, run_root=run_root,
        diagnose=lambda *_: pytest.fail("unknown optimizer stage must not retry"),
    )
    assert unknown_result["reason"] == "research_outcome_unknown"
    assert optimizer_calls["count"] == 1

    ticket["state"] = "human_rejected"
    ticket["research_progress"]["stages"] = {}
    ticket_path.write_text(json.dumps(ticket, sort_keys=True), encoding="utf-8")
    terminal_result = runner.run_fixed_soxl_rsi2_case(
        p1_root=root, ues_repo_root=synthetic_ues, run_root=run_root,
        diagnose=lambda *_: pytest.fail("terminal ticket must not retry optimizer"),
    )
    assert terminal_result["reason"] == "saved_research_ticket_terminal"
    assert optimizer_calls["count"] == 1

    bad_identity = dict(payload)
    bad_identity["research_identity"] = dict(payload["research_identity"])
    bad_identity["research_identity"]["input_revision"] = "f" * 64
    with pytest.raises(NewResearchInputError, match="rsi2_optimization_identity_mismatch"):
        run_trusted_soxl_rsi2_dual_window(
            bad_identity, p1_root=root, optimization_root=tmp_path / "optimization-identity-error",
            promotion_root=tmp_path / "promotion-identity-error",
            expected_p1_manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
            candidate_id="UNSCALED_SMA200", folds=folds, locked_oos_start=date(2025, 8, 1),
            locked_oos_end=date(2026, 9, 11), purge_days=20, embargo_days=20,
            source_revision="a" * 40, cost_model=PromotionCostModel("C2_5", 2.0, 5.0),
        )
    overlap_folds = (
        PurgedWalkForwardFold(date(2024, 11, 1), date(2024, 11, 15), date(2024, 12, 1), date(2024, 12, 10)),
        *folds[1:],
    )
    with pytest.raises(NewResearchInputError, match="rsi2_dual_window_invalid"):
        run_trusted_soxl_rsi2_dual_window(
            payload, p1_root=root, optimization_root=tmp_path / "optimization-overlap",
            promotion_root=tmp_path / "promotion-overlap",
            expected_p1_manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
            candidate_id="UNSCALED_SMA200", folds=overlap_folds, locked_oos_start=date(2025, 8, 1),
            locked_oos_end=date(2026, 9, 11), purge_days=20, embargo_days=20,
            source_revision="a" * 40, cost_model=PromotionCostModel("C2_5", 2.0, 5.0),
        )


def test_trusted_dual_window_rejects_bad_manifest_before_request(tmp_path, monkeypatch):
    called = {"run": 0}
    monkeypatch.setattr("scripts.run_new_research.run_request", lambda *_args, **_kwargs: called.__setitem__("run", 1))
    with pytest.raises(NewResearchInputError, match="p1_manifest_invalid"):
        run_trusted_soxl_rsi2_dual_window(
            _payload(tmp_path), p1_root=tmp_path, optimization_root=tmp_path / "optimization",
            promotion_root=tmp_path / "promotion", expected_p1_manifest_sha256="b" * 64,
            candidate_id="UNSCALED_SMA200", folds=(), locked_oos_start=date(2025, 8, 1),
            locked_oos_end=date(2026, 9, 11), purge_days=20, embargo_days=20,
            source_revision="a" * 40, cost_model=object(),
        )
    assert called["run"] == 0


def test_trusted_dual_window_rejects_missing_promotion_window_before_request(tmp_path, monkeypatch):
    root = _alpaca_source_root(tmp_path)
    payload = _payload(tmp_path)
    from us_equity_strategies.research.soxl_alpaca_input_adapter import materialize_soxl_alpaca_input
    from us_equity_strategies.research.soxl_rsi2_research_adapter import Rsi2OfflineInputPaths, load_rsi2_offline_input
    opt_paths = materialize_soxl_alpaca_input(
        root, tmp_path / "identity-check", start="2022-01-03", end_exclusive="2024-01-26", expected_sessions=753,
    )
    payload["request"]["source_revision"] = load_rsi2_offline_input(Rsi2OfflineInputPaths(
        opt_paths.manifest, opt_paths.artifact, opt_paths.readback,
    )).source_revision
    monkeypatch.setattr("scripts.run_new_research.run_request", lambda *_args, **_kwargs: pytest.fail("AI/request must not run"))
    with pytest.raises(NewResearchInputError, match="rsi2_dual_window_invalid"):
        run_trusted_soxl_rsi2_dual_window(
            payload, p1_root=root, optimization_root=tmp_path / "optimization",
            promotion_root=tmp_path / "promotion",
            expected_p1_manifest_sha256=hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
            candidate_id="UNSCALED_SMA200", folds=(), locked_oos_start=date(2025, 8, 1),
            locked_oos_end=date(2026, 9, 11), purge_days=20, embargo_days=20,
            source_revision="a" * 40, cost_model=object(),
        )


def _synthetic_ues_source(start: date, source_revision: str):
    from us_equity_strategies.research.soxl_soxx_offline_input_contract import (
        InputRow, OfflineInput, _canonical,
    )

    rows = []
    for index in range(753):
        day = (start + timedelta(days=index)).isoformat()
        for symbol, close in (("SOXL", 30.0 + (index % 7) * 0.2),
                              ("SOXX", 100.0 + (index % 11) * 0.1)):
            rows.append(InputRow(symbol, day, close, close + 1.0, close - 1.0, close, 100.0))
    typed = tuple(rows)
    canonical = _canonical(typed)
    return OfflineInput(typed, canonical, hashlib.sha256(canonical).hexdigest(), source_revision)


def _shift_ues_source(source, days: int):
    from us_equity_strategies.research.soxl_soxx_offline_input_contract import (
        InputRow, OfflineInput, _canonical,
    )

    rows = tuple(sorted(
        (InputRow(row.symbol, (date.fromisoformat(row.as_of) + timedelta(days=days)).isoformat(),
                   row.open, row.high, row.low, row.close, row.volume) for row in source.rows),
        key=lambda row: (row.as_of, row.symbol),
    ))
    canonical = _canonical(rows)
    return OfflineInput(rows, canonical, hashlib.sha256(canonical).hexdigest(), "promotion-source")


def _binding_for_source(source):
    from quant_platform_kit.strategy_lifecycle.contracts import PromotionCostModel, PurgedWalkForwardFold
    from us_equity_strategies.research.soxl_rsi2_promotion_runner import SoxlRsi2PromotionBinding

    dates = sorted({date.fromisoformat(row.as_of) for row in source.rows})
    folds = (
        PurgedWalkForwardFold(dates[0], dates[150], dates[200], dates[210]),
        PurgedWalkForwardFold(dates[220], dates[235], dates[250], dates[260]),
        PurgedWalkForwardFold(dates[270], dates[275], dates[285], dates[295]),
    )
    return SoxlRsi2PromotionBinding(
        source=source, candidate_id="UNSCALED_SMA200", folds=folds,
        locked_oos_start=dates[310], locked_oos_end=dates[752],
        purge_days=5, embargo_days=5, source_revision="a" * 40,
        cost_model=PromotionCostModel("C2_5", 2.0, 5.0),
    )


def _complete_paired_shadow():
    from quant_platform_kit.strategy_lifecycle.forward_observation import ForwardObservationPolicy
    from quant_platform_kit.strategy_lifecycle.forward_observation_receipt import (
        FORWARD_OBSERVATION_DEPENDENCY_DIGESTS, build_forward_observation_receipt,
    )
    from quant_platform_kit.strategy_lifecycle.paired_shadow_evidence import build_paired_shadow_evidence

    policy = ForwardObservationPolicy(
        candidate_id="UNSCALED_SMA200", strategy_profile="soxl_rsi2_mean_reversion",
        domain="us_equity", benchmark_symbol="SOXX", required_trading_sessions=2,
        review_milestones=(1,), automatic_non_live_modes=("shadow", "paper"),
        auto_resume_clean_sessions=2, observation_calendar="XNYS",
        observation_window_type="fixed", observation_start_session="2026-09-14",
        window_rationale_ref="sha256:soxl-rsi2-forward-window",
        non_live_evidence_modes=("shadow_decision", "simulated_replay"),
    )
    dependencies = {field: character * 64 for field, character in zip(
        sorted(FORWARD_OBSERVATION_DEPENDENCY_DIGESTS), "abcdef")}
    def leg(name):
        return {"signal": {"kind": "target_weight", "source": name},
                "hypothetical_order": {"kind": "rebalance_preview", "source": name},
                "position": {"kind": "end_of_snapshot", "source": name},
                "cost": {"kind": "configured_cost_model", "source": name},
                "return": {"kind": "one_snapshot_return", "source": name}}
    first_receipt = build_forward_observation_receipt(
        policy=policy, observation_session="2026-09-14", observation_index=1,
        dependency_digests=dependencies, evidence_modes=policy.non_live_evidence_modes,
    )
    earlier = build_paired_shadow_evidence(
        policy=policy, forward_observation_receipt=first_receipt,
        baseline_id="baseline", observed_at="2026-09-14T20:00:00Z",
        input_snapshot_sha256="a" * 64, candidate=leg("candidate"), baseline=leg("baseline"),
    )
    second_receipt = build_forward_observation_receipt(
        policy=policy, observation_session="2026-09-15", observation_index=2,
        dependency_digests=dependencies, evidence_modes=policy.non_live_evidence_modes,
        previous_receipt=first_receipt,
    )
    return {"status": "complete", "observation": dict(
        policy=policy, forward_observation_receipt=second_receipt,
        baseline_id="baseline", observed_at="2026-09-15T20:00:00Z",
        input_snapshot_sha256="a" * 64, candidate=leg("candidate"), baseline=leg("baseline"),
        previous_evidence=earlier, previous_forward_observation_receipt=first_receipt,
    )}


def test_actual_ues_binding_qpk_cycle_and_reentry(tmp_path, monkeypatch):
    from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore
    import quant_platform_kit.strategy_lifecycle.research_promotion_cycle as qpk_cycle
    from us_equity_strategies.research import soxl_rsi2_research_adapter as adapter

    optimization = _synthetic_ues_source(date(2022, 1, 3), "source-v1")
    promotion = _shift_ues_source(optimization, 800)
    binding = _binding_for_source(promotion)
    payload = _payload(tmp_path)
    payload["source_commit"] = "a" * 40
    payload["research_identity"] = {
        "code_revision": "a" * 40,
        "input_revision": optimization.input_digest,
        "param_space_revision": adapter._PARAM_SPACE_REVISION,
        "cost_model_revision": adapter._COST_MODEL_REVISION,
        "validator_revision": adapter._VALIDATOR_REVISION,
    }
    payload["input_paths"] = {"manifest": "unused", "artifact": "unused", "readback": "unused"}
    monkeypatch.setattr(adapter, "load_rsi2_offline_input", lambda _paths: optimization)
    monkeypatch.setattr(adapter, "persist_rsi2_mean_reversion_result", lambda *args, **kwargs: None)
    from quant_platform_kit.strategy_lifecycle.contracts import OptimizationProposal

    monkeypatch.setattr(
        adapter,
        "_proposal_from_result",
        lambda _result: OptimizationProposal(
            strategy_profile=adapter.PROFILE, domain=adapter.DOMAIN,
            current_params={"candidate_id": "UNSCALED_SMA200"},
            proposed_params={"candidate_id": "UNSCALED_SMA200"},
            recommendation="research_candidate", improvement_score=0.0,
            confidence=0.0, winning_dimensions=(), regressing_dimensions=(),
            walk_forward_passed=False, optimization_method="synthetic_fixture",
            search_iterations=4, computed_at=clock[0].isoformat(),
        ),
    )
    optimize_calls = {"count": 0}
    original_run = adapter.run_soxl_rsi2_mean_reversion

    def counted_run(source):
        optimize_calls["count"] += 1
        return original_run(source)

    monkeypatch.setattr(adapter, "run_soxl_rsi2_mean_reversion", counted_run)

    shadow_calls = {"count": 0}
    clock = [datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(qpk_cycle, "_clock_now", lambda: clock[0])

    def shadow(_proposal):
        shadow_calls["count"] += 1
        return {"status": "pending", "passed": False, "no_order": True,
                "live_authority_granted": False,
                "retry_at": (clock[0] + timedelta(seconds=60)).timestamp()}

    reader_calls = {"count": 0}

    def reader(_proposal):
        reader_calls["count"] += 1
        return _complete_paired_shadow()

    sync_calls = {"count": 0}
    synced_ticket = {}

    def sync(ticket):
        sync_calls["count"] += 1
        synced_ticket.update(ticket.to_dict(include_progress=True))
        return True

    pull_calls = {"count": 0}

    def pull(_ticket_id):
        pull_calls["count"] += 1
        from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import (
            ResearchPromotionTicket, apply_human_promotion_decision,
        )
        ticket = ResearchPromotionTicket.from_dict(synced_ticket)
        return apply_human_promotion_decision(
            ticket, decision="reject", decided_at=clock[0].isoformat(),
        ).to_dict(include_progress=True)

    kwargs = {
        "promotion_binding": binding,
        "promotion_store": PerformanceStore(local_root=tmp_path / "promotion-store"),
        "promotion_shadow_recorder": shadow,
        "read_pending_shadow": reader,
        "sync_console": sync,
        "pull_console": pull,
    }
    def diagnose(_context, _budget):
        return {"optimization_needed": True, "design": "固定模板研究设计"}

    summary_calls = {"count": 0}

    def summarize(_context):
        summary_calls["count"] += 1
        return {"status": "available", "text": "这是受限研究说明。", "provider": "codex",
                "model": "fixture-codex"}

    kwargs["summarize"] = summarize
    no_binding_payload = dict(payload)
    no_binding_payload["ticket_dir"] = str(tmp_path / "no-binding-tickets")
    no_binding = run_request(no_binding_payload, diagnose=diagnose)
    assert no_binding["notes"] == ["promotion_backtest_gate_failed", "missing_promotion_backtest_evidence"]
    assert shadow_calls["count"] == 0
    first = run_request(payload, diagnose=diagnose, **kwargs)
    assert reader_calls["count"] == 0
    clock[0] = datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc)
    second = run_request(payload, diagnose=diagnose, **kwargs)
    assert first["status"] == "deferred"
    assert second["resumed"] is True
    assert second["reason"] == "promotion_cycle_completed"
    assert optimize_calls["count"] == 2
    assert shadow_calls["count"] == 1
    assert reader_calls["count"] == 1
    assert sync_calls["count"] == 1
    assert summary_calls["count"] == 1
    assert synced_ticket["research_summary"]["ai_explanation"]["status"] == "available"
    third = run_request(payload, diagnose=diagnose, **kwargs)
    assert third["reason"] == "saved_research_ticket_reused"
    assert third["state"] == "human_rejected"
    assert third["live_authority_granted"] is False
    assert pull_calls["count"] == 1
    assert optimize_calls["count"] == 2
    assert shadow_calls["count"] == 1
    from dataclasses import replace

    changed_binding = replace(binding, embargo_days=6)
    changed_payload = dict(payload)
    changed = run_request(changed_payload, diagnose=diagnose,
                          promotion_binding=changed_binding,
                          promotion_store=kwargs["promotion_store"],
                          promotion_shadow_recorder=shadow)
    assert changed["status"] == "deferred"
    assert changed["reason"] == "paired_shadow_observation_pending"
    assert optimize_calls["count"] == 3
    assert shadow_calls["count"] == 2

    invalid_payload = dict(payload)
    invalid_payload["ticket_dir"] = str(tmp_path / "invalid-shadow-tickets")
    invalid_reader_calls = {"count": 0}

    def invalid_reader(_proposal):
        invalid_reader_calls["count"] += 1
        return {"status": "complete", "observation": {}}

    invalid_first = run_request(
        invalid_payload, diagnose=diagnose,
        promotion_binding=binding, promotion_store=kwargs["promotion_store"],
        promotion_shadow_recorder=shadow, read_pending_shadow=invalid_reader,
        sync_console=sync, pull_console=pull,
    )
    assert invalid_first["status"] == "deferred"
    sync_before_invalid = sync_calls["count"]
    clock[0] += timedelta(seconds=120)
    invalid_second = run_request(
        invalid_payload, diagnose=diagnose,
        promotion_binding=binding, promotion_store=kwargs["promotion_store"],
        promotion_shadow_recorder=shadow, read_pending_shadow=invalid_reader,
        sync_console=sync, pull_console=pull,
    )
    assert invalid_second["reason"] == "research_outcome_unknown"
    assert invalid_reader_calls["count"] == 1
    assert sync_calls["count"] == sync_before_invalid
def test_fixed_entry_reuses_request_and_ticket_across_processes(tmp_path):
    p1_root = tmp_path / "p1"
    p1_root.mkdir()
    manifest = b"synthetic-p1-manifest"
    (p1_root / "manifest.json").write_bytes(manifest)
    p1_sha = hashlib.sha256(manifest).hexdigest()
    ues_root = tmp_path / "ues"
    ues_root.mkdir()
    run_root = tmp_path / "run"
    count_path = tmp_path / "optimizer-count"
    source_commit = "a" * 40
    source_blobs = {"study.py": "b" * 40}
    child = r'''
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import scripts.run_new_research as runner
from quant_platform_kit.strategy_lifecycle.contracts import OptimizationProposal

runner.SOXL_P1_MANIFEST_SHA256 = os.environ["P1_SHA"]
runner.SOXL_UES_COMMIT = os.environ["UES_COMMIT"]
runner.read_soxl_ues_provenance = lambda _root: (
    os.environ["UES_COMMIT"], json.loads(os.environ["UES_BLOBS"])
)

package = types.ModuleType("us_equity_strategies")
research = types.ModuleType("us_equity_strategies.research")
alpaca = types.ModuleType("us_equity_strategies.research.soxl_alpaca_input_adapter")
adapter = types.ModuleType("us_equity_strategies.research.soxl_rsi2_research_adapter")
class Paths:
    def __init__(self, manifest, artifact, readback):
        self.manifest, self.artifact, self.readback = manifest, artifact, readback
class Binding:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
source = SimpleNamespace(input_digest="optimization-v1", source_revision="source-v1")
def materialize(root, output, **_kwargs):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    paths = Paths(output / "manifest.json", output / "artifact.json", output / "readback.json")
    for path in (paths.manifest, paths.artifact, paths.readback): path.write_text("{}", encoding="utf-8")
    return paths
def load(_paths): return source
def make_optimizer(**_kwargs):
    def optimize(request, _budget):
        count_path = Path(os.environ["COUNT_PATH"])
        count = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
        count_path.write_text(str(count + 1), encoding="utf-8")
        return OptimizationProposal(
            strategy_profile=request.strategy_profile, domain=request.domain,
            proposed_params={"candidate_id": "UNSCALED_SMA200"},
            recommendation="research_candidate",
        )
    return optimize
def prepare(**_kwargs):
    return ({"code_revision": "a" * 40, "input_revision": "optimization-v1",
             "param_space_revision": "param-v1", "cost_model_revision": "cost-v1",
             "validator_revision": "validator-v1"},
            lambda _proposal: None,
            lambda _proposal: {"status": "pending", "passed": False, "no_order": True,
                               "live_authority_granted": False})
alpaca.materialize_soxl_alpaca_input = materialize
adapter.Rsi2OfflineInputPaths = Paths
adapter.SoxlRsi2PromotionBinding = Binding
adapter._COST_MODEL_REVISION = "cost-v1"
adapter._PARAM_SPACE_REVISION = "param-v1"
adapter._VALIDATOR_REVISION = "validator-v1"
adapter.load_rsi2_offline_input = load
adapter._validate_research_identity = lambda *_args: None
adapter.make_soxl_rsi2_optimize = make_optimizer
adapter.prepare_soxl_rsi2_promotion = prepare
sys.modules[package.__name__] = package
sys.modules[research.__name__] = research
sys.modules[alpaca.__name__] = alpaca
sys.modules[adapter.__name__] = adapter

result = runner.run_fixed_soxl_rsi2_case(
    p1_root=os.environ["P1_ROOT"], ues_repo_root=os.environ["UES_ROOT"],
    run_root=os.environ["RUN_ROOT"],
    diagnose=lambda _context, _budget: {"optimization_needed": True, "design": "fixture"},
)
print(json.dumps(result, sort_keys=True))
'''
    env = dict(os.environ)
    env.update({
        "P1_ROOT": str(p1_root), "P1_SHA": p1_sha, "UES_ROOT": str(ues_root),
        "RUN_ROOT": str(run_root), "COUNT_PATH": str(count_path),
        "UES_COMMIT": source_commit, "UES_BLOBS": json.dumps(source_blobs),
    })
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", child], cwd=Path(__file__).parents[1],
            env=env, check=True, capture_output=True, text=True,
        )
        result = json.loads(completed.stdout)
        assert result["live_authority_granted"] is False
    assert result["optimizer_executions"] == 0
    assert count_path.read_text(encoding="utf-8") == "1"
    assert (run_root / "request.json").is_file()
