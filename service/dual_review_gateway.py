"""Gateway-backed GPT + Claude secondary reviews for CI runners (task 11b)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from service.dual_review_secondary import (
    _SECONDARY_SYSTEM,
    _result_to_review,
    build_secondary_prompt,
)
from service.adapters.llm_adapter import LlmResult

if TYPE_CHECKING:
    from service.dual_review_orchestrator import DualReviewRequest


def gateway_service_url() -> str:
    for key in ("AI_GATEWAY_SERVICE_URL", "DUAL_REVIEW_GATEWAY_URL", "CODEX_AUDIT_SERVICE_URL"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value.rstrip("/")
    return ""


def gateway_secondary_available() -> bool:
    return bool(gateway_service_url())


def _ai_result_to_llm(result: Any) -> LlmResult:
    provider = str(getattr(result, "provider", "") or "")
    if provider in {"openai", "gpt"}:
        provider = "openai"
    elif provider in {"anthropic", "claude"}:
        provider = "anthropic"
    return LlmResult(
        provider=provider,
        model=str(getattr(result, "model", "") or ""),
        output=str(getattr(result, "output", "") or ""),
        success=bool(getattr(result, "success", False)),
        error=str(getattr(result, "error", "") or ""),
        latency_seconds=float(getattr(result, "latency_seconds", 0.0) or 0.0),
    )


def run_gateway_dual_api_secondary_review(request: "DualReviewRequest") -> dict[str, Any]:
    """Run GPT + Claude via AiGateway ``/v1/ai/analyze`` (keys stay on VPS)."""
    from client.config import GatewayConfig
    from client.gateway_client import AiGatewayClient

    service_url = gateway_service_url()
    if not service_url:
        raise RuntimeError("AI gateway URL is not configured")

    gpt_model = str(os.environ.get("DUAL_REVIEW_GPT_MODEL", "gpt-5.4-mini")).strip()
    claude_model = str(os.environ.get("DUAL_REVIEW_CLAUDE_MODEL", "claude-sonnet-4-6")).strip()
    user_prompt = build_secondary_prompt(request)

    config = GatewayConfig(
        service_url=service_url,
        audience=str(os.environ.get("CODEX_AUDIT_SERVICE_AUDIENCE", "quant-codex-audit")).strip(),
        source_repository=str(os.environ.get("GITHUB_REPOSITORY", "")).strip(),
    )
    client = AiGatewayClient(config)

    gpt_result = client.analyze(user_prompt, model=gpt_model, system=_SECONDARY_SYSTEM)
    claude_result = client.analyze(user_prompt, model=claude_model, system=_SECONDARY_SYSTEM)

    return {
        "mode": "dual_api_gateway",
        "gpt": _result_to_review(_ai_result_to_llm(gpt_result)),
        "claude": _result_to_review(_ai_result_to_llm(claude_result)),
        "prompt": user_prompt,
        "gateway_url": service_url,
    }


def gateway_dual_api_secondary_reviewer(request: "DualReviewRequest") -> dict[str, Any]:
    try:
        return run_gateway_dual_api_secondary_review(request)
    except Exception as exc:
        return {
            "mode": "dual_api_gateway",
            "gpt": {"verdict": "reject", "confidence": 0.0, "error": str(exc)},
            "claude": {"verdict": "reject", "confidence": 0.0, "error": str(exc)},
        }
