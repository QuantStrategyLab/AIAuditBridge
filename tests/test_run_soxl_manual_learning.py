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
    run_watcher_learning,
    read_issue_comments,
    record_watcher_pre_numeric_failure,
    write_issue_comment,
)
from service.research_task import SOXL_WATCHER_PARAMETER_BOUNDS_SHA256, build_strategy_diagnosis_task


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


def watcher_inputs() -> tuple[dict[str, object], dict[str, object]]:
    task = build_strategy_diagnosis_task(
        event_key="123456789abc", created_at="2026-09-10T00:00:00Z",
        candidate_id="soxl_soxx_core_only_p2_v3", candidate_kind="individual",
        domain="us_equity", strategy_repository="QuantStrategyLab/UsEquityStrategies",
        evidence={"p1_input_digest": "0" * 64, "p2_config_digest": "ff8fa0acf4f175a7c40c3e1e6a3304ea2748b6b81c3797342085a4df3810ab4d", "p3_evidence_id": "c" * 64, "strategy_revision": UES_REVISION, "producer_revision": "e" * 40},
    )
    result = {
        "research_task_source_snapshot": {"data_status": "ready", "tasks": [task]},
        "issues": [{"repo": "QuantStrategyLab/UsEquitySnapshotPipelines", "url": "https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/issues/1", "task": {"event_key": "123456789abc", "trigger": {}}}],
    }
    diagnosis = {"diagnoses": [{"status": "diagnosed", "task_id": task["task_id"], "task_sha256": task["task_sha256"], "issue_url": "https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/issues/1"}]}
    return result, diagnosis


def trusted_comment(body: str) -> dict[str, object]:
    return {"body": body, "performed_via_github_app": {"id": 42}}


def trusted_diagnosis_comment(watcher: dict[str, object]) -> dict[str, object]:
    task = watcher["research_task_source_snapshot"]["tasks"][0]
    marker = __import__("service.research_diagnosis", fromlist=["marker_for_research_diagnosis"]).marker_for_research_diagnosis(__import__("service.research_diagnosis", fromlist=["build_research_diagnosis_request"]).build_research_diagnosis_request(task))
    return trusted_comment(marker + "\ntrusted diagnosis")


def test_watcher_learning_runs_synthetic_subprocess_stub_once_and_writes_bound_terminal(
    paths: dict[str, Path],
) -> None:
    watcher, diagnosis = watcher_inputs()
    script = paths["consumer"] / "scripts/run_soxl_three_asset_learning.py"
    script.write_text("import json\nprint(json.dumps(" + repr(numeric_result()) + "))\n")
    comments: list[str] = []
    artifact = run_watcher_learning(
        watcher_result=watcher, diagnosis_result=diagnosis, manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        github_app_id="42", read_comments=lambda *_: [trusted_diagnosis_comment(watcher)],
        write_comment=lambda _repo, _url, body: comments.append(body) or "comment",
    )
    assert artifact["status"] == "accepted"
    assert artifact["source"] == "watcher_event_independent_learning"
    assert artifact["promotion_eligible"] is False
    assert artifact["experiment"]["parameter_bounds_sha256"] == SOXL_WATCHER_PARAMETER_BOUNDS_SHA256
    assert len(artifact["numeric_summary"]) == 9
    assert len(comments) == 2
    assert ":started:" in comments[0] and ":terminal:" in comments[1]


