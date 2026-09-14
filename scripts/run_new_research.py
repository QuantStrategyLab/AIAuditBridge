#!/usr/bin/env python3
"""Run one source-bound, non-live research request through the shared QPK cycle."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from quant_platform_kit.research_factory import (
    RESEARCH_SOURCE_RECEIPT_SCHEMA_VERSION,
    ResearchSourceReceipt,
    ResearchWorkerManifest,
    ResearchWorkerRole,
    validate_source_receipt,
    validate_worker_manifest,
)
from quant_platform_kit.strategy_lifecycle.candidate_control import ResearchSourceReceiptRef
from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import (
    NewResearchRequest,
    ResearchPromotionBudget,
    run_saved_research_promotion_cycle,
)


_REVISION = re.compile(r"^[0-9a-f]{40}$")
_WORKER_CAPABILITIES = frozenset({"quarantine_read", "sandbox_write"})
_LOCAL_STRATEGY_FACTS = {
    "strategy_description": "SOXL/SOXX RSI2 mean reversion with SMA200 baseline and SOXL next-open execution.",
    "plugins": ["SOXL RSI2", "SMA200", "SOXL next-open"],
    "limitations": ["plugins disabled", "research_only", "strict promotion evidence required"],
}
_REQUIRED = frozenset({
    "request", "worker_manifest", "source_receipts", "research_identity",
    "source_commit", "source_blobs", "input_paths", "output_root", "ticket_dir",
})
_OPTIONAL = frozenset({"ues_repo_root", "caller_ref", "strategy_facts"})


class NewResearchInputError(ValueError):
    """Sanitized request validation failure."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception as exc:
        raise NewResearchInputError("request_json_invalid") from exc


def _receipt(raw: Mapping[str, Any]) -> ResearchSourceReceipt:
    try:
        value = ResearchSourceReceipt(
            schema_version=str(raw["schema_version"]),
            source_id=str(raw["source_id"]),
            source_url=str(raw["source_url"]),
            publisher=str(raw["publisher"]),
            retrieved_at=datetime.fromisoformat(str(raw["retrieved_at"]).replace("Z", "+00:00")),
            content_sha256=str(raw["content_sha256"]),
            declared_license=raw.get("declared_license"),
            usage_scope=str(raw["usage_scope"]),
            license_review_id=raw.get("license_review_id"),
            untrusted=raw.get("untrusted", True),
        )
    except Exception as exc:
        raise NewResearchInputError("source_receipt_invalid") from exc
    if validate_source_receipt(value) or raw.get("receipt_sha256") != value.receipt_sha256:
        raise NewResearchInputError("source_receipt_invalid")
    return value


def _worker(raw: Mapping[str, Any]) -> None:
    try:
        value = ResearchWorkerManifest(
            schema_version=str(raw["schema_version"]), worker_id=str(raw["worker_id"]),
            role=ResearchWorkerRole(str(raw["role"])),
            capabilities=frozenset(raw["capabilities"]),
            secret_access=raw.get("secret_access", False),
            broker_access=raw.get("broker_access", False),
            cloud_runtime_access=raw.get("cloud_runtime_access", False),
            deployment_write_access=raw.get("deployment_write_access", False),
        )
    except Exception as exc:
        raise NewResearchInputError("worker_manifest_invalid") from exc
    if value.role is not ResearchWorkerRole.PLANNER_BUILDER or value.capabilities != _WORKER_CAPABILITIES:
        raise NewResearchInputError("worker_manifest_invalid")
    if validate_worker_manifest(value):
        raise NewResearchInputError("worker_manifest_invalid")


def _request(raw: Mapping[str, Any], receipts: list[ResearchSourceReceipt]) -> NewResearchRequest:
    try:
        refs = tuple(sorted(
            (ResearchSourceReceiptRef(RESEARCH_SOURCE_RECEIPT_SCHEMA_VERSION, item.receipt_sha256)
             for item in receipts),
            key=lambda item: item.receipt_sha256,
        ))
        value = NewResearchRequest(
            strategy_profile=str(raw["strategy_profile"]), domain=str(raw["domain"]),
            as_of=date.fromisoformat(str(raw["as_of"])),
            source_revision=str(raw["source_revision"]),
            research_intent=str(raw["research_intent"]), source_receipt_refs=refs,
        )
    except Exception as exc:
        raise NewResearchInputError("new_research_request_invalid") from exc
    if value.strategy_profile != "soxl_rsi2_mean_reversion" or value.domain != "us_equity":
        raise NewResearchInputError("new_research_target_invalid")
    return value


