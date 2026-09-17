"""Named AI call scenarios: provider chain, mode, and research stage.

Default remains Codex. Cursor is only selectable for scenarios marked
``cursor_eligible`` and only when the caller passes an explicit research
provider chain that includes ``cursor``. Promotion, codegen, and
platform_bugfix stay Codex-fixed regardless of ``AI_GATEWAY_RESEARCH_PROVIDERS``.

This module does not enable Cursor, deploy, or spend quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from service.contracts import (
    MODE_REVIEW_AND_FIX,
    MODE_REVIEW_ONLY,
    PROVIDER_CODEX,
    PROVIDER_CURSOR,
)

ENDPOINT_EXECUTE = "execute"
ENDPOINT_ANALYZE = "analyze"
ENDPOINT_REVIEW = "review"

POLICY_FIXED_CODEX = "fixed_codex"
POLICY_RESEARCH_ENV = "research_env"
POLICY_API = "api"

SCENARIO_RESEARCH_TASK_DIAGNOSIS = "research_task_diagnosis"
SCENARIO_PORTFOLIO_PROPOSAL_DIAGNOSIS = "portfolio_proposal_diagnosis"
SCENARIO_DAILY_BRIEFING = "daily_briefing"
SCENARIO_RESEARCH_SUMMARY = "research_summary"
SCENARIO_PROMOTION_PRIMARY_REVIEW = "promotion_primary_review"
SCENARIO_PLATFORM_BUGFIX = "platform_bugfix"
SCENARIO_SOXL_RSI2_CODEGEN = "soxl_rsi2_codegen"
SCENARIO_GLOBAL_ETF_CODEGEN = "global_etf_codegen"
SCENARIO_CN_INDEX_ETF_RESEARCH = "cn_index_etf_research"
SCENARIO_SEMANTIC_QUALITY_ACCEPTANCE = "semantic_quality_acceptance"
SCENARIO_API_ANALYZE = "api_analyze"
SCENARIO_API_DUAL_REVIEW = "api_dual_review"


@dataclass(frozen=True)
class ProviderScenario:
    scenario_id: str
    endpoint: str
    mode: str
    research_stage: str
    provider_policy: str
    cursor_eligible: bool
    fallback_chain_allowed: bool
    purpose: str


SCENARIOS: Mapping[str, ProviderScenario] = {
    SCENARIO_RESEARCH_TASK_DIAGNOSIS: ProviderScenario(
        scenario_id=SCENARIO_RESEARCH_TASK_DIAGNOSIS,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="drift_analysis",
        provider_policy=POLICY_RESEARCH_ENV,
        cursor_eligible=True,
        fallback_chain_allowed=True,
        purpose="Watcher-bound advisory diagnosis; no experiment or promotion authority.",
    ),
    SCENARIO_PORTFOLIO_PROPOSAL_DIAGNOSIS: ProviderScenario(
        scenario_id=SCENARIO_PORTFOLIO_PROPOSAL_DIAGNOSIS,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="drift_analysis",
        provider_policy=POLICY_RESEARCH_ENV,
        cursor_eligible=True,
        fallback_chain_allowed=True,
        purpose="Portfolio research proposal advisory; same Cursor canary class as task diagnosis.",
    ),
    SCENARIO_DAILY_BRIEFING: ProviderScenario(
        scenario_id=SCENARIO_DAILY_BRIEFING,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="research_summary",
        provider_policy=POLICY_RESEARCH_ENV,
        cursor_eligible=True,
        fallback_chain_allowed=True,
        purpose="Daily count summary advisory; expand only after diagnosis canary passes.",
    ),
    SCENARIO_RESEARCH_SUMMARY: ProviderScenario(
        scenario_id=SCENARIO_RESEARCH_SUMMARY,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="research_summary",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Standalone research summary scripts; stay Codex until separately authorized.",
    ),
    SCENARIO_PROMOTION_PRIMARY_REVIEW: ProviderScenario(
        scenario_id=SCENARIO_PROMOTION_PRIMARY_REVIEW,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="promotion_review",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Dual-review primary stays Codex-only even when research env lists Cursor.",
    ),
    SCENARIO_PLATFORM_BUGFIX: ProviderScenario(
        scenario_id=SCENARIO_PLATFORM_BUGFIX,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Platform bugfix; Codex only; review_and_fix needs explicit approval path.",
    ),
    SCENARIO_SOXL_RSI2_CODEGEN: ProviderScenario(
        scenario_id=SCENARIO_SOXL_RSI2_CODEGEN,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="optimization",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Bounded SOXL research codegen; Codex-only review_only optimization.",
    ),
    SCENARIO_GLOBAL_ETF_CODEGEN: ProviderScenario(
        scenario_id=SCENARIO_GLOBAL_ETF_CODEGEN,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="optimization",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Bounded global ETF research codegen; Codex-only.",
    ),
    SCENARIO_CN_INDEX_ETF_RESEARCH: ProviderScenario(
        scenario_id=SCENARIO_CN_INDEX_ETF_RESEARCH,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="optimization",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="CN index ETF research dispatch; Codex-only.",
    ),
    SCENARIO_SEMANTIC_QUALITY_ACCEPTANCE: ProviderScenario(
        scenario_id=SCENARIO_SEMANTIC_QUALITY_ACCEPTANCE,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="drift_analysis",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Offline/service acceptance probe stays Codex; not a Cursor canary.",
    ),
    SCENARIO_API_ANALYZE: ProviderScenario(
        scenario_id=SCENARIO_API_ANALYZE,
        endpoint=ENDPOINT_ANALYZE,
        mode="",
        research_stage="",
        provider_policy=POLICY_API,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Sync OpenAI/Anthropic analyze; separate API budget; never Cursor substitute.",
    ),
    SCENARIO_API_DUAL_REVIEW: ProviderScenario(
        scenario_id=SCENARIO_API_DUAL_REVIEW,
        endpoint=ENDPOINT_REVIEW,
        mode=MODE_REVIEW_ONLY,
        research_stage="",
        provider_policy=POLICY_API,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Multi-model API review with optional Codex verifier; not Cursor.",
    ),
}


def get_scenario(scenario_id: str) -> ProviderScenario:
    scenario = SCENARIOS.get(str(scenario_id or "").strip())
    if scenario is None:
        raise ValueError(f"unknown provider scenario: {scenario_id!r}")
    return scenario


def _normalize_research_providers(research_providers: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    providers = tuple(str(item).strip() for item in research_providers if str(item).strip())
    if providers not in ((PROVIDER_CODEX,), (PROVIDER_CURSOR,), (PROVIDER_CODEX, PROVIDER_CURSOR)):
        raise ValueError("research_providers must be (codex,), (cursor,), or (codex, cursor)")
    return providers


def resolve_allowed_providers(
    scenario_id: str,
    *,
    research_providers: tuple[str, ...] | list[str] = (PROVIDER_CODEX,),
) -> list[str]:
    """Resolve execution provider chain for a named scenario.

    Cursor appears only when the scenario is cursor_eligible and the caller
    explicitly requests it. Fallback chains are refused for fixed_codex lanes.
    """
    scenario = get_scenario(scenario_id)
    if scenario.provider_policy == POLICY_API:
        raise ValueError(f"{scenario_id} uses API endpoint, not allowed_providers")
    if scenario.provider_policy == POLICY_FIXED_CODEX or not scenario.cursor_eligible:
        return [PROVIDER_CODEX]
    providers = _normalize_research_providers(research_providers)
    if providers == (PROVIDER_CODEX, PROVIDER_CURSOR) and not scenario.fallback_chain_allowed:
        raise ValueError(f"{scenario_id} does not allow Codex→Cursor fallback chain")
    return list(providers)


def resolve_execute_kwargs(
    scenario_id: str,
    *,
    research_providers: tuple[str, ...] | list[str] = (PROVIDER_CODEX,),
    mode: str | None = None,
) -> dict[str, Any]:
    """Build execute() kwargs for a named scenario without enabling Cursor."""
    scenario = get_scenario(scenario_id)
    if scenario.endpoint != ENDPOINT_EXECUTE:
        raise ValueError(f"{scenario_id} is not an execute scenario")
    effective_mode = MODE_REVIEW_ONLY if mode is None else str(mode).strip().lower()
    if scenario.scenario_id == SCENARIO_PLATFORM_BUGFIX:
        if effective_mode not in {MODE_REVIEW_ONLY, MODE_REVIEW_AND_FIX}:
            raise ValueError("platform_bugfix requires review_only or review_and_fix")
    elif effective_mode != scenario.mode:
        raise ValueError(f"{scenario_id} requires mode={scenario.mode}")
    allowed = resolve_allowed_providers(scenario_id, research_providers=research_providers)
    if PROVIDER_CURSOR in allowed and effective_mode != MODE_REVIEW_ONLY:
        raise ValueError("Cursor scenarios require review_only")
    kwargs: dict[str, Any] = {
        "mode": effective_mode,
        "allowed_providers": allowed,
    }
    if scenario.research_stage:
        kwargs["research_stage"] = scenario.research_stage
    return kwargs


def cursor_canary_scenarios() -> tuple[str, ...]:
    return tuple(
        scenario.scenario_id
        for scenario in SCENARIOS.values()
        if scenario.cursor_eligible and scenario.endpoint == ENDPOINT_EXECUTE
    )
