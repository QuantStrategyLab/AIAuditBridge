from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.run_soxl_manual_learning import (
    ManualLearningError,
    initialize_record,
    parse_parameter_grid,
    run_manual_learning,
)


UES_REVISION = "7756fe32585e85cf1d09a163203a02e3eee39fe1"


class FakeClient:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, prompt: str, **kwargs: object) -> object:
        self.calls.append((prompt, kwargs))
        return self.result


def ai_result(*, recommendation: str = "execute", status: str = "succeeded") -> SimpleNamespace:
    output = json.dumps(
        {
            "schema_version": "qsl.soxl-manual-learning-advice.v1",
            "recommendation": recommendation,
            "reason_code": "bounded_hypothesis_worth_testing",
            "parameter_values": [0.65, 0.6, 0.55],
            "learning_only": True,
            "no_order": True,
            "promotion_eligible": False,
        }
    )
    return SimpleNamespace(
        provider="codex",
        model="gpt-5.6-sol",
        success=status == "succeeded",
        output=output,
        error="" if status == "succeeded" else "private provider detail",
        note="deferred" if status == "deferred" else "",
        raw={
            "status": status,
            "job_id": "job-123",
            "provider": "codex",
            "research_stage": "optimization",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "output": output,
            "policy_verdict": "advisory",
        },
    )


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "root"
    root.mkdir()
    for name in ("binding.json", "manifest.json", "bars.json"):
        (root / name).write_text("{}")
    consumer = tmp_path / "consumer"
    (consumer / "scripts").mkdir(parents=True)
    (consumer / "config").mkdir()
    (consumer / ".venv/bin").mkdir(parents=True)
    (consumer / ".venv/bin/python").symlink_to(sys.executable)
    (consumer / "scripts/run_soxl_three_asset_learning.py").write_text("# synthetic")
    (consumer / "config/soxl_soxx_core_only_p2_v3.json").write_text("{}")
    ues = tmp_path / "ues"
    ues.mkdir()
    return {"root": root, "consumer": consumer, "ues": ues}


def context() -> dict[str, str]:
    return {
        "repository": "QuantStrategyLab/AIAuditBridge",
        "ref": "refs/heads/main",
        "event_name": "workflow_dispatch",
        "actor": "human-reviewer",
        "run_id": "12345",
        "run_attempt": "1",
    }


def numeric_result() -> dict[str, object]:
    rows = []
    for parameter in (0.65, 0.6, 0.55):
        for cost in (5.0, 10.0, 15.0):
            rows.append(
                {
                    "schema_version": "qsl.soxl-soxx-three-asset-learning-replay-result.v1",
                    "status": "SUCCESS",
                    "parameter_override": {"blend_gate_mid_soxl_weight": parameter},
                    "cost_bps": cost,
                    "backtest_result": {
                        "strategy_profile": "soxl_soxx_three_asset_mid_weight_learning_v1",
                        "sharpe_ratio": 0.25,
                        "max_drawdown": 0.1,
                        "cagr": 0.05,
                        "volatility": 0.2,
                        "total_return": 0.03,
                        "start_date": "2025-01-02",
                        "end_date": "2025-07-31",
                        "observation_count": 80,
                        "target_values": {"SOXL": 100.0},
                    },
                    "output_sha256": "a" * 64,
                }
            )
    return {
        "schema_version": "qsl.soxl-soxx-three-asset-learning.v1",
        "status": "SUCCESS",
        "learning_profile": "soxl_soxx_three_asset_mid_weight_learning_v1",
        "learning_only": True,
        "no_order": True,
        "size_zero_required": True,
        "promotion_eligible": False,
        "research_executed": True,
        "development_cutoff": "2025-07-31",
        "p1_identity": {"input_manifest_sha256": "0" * 64},
        "source_identity": {
            "repository": "QuantStrategyLab/UsEquityStrategies",
            "revision": UES_REVISION,
            "quant_platform_kit_revision": "3acab1923a97b805b077c85c6c19657be0143bac",
            "uv_lock_sha256": "6c12df9b3412681829295f15de7e2ce7fc5b708d1de815f72d654fc16b7848e6",
            "private_path": "/private/source",
        },
        "parameter_key": "blend_gate_mid_soxl_weight",
        "trial_count": 3,
        "cost_bps": [5.0, 10.0, 15.0],
        "results": rows,
        "result_sha256": "b" * 64,
    }


def test_parameter_grid_requires_unique_bounded_baseline() -> None:
    assert parse_parameter_grid("0.65,0.60,0.55") == (0.65, 0.6, 0.55)
    for invalid in ("0.60", "0.65,0.65", "0.65,0.60,0.55,0.50", "0.65,nan", "0.70,0.65"):
        with pytest.raises(ManualLearningError):
            parse_parameter_grid(invalid)