def _identity(raw: Any) -> dict[str, str]:
    fields = {"code_revision", "input_revision", "param_space_revision", "cost_model_revision", "validator_revision"}
    if not isinstance(raw, Mapping) or set(raw) != fields or any(not isinstance(v, str) or not v.strip() for v in raw.values()):
        raise NewResearchInputError("research_identity_invalid")
    return dict(raw)


def _digest_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) or not _REVISION.fullmatch(v) for k, v in raw.items()):
        raise NewResearchInputError("source_blobs_invalid")
    return dict(raw)


def _codex_callbacks(*, source_ref: str, facts: Mapping[str, Any]):
    """Bind the existing Codex-only bounded design and summary calls.

    The callbacks only receive frozen local request/facts.  Their text is
    advisory; lifecycle gates and source evidence remain deterministic.
    """
    from ai_gateway_client import AiGatewayClient, GatewayConfig

    config = GatewayConfig.from_env()
    if config.research_providers != ("codex",):
        raise NewResearchInputError("codex_only_required")
    client = AiGatewayClient(config)
    encoded_facts = json.dumps(dict(facts), ensure_ascii=False, allow_nan=False, sort_keys=True)

    def diagnose(context, _budget):
        prompt = (
            "只做受限研究设计选择。只能解释下面冻结的请求和本地事实，禁止联网、工具、"
            "执行代码、改生产代码、交易或改变准入。只输出 JSON，字段必须恰好为 "
            "optimization_needed (boolean) 和 design (string，不超过240字符)。"
            f"\nREQUEST:\n{json.dumps(context.to_dict(), ensure_ascii=False, sort_keys=True)}"
            f"\nLOCAL_FACTS:\n{encoded_facts}"
        )
        response = client.execute(
            prompt, task="new_research_design", mode="review_only", complexity="low",
            research_stage="optimization", sandbox="read-only",
            allowed_providers=["codex"], source_repository="QuantStrategyLab/AIAuditBridge",
            source_ref=source_ref, timeout=300,
        )
        raw = response.raw if isinstance(response.raw, dict) else {}
        if not (response.success is True and raw.get("status") == "succeeded"
                and response.provider == "codex"
                and raw.get("provider") == "codex"
                and raw.get("research_stage") == "optimization"):
            if (raw.get("status") == "deferred" and type(raw.get("retry_at")) in (int, float)
                    and raw["retry_at"] > time.time()):
                return {"optimization_needed": False, "reason": "codex_research_deferred",
                        "retry_at": raw["retry_at"]}
            raise NewResearchInputError("codex_result_invalid")
        try:
            result = json.loads(response.output)
        except Exception:
            raise NewResearchInputError("codex_result_invalid") from None
        if (not isinstance(result, dict) or set(result) != {"optimization_needed", "design"}
                or type(result["optimization_needed"]) is not bool
                or not isinstance(result["design"], str) or not result["design"].strip()
                or len(result["design"]) > 240):
            raise NewResearchInputError("codex_result_invalid")
        effort = raw.get("reasoning_effort")
        job_id = raw.get("job_id")
        if (not isinstance(response.model, str) or not response.model.strip()
                or not isinstance(effort, str) or not effort.strip()
                or not isinstance(job_id, str) or not job_id.strip()):
            raise NewResearchInputError("codex_result_invalid")
        return {"optimization_needed": result["optimization_needed"],
                "design": result["design"].strip(), "provider": response.provider,
                "model": response.model, "reasoning_effort": effort, "job_id": job_id}

    from scripts.research_summary import make_summary_callback
    summarize = make_summary_callback(
        runtime=SimpleNamespace(config=GatewayConfig, client=AiGatewayClient),
        revision=source_ref, repository="QuantStrategyLab/AIAuditBridge",
        local_facts=facts,
        validate_context=lambda value: dict(value) if isinstance(value, Mapping) else (_ for _ in ()).throw(ValueError("summary_context_invalid")),
        research_stage="research_summary",
    )

    return diagnose, summarize


