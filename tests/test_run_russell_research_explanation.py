import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_russell_research_explanation import (
    SOURCE_HEAD_SHA, SOURCE_RUN_ID, _safe_execution_details, extract_summary, validate_input,
)


def _run():
    return {"id": int(SOURCE_RUN_ID), "run_attempt": 1, "status": "completed", "conclusion": "success", "head_sha": SOURCE_HEAD_SHA, "repository": {"full_name": "QuantStrategyLab/UsEquitySnapshotPipelines"}}


def _summary():
    return {"data_kind": "research", "strategy_profile": "russell_top50_leader_rotation", "variant": "blend_top2_50_top4_50", "sample_count": 8, "total_return": 0.02696707823985056, "max_drawdown": -0.044293008209077556, "fees": 249.37655860349128, "research_flags": {"runner_kind": "research_only", "research_scope": "core_signal_only", "learning_only": True, "no_order": True, "promotion_eligible": False, "live_ready": False, "runtime_parity_verified": False, "size_zero_required": True}, "source": {"feature_generation": "1788239901963544", "feature_sha256": "493f5ff986e1d421a36e20efefc5ecef0142b01c2cca8a139942e73ee7480967", "window_start": "2026-09-01T00:00:00-04:00", "window_end": "2026-09-12T00:00:00-04:00"}}


def test_extracts_only_final_summary():
    summary = _summary()
    text = "noise\nresearch-case step " + json.dumps(summary) + "\n"
    assert extract_summary(text) == summary


def test_validates_fixed_source_and_preserves_research_flags():
    result = validate_input(_run(), _summary())
    assert result["source_run"]["run_id"] == SOURCE_RUN_ID
    assert result["source_result"]["total_return"] > 0
    assert result["source_result"]["research_flags"]["no_order"] is True


def test_rejects_non_successful_or_wrong_head():
    bad = _run()
    bad["conclusion"] = "failure"
    with pytest.raises(ValueError):
        validate_input(bad, _summary())


def test_rejects_nonfinite_or_boolean_numeric_values():
    for key, value in (("fees", True), ("fees", float("nan")), ("total_return", float("inf"))):
        summary = _summary()
        summary[key] = value
        with pytest.raises(ValueError):
            validate_input(_run(), summary)


def test_subprocess_explain_writes_only_sanitized_success_artifact(tmp_path: Path):
    stub = tmp_path / "stub"
    (stub / "client").mkdir(parents=True)
    (stub / "client" / "__init__.py").write_text("")
    (stub / "client" / "config.py").write_text("class GatewayConfig:\n  research_providers=('codex',)\n  @classmethod\n  def from_env(cls): return cls()\n")
    (stub / "client" / "gateway_client.py").write_text(
        "class Result:\n  success=True\n  provider='codex'\n  model='gpt-5.6-luna'\n  output='中文 advisory：研究结果仅供人工参考。'\n  raw={'status':'succeeded','job_id':'job-test','provider':'codex','model':'gpt-5.6-luna','research_stage':'research_summary','reasoning_effort':'low'}\n"
        "class AiGatewayClient:\n  def __init__(self, config): pass\n  def execute(self, *args, **kwargs): return Result()\n"
    )
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(_run()))
    log_path = tmp_path / "run.log"
    log_path.write_text("research-case step " + json.dumps(_summary()) + "\n")
    output = tmp_path / "out" / "summary.json"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(stub), str(Path(__file__).parents[1]))))
    completed = subprocess.run([sys.executable, "-m", "scripts.run_russell_research_explanation", "--run-metadata", str(run_path), "--run-log", str(log_path), "--source-ref", "16c5727", "--output", str(output)], cwd=stub, env=env, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    artifact = json.loads(output.read_text())
    assert artifact["ai_execution"]["provider"] == "codex"
    assert artifact["source_result"]["window_end_semantics"] == "exclusive"


def test_subprocess_explicit_cursor_writes_success_artifact(tmp_path: Path):
    stub = tmp_path / "stub"
    (stub / "client").mkdir(parents=True)
    (stub / "client" / "__init__.py").write_text("")
    (stub / "client" / "config.py").write_text(
        "class GatewayConfig:\n  research_providers=('cursor',)\n  @classmethod\n  def from_env(cls): return cls()\n"
    )
    (stub / "client" / "gateway_client.py").write_text(
        "class Result:\n  success=True\n  provider='cursor'\n  model='cursor-grok-4.6-low'\n  "
        "output='中文 advisory：研究结果仅供人工参考。'\n  "
        "raw={'status':'succeeded','job_id':'job-test','provider':'cursor',"
        "'model':'cursor-grok-4.6-low','research_stage':'research_summary',"
        "'reasoning_effort':'low'}\n"
        "class AiGatewayClient:\n  def __init__(self, config): pass\n"
        "  def execute(self, *args, **kwargs):\n"
        "    assert kwargs.get('allowed_providers') == ['cursor']\n"
        "    assert kwargs.get('research_stage') == 'research_summary'\n"
        "    return Result()\n"
    )
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(_run()))
    log_path = tmp_path / "run.log"
    log_path.write_text("research-case step " + json.dumps(_summary()) + "\n")
    output = tmp_path / "out" / "summary.json"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(stub), str(Path(__file__).parents[1]))))
    completed = subprocess.run(
        [
            sys.executable, "-m", "scripts.run_russell_research_explanation",
            "--run-metadata", str(run_path), "--run-log", str(log_path),
            "--source-ref", "16c5727", "--output", str(output),
        ],
        cwd=stub, env=env, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    artifact = json.loads(output.read_text())
    assert artifact["ai_execution"]["provider"] == "cursor"
    assert artifact["ai_execution"]["model"] == "cursor-grok-4.6-low"


def test_subprocess_codex_cursor_chain_is_rejected(tmp_path: Path):
    stub = tmp_path / "stub"
    (stub / "client").mkdir(parents=True)
    (stub / "client" / "__init__.py").write_text("")
    (stub / "client" / "config.py").write_text(
        "class GatewayConfig:\n  research_providers=('codex', 'cursor')\n"
        "  @classmethod\n  def from_env(cls): return cls()\n"
    )
    (stub / "client" / "gateway_client.py").write_text(
        "class AiGatewayClient:\n  def __init__(self, config): pass\n"
        "  def execute(self, *args, **kwargs):\n"
        "    raise AssertionError('execute must not run for fallback chain')\n"
    )
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(_run()))
    log_path = tmp_path / "run.log"
    log_path.write_text("research-case step " + json.dumps(_summary()) + "\n")
    output = tmp_path / "out" / "summary.json"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(stub), str(Path(__file__).parents[1]))))
    completed = subprocess.run(
        [
            sys.executable, "-m", "scripts.run_russell_research_explanation",
            "--run-metadata", str(run_path), "--run-log", str(log_path),
            "--source-ref", "16c5727", "--output", str(output),
        ],
        cwd=stub, env=env, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 2
    assert not output.exists()


def test_subprocess_failure_does_not_write_artifact(tmp_path: Path):
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps({"id": 1, "repository": {"full_name": "QuantStrategyLab/UsEquitySnapshotPipelines"}}))
    log_path = tmp_path / "run.log"
    log_path.write_text("not a source log")
    output = tmp_path / "out" / "summary.json"
    completed = subprocess.run([sys.executable, "-m", "scripts.run_russell_research_explanation", "--run-metadata", str(run_path), "--run-log", str(log_path), "--source-ref", "16c5727", "--output", str(output)], cwd=Path(__file__).parents[1], env=dict(os.environ), capture_output=True, text=True, check=False)
    assert completed.returncode == 2
    assert not output.exists()
    assert "Traceback" not in completed.stderr


