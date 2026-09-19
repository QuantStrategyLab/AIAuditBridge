"""Named AI call scenarios: adaptive defaults and forced overrides.

Two selection modes for execute consumers:

* Adaptive (default): scenario fills ``mode`` / ``research_stage`` /
  ``allowed_providers`` (and optional complexity / pinned effort). Empty
  model and effort stay omitted so the gateway subscription admission picks
  them. ``AI_GATEWAY_RESEARCH_PROVIDERS`` only soft-selects Cursor on
  ``cursor_eligible`` scenarios; fixed Codex lanes ignore that env.
* Forced: callers may pass ``allowed_providers``, ``model``,
  ``reasoning_effort``, or ``complexity``. Incompatible values raise
  ``ValueError``—never silently rewritten onto another provider or stage.

Codex and Cursor are subscription CLI lanes. OpenAI/Anthropic analyze/review
are separate API-budget scenarios and are not substitutes for either CLI.
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
SCENARIO_ACCOUNT_OPERATIONAL_DIAGNOSIS = "account_operational_diagnosis"
SCENARIO_SOXL_MANUAL_LEARNING = "soxl_manual_learning"
SCENARIO_NEW_RESEARCH_DESIGN = "new_research_design"
SCENARIO_API_ANALYZE = "api_analyze"
SCENARIO_API_DUAL_REVIEW = "api_dual_review"

_VALID_COMPLEXITY = frozenset({"low", "medium", "high"})
_VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


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
    default_complexity: str = ""
    pin_reasoning_effort: str = ""
    pin_model: str = ""


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
        default_complexity="medium",
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
        default_complexity="medium",
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
        default_complexity="low",
    ),
    SCENARIO_RESEARCH_SUMMARY: ProviderScenario(
        scenario_id=SCENARIO_RESEARCH_SUMMARY,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="research_summary",
        provider_policy=POLICY_RESEARCH_ENV,
        cursor_eligible=True,
        fallback_chain_allowed=False,
        purpose="Standalone research summary advisory; explicit Cursor canary, no Codex→Cursor chain.",
        default_complexity="low",
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
        default_complexity="high",
        pin_reasoning_effort="xhigh",
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
        default_complexity="medium",
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
        default_complexity="high",
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
        default_complexity="high",
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
        default_complexity="high",
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
        default_complexity="medium",
    ),
    SCENARIO_ACCOUNT_OPERATIONAL_DIAGNOSIS: ProviderScenario(
        scenario_id=SCENARIO_ACCOUNT_OPERATIONAL_DIAGNOSIS,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="drift_analysis",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="Account operational diagnosis; Codex-only advisory.",
        default_complexity="high",
    ),
    SCENARIO_SOXL_MANUAL_LEARNING: ProviderScenario(
        scenario_id=SCENARIO_SOXL_MANUAL_LEARNING,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="optimization",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="SOXL manual learning advisory; Codex-only optimization.",
        default_complexity="medium",
    ),
    SCENARIO_NEW_RESEARCH_DESIGN: ProviderScenario(
        scenario_id=SCENARIO_NEW_RESEARCH_DESIGN,
        endpoint=ENDPOINT_EXECUTE,
        mode=MODE_REVIEW_ONLY,
        research_stage="optimization",
        provider_policy=POLICY_FIXED_CODEX,
        cursor_eligible=False,
        fallback_chain_allowed=False,
        purpose="New-research design choice; Codex-only.",
        default_complexity="low",
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


def _normalize_forced_providers(allowed_providers: list[str] | tuple[str, ...]) -> list[str]:
    providers = [str(item).strip() for item in allowed_providers if str(item).strip()]
    if providers not in ([PROVIDER_CODEX], [PROVIDER_CURSOR], [PROVIDER_CODEX, PROVIDER_CURSOR]):
        raise ValueError("allowed_providers must be [codex], [cursor], or [codex, cursor]")
    return providers


def resolve_allowed_providers(
    scenario_id: str,
    *,
    research_providers: tuple[str, ...] | list[str] = (PROVIDER_CODEX,),
    allowed_providers: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Resolve execution provider chain for a named scenario.

    Adaptive: soft research_providers env on cursor_eligible scenarios.
    Forced: explicit allowed_providers must be legal for the scenario or raise.
    """
    scenario = get_scenario(scenario_id)
    if scenario.provider_policy == POLICY_API:
        raise ValueError(f"{scenario_id} uses API endpoint, not allowed_providers")

    if allowed_providers is not None:
        forced = _normalize_forced_providers(allowed_providers)
        if PROVIDER_CURSOR in forced:
            if not scenario.cursor_eligible:
                raise ValueError(f"{scenario_id} does not allow Cursor")
            if forced == [PROVIDER_CODEX, PROVIDER_CURSOR] and not scenario.fallback_chain_allowed:
                raise ValueError(f"{scenario_id} does not allow Codex→Cursor fallback chain")
        if scenario.provider_policy == POLICY_FIXED_CODEX and forced != [PROVIDER_CODEX]:
            raise ValueError(f"{scenario_id} requires allowed_providers=['codex']")
        return forced

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
    allowed_providers: list[str] | tuple[str, ...] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    complexity: str | None = None,
    research_stage: str | None = None,
) -> dict[str, Any]:
    """Build execute() kwargs: adaptive scenario defaults plus optional forced pins."""
    scenario = get_scenario(scenario_id)
    if scenario.endpoint != ENDPOINT_EXECUTE:
        raise ValueError(f"{scenario_id} is not an execute scenario")

    effective_mode = MODE_REVIEW_ONLY if mode is None else str(mode).strip().lower()
    if scenario.scenario_id == SCENARIO_PLATFORM_BUGFIX:
        if effective_mode not in {MODE_REVIEW_ONLY, MODE_REVIEW_AND_FIX}:
            raise ValueError("platform_bugfix requires review_only or review_and_fix")
    elif effective_mode != scenario.mode:
        raise ValueError(f"{scenario_id} requires mode={scenario.mode}")

    allowed = resolve_allowed_providers(
        scenario_id,
        research_providers=research_providers,
        allowed_providers=allowed_providers,
    )
    if PROVIDER_CURSOR in allowed and effective_mode != MODE_REVIEW_ONLY:
        raise ValueError("Cursor scenarios require review_only")

    kwargs: dict[str, Any] = {
        "mode": effective_mode,
        "allowed_providers": allowed,
    }
    effective_stage = scenario.research_stage
    if research_stage not in (None, ""):
        override = str(research_stage).strip()
        if override not in {"research_summary", "drift_analysis", "optimization", "promotion_review"}:
            raise ValueError("unsupported research_stage override")
        if scenario.provider_policy == POLICY_FIXED_CODEX:
            effective_stage = override
        elif (
            scenario.scenario_id == SCENARIO_RESEARCH_SUMMARY
            and PROVIDER_CURSOR not in allowed
        ):
            # Codex-only shared summary callback may still pin stage (e.g. optimization).
            effective_stage = override
        else:
            raise ValueError("research_stage override requires a fixed_codex scenario")
    if effective_stage:
        kwargs["research_stage"] = effective_stage
        if PROVIDER_CURSOR in allowed and effective_stage not in cursor_canary_research_stages():
            raise ValueError("Cursor is only allowed for canary research stages")

    effective_complexity = (
        str(complexity).strip().lower()
        if complexity not in (None, "")
        else scenario.default_complexity
    )
    if effective_complexity:
        if effective_complexity not in _VALID_COMPLEXITY:
            raise ValueError("complexity must be low, medium, or high")
        kwargs["complexity"] = effective_complexity

    if reasoning_effort not in (None, "", "auto"):
        effort = str(reasoning_effort).strip().lower()
        if effort not in _VALID_EFFORTS:
            raise ValueError("reasoning_effort must be low, medium, high, or xhigh")
        if scenario.pin_reasoning_effort and effort != scenario.pin_reasoning_effort:
            raise ValueError(
                f"{scenario_id} requires reasoning_effort={scenario.pin_reasoning_effort}"
            )
        kwargs["reasoning_effort"] = effort
    elif scenario.pin_reasoning_effort:
        kwargs["reasoning_effort"] = scenario.pin_reasoning_effort

    if model not in (None, "", "auto"):
        forced_model = str(model).strip()
        if not forced_model:
            raise ValueError("model must be non-empty when forced")
        if scenario.pin_model and forced_model != scenario.pin_model:
            raise ValueError(f"{scenario_id} requires model={scenario.pin_model}")
        kwargs["model"] = forced_model
    elif scenario.pin_model:
        kwargs["model"] = scenario.pin_model

    return kwargs


def cursor_canary_scenarios() -> tuple[str, ...]:
    return tuple(
        scenario.scenario_id
        for scenario in SCENARIOS.values()
        if scenario.cursor_eligible and scenario.endpoint == ENDPOINT_EXECUTE
    )


def cursor_canary_research_stages() -> frozenset[str]:
    """Research stages that may select Cursor (gateway + consumer enforce)."""
    return frozenset(
        scenario.research_stage
        for scenario in SCENARIOS.values()
        if scenario.cursor_eligible and scenario.research_stage
    )