def run_request(
    payload: Mapping[str, Any], *, diagnose=None, summarize=None,
    promotion_binding=None, promotion_store=None, promotion_shadow_recorder=None,
    read_pending_shadow=None, sync_console=None, pull_console=None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or not _REQUIRED <= set(payload) or set(payload) - _REQUIRED - _OPTIONAL:
        raise NewResearchInputError("new_research_input_fields_invalid")
    _worker(payload["worker_manifest"])
    if not isinstance(payload["source_receipts"], list) or not payload["source_receipts"]:
        raise NewResearchInputError("source_receipts_required")
    receipts = [_receipt(item) for item in payload["source_receipts"] if isinstance(item, Mapping)]
    if len(receipts) != len(payload["source_receipts"]):
        raise NewResearchInputError("source_receipt_invalid")
    request = _request(payload["request"], receipts)
    source_commit = payload["source_commit"]
    if not isinstance(source_commit, str) or not _REVISION.fullmatch(source_commit):
        raise NewResearchInputError("source_commit_invalid")
    source_blobs = _digest_map(payload["source_blobs"])
    paths = payload["input_paths"]
    if not isinstance(paths, Mapping) or set(paths) != {"manifest", "artifact", "readback"}:
        raise NewResearchInputError("input_paths_invalid")
    from us_equity_strategies.research.soxl_rsi2_research_adapter import (
        Rsi2OfflineInputPaths, _validate_research_identity, load_rsi2_offline_input,
        make_soxl_rsi2_optimize, prepare_soxl_rsi2_promotion,
    )
    research_identity = _identity(payload["research_identity"])
    paths_value = Rsi2OfflineInputPaths(**{key: Path(paths[key]) for key in paths})
    try:
        typed_source = load_rsi2_offline_input(paths_value)
        _validate_research_identity(typed_source, source_commit, research_identity)
    except Exception:
        raise NewResearchInputError("rsi2_research_identity_mismatch") from None
    facts = payload.get("strategy_facts", _LOCAL_STRATEGY_FACTS)
    if facts != _LOCAL_STRATEGY_FACTS:
        raise NewResearchInputError("strategy_facts_unverified")
    optimize = make_soxl_rsi2_optimize(
        input_paths=paths_value,
        output_root=Path(str(payload["output_root"])), source_commit=source_commit,
        source_blobs=source_blobs, ues_repo_root=payload.get("ues_repo_root"),
        source=typed_source, expected_identity=research_identity,
    )
    try:
        cycle_identity, enforce_backtest_gates, record_shadow = prepare_soxl_rsi2_promotion(
            optimization_source=typed_source,
            source_commit=source_commit,
            research_identity=research_identity,
            promotion_binding=promotion_binding,
            promotion_store=promotion_store,
            promotion_shadow_recorder=promotion_shadow_recorder,
        )
    except Exception as exc:
        if isinstance(exc, NewResearchInputError):
            raise
        raise NewResearchInputError("rsi2_promotion_binding_invalid") from None
    if diagnose is None and summarize is None:
        source_ref = payload.get("caller_ref")
        if (not isinstance(facts, Mapping) or not isinstance(source_ref, str)
                or not _REVISION.fullmatch(source_ref)):
            raise NewResearchInputError("strategy_facts_or_caller_ref_invalid")
        diagnose, summarize = _codex_callbacks(source_ref=source_ref, facts=facts)
    result = run_saved_research_promotion_cycle(
        new_request=request, research_identity=cycle_identity,
        ticket_dir=Path(str(payload["ticket_dir"])), optimize=optimize,
        enforce_backtest_gates=enforce_backtest_gates,
        record_shadow=record_shadow,
        read_pending_shadow=read_pending_shadow,
        budget=ResearchPromotionBudget(require_paired_shadow=True),
        diagnose=diagnose, summarize=summarize,
        sync_console=sync_console, pull_console=pull_console,
    )
    summary = {key: result[key] for key in ("status", "reason", "research_key", "resumed", "live_authority_granted") if key in result}
    ticket = result.get("ticket")
    if isinstance(ticket, Mapping):
        summary.update({key: ticket[key] for key in ("state", "drift_score", "drift_status", "notes") if key in ticket})
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_request(_read_json(args.request))
        args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return 0
    except NewResearchInputError as exc:
        print(f"new_research_{exc}")
        return 2
    except Exception:
        print("new_research_unavailable")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