def test_workflow_uses_module_entrypoint_and_main_gate():
    workflow = (Path(__file__).parents[1] / ".github/workflows/research_input_readback.yml").read_text()
    assert "python3 -m scripts.run_russell_research_explanation" in workflow
    assert "persist-credentials: false" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "- russell_explanation" in workflow
    assert "inputs.operation != 'russell_explanation'" in workflow
    assert "inputs.operation == 'russell_explanation'" in workflow


def test_execution_failure_preserves_safe_category_and_identifiers_only():
    response = type("Result", (), {
        "success": False, "provider": "codex", "model": "gpt-5.6-luna",
        "error": "secret-token / private prompt", "note": "private note",
        "raw": {"failure_category": "quota_or_capacity_failure", "job_id": "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6", "request_id": "req-456", "status": "failed", "provider": "codex", "model": "sk-example-secret", "reasoning_effort": "low", "research_stage": "research_summary", "output": "private output"},
    })()
    details = _safe_execution_details(response)
    assert details == {"failure_category": "quota_or_capacity_failure", "status": "failed", "job_id": "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"}
    assert "secret" not in json.dumps(details)
    assert "private" not in json.dumps(details)


def test_execution_failure_invalid_category_becomes_unknown():
    response = type("Result", (), {"success": False, "provider": "codex", "model": "sk-example-secret", "error": "private", "note": "private", "raw": {"failure_category": [], "status": {"private": "value"}, "job_id": "bad id"}})()
    assert _safe_execution_details(response) == {"failure_category": "unknown_failure", "status": "unknown", "job_id": "unknown"}


def test_route_contract_failure_does_not_write_success_artifact(tmp_path: Path, monkeypatch):
    from client.config import GatewayConfig
    from client.gateway_client import AiGatewayClient

    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps(_run()))
    log_path = tmp_path / "run.log"
    log_path.write_text("research-case step " + json.dumps(_summary()) + "\n")
    output = tmp_path / "out" / "summary.json"
    monkeypatch.setattr(GatewayConfig, "from_env", classmethod(lambda cls: type("Config", (), {"research_providers": ("codex",)})()))
    response = type("Result", (), {"success": False, "provider": "codex", "model": "", "output": "", "error": "private", "note": "private", "raw": {"failure_category": "auth_or_config_failure", "request_id": "req-1"}})()
    monkeypatch.setattr(AiGatewayClient, "execute", lambda self, *args, **kwargs: response)
    with pytest.raises(ValueError, match="auth_or_config_failure"):
        from scripts.run_russell_research_explanation import explain
        explain(log_path=log_path, run_path=run_path, output_path=output, source_ref="6960b9f")
    assert not output.exists()
