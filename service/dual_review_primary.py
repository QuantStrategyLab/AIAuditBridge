"""Codex primary review for dual-review pipeline (task 11b)."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from client.config import GatewayConfig
from client.gateway_client import AiGatewayClient
from service.dual_review import VERDICT_FAIL, VERDICT_INVALID, VERDICT_PASS, VERDICT_UNAVAILABLE, extract_verdict
from service.dual_review_secondary import parse_llm_review_output

_PRIMARY_SYSTEM = (
    "You are the primary Codex reviewer for quantitative strategy promotion, risk, and recovery decisions. "
    "Respond with JSON only: "
    '{"verdict":"approve"|"reject","confidence":0.0-1.0,"summary":"..."}'
)


def build_primary_prompt(
    *,
    trigger: str,
    strategy_profile: str,
    context: dict[str, Any],
    evidence_path: Path | None = None,
) -> str:
    lines = [
        f"Strategy profile: {strategy_profile}",
        f"Trigger: {trigger}",
    ]
    for key in (
        "domain",
        "old_status",
        "new_status",
        "drift_sigma",
        "drift_score",
        "repository",
        "reconciliation_candidate_sha256",
        "source_evidence_count",
        "observation_window_seconds",
    ):
        value = context.get(key)
        if value not in (None, ""):
            lines.append(f"{key}: {value}")
    monthly = context.get("monthly_hit_rates")
    if isinstance(monthly, list) and monthly:
        lines.append(f"monthly_hit_rates: {monthly}")
    if evidence_path and evidence_path.is_file():
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if isinstance(evidence, dict):
                summary_bits = {
                    k: evidence.get(k)
                    for k in (
                        "strategy_profile",
                        "status",
                        "oos_sharpe",
                        "max_drawdown",
                        "hit_rate",
                        "evidence_version",
                    )
                    if evidence.get(k) not in (None, "")
                }
                if summary_bits:
                    lines.append(f"evidence_summary: {json.dumps(summary_bits, ensure_ascii=False)}")
        except (OSError, json.JSONDecodeError):
            lines.append(f"evidence_file: {evidence_path}")
    lines.append(
        "Provide an independent primary review. For reconciliation_baseline, approve only when the "
        "candidate is a fresh, matching, read-only observation set; this decision still requires "
        "independent secondary approval and never authorises orders."
    )
    return "\n".join(lines)


def parse_primary_review_output(output: str) -> dict[str, Any]:
    review = parse_llm_review_output(output, provider="codex", model="codex-primary")
    review["source"] = "codex_primary"
    return review


def run_codex_primary_review(
    *,
    prompt: str,
    timeout_minutes: int | None = None,
    research_stage: str = "",
) -> dict[str, Any]:
    """Call VPS Codex audit service for the primary review."""
    if research_stage:
        return _run_codex_research_primary_review(
            prompt=prompt, research_stage=research_stage, timeout_minutes=timeout_minutes,
        )
    service_url = str(os.environ.get("CODEX_AUDIT_SERVICE_URL") or "").strip()
    if not service_url:
        raise RuntimeError("CODEX_AUDIT_SERVICE_URL is not configured")

    timeout = int(timeout_minutes or os.environ.get("DUAL_REVIEW_PRIMARY_TIMEOUT_MINUTES", "15"))
    review_prompt = f"{_PRIMARY_SYSTEM}\n\n{prompt}"
    result = AiGatewayClient(GatewayConfig.from_env()).execute(
        review_prompt,
        task="dual_review",
        mode="review_only",
        complexity="high",
        source_repository=os.environ.get("GITHUB_REPOSITORY") or None,
        timeout=timeout * 60,
    )
    if not result.success:
        message = result.error or result.note or "Codex service review unavailable"
        failure_category = ""
        if isinstance(result.raw, dict):
            failure_category = str(result.raw.get("failure_category") or "").strip().lower()
        unavailable_markers = (
            "daily budget exceeded",
            "quota",
            "http 429",
            "status 429",
            "service job failed [unknown_failure]: codex exec failed",
            "not configured on the service host",
            "request timed out",
            "too many active jobs",
        )
        unavailable = failure_category in {
            "auth_or_config_failure",
            "quota_or_capacity_failure",
            "service_restart",
            "stale_job_timeout",
            "transient_service_failure",
        } or any(
            marker in message.lower() for marker in unavailable_markers
        )
        verdict = VERDICT_UNAVAILABLE if unavailable else VERDICT_INVALID
        return {
            "source": "codex_primary",
            "verdict": verdict,
            "confidence": 0.0,
            "error": message,
        }
    return parse_primary_review_output(result.output)


def _run_codex_research_primary_review(
    *, prompt: str, research_stage: str, timeout_minutes: int | None,
) -> dict[str, Any]:
    """Research-only Codex path; legacy recovery/API review paths stay separate."""
    def unavailable(reason: str, *, verdict: str = VERDICT_UNAVAILABLE) -> dict[str, Any]:
        return {"source": "codex_primary", "provider": "codex", "research_stage": "promotion_review",
                "verdict": verdict, "confidence": 0.0, "error": reason}

    if research_stage != "promotion_review":
        return unavailable("invalid_research_stage", verdict=VERDICT_INVALID)
    if not all(os.environ.get(key, "").strip() for key in (
        "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    )):
        return unavailable("github_oidc_required")
    try:
        config = GatewayConfig.from_env()
        source_repository = config.source_repository or os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not source_repository:
            return unavailable("source_repository_required")
        timeout = int(timeout_minutes or os.environ.get("DUAL_REVIEW_PRIMARY_TIMEOUT_MINUTES", "15"))
        if not 1 <= timeout <= 60:
            return unavailable("research_primary_invalid_timeout")
        result = AiGatewayClient(config).execute(
            f"{_PRIMARY_SYSTEM}\n\n{prompt}", task="dual_review", mode="review_only",
            research_stage="promotion_review", reasoning_effort="xhigh", allowed_providers=["codex"],
            source_repository=source_repository, timeout=timeout * 60,
        )
    except Exception:
        return unavailable("research_primary_unavailable")
    raw = result.raw
    if (result.provider == "codex" and result.success is False and isinstance(raw, dict)
        and raw.get("status") == "deferred"):
        retry = raw.get("retry_at")
        if type(retry) not in (int, float) or not math.isfinite(retry) or retry <= 0:
            retry = None
        return {**unavailable("research_primary_deferred"), "status": "deferred", "retry_at": retry}
    if not (
        result.provider == "codex" and result.success is True and not result.error and not result.note
        and isinstance(result.output, str) and result.output.strip()
        and isinstance(raw, dict) and raw.get("status") == "succeeded" and raw.get("provider") == "codex"
        and raw.get("research_stage") == "promotion_review" and raw.get("reasoning_effort") == "xhigh"
        and isinstance(result.model, str) and result.model.strip() and raw.get("model") == result.model
        and raw.get("output") == result.output
    ):
        return unavailable("research_primary_result_unavailable")
    review = parse_llm_review_output(result.output, provider="codex", model=result.model)
    if extract_verdict(review) not in {VERDICT_PASS, VERDICT_FAIL}:
        return unavailable("research_primary_invalid_response", verdict=VERDICT_INVALID)
    review.update(source="codex_primary", provider="codex", research_stage="promotion_review",
                  reasoning_effort=raw["reasoning_effort"])
    return review


def primary_review_available() -> bool:
    return bool(str(os.environ.get("CODEX_AUDIT_SERVICE_URL") or "").strip())
