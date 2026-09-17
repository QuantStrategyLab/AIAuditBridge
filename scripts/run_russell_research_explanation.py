#!/usr/bin/env python3
"""Explain one verified Russell research result with the existing Codex route."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

SOURCE_REPOSITORY = "QuantStrategyLab/UsEquitySnapshotPipelines"
SOURCE_RUN_ID = "34807588512"
SOURCE_HEAD_SHA = "e0c2d89a878a0ae9b00d26c8d87b58fe59dd1ff0"
SOURCE_FEATURE_GENERATION = "1788239901963544"
SOURCE_FEATURE_SHA256 = "493f5ff986e1d421a36e20efefc5ecef0142b01c2cca8a139942e73ee7480967"
EXPECTED_SYMBOLS = ["AMD", "INTC", "MU", "PANW"]
EXPECTED_WINDOW_START = "2026-09-01T00:00:00-04:00"
EXPECTED_WINDOW_END = "2026-09-12T00:00:00-04:00"


class RussellInputError(ValueError):
    pass


SAFE_FAILURE_CATEGORIES = {
    "quota_or_capacity_failure", "auth_or_config_failure", "transient_service_failure",
    "patch_contract_failure", "unknown_failure",
}
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
_SAFE_STATUSES = {"queued", "running", "succeeded", "failed", "deferred"}


def _safe_execution_details(response: Any) -> dict[str, str]:
    raw = response.raw if isinstance(response.raw, dict) else {}
    category = raw.get("failure_category")
    if not isinstance(category, str) or category not in SAFE_FAILURE_CATEGORIES:
        category = "unknown_failure"
    status = raw.get("status")
    if not isinstance(status, str) or status not in _SAFE_STATUSES:
        status = "unknown"
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or not _SAFE_JOB_ID.fullmatch(job_id):
        job_id = "unknown"
    return {
        "failure_category": category,
        "status": status,
        "job_id": job_id,
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RussellInputError("source metadata must be an object")
    return value


def extract_summary(log_text: str) -> dict[str, Any]:
    """Extract only the runner's final JSON summary; never expose raw logs."""
    candidates = re.findall(r"research-case[^\n]*?(\{\"data_kind\":.*\})\s*$", log_text, re.MULTILINE)
    if not candidates:
        raise RussellInputError("research summary not found")
    try:
        summary = json.loads(candidates[-1])
    except json.JSONDecodeError as exc:
        raise RussellInputError("research summary is not valid JSON") from exc
    if not isinstance(summary, dict):
        raise RussellInputError("research summary must be an object")
    return summary


