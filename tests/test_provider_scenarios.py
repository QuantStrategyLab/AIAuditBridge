"""Tests for named provider call scenarios and routing defaults."""

from __future__ import annotations

import unittest

from service.contracts import MODE_REVIEW_AND_FIX, MODE_REVIEW_ONLY, PROVIDER_CODEX, PROVIDER_CURSOR
from service import provider_scenarios as scenarios


class ProviderScenarioTests(unittest.TestCase):
    def test_default_research_diagnosis_stays_codex(self) -> None:
        kwargs = scenarios.resolve_execute_kwargs(scenarios.SCENARIO_RESEARCH_TASK_DIAGNOSIS)
        self.assertEqual(
            kwargs,
            {
                "mode": MODE_REVIEW_ONLY,
                "research_stage": "drift_analysis",
                "allowed_providers": [PROVIDER_CODEX],
            },
        )

    def test_cursor_env_only_applies_to_canary_eligible_scenarios(self) -> None:
        providers = (PROVIDER_CURSOR,)
        for scenario_id in scenarios.cursor_canary_scenarios():
            kwargs = scenarios.resolve_execute_kwargs(scenario_id, research_providers=providers)
            self.assertEqual(kwargs["allowed_providers"], [PROVIDER_CURSOR])
            self.assertEqual(kwargs["mode"], MODE_REVIEW_ONLY)

        for scenario_id in (
            scenarios.SCENARIO_PROMOTION_PRIMARY_REVIEW,
            scenarios.SCENARIO_SOXL_RSI2_CODEGEN,
            scenarios.SCENARIO_GLOBAL_ETF_CODEGEN,
            scenarios.SCENARIO_PLATFORM_BUGFIX,
            scenarios.SCENARIO_SEMANTIC_QUALITY_ACCEPTANCE,
        ):
            self.assertEqual(
                scenarios.resolve_allowed_providers(scenario_id, research_providers=providers),
                [PROVIDER_CODEX],
            )

    def test_promotion_review_ignores_cursor_env(self) -> None:
        kwargs = scenarios.resolve_execute_kwargs(
            scenarios.SCENARIO_PROMOTION_PRIMARY_REVIEW,
            research_providers=(PROVIDER_CODEX, PROVIDER_CURSOR),
        )
        self.assertEqual(kwargs["allowed_providers"], [PROVIDER_CODEX])
        self.assertEqual(kwargs["research_stage"], "promotion_review")

    def test_platform_bugfix_allows_review_and_fix_but_not_cursor(self) -> None:
        kwargs = scenarios.resolve_execute_kwargs(
            scenarios.SCENARIO_PLATFORM_BUGFIX,
            mode=MODE_REVIEW_AND_FIX,
            research_providers=(PROVIDER_CURSOR,),
        )
        self.assertEqual(kwargs["mode"], MODE_REVIEW_AND_FIX)
        self.assertEqual(kwargs["allowed_providers"], [PROVIDER_CODEX])
        self.assertNotIn("research_stage", kwargs)

    def test_api_scenarios_are_not_execute_routes(self) -> None:
        with self.assertRaisesRegex(ValueError, "not an execute scenario"):
            scenarios.resolve_execute_kwargs(scenarios.SCENARIO_API_ANALYZE)
        with self.assertRaisesRegex(ValueError, "API endpoint"):
            scenarios.resolve_allowed_providers(scenarios.SCENARIO_API_DUAL_REVIEW)

    def test_unknown_scenario_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown provider scenario"):
            scenarios.get_scenario("trade_execution")

    def test_canary_set_is_explicit_and_small(self) -> None:
        self.assertEqual(
            scenarios.cursor_canary_scenarios(),
            (
                scenarios.SCENARIO_RESEARCH_TASK_DIAGNOSIS,
                scenarios.SCENARIO_PORTFOLIO_PROPOSAL_DIAGNOSIS,
                scenarios.SCENARIO_DAILY_BRIEFING,
            ),
        )


if __name__ == "__main__":
    unittest.main()
