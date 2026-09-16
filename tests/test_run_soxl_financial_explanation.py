from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_soxl_financial_explanation as explanation


def _metric() -> dict[str, object]:
    return {
        "cumulative_return": 0.1,
        "cagr": 0.2,
        "max_drawdown": -0.1,
        "expected_shortfall_95": -0.2,
        "total_cost": 12.0,
        "commission_paid": 4.0,
        "slippage_impact_vs_open": 8.0,
        "turnover": 1.2,
        "exposure_session_count": 10,
        "activity_observed": True,
        "soxx_close_path_cagr": 0.1,
        "soxx_close_path_cumulative_return": 0.05,
        "soxx_close_path_max_drawdown": -0.2,
        "soxx_close_path_basis": "gross_close_price_path_without_execution_costs",
        "matches_or_beats_soxx_drawdown": True,
        "entry_open": 99.0,
        "entry_quantity": 123.0,
        "daily_points": [{"date": "private"}],
        "buy_and_hold_benchmarks": {
            "SOXL": {"cumulative_return": 0.08, "cagr": 0.1, "max_drawdown": -0.3, "expected_shortfall_95": -0.2, "total_cost": 9.0, "commission_paid": 3.0, "slippage_impact_vs_open": 6.0, "entry_open": 98.0},
            "SOXX": {"cumulative_return": 0.04, "cagr": 0.06, "max_drawdown": -0.2, "expected_shortfall_95": -0.1, "total_cost": 9.0, "commission_paid": 3.0, "slippage_impact_vs_open": 6.0, "entry_quantity": 44.0},
        },
    }


def _raw_result() -> dict[str, object]:
    validation = {candidate: [_metric(), _metric(), _metric()] for candidate in explanation.CANDIDATES}
    post = {
        "RSI2_ENTRY_5_EXIT_70": {"FINAL_HOLDOUT": {scenario: _metric() for scenario in explanation.SCENARIOS}},
        "UNSCALED_SMA200": {"FINAL_HOLDOUT": {scenario: _metric() for scenario in explanation.SCENARIOS}},
    }
    return {
        "schema": "qsl.research.soxl_rsi2_mean_reversion.v1",
        "outcome": "NO_IMPROVEMENT",
        "failure_codes": ["GATE_FAILED"],
        "evidence_gates": {"no_lookahead": True},
        "locked_winner": "RSI2_ENTRY_5_EXIT_70",
        "validation_metrics_c2_5": validation,
        "post_lock_metrics": post,
    }


def test_projection_is_aggregate_only_and_limits_post_lock():
    projected = explanation._project_metric(_metric())
    assert "entry_open" not in projected
    assert "entry_quantity" not in projected
    assert "daily_points" not in projected
    assert set(projected["buy_and_hold_benchmarks"]) == {"SOXL", "SOXX"}
    assert "entry_open" not in projected["buy_and_hold_benchmarks"]["SOXL"]
    post = explanation._project_post_lock(_raw_result()["post_lock_metrics"], "RSI2_ENTRY_5_EXIT_70")
    assert set(post) == {"RSI2_ENTRY_5_EXIT_70", "UNSCALED_SMA200"}
    assert set(post["RSI2_ENTRY_5_EXIT_70"]) == set(explanation.SCENARIOS)


def test_explanation_runs_deterministic_evaluator_and_cleans_materialized_input(tmp_path, monkeypatch):
    p1 = tmp_path / "p1"
    p1.mkdir()
    manifest = p1 / "manifest.json"
    manifest.write_text("synthetic manifest", encoding="utf-8")
    monkeypatch.setattr(explanation, "SOXL_P1_MANIFEST_SHA256", hashlib.sha256(manifest.read_bytes()).hexdigest())
    materialized = {}

    def materialize(_p1, output, **kwargs):
        materialized["kwargs"] = kwargs
        output.mkdir(parents=True)
        for name in ("manifest.json", "artifact.json", "readback.json"):
            (output / name).write_text("{}", encoding="utf-8")
        return SimpleNamespace(manifest=output / "manifest.json", artifact=output / "artifact.json", readback=output / "readback.json")

    def load(_paths):
        return SimpleNamespace(input_digest=explanation.SOXL_OPTIMIZATION_DIGEST)

    calls = {"evaluate": 0}
    def evaluate(source):
        calls["evaluate"] += 1
        assert source.input_digest == explanation.SOXL_OPTIMIZATION_DIGEST
        return _raw_result()

    monkeypatch.setattr(explanation, "_load_pinned_api", lambda *_: (materialize, lambda *args: args, load, evaluate))
    result_root = tmp_path / "run"
    result_root.mkdir()
    sentinel = result_root / "keep.txt"
    sentinel.write_text("existing", encoding="utf-8")
    result = explanation.explain_fixed_input(
        p1_root=p1, ues_repo_root=tmp_path / "ues", qpk_repo_root=tmp_path / "qpk", run_root=result_root,
    )
    assert calls["evaluate"] == 1
    assert materialized["kwargs"]["start"] == explanation.OPTIMIZATION_START
    assert result["status"] == "NO_IMPROVEMENT"
    assert result["research_only"] is True and result["no_order"] is True
    assert result["promotion_eligible"] is False
    assert sentinel.exists()
    assert not any(result_root.glob(".soxl-financial-explanation-*"))