def validate_input(run: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    repository = run.get("repository")
    if isinstance(repository, dict):
        repository = repository.get("full_name")
    if repository != SOURCE_REPOSITORY:
        raise RussellInputError("source repository mismatch")
    if str(run.get("id")) != SOURCE_RUN_ID or str(run.get("run_attempt")) != "1":
        raise RussellInputError("source run identity mismatch")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise RussellInputError("source run is not successful")
    if run.get("head_sha") != SOURCE_HEAD_SHA:
        raise RussellInputError("source head mismatch")
    flags = summary.get("research_flags")
    source = summary.get("source")
    required_flags = {
        "runner_kind": "research_only",
        "research_scope": "core_signal_only",
        "learning_only": True,
        "no_order": True,
        "promotion_eligible": False,
        "live_ready": False,
        "runtime_parity_verified": False,
        "size_zero_required": True,
    }
    if summary.get("data_kind") != "research" or summary.get("strategy_profile") != "russell_top50_leader_rotation":
        raise RussellInputError("research identity mismatch")
    if summary.get("variant") != "blend_top2_50_top4_50" or summary.get("sample_count") != 8:
        raise RussellInputError("research result fields mismatch")
    if not isinstance(flags, dict) or any(flags.get(k) != v for k, v in required_flags.items()):
        raise RussellInputError("research safety flags mismatch")
    if not isinstance(source, dict) or source.get("feature_generation") != SOURCE_FEATURE_GENERATION or source.get("feature_sha256") != SOURCE_FEATURE_SHA256:
        raise RussellInputError("feature source identity mismatch")
    if source.get("window_start") != EXPECTED_WINDOW_START or source.get("window_end") != EXPECTED_WINDOW_END:
        raise RussellInputError("research window mismatch")
    if any(isinstance(summary.get(key), bool) or not isinstance(summary.get(key), (int, float)) or not math.isfinite(float(summary[key])) for key in ("total_return", "max_drawdown", "fees")):
        raise RussellInputError("numeric result missing")
    return {
        "source_run": {"repository": SOURCE_REPOSITORY, "run_id": SOURCE_RUN_ID, "run_attempt": 1, "head_sha": SOURCE_HEAD_SHA},
        "source_result": {
            "strategy_profile": summary["strategy_profile"], "variant": summary["variant"], "symbols": EXPECTED_SYMBOLS, "symbols_source": "verified_run_context",
            "window_start": source["window_start"], "window_end": source["window_end"], "window_end_semantics": "exclusive", "sample_count": summary["sample_count"],
            "total_return": summary["total_return"], "max_drawdown": summary["max_drawdown"], "fees": summary["fees"],
            "initial_cash": 100000, "fee_assumption_bps_per_side": 25,
            "feature_generation": source["feature_generation"], "feature_sha256": source["feature_sha256"],
            "research_flags": {key: flags[key] for key in required_flags},
        },
    }


def build_prompt(input_record: dict[str, Any]) -> str:
    return (
        "用简短中文解释下面一项已完成的 Russell 研究结果，供人工研究记录使用。"
        "说明策略与插件/执行链的区别、这次结果和限制、下一步建议。"
        "这是历史结果解释，不是原研究任务的 AI 身份，也不是自然漂移分析。"
        "不得声称晋级、上线合格、优化胜出或建议下单；必须保留 research_only、no_order、"
        "promotion_eligible=false、live_ready=false、runtime_parity_verified=false、size_zero_required=true。"
        "只使用输入中的事实，不补充行情、交易、源码或账户信息。\n"
        + json.dumps(input_record, ensure_ascii=False, sort_keys=True)
    )


def explain(*, log_path: Path, run_path: Path, output_path: Path, source_ref: str) -> dict[str, Any]:
    input_record = validate_input(_read_json(run_path), extract_summary(log_path.read_text(encoding="utf-8")))
    from client.config import GatewayConfig
    from client.gateway_client import AiGatewayClient

    config = GatewayConfig.from_env()
    if config.research_providers != ("codex",):
        raise RussellInputError("Codex-only research route is not configured")
    from service.provider_scenarios import SCENARIO_RESEARCH_SUMMARY, resolve_execute_kwargs

    response = AiGatewayClient(config).execute(
        build_prompt(input_record),
        task="russell_research_explanation",
        **resolve_execute_kwargs(
            SCENARIO_RESEARCH_SUMMARY,
            complexity="low",
            reasoning_effort="low",
        ),
        sandbox="read-only",
        source_repository="QuantStrategyLab/AIAuditBridge",
        source_ref=source_ref,
        timeout=300,
    )
    raw = response.raw if isinstance(response.raw, dict) else {}
    if not (response.success is True and response.provider == "codex" and response.output and raw.get("status") == "succeeded" and raw.get("provider") == "codex" and raw.get("research_stage") == "research_summary"):
        raise RussellInputError("Codex result did not satisfy the existing route contract: " + json.dumps(_safe_execution_details(response), sort_keys=True))
    artifact = {
        "status": "available", "advisory_only": True, "interpretation_kind": "manual_historical_result_explanation",
        "source_run": input_record["source_run"], "source_result": input_record["source_result"],
        "ai_execution": {"job_id": raw.get("job_id"), "provider": response.provider, "model": response.model,
                         "reasoning_effort": raw.get("reasoning_effort"), "research_stage": raw.get("research_stage"),
                         "source_repository": "QuantStrategyLab/AIAuditBridge", "source_ref": source_ref,
                         "output": response.output},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-metadata", type=Path, required=True)
    parser.add_argument("--run-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    args = parser.parse_args()
    explain(log_path=args.run_log, run_path=args.run_metadata, output_path=args.output, source_ref=args.source_ref)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RussellInputError as exc:
        print(f"russell research explanation unavailable: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
    except Exception:
        print("russell research explanation unavailable: execution failure", file=__import__("sys").stderr)
        raise SystemExit(2)