@pytest.mark.parametrize("state", ["old_null", "not_diagnosed", "started", "failed"])
def test_watcher_learning_non_executable_or_terminal_state_never_calls_numeric(state: str, paths: dict[str, Path]) -> None:
    watcher, diagnosis = watcher_inputs()
    task = watcher["research_task_source_snapshot"]["tasks"][0]
    comments: list[dict[str, object]] = []
    if state == "old_null":
        from service.research_task import calculate_task_sha256
        task["experiment"]["parameter_bounds_sha256"] = None
        task["task_sha256"] = calculate_task_sha256(task)
    elif state == "not_diagnosed":
        diagnosis = {"diagnoses": [{"status": "deferred", "task_id": task["task_id"]}]}
    else:
        diagnosis = {"status": "skipped", "diagnoses": []}
        phase = "started" if state == "started" else "terminal"
        body = __import__("scripts.run_soxl_manual_learning", fromlist=["watcher_learning_comment"]).watcher_learning_comment(task, phase=phase, status="failed" if state == "failed" else "started")
        comments = [trusted_comment(body), trusted_comment(__import__("service.research_diagnosis", fromlist=["marker_for_research_diagnosis"]).marker_for_research_diagnosis(__import__("service.research_diagnosis", fromlist=["build_research_diagnosis_request"]).build_research_diagnosis_request(task)))]
    calls: list[list[str]] = []
    artifact = run_watcher_learning(
        watcher_result=watcher, diagnosis_result=diagnosis, manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"], github_app_id="42",
        read_comments=lambda *_: comments, write_comment=lambda *_: "comment",
        command_runner=lambda argv: calls.append(argv),
    )
    assert artifact["status"] in {"parked", "rejected"}
    assert calls == []