def test_initialized_terminal_record_binds_the_manual_run(tmp_path: Path) -> None:
    output = tmp_path / "summary.json"
    initialize_record(output, context())
    value = json.loads(output.read_text())
    assert value["status"] == "parked"
    assert value["failure_stage"] == "setup_incomplete"
    assert value["authority"]["run_id"] == "12345"
    assert value["research_executed"] is False


def test_invalid_manual_context_calls_neither_ai_nor_numeric(paths: dict[str, Path]) -> None:
    client = FakeClient(ai_result())
    commands: list[list[str]] = []
    bad = context() | {"run_attempt": "2"}
    with pytest.raises(ManualLearningError, match="manual_authority_invalid"):
        run_manual_learning(
            parameter_grid="0.65,0.60,0.55", manifest_sha256="0" * 64,
            root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
            context=bad, client_factory=lambda _config: client,
            gateway_config=SimpleNamespace(),
            command_runner=lambda argv: commands.append(argv),
        )
    assert not client.calls
    assert not commands


@pytest.mark.parametrize("kind", ["deferred", "unknown", "reject"])
def test_non_executable_codex_outcome_never_calls_numeric(
    kind: str, paths: dict[str, Path]
) -> None:
    result = ai_result(recommendation="reject" if kind == "reject" else "execute", status="deferred" if kind == "deferred" else "succeeded")
    if kind == "unknown":
        result.raw["job_id"] = ""
    client = FakeClient(result)
    commands: list[list[str]] = []
    artifact = run_manual_learning(
        parameter_grid="0.65,0.60,0.55", manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        context=context(), client_factory=lambda _config: client,
        gateway_config=SimpleNamespace(),
        command_runner=lambda argv: commands.append(argv),
    )
    assert artifact["status"] in {"deferred", "unavailable", "rejected"}
    assert artifact["research_executed"] is False
    assert not commands


def test_free_text_reason_code_is_rejected_before_numeric(paths: dict[str, Path]) -> None:
    result = ai_result()
    payload = json.loads(result.output)
    payload["reason_code"] = "private model prose with spaces"
    result.output = json.dumps(payload)
    result.raw["output"] = result.output
    client = FakeClient(result)
    commands: list[list[str]] = []
    artifact = run_manual_learning(
        parameter_grid="0.65,0.60,0.55", manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        context=context(), client_factory=lambda _config: client,
        command_runner=lambda argv: commands.append(argv), gateway_config=SimpleNamespace(),
    )
    assert artifact["status"] == "unavailable"
    assert not commands


def test_success_calls_one_codex_job_then_exact_fixed_numeric_cli(
    paths: dict[str, Path]
) -> None:
    client = FakeClient(ai_result())
    commands: list[list[str]] = []

    def run_command(argv: list[str]) -> SimpleNamespace:
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(numeric_result()), stderr="")

    artifact = run_manual_learning(
        parameter_grid="0.65,0.60,0.55", manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        context=context(), client_factory=lambda _config: client, command_runner=run_command,
        gateway_config=SimpleNamespace(),
    )

    assert len(client.calls) == 1
    assert client.calls[0][1] == {
        "mode": "review_only", "research_stage": "optimization",
        "allowed_providers": ["codex"], "source_repository": "QuantStrategyLab/AIAuditBridge",
        "source_ref": "main", "timeout": 600,
    }
    assert len(commands) == 1
    argv = commands[0]
    assert argv[:2] == [
        str(paths["consumer"] / ".venv/bin/python"),
        str(paths["consumer"] / "scripts/run_soxl_three_asset_learning.py"),
    ]
    assert argv.count("--blend-gate-mid-soxl-weight") == 3
    assert "0.65" in argv and "0.6" in argv and "0.55" in argv
    assert artifact["status"] == "accepted"
    assert artifact["research_executed"] is True
    assert artifact["learning_only"] is True
    assert artifact["no_order"] is True
    assert artifact["size_zero_required"] is True
    assert artifact["promotion_eligible"] is False
    assert artifact["authority"]["actor"] == "human-reviewer"
    assert artifact["ai_execution"]["job_id"] == "job-123"
    assert len(artifact["numeric_summary"]) == 9
    serialized = json.dumps(artifact)
    assert "decisions" not in serialized
    assert "target_values" not in serialized
    assert "private_path" not in serialized
    assert "private provider detail" not in serialized


