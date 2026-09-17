"""Tests for named provider call scenarios: adaptive defaults and forced pins."""

from __future__ import annotations

import unittest

from service.contracts import MODE_REVIEW_AND_FIX, MODE_REVIEW_ONLY, PROVIDER_CODEX, PROVIDER_CURSOR
from service import provider_scenarios as scenarios


class ProviderScenarioTests(unittest.TestCase):
    def test_adaptive_research_diagnosis_defaults(self) -> None:
        kwargs = scenarios.resolve_execute_kwargs(scenarios.SCENARIO_RESEARCH_TASK_DIAGNOSIS)
        self.assertEqual(
            kwargs,
            {
                "mode": MODE_REVIEW_ONLY,
                "research_stage": "drift_analysis",
                "allowed_providers": [PROVIDER_CODEX],
                "complexity": "medium",
            },
        )
        self.assertNotIn("model", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)

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

    def test_forced_cursor_on_fixed_scenario_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not allow Cursor"):
            scenarios.resolve_execute_kwargs(
                scenarios.SCENARIO_PROMOTION_PRIMARY_REVIEW,
                allowed_providers=[PROVIDER_CURSOR],
            )

    def test_forced_model_and_effort_pass_through(self) -> None:
        kwargs = scenarios.resolve_execute_kwargs(
            scenarios.SCENARIO_SEMANTIC_QUALITY_ACCEPTANCE,
            model="gpt-5.6-terra",
            reasoning_effort="medium",
        )
        self.assertEqual(kwargs["model"], "gpt-5.6-terra")
        self.assertEqual(kwargs["reasoning_effort"], "medium")
        self.assertEqual(kwargs["allowed_providers"], [PROVIDER_CODEX])

    def test_promotion_pins_xhigh_and_ignores_cursor_env(self) -> None:
        kwargs = scenarios.resolve_execute_kwargs(
            scenarios.SCENARIO_PROMOTION_PRIMARY_REVIEW,
            research_providers=(PROVIDER_CODEX, PROVIDER_CURSOR),
        )
        self.assertEqual(kwargs["allowed_providers"], [PROVIDER_CODEX])
        self.assertEqual(kwargs["research_stage"], "promotion_review")
        self.assertEqual(kwargs["reasoning_effort"], "xhigh")
        self.assertEqual(kwargs["complexity"], "high")

    def test_forced_effort_conflict_with_pin_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires reasoning_effort=xhigh"):
            scenarios.resolve_execute_kwargs(
                scenarios.SCENARIO_PROMOTION_PRIMARY_REVIEW,
                reasoning_effort="medium",
            )

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
        self.assertEqual(
            scenarios.cursor_canary_research_stages(),
            frozenset({"drift_analysis", "research_summary"}),
        )

    def test_fixed_codex_may_override_research_stage(self) -> None:
        kwargs = scenarios.resolve_execute_kwargs(
            scenarios.SCENARIO_RESEARCH_SUMMARY,
            research_stage="optimization",
        )
        self.assertEqual(kwargs["research_stage"], "optimization")
        self.assertEqual(kwargs["allowed_providers"], [PROVIDER_CODEX])


if __name__ == "__main__":
    unittest.main()
