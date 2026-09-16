#!/usr/bin/env python3
"""Produce bounded, research-only SOXL rejection explanations from fixed input."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping

SOXL_P1_MANIFEST_SHA256 = "b39008e05eeebd4a126eefe289a18bcae05869d937dc76e6040b71d3b0daf36d"
SOXL_UES_COMMIT = "86aa4e03c30eb2fb561748d6e9c22e68d3267cfa"
SOXL_QPK_COMMIT = "de13e486da1bdba60f425e576e944591fc97b809"
SOXL_OPTIMIZATION_DIGEST = "108335c438b07927b10de53317f90aaf0b797bf53b3f029ad9167dba22f78a2a"
OPTIMIZATION_START = "2022-01-03"
OPTIMIZATION_END = "2025-01-01"
OPTIMIZATION_SESSIONS = 753
SCENARIOS = ("C2_5", "C5_10_STRESS")
CANDIDATES = (
    "RSI2_ENTRY_5_EXIT_70",
    "RSI2_ENTRY_10_EXIT_70",
    "RSI2_ENTRY_15_EXIT_70",
    "UNSCALED_SMA200",
)
_SAFE_FAILURE_REASONS = frozenset({
    "p1_manifest_mismatch", "source_identity_unavailable", "source_identity_mismatch",
    "source_module_preloaded", "pinned_source_import_unavailable",
    "optimization_input_digest_mismatch", "metric_shape_invalid", "validation_metrics_missing",
    "post_lock_winner_missing", "post_lock_missing", "evidence_gates_invalid",
    "evaluation_result_invalid", "financial_explanation_input_invalid",
})

class ExplanationError(ValueError):
    pass


def _safe_code(exc: BaseException) -> str:
    value = str(exc)
    return value if value in _SAFE_FAILURE_REASONS else "financial_explanation_failed"


def _git_head(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise ExplanationError("source_identity_unavailable") from None


def _verify_source(ues_root: Path, qpk_root: Path) -> None:
    if _git_head(ues_root) != SOXL_UES_COMMIT or _git_head(qpk_root) != SOXL_QPK_COMMIT:
        raise ExplanationError("source_identity_mismatch")
    tracked = (
        (ues_root, "src/us_equity_strategies/research/soxl_core_optimization.py"),
        (ues_root, "src/us_equity_strategies/research/soxl_soxx_offline_input_contract.py"),
        (ues_root, "src/us_equity_strategies/research/soxl_soxx_typed_baseline_result.py"),
    )
    try:
        for repo, relative in tracked:
            expected = subprocess.run(["git", "-C", str(repo), "rev-parse", f"HEAD:{relative}"], check=True, capture_output=True, text=True).stdout.strip()
            actual = subprocess.run(["git", "-C", str(repo), "hash-object", relative], check=True, capture_output=True, text=True).stdout.strip()
            if expected != actual:
                raise ExplanationError("source_identity_mismatch")
        for repo in (ues_root, qpk_root):
            changed = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", "HEAD", "--", "src"], check=True, capture_output=True, text=True).stdout.strip()
            untracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "--", "src"], check=True, capture_output=True, text=True).stdout.strip()
            changed_code = [line for line in changed.splitlines() if line.endswith(".py")]
            untracked_code = [line for line in untracked.splitlines() if line.endswith(".py")]
            if changed_code or untracked_code:
                raise ExplanationError("source_identity_mismatch")
    except ExplanationError:
        raise
    except (OSError, subprocess.CalledProcessError):
        raise ExplanationError("source_identity_unavailable") from None


def _load_pinned_api(ues_root: Path, qpk_root: Path):
    _verify_source(ues_root, qpk_root)
    if any(name == "us_equity_strategies" or name.startswith("us_equity_strategies.") or name == "quant_platform_kit" or name.startswith("quant_platform_kit.") for name in sys.modules):
        raise ExplanationError("source_module_preloaded")
    sys.path[:0] = [str(ues_root / "src"), str(qpk_root / "src")]
    try:
        from us_equity_strategies.research.soxl_alpaca_input_adapter import materialize_soxl_alpaca_input
        from us_equity_strategies.research.soxl_rsi2_research_adapter import Rsi2OfflineInputPaths, load_rsi2_offline_input
        from us_equity_strategies.research.soxl_core_optimization import run_soxl_rsi2_mean_reversion
    except ImportError:
        raise ExplanationError("pinned_source_import_unavailable") from None
    roots = (ues_root / "src").resolve(), (qpk_root / "src").resolve()
    for name, module in tuple(sys.modules.items()):
        if name == "us_equity_strategies" or name.startswith("us_equity_strategies.") or name == "quant_platform_kit" or name.startswith("quant_platform_kit."):
            origin = getattr(module, "__file__", None)
            locations = [origin] if origin is not None else list(getattr(module, "__path__", ()))
            if not locations or not all(any(Path(location).resolve().is_relative_to(root) for root in roots) for location in locations):
                raise ExplanationError("source_identity_mismatch")
    return materialize_soxl_alpaca_input, Rsi2OfflineInputPaths, load_rsi2_offline_input, run_soxl_rsi2_mean_reversion


def _project_metric(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ExplanationError("metric_shape_invalid")
    required = ("cumulative_return", "cagr", "max_drawdown", "expected_shortfall_95", "total_cost", "commission_paid", "slippage_impact_vs_open")
    if any(key not in value for key in required):
        raise ExplanationError("metric_shape_invalid")
    scalar_keys = (
        "observation_count", "cumulative_return", "cagr", "max_drawdown", "expected_shortfall_95", "turnover",
        "total_cost", "commission_paid", "slippage_impact_vs_open", "exposure_session_count", "activity_observed",
        "max_drawdown_recovery_sessions", "max_drawdown_unrecovered_sessions",
        "soxx_close_path_cumulative_return",
        "soxx_close_path_cagr", "soxx_close_path_max_drawdown", "soxx_close_path_basis",
        "matches_or_beats_soxx_drawdown",
    )
    output = {key: value[key] for key in scalar_keys if key in value}
    benchmarks = value.get("buy_and_hold_benchmarks")
    if not isinstance(benchmarks, Mapping) or set(benchmarks) != {"SOXL", "SOXX"}:
        raise ExplanationError("metric_shape_invalid")
    projected: dict[str, object] = {}
    for symbol in ("SOXL", "SOXX"):
        metric = benchmarks[symbol]
        if not isinstance(metric, Mapping) or any(key not in metric for key in required):
            raise ExplanationError("metric_shape_invalid")
        projected[symbol] = {key: metric[key] for key in scalar_keys if key in metric}
    output["buy_and_hold_benchmarks"] = projected
    return output


def _project_validation(raw: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(raw, Mapping):
        raise ExplanationError("validation_metrics_missing")
    output: dict[str, list[dict[str, object]]] = {}
    for candidate in CANDIDATES:
        values = raw.get(candidate)
        if not isinstance(values, (list, tuple)) or len(values) != 3:
            raise ExplanationError("validation_metrics_missing")
        output[candidate] = [_project_metric(metric) for metric in values]
    return output


def _project_post_lock(raw: object, winner: object) -> dict[str, dict[str, dict[str, object]]]:
    if not isinstance(raw, Mapping):
        raise ExplanationError("post_lock_missing")
    if winner is None:
        candidates = ("UNSCALED_SMA200",)
    elif isinstance(winner, str) and winner in CANDIDATES:
        candidates = (winner, "UNSCALED_SMA200") if winner != "UNSCALED_SMA200" else ("UNSCALED_SMA200",)
    else:
        raise ExplanationError("post_lock_winner_missing")
    output: dict[str, dict[str, dict[str, object]]] = {}
    for candidate in candidates:
        value = raw.get(candidate)
        if not isinstance(value, Mapping):
            raise ExplanationError("post_lock_missing")
        output[candidate] = {}
        for scenario in SCENARIOS:
            metric = value.get("FINAL_HOLDOUT", {}).get(scenario) if isinstance(value.get("FINAL_HOLDOUT"), Mapping) else None
            if metric is None:
                raise ExplanationError("post_lock_missing")
            output[candidate][scenario] = _project_metric(metric)
    return output


def explain_fixed_input(*, p1_root: str | Path, ues_repo_root: str | Path, qpk_repo_root: str | Path, run_root: str | Path) -> dict[str, object]:
    base = Path(run_root)
    try:
        base.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix=".soxl-financial-explanation-", dir=base))
    except OSError:
        raise ExplanationError("financial_explanation_input_invalid") from None
    optimization_root = root / "input-optimization"
    try:
        p1 = Path(p1_root)
        manifest = p1 / "manifest.json"
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != SOXL_P1_MANIFEST_SHA256:
            raise ExplanationError("p1_manifest_mismatch")
        materialize, paths_type, load_input, evaluate = _load_pinned_api(Path(ues_repo_root), Path(qpk_repo_root))
        paths = materialize(p1, optimization_root, start=OPTIMIZATION_START, end_exclusive=OPTIMIZATION_END, expected_sessions=OPTIMIZATION_SESSIONS)
        source = load_input(paths_type(paths.manifest, paths.artifact, paths.readback))
        if getattr(source, "input_digest", None) != SOXL_OPTIMIZATION_DIGEST:
            raise ExplanationError("optimization_input_digest_mismatch")
        result = evaluate(source)
        if not isinstance(result, Mapping) or result.get("schema") != "qsl.research.soxl_rsi2_mean_reversion.v1":
            raise ExplanationError("evaluation_result_invalid")
        gates = result.get("evidence_gates")
        if not isinstance(gates, Mapping) or any(type(value) is not bool for value in gates.values()):
            raise ExplanationError("evidence_gates_invalid")
        winner = result.get("locked_winner")
        return {
            "schema": "qsl.soxl-financial-explanation.v1",
            "status": "NO_IMPROVEMENT" if result.get("outcome") == "NO_IMPROVEMENT" else "CHARACTERIZATION_ONLY",
            "research_only": True, "no_order": True, "promotion_eligible": False,
            "seen_development": True, "failure_codes": list(result.get("failure_codes", [])),
            "evidence_gates": dict(gates), "validation_metrics_c2_5": _project_validation(result.get("validation_metrics_c2_5")),
            "post_lock_metrics": _project_post_lock(result.get("post_lock_metrics"), winner),
            "source_identity": {"ues_commit": SOXL_UES_COMMIT, "qpk_commit": SOXL_QPK_COMMIT, "p1_manifest_sha256": SOXL_P1_MANIFEST_SHA256, "optimization_input_digest": SOXL_OPTIMIZATION_DIGEST},
            "window": {"start": OPTIMIZATION_START, "end_exclusive": OPTIMIZATION_END, "sessions": OPTIMIZATION_SESSIONS},
        }
    except ExplanationError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ExplanationError("financial_explanation_input_invalid") from None
    finally:
        try:
            shutil.rmtree(root)
        except OSError:
            raise ExplanationError("financial_explanation_input_invalid") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p1-root", required=True, type=Path)
    parser.add_argument("--ues-repo-root", required=True, type=Path)
    parser.add_argument("--qpk-repo-root", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path, help="explicit temporary parent, preferably under /dev/shm")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(explain_fixed_input(p1_root=args.p1_root, ues_repo_root=args.ues_repo_root, qpk_repo_root=args.qpk_repo_root, run_root=args.run_root), ensure_ascii=False, sort_keys=True))
    except ExplanationError as exc:
        print(json.dumps({"status": "failed", "failure_reason": _safe_code(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
