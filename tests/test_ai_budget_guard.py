from __future__ import annotations

import time

from service.ai_budget_guard import AIBudgetGuard, DECISION_SCHEMA


def _codex_snapshot(primary: int = 50, secondary: int = 80, observed_at: float | None = None) -> dict:
    return {
        "updated_at": time.time() if observed_at is None else observed_at,
        "rate_limits": {
            "primary": {"remaining_percent": primary, "resets_at": 123},
            "secondary": {"remaining_percent": secondary, "resets_at": 456},
        },
    }


def test_unconfigured_api_budget_is_zero_and_deferred() -> None:
    guard = AIBudgetGuard({"billing_timezone": "UTC"})
    decision = guard.preflight(
        task_class="research", provider="openai", provider_scope="project", repo="o/r", estimated_cost_usd=1,
        usage_snapshot={"updated_at": time.time(), "used_usd": 0},
    )
    assert decision["schema"] == DECISION_SCHEMA
    assert decision["decision"] == "defer"
    assert decision["hard_limit"] == 0
    assert "monthly_budget_not_configured" in decision["reason_codes"]


def test_api_hard_limit_is_min_of_user_and_provider_eighty_percent() -> None:
    guard = AIBudgetGuard({"monthly_budgets": {"openai:project": {
        "user_monthly_budget_usd": 100, "provider_project_limit_usd": 50,
    }}})
    decision = guard.preflight(
        task_class="review", provider="openai", provider_scope="project", repo="o/r", estimated_cost_usd=1,
        usage_snapshot={"updated_at": time.time(), "used_usd": 0},
    )
    assert decision["decision"] == "allow"
    assert decision["hard_limit"] == 40


def test_stale_usage_fails_closed_and_does_not_fallback() -> None:
    guard = AIBudgetGuard({"monthly_budgets": {"openai": {"user_monthly_budget_usd": 10}}})
    decision = guard.preflight(
        task_class="auto_fix", provider="openai", estimated_cost_usd=1,
        usage_snapshot={"updated_at": time.time() - 100_000, "used_usd": 0},
    )
    assert decision["decision"] == "block"
    assert decision["auto_fallback_allowed"] is False


def test_codex_uses_tightest_window_and_keeps_reserve() -> None:
    guard = AIBudgetGuard()
    assert guard.preflight(task_class="research", provider="codex", codex_snapshot=_codex_snapshot(35, 90))["decision"] == "allow"
    assert guard.preflight(task_class="research", provider="codex", codex_snapshot=_codex_snapshot(29, 90))["decision"] == "defer"
    assert guard.preflight(task_class="incident", provider="codex", codex_snapshot=_codex_snapshot(11, 90))["decision"] == "allow"
    assert guard.preflight(task_class="incident", provider="codex", codex_snapshot=_codex_snapshot(19, 90))["remaining_after_reservation"] == 0.09


def test_missing_codex_snapshot_defers_research_and_auto_fix() -> None:
    guard = AIBudgetGuard()
    for task in ("research", "auto_fix"):
        decision = guard.preflight(task_class=task, provider="codex")
        assert decision["decision"] == "defer"
        assert decision["reason_codes"] == ["codex_rate_limit_snapshot_unavailable"]


def test_reservations_are_atomic_and_released_or_settled() -> None:
    guard = AIBudgetGuard({"monthly_budgets": {"openai": {"user_monthly_budget_usd": 10}}})
    kwargs = dict(task_class="review", provider="openai", estimated_cost_usd=6,
                  usage_snapshot={"updated_at": time.time(), "used_usd": 0})
    first = guard.preflight(**kwargs)
    second = guard.preflight(**kwargs)
    one = guard.reserve(first, 6)
    two = guard.reserve(second, 6)
    assert one is not None
    assert two is None
    assert guard.settle(one, 4)
    assert guard.release(one) is False


def test_month_period_is_explicit_and_fallback_requires_human_approval() -> None:
    guard = AIBudgetGuard({"billing_timezone": "UTC", "monthly_budgets": {"openai": {"user_monthly_budget_usd": 10}}})
    decision = guard.preflight(
        task_class="maintenance", provider="openai", estimated_cost_usd=1,
        usage_snapshot={"updated_at": time.time(), "organization_costs": {"total_cost": 0}},
    )
    assert len(decision["period"]) == 7
    assert decision["billing_timezone"] == "UTC"
    assert decision["auto_fallback_allowed"] is False