def test_numeric_failure_is_sanitized_and_nonaccepted(paths: dict[str, Path]) -> None:
    client = FakeClient(ai_result())

    def fail(_argv: list[str]) -> SimpleNamespace:
        return SimpleNamespace(returncode=2, stdout="private bars", stderr="private trace")

    artifact = run_manual_learning(
        parameter_grid="0.65,0.60,0.55", manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        context=context(), client_factory=lambda _config: client, command_runner=fail,
        gateway_config=SimpleNamespace(),
    )
    assert artifact["status"] == "parked"
    assert artifact["research_executed"] is None
    assert artifact["failure_stage"] == "numeric_execution_failed"
    assert artifact["numeric_execution"] == {"status": "failed"}
    assert artifact["ai_execution"]["job_id"] == "job-123"
    assert "private" not in json.dumps(artifact)


def test_numeric_unknown_keeps_manual_and_ai_binding(paths: dict[str, Path]) -> None:
    client = FakeClient(ai_result())
    progress: list[dict[str, object]] = []

    def unknown(_argv: list[str]) -> object:
        raise subprocess.TimeoutExpired("private command", 1)

    artifact = run_manual_learning(
        parameter_grid="0.65,0.60,0.55", manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        context=context(), client_factory=lambda _config: client, command_runner=unknown,
        gateway_config=SimpleNamespace(),
        progress_writer=lambda value: progress.append(dict(value)),
    )
    assert artifact["status"] == "parked"
    assert artifact["research_executed"] is None
    assert artifact["numeric_execution"] == {"status": "outcome_unknown"}
    assert artifact["authority"]["run_id"] == "12345"
    assert artifact["ai_execution"]["job_id"] == "job-123"
    assert progress[0]["research_executed"] is None
    assert progress[0]["numeric_execution"] == {"status": "started"}


def test_cli_failure_writes_only_safe_terminal_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import run_soxl_manual_learning as module

    output = tmp_path / "summary.json"
    monkeypatch.setattr(
        module,
        "run_manual_learning",
        lambda **_kwargs: (_ for _ in ()).throw(ManualLearningError("numeric_execution_unavailable")),
    )
    code = module.main(
        [
            "--parameter-grid", "0.65,0.60", "--manifest-sha256", "0" * 64,
            "--root", str(tmp_path), "--consumer-source", str(tmp_path),
            "--ues-source", str(tmp_path), "--output", str(output),
            "--repository", "QuantStrategyLab/AIAuditBridge", "--ref", "refs/heads/main",
            "--event-name", "workflow_dispatch", "--actor", "reviewer",
            "--run-id", "123", "--run-attempt", "1",
        ]
    )
    assert code != 0
    assert json.loads(output.read_text()) == {
        "schema_version": "qsl.soxl-manual-learning-run.v1",
        "operation": "soxl_learning",
        "status": "parked",
        "failure_stage": "numeric_execution_unavailable",
        "research_executed": False,
        "learning_only": True,
        "no_order": True,
        "size_zero_required": True,
        "promotion_eligible": False,
    }
    assert "private" not in capsys.readouterr().out


def test_gateway_auth_accepts_only_the_exact_manual_workflow_on_main() -> None:
    from scripts import codex_audit_service

    workflow_ref = (
        "QuantStrategyLab/AIAuditBridge/.github/workflows/"
        "research_input_readback.yml@refs/heads/main"
    )
    payload = {
        "aud": "quant-codex-audit",
        "iss": codex_audit_service.GITHUB_OIDC_ISSUER,
        "exp": int(time.time()) + 300,
        "repository": "QuantStrategyLab/AIAuditBridge",
        "workflow_ref": workflow_ref,
        "ref": "refs/heads/main",
        "repository_visibility": "public",
    }
    env = {
        "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
        "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": workflow_ref,
        "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
        "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
        "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES": "public",
    }

    def verify(active: dict[str, object]) -> dict[str, object]:
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                codex_audit_service,
                "_jwt_parts",
                return_value=({"alg": "RS256", "kid": "1"}, active, b"x", b"y"),
            ),
            patch.object(
                codex_audit_service,
                "_load_jwks",
                return_value={"keys": [{"kid": "1", "kty": "RSA"}]},
            ),
            patch.object(codex_audit_service, "_verify_rs256", return_value=None),
        ):
            return codex_audit_service._verify_github_oidc("header.payload.signature")

    assert verify(payload)["workflow_ref"] == workflow_ref
    with pytest.raises(PermissionError, match="workflow_ref .* not allowed"):
        verify(payload | {"workflow_ref": workflow_ref.replace("refs/heads/main", "refs/heads/other")})