def test_winner_none_keeps_baseline_only_and_safe_codes_do_not_leak(tmp_path):
    raw = _raw_result()
    raw["locked_winner"] = None
    post = explanation._project_post_lock(raw["post_lock_metrics"], None)
    assert set(post) == {"UNSCALED_SMA200"}
    assert explanation._safe_code(ValueError("account=secret")) == "financial_explanation_failed"
    assert explanation._safe_code(explanation.ExplanationError("p1_manifest_mismatch")) == "p1_manifest_mismatch"


def test_missing_aggregate_metrics_fail_closed():
    metric = _metric()
    del metric["total_cost"]
    with pytest.raises(explanation.ExplanationError, match="metric_shape_invalid"):
        explanation._project_metric(metric)


def test_missing_validation_window_fails_closed():
    raw = {candidate: [_metric(), _metric()] for candidate in explanation.CANDIDATES}
    with pytest.raises(explanation.ExplanationError, match="validation_metrics_missing"):
        explanation._project_validation(raw)


@pytest.mark.skipif(os.environ.get("SOXL_RUN_INTEGRATION") != "1", reason="opt-in pinned source integration")
def test_exact_pinned_evaluator_accepts_synthetic_p1_through_explanation(tmp_path):
    ues_repo = os.environ.get("SOXL_UES_REPO_ROOT")
    qpk_repo = os.environ.get("SOXL_QPK_REPO_ROOT")
    if not ues_repo or not qpk_repo:
        pytest.fail("opt-in integration requires SOXL_UES_REPO_ROOT and SOXL_QPK_REPO_ROOT")
    code = textwrap.dedent(
        """
        import hashlib, io, os, subprocess, sys, tarfile, tempfile
        from pathlib import Path
        from test_run_new_research import _alpaca_source_root
        import scripts.run_soxl_financial_explanation as explanation
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for source, revision, name in ((sys.argv[1], sys.argv[3], "ues"), (sys.argv[2], sys.argv[4], "qpk")):
                archive = subprocess.check_output(["git", "-C", source, "archive", revision])
                target = root / name
                target.mkdir()
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
                    bundle.extractall(target)
            p1 = _alpaca_source_root(root)
            for name in tuple(sys.modules):
                if name == "us_equity_strategies" or name.startswith("us_equity_strategies.") or name == "quant_platform_kit" or name.startswith("quant_platform_kit."):
                    del sys.modules[name]
            explanation._verify_source = lambda *_: None
            api = explanation._load_pinned_api(root / "ues", root / "qpk")
            materialize, paths_type, load_input, _evaluate = api
            expected = materialize(p1, root / "expected", start=explanation.OPTIMIZATION_START, end_exclusive=explanation.OPTIMIZATION_END, expected_sessions=explanation.OPTIMIZATION_SESSIONS)
            expected_source = load_input(paths_type(expected.manifest, expected.artifact, expected.readback))
            manifest = p1 / "manifest.json"
            with manifest.open("rb") as handle:
                explanation.SOXL_P1_MANIFEST_SHA256 = hashlib.sha256(handle.read()).hexdigest()
            explanation.SOXL_OPTIMIZATION_DIGEST = expected_source.input_digest
            explanation._load_pinned_api = lambda *_: api
            result = explanation.explain_fixed_input(
                p1_root=p1, ues_repo_root=root / "ues", qpk_repo_root=root / "qpk", run_root=root / "run",
            )
            assert result["schema"] == "qsl.soxl-financial-explanation.v1"
            assert result["status"] == "NO_IMPROVEMENT"
            assert set(result["validation_metrics_c2_5"]) == set(explanation.CANDIDATES)
            assert set(result["post_lock_metrics"]) == {"UNSCALED_SMA200"}
            assert "total_cost" in result["validation_metrics_c2_5"]["UNSCALED_SMA200"][0]
            assert set(result["validation_metrics_c2_5"]["UNSCALED_SMA200"][0]["buy_and_hold_benchmarks"]) == {"SOXL", "SOXX"}
            print("ok")
        """
    )
    env = os.environ.copy()
    site_packages = str(Path(sys.executable).parent.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
    env["PYTHONPATH"] = os.pathsep.join((str(Path(__file__).parent), site_packages, env.get("PYTHONPATH", "")))
    completed = subprocess.run(
        [sys.executable, "-S", "-c", code, ues_repo, qpk_repo, "86aa4e03c30eb2fb561748d6e9c22e68d3267cfa", "de13e486da1bdba60f425e576e944591fc97b809"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_source_verifier_rejects_unavailable_checkout(tmp_path):
    ues = tmp_path / "ues"
    qpk = tmp_path / "qpk"
    with pytest.raises(explanation.ExplanationError, match="source_identity_unavailable"):
        explanation._verify_source(ues, qpk)


def test_manifest_and_digest_mismatch_fail_closed(tmp_path, monkeypatch):
    p1 = tmp_path / "p1"
    p1.mkdir()
    manifest = p1 / "manifest.json"
    manifest.write_text("wrong", encoding="utf-8")
    with pytest.raises(explanation.ExplanationError, match="p1_manifest_mismatch"):
        explanation.explain_fixed_input(p1_root=p1, ues_repo_root=tmp_path / "ues", qpk_repo_root=tmp_path / "qpk", run_root=tmp_path / "run")

    manifest.write_text("valid synthetic manifest", encoding="utf-8")
    monkeypatch.setattr(explanation, "SOXL_P1_MANIFEST_SHA256", hashlib.sha256(manifest.read_bytes()).hexdigest())

    def materialize(_p1, output, **_kwargs):
        output.mkdir(parents=True)
        for name in ("manifest.json", "artifact.json", "readback.json"):
            (output / name).write_text("{}", encoding="utf-8")
        return SimpleNamespace(manifest=output / "manifest.json", artifact=output / "artifact.json", readback=output / "readback.json")

    def load(_paths):
        return SimpleNamespace(input_digest="wrong" * 16)

    monkeypatch.setattr(explanation, "_load_pinned_api", lambda *_: (materialize, lambda *args: args, load, lambda _source: _raw_result()))
    with pytest.raises(explanation.ExplanationError, match="optimization_input_digest_mismatch"):
        explanation.explain_fixed_input(p1_root=p1, ues_repo_root=tmp_path / "ues", qpk_repo_root=tmp_path / "qpk", run_root=tmp_path / "run")


def test_cleanup_failure_is_safe_and_fail_closed(tmp_path, monkeypatch):
    p1 = tmp_path / "p1"
    p1.mkdir()
    manifest = p1 / "manifest.json"
    manifest.write_text("valid synthetic manifest", encoding="utf-8")
    monkeypatch.setattr(explanation, "SOXL_P1_MANIFEST_SHA256", hashlib.sha256(manifest.read_bytes()).hexdigest())

    def materialize(_p1, output, **_kwargs):
        output.mkdir(parents=True)
        for name in ("manifest.json", "artifact.json", "readback.json"):
            (output / name).write_text("{}", encoding="utf-8")
        return SimpleNamespace(manifest=output / "manifest.json", artifact=output / "artifact.json", readback=output / "readback.json")

    monkeypatch.setattr(explanation, "_load_pinned_api", lambda *_: (materialize, lambda *args: args, lambda _paths: SimpleNamespace(input_digest=explanation.SOXL_OPTIMIZATION_DIGEST), lambda _source: _raw_result()))
    monkeypatch.setattr(explanation.shutil, "rmtree", lambda _path: (_ for _ in ()).throw(OSError("cleanup")))
    with pytest.raises(explanation.ExplanationError, match="financial_explanation_input_invalid"):
        explanation.explain_fixed_input(p1_root=p1, ues_repo_root=tmp_path / "ues", qpk_repo_root=tmp_path / "qpk", run_root=tmp_path / "run")