def test_watcher_learning_reuses_trusted_terminal_when_current_diagnosis_is_skipped(paths: dict[str, Path]) -> None:
    watcher, _ = watcher_inputs()
    task = watcher["research_task_source_snapshot"]["tasks"][0]
    diagnosis_marker = __import__("service.research_diagnosis", fromlist=["marker_for_research_diagnosis"]).marker_for_research_diagnosis(__import__("service.research_diagnosis", fromlist=["build_research_diagnosis_request"]).build_research_diagnosis_request(task))
    raw = numeric_result()
    safe = [
        {key: item[key] for key in ("parameter_override", "cost_bps", "backtest_result", "output_sha256")}
        for item in raw["results"]
    ]
    terminal = __import__("scripts.run_soxl_manual_learning", fromlist=["watcher_learning_comment"]).watcher_learning_comment(
        task, phase="terminal", status="accepted", numeric_result_sha256="b" * 64,
        numeric_summary=safe, numeric_source_identity={key: raw["source_identity"][key] for key in ("repository", "revision", "quant_platform_kit_revision", "uv_lock_sha256")},
    )
    artifact = run_watcher_learning(
        watcher_result=watcher, diagnosis_result={"status": "skipped", "diagnoses": []}, manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"], github_app_id="42",
        read_comments=lambda *_: [trusted_comment(diagnosis_marker), trusted_comment(terminal)],
        write_comment=lambda *_: (_ for _ in ()).throw(AssertionError("must reuse")),
        command_runner=lambda _argv: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert artifact["status"] == "accepted"
    assert artifact["numeric_execution"] == {"status": "reused"}
    assert len(artifact["numeric_summary"]) == 9


def test_watcher_learning_rejects_unreadable_or_untrusted_comment_history(paths: dict[str, Path]) -> None:
    watcher, diagnosis = watcher_inputs()
    task = watcher["research_task_source_snapshot"]["tasks"][0]
    diagnosis_marker = __import__("service.research_diagnosis", fromlist=["marker_for_research_diagnosis"]).marker_for_research_diagnosis(__import__("service.research_diagnosis", fromlist=["build_research_diagnosis_request"]).build_research_diagnosis_request(task))
    for active_diagnosis, reader in (
        (diagnosis, lambda *_: (_ for _ in ()).throw(OSError("private failure"))),
        ({"status": "skipped", "diagnoses": []}, lambda *_: [{"body": diagnosis_marker, "performed_via_github_app": {"id": 7}}]),
    ):
        calls: list[list[str]] = []
        artifact = run_watcher_learning(
            watcher_result=watcher, diagnosis_result=active_diagnosis, manifest_sha256="0" * 64,
            root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
            github_app_id="42", read_comments=reader, write_comment=lambda *_: "comment",
            command_runner=lambda argv: calls.append(argv),
        )
        assert artifact["status"] == "parked"
        assert calls == []
        assert "private failure" not in json.dumps(artifact)


def test_comment_reader_slurps_and_flattens_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [[{"body": f"comment-{index}"} for index in range(100)], [{"body": "comment-100"}]]
    observed: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        observed.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(pages), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    comments = read_issue_comments(
        "QuantStrategyLab/UsEquitySnapshotPipelines",
        "https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/issues/123",
    )
    assert len(comments) == 101
    assert "--paginate" in observed[0] and "--slurp" in observed[0]


def test_multiline_comment_uses_body_file_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        observed.update(argv=argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout="comment-url", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert write_issue_comment(
        "QuantStrategyLab/UsEquitySnapshotPipelines",
        "https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/issues/123",
        "line one\nline two",
    ) == "comment-url"
    assert observed["argv"][-2:] == ["--body-file", "-"]
    assert observed["input"] == "line one\nline two"


def test_known_pre_numeric_failure_writes_terminal_and_prevents_future_execution(paths: dict[str, Path]) -> None:
    watcher, diagnosis = watcher_inputs()
    written: list[str] = []
    result = record_watcher_pre_numeric_failure(
        watcher_result=watcher, diagnosis_result=diagnosis, github_app_id="42",
        read_comments=lambda *_: [trusted_diagnosis_comment(watcher)], write_comment=lambda _repo, _url, body: written.append(body) or "comment",
    )
    assert result == {"status": "failed", "failure_stage": "pre_numeric_failed"}
    assert len(written) == 1 and ":terminal:" in written[0]
    task = watcher["research_task_source_snapshot"]["tasks"][0]
    diagnosis_marker = __import__("service.research_diagnosis", fromlist=["marker_for_research_diagnosis"]).marker_for_research_diagnosis(__import__("service.research_diagnosis", fromlist=["build_research_diagnosis_request"]).build_research_diagnosis_request(task))
    calls: list[list[str]] = []
    artifact = run_watcher_learning(
        watcher_result=watcher, diagnosis_result={"status": "skipped", "diagnoses": []}, manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"], github_app_id="42",
        read_comments=lambda *_: [trusted_comment(diagnosis_marker), trusted_comment(written[0])],
        write_comment=lambda *_: "comment", command_runner=lambda argv: calls.append(argv),
    )
    assert artifact["failure_stage"] == "pre_numeric_failed"
    assert artifact["numeric_execution"] == {"status": "reused"}
    assert calls == []


def test_malformed_exact_terminal_is_rejected_without_replay(paths: dict[str, Path]) -> None:
    watcher, _ = watcher_inputs()
    task = watcher["research_task_source_snapshot"]["tasks"][0]
    diagnosis_marker = __import__("service.research_diagnosis", fromlist=["marker_for_research_diagnosis"]).marker_for_research_diagnosis(__import__("service.research_diagnosis", fromlist=["build_research_diagnosis_request"]).build_research_diagnosis_request(task))
    prefix = __import__("scripts.run_soxl_manual_learning", fromlist=["_marker"])._marker(task, "terminal")
    calls: list[list[str]] = []
    artifact = run_watcher_learning(
        watcher_result=watcher, diagnosis_result={"status": "skipped", "diagnoses": []}, manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"], github_app_id="42",
        read_comments=lambda *_: [trusted_comment(diagnosis_marker), trusted_comment(f"<!-- {prefix} --> malformed")],
        write_comment=lambda *_: "comment", command_runner=lambda argv: calls.append(argv),
    )
    assert artifact["status"] == "parked"
    assert artifact["failure_stage"] == "watcher_terminal_invalid"
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/QuantStrategyLab/UsEquitySnapshotPipelines/issues/1",
        "https://github.com/QuantStrategyLab/OtherRepository/issues/1",
        "https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/pull/1",
    ],
)
def test_watcher_learning_rejects_issue_outside_exact_source_repository(url: str, paths: dict[str, Path]) -> None:
    watcher, diagnosis = watcher_inputs()
    watcher["issues"][0]["url"] = url
    diagnosis["diagnoses"][0]["issue_url"] = url
    calls: list[list[str]] = []
    artifact = run_watcher_learning(
        watcher_result=watcher, diagnosis_result=diagnosis, manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"], github_app_id="42",
        read_comments=lambda *_: [], write_comment=lambda *_: "comment",
        command_runner=lambda argv: calls.append(argv),
    )
    assert artifact["status"] == "parked"
    assert artifact["failure_stage"] == "watcher_issue_invalid"
    assert calls == []


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
    deploy_text = Path("scripts/deploy_codex_audit_service.sh").read_text()
    ops_text = Path(".github/workflows/vps_codex_service_ops.yml").read_text()
    deploy_prefix = (
        'ALLOWED_JOB_WORKFLOW_REFS="${CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS:-'
    )
    deploy_line = next(line for line in deploy_text.splitlines() if line.startswith(deploy_prefix))
    deployed_job_refs = deploy_line.removeprefix(deploy_prefix).removesuffix('}"')
    ops_prefix = "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS: "
    ops_line = next(line.strip() for line in ops_text.splitlines() if line.strip().startswith(ops_prefix))
    assert deployed_job_refs == ops_line.removeprefix(ops_prefix)

    payload = {
        "aud": "quant-codex-audit",
        "iss": codex_audit_service.GITHUB_OIDC_ISSUER,
        "exp": int(time.time()) + 300,
        "repository": "QuantStrategyLab/AIAuditBridge",
        "workflow_ref": workflow_ref,
        "job_workflow_ref": workflow_ref,
        "ref": "refs/heads/main",
        "repository_visibility": "public",
    }
    env = {
        "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
        "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": workflow_ref,
        "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS": deployed_job_refs,
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
    assert verify({key: value for key, value in payload.items() if key != "job_workflow_ref"})[
        "workflow_ref"
    ] == workflow_ref
    with pytest.raises(PermissionError, match="job workflow ref .* not allowed"):
        verify(payload | {"job_workflow_ref": workflow_ref.replace("refs/heads/main", "refs/heads/other")})
    other_ref = workflow_ref.replace("refs/heads/main", "refs/heads/other")
    with pytest.raises(PermissionError, match="workflow_ref .* not allowed"):
        verify(payload | {"workflow_ref": other_ref, "job_workflow_ref": other_ref, "ref": "refs/heads/other"})


def promotion_validation_result(development_digest=None) -> dict[str, object]:
    from datetime import date
    from quant_platform_kit.strategy_lifecycle.contracts import (
        BacktestResult, BacktestValidationIdentity, OptimizationProposal, PromotionBacktestRun,
        PromotionCostModel, PurgedWalkForwardFold,
    )
    from scripts import run_soxl_manual_learning as module

    development_digest = development_digest or module.DEVELOPMENT_SUMMARY_SHA256
    profile = "soxl_soxx_three_asset_mid_weight_learning_v1"
    folds = tuple(PurgedWalkForwardFold(*map(date.fromisoformat, item)) for item in (
        ("2022-12-28", "2023-06-30", "2023-07-03", "2023-12-29"),
        ("2024-01-02", "2024-06-28", "2024-07-01", "2024-12-31"),
        ("2025-01-02", "2025-02-28", "2025-03-03", "2025-07-31"),
    ))
    proposal = OptimizationProposal(
        strategy_profile=profile, domain="us_equity",
        current_params={"blend_gate_mid_soxl_weight": 0.65},
        proposed_params={"blend_gate_mid_soxl_weight": 0.55},
        recommendation="research_candidate", search_iterations=3,
        optimization_method=f"bounded_development_tradeoff:sha256:{development_digest}",
    )

    def runs(weight, role):
        result = []
        for cost in (5.0, 10.0, 15.0):
            def metrics(start, end, fold=None):
                suffix = f"_wf{folds.index(fold)}" if fold else "_locked_oos"
                result_id = f"soxl-three-asset-{development_digest}-{role}-cost-{cost:g}{suffix}"
                return BacktestResult(
                    strategy_profile=profile, domain="us_equity",
                    param_set_id=result_id,
                    params={"blend_gate_mid_soxl_weight": weight},
                    cagr=weight / 2, max_drawdown=weight / 3, sharpe_ratio=1.1,
                    total_return=weight / 2, volatility=0.2, observation_count=100,
                    start_date=start, end_date=end, source_revision=UES_REVISION,
                    cost_model=f"all_in_per_side_{cost:g}bps",
                    cost_inputs={"commission_bps": 0.0, "slippage_bps": cost, "market_impact_bps": 0.0},
                    validation_identity=BacktestValidationIdentity(
                        protocol="purged_walk_forward.v1", fold_id=result_id,
                        fold_role="test" if fold else "locked_oos",
                        train_start=fold.train_start if fold else None,
                        train_end=fold.train_end if fold else None,
                        test_start=start, test_end=end,
                        locked_oos_start=date(2025, 8, 4), locked_oos_end=date(2026, 8, 4),
                        purge_days=1, embargo_days=1,
                    ),
                )
            result.append(PromotionBacktestRun(
                strategy_profile=profile, domain="us_equity", folds=folds,
                fold_results=tuple(metrics(fold.test_start, fold.test_end, fold) for fold in folds),
                locked_oos_result=metrics(date(2025, 8, 4), date(2026, 8, 4)),
                locked_oos_start=date(2025, 8, 4), locked_oos_end=date(2026, 8, 4),
                purge_days=1, embargo_days=1, source_revision=UES_REVISION,
                cost_model=PromotionCostModel(model_id=f"all_in_per_side_{cost:g}bps", commission_bps=0.0, slippage_bps=cost, market_impact_bps=0.0),
            ).to_dict())
        return result

    return {
        "status": "PROMOTION_BACKTEST_RUNS_BUILT", "stage": "promotion_validation",
        "learning_only": True, "no_order": True, "size_zero_required": True,
        "live_authority_granted": False,
        "promotion_eligible": False, "proposal": proposal.to_dict(),
        "baseline_promotion_runs": runs(0.65, "baseline"),
        "candidate_promotion_runs": runs(0.55, "candidate"),
    }


def accepted_watcher_comments(watcher):
    from scripts import run_soxl_manual_learning as module
    task = watcher["research_task_source_snapshot"]["tasks"][0]
    safe, source, digest = module._sanitize_numeric(numeric_result(), (0.65, 0.6, 0.55), "0" * 64)
    return [trusted_diagnosis_comment(watcher), trusted_comment(module.watcher_learning_comment(
        task, phase="terminal", status="accepted", numeric_result_sha256=digest,
        numeric_summary=safe, numeric_source_identity=source,
    ))]


def test_watcher_validation_consumes_same_task_learning_once_and_reuses_terminal(paths):
    from scripts import run_soxl_manual_learning as module
    watcher, diagnosis = watcher_inputs()
    comments = accepted_watcher_comments(watcher)
    calls = []

    def run(argv):
        calls.append(argv)
        summary = json.loads(Path(argv[argv.index("--promotion-validation-development-summary") + 1]).read_text())
        digest = argv[argv.index("--watcher-development-summary-sha256") + 1]
        assert module._summary_digest(summary) == digest != module.DEVELOPMENT_SUMMARY_SHA256
        assert summary["task_sha256"] == watcher["research_task_source_snapshot"]["tasks"][0]["task_sha256"]
        assert summary["input_identity"] == {"manifest_sha256": "0" * 64, "member_count": 4}
        assert summary["research_executed"] is True
        assert "--blend-gate-mid-soxl-weight" not in argv
        return SimpleNamespace(returncode=0, stdout=json.dumps(promotion_validation_result(digest)))

    kwargs = dict(
        watcher_result=watcher, diagnosis_result=diagnosis, manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        github_app_id="42", read_comments=lambda *_: comments,
        write_comment=lambda _repo, _url, body: comments.append(trusted_comment(body)) or "comment",
        command_runner=run,
    )
    with patch.object(module.AiGatewayClient, "execute", side_effect=AssertionError("no AI in validation")):
        output = module.run_watcher_validation(**kwargs)
        reused = module.run_watcher_validation(**kwargs)
    assert len(calls) == 1
    assert len(comments) == 4
    assert len(comments[-1]["body"]) < 65536
    assert output["status"] == reused["status"] == "accepted"
    assert output["strict_backtest_gate"]["checks"] == 6
    assert reused["numeric_execution"] == {"status": "reused"}
    assert output["candidate_promotion_runs"] == reused["candidate_promotion_runs"]
    assert output["promotion_eligible"] is False
    assert output["human_quality_decision_required"] is True
    assert module.prepare_watcher_learning(watcher, diagnosis, github_app_id="42", read_comments=lambda *_: comments, include_validation=True)["ready"] is False


@pytest.mark.parametrize("failure", ["missing_learning", "untrusted_learning", "manifest", "failed_learning"])
def test_watcher_validation_requires_trusted_matching_successful_learning(paths, failure):
    from scripts import run_soxl_manual_learning as module
    watcher, diagnosis = watcher_inputs()
    comments = accepted_watcher_comments(watcher)
    manifest = "0" * 64
    if failure == "missing_learning":
        comments.pop()
    elif failure == "untrusted_learning":
        comments[-1]["performed_via_github_app"]["id"] = 99
    elif failure == "manifest":
        manifest = "f" * 64
    else:
        task = watcher["research_task_source_snapshot"]["tasks"][0]
        comments[-1] = trusted_comment(module.watcher_learning_comment(task, phase="terminal", status="failed"))
    calls = []
    output = module.run_watcher_validation(
        watcher_result=watcher, diagnosis_result=diagnosis, manifest_sha256=manifest,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        github_app_id="42", read_comments=lambda *_: comments,
        write_comment=lambda *args: calls.append(args), command_runner=lambda argv: calls.append(argv),
    )
    assert output["status"] == "parked"
    assert calls == []


@pytest.mark.parametrize("failure", ["timeout", "numeric_failure", "old_manual_result", "terminal_write"])
def test_watcher_validation_failed_or_unknown_outcome_never_reexecutes(paths, failure):
    from scripts import run_soxl_manual_learning as module
    watcher, diagnosis = watcher_inputs()
    comments = accepted_watcher_comments(watcher)
    calls = []

    def write(_repo, _url, body):
        if failure == "terminal_write" and ":validation_terminal:" in body:
            raise subprocess.TimeoutExpired("synthetic", 1)
        comments.append(trusted_comment(body))
        return "comment"

    def run(argv):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("synthetic", 1)
        if failure == "numeric_failure":
            return SimpleNamespace(returncode=2)
        digest = argv[argv.index("--watcher-development-summary-sha256") + 1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(promotion_validation_result(None if failure == "old_manual_result" else digest)))

    kwargs = dict(
        watcher_result=watcher, diagnosis_result=diagnosis, manifest_sha256="0" * 64,
        root=paths["root"], consumer_source=paths["consumer"], ues_source=paths["ues"],
        github_app_id="42", read_comments=lambda *_: comments, write_comment=write, command_runner=run,
    )
    output = module.run_watcher_validation(**kwargs)
    repeated = module.run_watcher_validation(**kwargs)
    assert len(calls) == 1
    assert output["status"] == repeated["status"] == "parked"
    assert "oos_comparison" not in output
    assert module.prepare_watcher_learning(watcher, diagnosis, github_app_id="42", read_comments=lambda *_: comments, include_validation=True)["ready"] is False


def test_watcher_preflight_can_continue_accepted_learning_without_relearning():
    from scripts import run_soxl_manual_learning as module
    watcher, diagnosis = watcher_inputs()
    comments = accepted_watcher_comments(watcher)
    assert module.prepare_watcher_learning(watcher, diagnosis, github_app_id="42", read_comments=lambda *_: comments)["ready"] is False
    ready = module.prepare_watcher_learning(watcher, diagnosis, github_app_id="42", read_comments=lambda *_: comments, include_validation=True)
    assert ready["ready"] is True
    assert ready["p1_manifest_sha256"] == "0" * 64


@pytest.mark.parametrize("path,replacement", [
    (("baseline_promotion_runs", 0, "folds", 0, "train_start"), "2022-12-29"),
    (("baseline_promotion_runs", 0, "purge_days"), 2),
    (("baseline_promotion_runs", 0, "fold_results", 0, "validation_identity", "train_start"), "2022-12-29"),
    (("live_authority_granted",), True),
    (("proposal", "walk_forward_passed"), True),
    (("candidate_promotion_runs", 0, "locked_oos_result", "param_set_id"), lambda value: value + "-unbound"),
    (("candidate_promotion_runs", 0, "locked_oos_result", "cost_model"), "different-cost"),
    (("candidate_promotion_runs", 0, "locked_oos_result", "cost_inputs", "slippage_bps"), 999.0),
])
def test_selected_validation_rejects_conflicting_plan_or_evidence(path, replacement):
    from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import enforce_promotion_backtest_gates
    from scripts import run_soxl_manual_learning as module
    value = promotion_validation_result()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement(target[path[-1]]) if callable(replacement) else replacement
    with pytest.raises(module.ManualLearningError):
        module._strict_validation_summary(value, enforce_promotion_backtest_gates)


def test_selected_validation_uses_strict_gate_and_reports_tradeoff_without_ai(paths, tmp_path, monkeypatch):
    from scripts import run_soxl_manual_learning as module
    summary = tmp_path / "development.json"
    summary.write_text('{"synthetic":true}')
    monkeypatch.setattr(module, "DEVELOPMENT_SUMMARY_SHA256", module._summary_digest(json.loads(summary.read_text())))
    result = promotion_validation_result()
    result["private_extra"] = "must-not-be-published"
    calls = []
    progress = []

    def run(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    with patch.object(module.AiGatewayClient, "execute", side_effect=AssertionError("validation must not call AI")):
        output = module.run_manual_validation(
            development_summary=summary, manifest_sha256="a" * 64, root=paths["root"],
            consumer_source=paths["consumer"], ues_source=paths["ues"], context=context(),
            command_runner=run, progress_writer=progress.append,
        )
    assert len(calls) == 1
    assert "--promotion-validation-development-summary" in calls[0]
    assert "--blend-gate-mid-soxl-weight" not in calls[0]
    assert output["status"] == "accepted"
    assert output["strict_backtest_gate"]["status"] == "passed"
    assert output["promotion_eligible"] is False
    assert output["human_quality_decision_required"] is True
    assert output["candidate_promotion_runs"][0]["locked_oos_result"]["validation_identity"]["protocol"] == "purged_walk_forward.v1"
    assert all(row["candidate_max_drawdown"] < row["baseline_max_drawdown"] for row in output["oos_comparison"])
    assert "must-not-be-published" not in json.dumps(output)
    assert progress[0]["numeric_execution"]["status"] == "started"


def accepted_validation_artifact():
    from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import enforce_promotion_backtest_gates
    from scripts import run_soxl_manual_learning as module

    artifact = module._safe_base(context(), (0.65, 0.55), "a" * 64)
    artifact.update(
        operation="soxl_validation",
        status="accepted",
        development_summary_sha256=module.DEVELOPMENT_SUMMARY_SHA256,
        numeric_execution={"status": "succeeded"},
        research_executed=True,
    )
    artifact["consumer_source"]["revision"] = module.VALIDATION_CONSUMER_REVISION
    artifact["authority"]["run_id"] = "34407783620"
    artifact.update(
        module._strict_validation_summary(
            promotion_validation_result(), enforce_promotion_backtest_gates
        )
    )
    return artifact


def test_selected_validation_builds_parked_read_only_control_plane_source():
    from scripts import run_soxl_manual_learning as module

    result = module.build_validation_control_plane_source(
        accepted_validation_artifact(),
        expected_run_id="34407783620",
        source_revision="e" * 40,
        computed_at="2026-09-09T22:18:54Z",
        published_at="2026-09-10T06:00:00Z",
    )

    assert result["schema_version"] == "qsl_control_plane_source_snapshot.v1"
    assert result["source_id"] == "aiaudit.soxl_manual_validation"
    assert result["generated_at"] == "2026-09-09T22:18:54Z"
    assert result["computed_at"] == "2026-09-09T22:18:54Z"
    assert result["errors"] == []
    candidate = result["candidates"][0]
    assert candidate["candidate_id"] == "soxl_three_asset_mid_weight_validation_34407783620"
    assert candidate["lifecycle"] == {"stage": "P3", "status": "parked"}
    assert candidate["recommendation"]["code"] == "park"
    assert "5/10/15 基点" in candidate["recommendation"]["reason"]
    assert "年化收益保留" in candidate["recommendation"]["reason"]
    assert candidate["evidence"] == {
        "p1_input_digest": "a" * 64,
        "p2_config_digest": None,
        "p3_evidence_id": "34407783620",
        "source_revision": "e" * 40,
    }
    assert candidate["freshness"] == {"status": "fresh", "age_seconds": 27666}
    serialized = json.dumps(result)
    assert "awaiting_human" not in serialized
    assert "owner_live_decision" not in serialized


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("status",), "parked"),
        (("promotion_eligible",), True),
        (("human_quality_decision_required",), False),
        (("strict_backtest_gate", "status"), "failed"),
        (("authority", "repository"), "Other/repo"),
        (("authority", "run_id"), "999"),
    ],
)
def test_control_plane_source_rejects_untrusted_validation_summary(path, replacement):
    from scripts import run_soxl_manual_learning as module

    artifact = accepted_validation_artifact()
    target = artifact
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement

    with pytest.raises(module.ManualLearningError, match="validation_control_plane_source_invalid"):
        module.build_validation_control_plane_source(
            artifact,
            expected_run_id="34407783620",
            source_revision="e" * 40,
            computed_at="2026-09-09T22:18:54Z",
            published_at="2026-09-10T06:00:00Z",
        )


