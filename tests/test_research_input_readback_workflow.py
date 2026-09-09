"""Behavior checks for the bounded VPS P1 input readback workflow."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/research_input_readback.yml"
OBJECTS = {
    "binding.json": b"binding",
    "bars.json": b"bars-data",
    "manifest.json": b"manifest",
    "p1-complete.json": b"completion",
}


def workflow_text() -> str:
    return WORKFLOW.read_text()


def python_blocks() -> tuple[str, str]:
    parts = workflow_text().split("<<'PY'\n")[1:]
    assert len(parts) == 2
    blocks = [textwrap.dedent(part.split("\n          PY", maxsplit=1)[0]) for part in parts]
    return blocks[0], blocks[1]


def configure_readback_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    credential = tmp_path / "gha-creds.json"
    credential.write_text('{"type":"external_account"}')
    workspace = tmp_path / "readback"
    (workspace / "root").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credential))
    monkeypatch.setenv("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", str(credential))
    monkeypatch.setenv("INPUT_ROOT", "gs://fixed-root/")
    monkeypatch.setenv("READBACK_ROOT", str(workspace))
    return workspace


def install_fake_validator(
    monkeypatch: pytest.MonkeyPatch, *, manifest_sha256: str, reject: bool = False
) -> list[tuple[str, Path, Path | None]]:
    calls: list[tuple[str, Path, Path | None]] = []
    package = types.ModuleType("us_equity_snapshot_pipelines")
    lifecycle = types.ModuleType("us_equity_snapshot_pipelines.lifecycle")
    publisher = types.ModuleType(
        "us_equity_snapshot_pipelines.lifecycle.soxl_core_only_p1_publisher"
    )

    def verify_root(root: Path) -> str:
        calls.append(("root", root, None))
        if reject:
            raise ValueError("hidden input detail")
        return manifest_sha256

    def verify_completion(root: Path, marker: Path) -> str:
        calls.append(("completion", root, marker))
        return manifest_sha256

    publisher.verify_soxl_core_only_input_root = verify_root  # type: ignore[attr-defined]
    publisher.verify_soxl_core_only_p1_remote_completion = verify_completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, lifecycle.__name__, lifecycle)
    monkeypatch.setitem(sys.modules, publisher.__name__, publisher)
    return calls


def test_success_reads_exactly_four_objects_and_calls_both_original_gates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    download_block, validator_block = python_blocks()
    workspace = configure_readback_environment(monkeypatch, tmp_path)
    subprocess_calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        subprocess_calls.append(args)
        name = args[4].removeprefix("gs://fixed-root/")
        if args[2:4] == ["objects", "describe"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"size": len(OBJECTS[name])}))
        destination = Path(args[-1])
        destination.write_bytes(OBJECTS[name])
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    exec(compile(download_block, "<download-block>", "exec"), {})

    manifest_sha256 = "a" * 64
    monkeypatch.setenv("ROOT", str(workspace / "root"))
    monkeypatch.setenv("COMPLETION", str(workspace / "p1-complete.json"))
    monkeypatch.setenv("EXPECTED_MANIFEST_SHA256", manifest_sha256)
    validator_calls = install_fake_validator(monkeypatch, manifest_sha256=manifest_sha256)
    exec(compile(validator_block, "<validator-block>", "exec"), {})

    described = [call[4] for call in subprocess_calls if call[2:4] == ["objects", "describe"]]
    copied = [call[4] for call in subprocess_calls if call[2:4] == ["cp", "--quiet"]]
    assert described == ["gs://fixed-root/" + name for name in OBJECTS]
    assert copied == ["gs://fixed-root/" + name for name in OBJECTS]
    assert [call[0] for call in validator_calls] == ["root", "completion"]
    assert json.loads(capsys.readouterr().out) == {
        "status": "accepted",
        "identity": "github_oidc",
        "manifest_sha256": manifest_sha256,
        "member_count": 4,
        "research_executed": False,
    }


def test_oversize_object_is_rejected_before_any_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    download_block, _ = python_blocks()
    configure_readback_environment(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"size": 1024 * 1024 + 1}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit):
        exec(compile(download_block, "<download-block>", "exec"), {})

    assert not any(call[2:4] == ["cp", "--quiet"] for call in calls)
    assert json.loads(capsys.readouterr().out) == {
        "status": "unavailable",
        "reason": "object_size_invalid",
    }


def test_missing_temporary_identity_never_calls_gcloud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    download_block, _ = python_blocks()
    workspace = tmp_path / "readback"
    (workspace / "root").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("READBACK_ROOT", str(workspace))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", raising=False)

    def forbidden_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("gcloud must not run")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    with pytest.raises(SystemExit):
        exec(compile(download_block, "<download-block>", "exec"), {})

    assert json.loads(capsys.readouterr().out) == {
        "status": "unavailable",
        "reason": "temporary_identity_missing",
    }


def test_copy_failure_outputs_only_the_fixed_safe_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    download_block, _ = python_blocks()
    configure_readback_environment(monkeypatch, tmp_path)

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        if args[2:4] == ["objects", "describe"]:
            name = args[4].removeprefix("gs://fixed-root/")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"size": len(OBJECTS[name])}))
        return SimpleNamespace(returncode=1, stdout="", stderr="sensitive provider detail")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit):
        exec(compile(download_block, "<download-block>", "exec"), {})

    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "unavailable", "reason": "object_read_unavailable"}
    assert "sensitive" not in output
    assert "accepted" not in output


def test_validator_rejection_outputs_only_the_fixed_safe_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, validator_block = python_blocks()
    manifest_sha256 = "a" * 64
    monkeypatch.setenv("ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("COMPLETION", str(tmp_path / "p1-complete.json"))
    monkeypatch.setenv("EXPECTED_MANIFEST_SHA256", manifest_sha256)
    install_fake_validator(monkeypatch, manifest_sha256=manifest_sha256, reject=True)

    with pytest.raises(SystemExit):
        exec(compile(validator_block, "<validator-block>", "exec"), {})

    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "unavailable", "reason": "p1_validation_failed"}
    assert "hidden" not in output
    assert "accepted" not in output


def test_workflow_keeps_frozen_source_main_identity_and_no_research_execution() -> None:
    text = workflow_text()

    assert "github.ref == 'refs/heads/main'" in text
    assert "repository: QuantStrategyLab/UsEquitySnapshotPipelines" in text
    assert "ref: ca61b82c2a508a1cc81fb5831294ba9835ac41c2" in text
    assert "uv sync --locked --no-dev --no-editable --python 3.11" in text
    assert text.index("name: Install the frozen validator runtime") < text.index(
        "name: Authenticate for this research input only"
    )
    assert "trap cleanup EXIT" in text
    assert "if: always()" in text
    assert "upload-artifact" not in text
    assert "run_soxl_core_only_p3_evidence" not in text
