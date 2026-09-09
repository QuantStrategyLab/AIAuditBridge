#!/usr/bin/env python3
"""Gate one bounded, human-dispatched SOXL three-asset learning run through Codex."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from client.config import GatewayConfig
from client.gateway_client import AiGatewayClient

ADVICE_SCHEMA = "qsl.soxl-manual-learning-advice.v1"
ARTIFACT_SCHEMA = "qsl.soxl-manual-learning-run.v1"
NUMERIC_SCHEMA = "qsl.soxl-soxx-three-asset-learning.v1"
REPLAY_SCHEMA = "qsl.soxl-soxx-three-asset-learning-replay-result.v1"
BASELINE = 0.65
COST_BPS = (5.0, 10.0, 15.0)
DEVELOPMENT_CUTOFF = "2025-07-31"
UESP_REVISION = "b03ecbe4e0a7a0de22f298499f867a7039e4b60a"
UES_REVISION = "7756fe32585e85cf1d09a163203a02e3eee39fe1"
EXPECTED_REPOSITORY = "QuantStrategyLab/AIAuditBridge"
EXPECTED_REF = "refs/heads/main"
EXPECTED_EVENT = "workflow_dispatch"
SAFE_REASON = re.compile(r"[a-z0-9_]{1,64}\Z")


class ManualLearningError(ValueError):
    """A fixed, safe failure at the manual-learning boundary."""


def parse_parameter_grid(value: str) -> tuple[float, ...]:
    try:
        parts = value.split(",")
        values = tuple(float(item.strip()) for item in parts)
    except (AttributeError, ValueError) as exc:
        raise ManualLearningError("learning_parameters_invalid") from exc
    if (
        not 1 <= len(values) <= 3
        or any(not math.isfinite(item) or item < 0 or item > BASELINE for item in values)
        or len(set(values)) != len(values)
        or BASELINE not in values
    ):
        raise ManualLearningError("learning_parameters_invalid")
    return values


def _validate_authority(context: Mapping[str, str]) -> None:
    if (
        context.get("repository") != EXPECTED_REPOSITORY
        or context.get("ref") != EXPECTED_REF
        or context.get("event_name") != EXPECTED_EVENT
        or not context.get("actor", "").strip()
        or not context.get("run_id", "").isdigit()
        or context.get("run_attempt") != "1"
    ):
        raise ManualLearningError("manual_authority_invalid")


def _safe_base(context: Mapping[str, str], values: Sequence[float], manifest_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "operation": "soxl_learning",
        "status": "unavailable",
        "authority": {
            "event_name": context["event_name"],
            "actor": context["actor"],
            "repository": context["repository"],
            "ref": context["ref"],
            "run_id": context["run_id"],
            "run_attempt": 1,
        },
        "parameter_key": "blend_gate_mid_soxl_weight",
        "parameter_values": list(values),
        "cost_bps": list(COST_BPS),
        "development_cutoff": DEVELOPMENT_CUTOFF,
        "input_identity": {"manifest_sha256": manifest_sha256, "member_count": 4},
        "consumer_source": {
            "repository": "QuantStrategyLab/UsEquitySnapshotPipelines",
            "revision": UESP_REVISION,
        },
        "learning_only": True,
        "no_order": True,
        "size_zero_required": True,
        "promotion_eligible": False,
        "research_executed": False,
    }


def _prompt(context: Mapping[str, str], values: Sequence[float], manifest_sha256: str) -> str:
    request = {
        "task": "human_dispatched_soxl_three_asset_learning_advice",
        "authority": {
            "event_name": context["event_name"], "actor": context["actor"],
            "run_id": context["run_id"], "run_attempt": 1,
        },
        "facts": {
            "manual_research": True, "drift_detected": False, "fault_detected": False,
            "symbols": ["SOXL", "SOXX", "BOXX"],
            "parameter_key": "blend_gate_mid_soxl_weight",
            "parameter_values": list(values), "baseline": BASELINE,
            "cost_bps": list(COST_BPS), "development_cutoff": DEVELOPMENT_CUTOFF,
            "input_manifest_sha256": manifest_sha256,
            "learning_only": True, "no_order": True, "promotion_eligible": False,
        },
        "allowed_decision": "Recommend execute or reject for this exact fixed grid only.",
        "forbidden": [
            "change code, parameters, commands, data permissions, symbols, or source revisions",
            "claim drift, fault, promotion, WFA, OOS, shadow, or trading authority",
        ],
        "required_output": {
            "schema_version": ADVICE_SCHEMA,
            "recommendation": "execute|reject",
            "reason_code": "short_fixed_identifier",
            "parameter_values": list(values),
            "learning_only": True, "no_order": True, "promotion_eligible": False,
        },
    }
    return json.dumps(request, sort_keys=True, separators=(",", ":"))


def _advice(result: object, values: Sequence[float]) -> tuple[dict[str, Any] | None, str]:
    raw = getattr(result, "raw", None)
    if isinstance(raw, Mapping) and raw.get("status") == "deferred":
        return None, "deferred"
    output = getattr(result, "output", None)
    if not (
        getattr(result, "success", False) is True
        and getattr(result, "provider", None) == "codex"
        and isinstance(getattr(result, "model", None), str)
        and bool(result.model.strip())
        and isinstance(output, str)
        and output.strip()
        and not getattr(result, "error", "")
        and not getattr(result, "note", "")
        and isinstance(raw, Mapping)
        and raw.get("status") == "succeeded"
        and raw.get("provider") == "codex"
        and raw.get("research_stage") == "optimization"
        and raw.get("model") == result.model
        and raw.get("reasoning_effort") in {"low", "medium", "high", "xhigh"}
        and isinstance(raw.get("job_id"), str)
        and bool(raw["job_id"].strip())
        and raw.get("output") == output
        and raw.get("policy_verdict", "advisory") in {"ok", "eligible", "advisory"}
    ):
        return None, "unavailable"
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return None, "unavailable"
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "recommendation", "reason_code", "parameter_values",
        "learning_only", "no_order", "promotion_eligible",
    }:
        return None, "unavailable"
    if (
        value["schema_version"] != ADVICE_SCHEMA
        or value["recommendation"] not in {"execute", "reject"}
        or not isinstance(value["reason_code"], str)
        or SAFE_REASON.fullmatch(value["reason_code"]) is None
        or value["parameter_values"] != list(values)
        or value["learning_only"] is not True
        or value["no_order"] is not True
        or value["promotion_eligible"] is not False
    ):
        return None, "unavailable"
    return {
        "status": "succeeded", "job_id": raw["job_id"], "provider": "codex",
        "model": result.model, "reasoning_effort": raw["reasoning_effort"],
        "research_stage": "optimization", "recommendation": value["recommendation"],
        "reason_code": value["reason_code"],
    }, "succeeded"


def _numeric_command(root: Path, consumer: Path, ues: Path, values: Sequence[float]) -> list[str]:
    command = [
        str(consumer / ".venv/bin/python"),
        str(consumer / "scripts/run_soxl_three_asset_learning.py"),
        "--p1-binding", str(root / "binding.json"),
        "--input-manifest", str(root / "manifest.json"),
        "--bars-member", str(root / "bars.json"),
        "--ues-project", str(ues),
        "--p2-candidate", str(consumer / "config/soxl_soxx_core_only_p2_v3.json"),
    ]
    for value in values:
        command.extend(("--blend-gate-mid-soxl-weight", f"{value:g}"))
    return command


def _sanitize_numeric(value: object, values: Sequence[float], manifest_sha256: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    if not isinstance(value, Mapping) or value.get("schema_version") != NUMERIC_SCHEMA or value.get("status") != "SUCCESS":
        raise ManualLearningError("numeric_result_invalid")
    if (
        value.get("learning_only") is not True or value.get("no_order") is not True
        or value.get("size_zero_required") is not True or value.get("promotion_eligible") is not False
        or value.get("research_executed") is not True or value.get("development_cutoff") != DEVELOPMENT_CUTOFF
        or value.get("parameter_key") != "blend_gate_mid_soxl_weight"
        or value.get("trial_count") != len(values) or value.get("cost_bps") != list(COST_BPS)
    ):
        raise ManualLearningError("numeric_result_invalid")
    p1 = value.get("p1_identity")
    source = value.get("source_identity")
    if not isinstance(p1, Mapping) or p1.get("input_manifest_sha256") != manifest_sha256:
        raise ManualLearningError("numeric_result_invalid")
    if (
        not isinstance(source, Mapping)
        or source.get("repository") != "QuantStrategyLab/UsEquityStrategies"
        or source.get("revision") != UES_REVISION
        or not isinstance(source.get("quant_platform_kit_revision"), str)
        or re.fullmatch(r"[0-9a-f]{40}", source["quant_platform_kit_revision"]) is None
        or not isinstance(source.get("uv_lock_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["uv_lock_sha256"]) is None
    ):
        raise ManualLearningError("numeric_result_invalid")
    results = value.get("results")
    expected = [(parameter, cost) for parameter in values for cost in COST_BPS]
    if not isinstance(results, list) or len(results) != len(expected):
        raise ManualLearningError("numeric_result_invalid")
    safe: list[dict[str, Any]] = []
    metrics = ("strategy_profile", "sharpe_ratio", "max_drawdown", "cagr", "volatility", "total_return", "start_date", "end_date", "observation_count")
    for item, (parameter, cost) in zip(results, expected, strict=True):
        if not isinstance(item, Mapping) or item.get("schema_version") != REPLAY_SCHEMA or item.get("status") != "SUCCESS" or item.get("parameter_override") != {"blend_gate_mid_soxl_weight": parameter} or item.get("cost_bps") != cost:
            raise ManualLearningError("numeric_result_invalid")
        backtest = item.get("backtest_result")
        if not isinstance(backtest, Mapping) or any(key not in backtest for key in metrics):
            raise ManualLearningError("numeric_result_invalid")
        numeric_metrics = ("sharpe_ratio", "max_drawdown", "cagr", "volatility", "total_return")
        if any(
            isinstance(backtest[key], bool)
            or not isinstance(backtest[key], (int, float))
            or not math.isfinite(float(backtest[key]))
            for key in numeric_metrics
        ):
            raise ManualLearningError("numeric_result_invalid")
        if (
            not isinstance(backtest["observation_count"], int)
            or isinstance(backtest["observation_count"], bool)
            or backtest["observation_count"] < 2
            or not isinstance(backtest["strategy_profile"], str)
            or backtest["strategy_profile"] != "soxl_soxx_three_asset_mid_weight_learning_v1"
            or not isinstance(backtest["start_date"], str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", backtest["start_date"]) is None
            or not isinstance(backtest["end_date"], str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", backtest["end_date"]) is None
            or backtest["start_date"] > backtest["end_date"]
            or backtest["end_date"] > DEVELOPMENT_CUTOFF
            or not isinstance(item.get("output_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["output_sha256"]) is None
        ):
            raise ManualLearningError("numeric_result_invalid")
        safe.append({
            "parameter_override": {"blend_gate_mid_soxl_weight": parameter},
            "cost_bps": cost,
            "backtest_result": {key: backtest[key] for key in metrics},
            "output_sha256": item.get("output_sha256"),
        })
    digest = value.get("result_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ManualLearningError("numeric_result_invalid")
    safe_source = {
        key: source[key]
        for key in ("repository", "revision", "quant_platform_kit_revision", "uv_lock_sha256")
    }
    return safe, safe_source, digest


def run_manual_learning(
    *, parameter_grid: str, manifest_sha256: str, root: Path, consumer_source: Path,
    ues_source: Path, context: Mapping[str, str],
    client_factory: Callable[[GatewayConfig], Any] = AiGatewayClient,
    gateway_config: GatewayConfig | None = None,
    command_runner: Callable[[list[str]], Any] | None = None,
    progress_writer: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    values = parse_parameter_grid(parameter_grid)
    _validate_authority(context)
    if len(manifest_sha256) != 64 or any(char not in "0123456789abcdef" for char in manifest_sha256):
        raise ManualLearningError("input_identity_invalid")
    required = (
        root / "binding.json", root / "manifest.json", root / "bars.json",
        consumer_source / "scripts/run_soxl_three_asset_learning.py",
        consumer_source / "config/soxl_soxx_core_only_p2_v3.json",
    )
    interpreter = consumer_source / ".venv/bin/python"
    if (
        any(path.is_symlink() or not path.is_file() for path in required)
        or not interpreter.is_file()
        or ues_source.is_symlink()
        or not ues_source.is_dir()
    ):
        raise ManualLearningError("source_or_input_unavailable")
    artifact = _safe_base(context, values, manifest_sha256)
    try:
        config = gateway_config or GatewayConfig.from_env()
        result = client_factory(config).execute(
            _prompt(context, values, manifest_sha256), mode="review_only",
            research_stage="optimization", allowed_providers=["codex"],
            source_repository=EXPECTED_REPOSITORY, source_ref="main", timeout=600,
        )
    except Exception:  # noqa: BLE001 - provider detail must not cross this boundary
        artifact["failure_stage"] = "codex_unavailable"
        return artifact
    advice, state = _advice(result, values)
    if advice is None:
        artifact["status"] = state
        artifact["failure_stage"] = "codex_admission_or_result"
        return artifact
    artifact["ai_execution"] = advice
    if advice["recommendation"] != "execute":
        artifact["status"] = "rejected"
        return artifact
    runner = command_runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=1200, check=False))
    artifact["numeric_execution"] = {"status": "started"}
    artifact["research_executed"] = None
    if progress_writer is not None:
        progress_writer(artifact)
    try:
        completed = runner(_numeric_command(root, consumer_source, ues_source, values))
    except (OSError, subprocess.SubprocessError):
        artifact["status"] = "parked"
        artifact["failure_stage"] = "numeric_outcome_unknown"
        artifact["numeric_execution"] = {"status": "outcome_unknown"}
        return artifact
    if getattr(completed, "returncode", None) != 0:
        artifact["status"] = "parked"
        artifact["failure_stage"] = "numeric_execution_failed"
        artifact["numeric_execution"] = {"status": "failed"}
        return artifact
    try:
        numeric = json.loads(completed.stdout)
        safe, source, digest = _sanitize_numeric(numeric, values, manifest_sha256)
    except (AttributeError, TypeError, json.JSONDecodeError, ManualLearningError):
        artifact["status"] = "parked"
        artifact["failure_stage"] = "numeric_result_invalid"
        artifact["numeric_execution"] = {"status": "outcome_unknown"}
        return artifact
    artifact.update(
        status="accepted", research_executed=True, numeric_summary=safe,
        numeric_source_identity=source, numeric_result_sha256=digest,
        numeric_execution={"status": "succeeded"},
    )
    return artifact


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def initialize_record(path: Path, context: Mapping[str, str]) -> None:
    """Write the bound terminal placeholder before setup or remote reads begin."""
    _validate_authority(context)
    _write(
        path,
        {
            "schema_version": ARTIFACT_SCHEMA,
            "operation": "soxl_learning",
            "status": "parked",
            "failure_stage": "setup_incomplete",
            "authority": {
                "event_name": context["event_name"],
                "actor": context["actor"],
                "repository": context["repository"],
                "ref": context["ref"],
                "run_id": context["run_id"],
                "run_attempt": 1,
            },
            "research_executed": False,
            "learning_only": True,
            "no_order": True,
            "size_zero_required": True,
            "promotion_eligible": False,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameter-grid", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--consumer-source", required=True, type=Path)
    parser.add_argument("--ues-source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    args = parser.parse_args(argv)
    context = {key: getattr(args, key) for key in ("repository", "ref", "event_name", "actor", "run_id", "run_attempt")}
    try:
        result = run_manual_learning(
            parameter_grid=args.parameter_grid, manifest_sha256=args.manifest_sha256,
            root=args.root, consumer_source=args.consumer_source, ues_source=args.ues_source,
            context=context, progress_writer=lambda value: _write(args.output, value),
        )
        exit_code = 0 if result["status"] == "accepted" else 2
    except ManualLearningError as exc:
        result = {
            "schema_version": ARTIFACT_SCHEMA, "operation": "soxl_learning",
            "status": "parked", "failure_stage": str(exc), "research_executed": False,
            "learning_only": True, "no_order": True, "size_zero_required": True,
            "promotion_eligible": False,
        }
        exit_code = 2
    _write(args.output, result)
    print(json.dumps({"status": result["status"], "operation": "soxl_learning"}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