def test_selected_validation_rejects_changed_source_before_execution(paths, tmp_path):
    from scripts import run_soxl_manual_learning as module
    summary = tmp_path / "development.json"
    summary.write_text('{"changed":true}')
    with pytest.raises(ManualLearningError, match="validation_development_source_invalid"):
        module.run_manual_validation(
            development_summary=summary, manifest_sha256="a" * 64, root=paths["root"],
            consumer_source=paths["consumer"], ues_source=paths["ues"], context=context(),
            command_runner=lambda _: pytest.fail("changed source must not execute"),
        )


@pytest.mark.parametrize("failure", ["short_oos", "changed_candidate", "nan_metrics", "timeout"])
def test_selected_validation_parks_failures_without_retry(paths, tmp_path, monkeypatch, failure):
    from scripts import run_soxl_manual_learning as module
    summary = tmp_path / "development.json"
    summary.write_text('{"synthetic":true}')
    monkeypatch.setattr(module, "DEVELOPMENT_SUMMARY_SHA256", module._summary_digest(json.loads(summary.read_text())))
    result = promotion_validation_result()
    candidate = result["candidate_promotion_runs"][0]
    if failure == "short_oos":
        candidate["locked_oos_end"] = "2026-07-31"
    elif failure == "changed_candidate":
        candidate["locked_oos_result"]["params"]["blend_gate_mid_soxl_weight"] = 0.5
    elif failure == "nan_metrics":
        candidate["locked_oos_result"]["cagr"] = float("nan")
    calls = []

    def run(argv):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("synthetic", 1)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    output = module.run_manual_validation(
        development_summary=summary, manifest_sha256="a" * 64, root=paths["root"],
        consumer_source=paths["consumer"], ues_source=paths["ues"], context=context(), command_runner=run,
    )
    assert len(calls) == 1
    assert output["status"] == "parked"
    assert output["promotion_eligible"] is False
    assert "oos_comparison" not in output
