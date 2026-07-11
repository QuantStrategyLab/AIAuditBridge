"""Fail-closed monthly AI budget and Codex rate-limit gate.

The guard is deliberately independent from model selection: a low budget never
silently selects another paid provider.  Callers must reserve before starting
work and settle or release the reservation when the work finishes.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DECISION_SCHEMA = "ai_budget_decision.v1"
DEFAULT_BILLING_TIMEZONE = "UTC"
DEFAULT_MAX_USAGE_AGE_SECONDS = 86_400
CODEX_RESERVE_RATIO = 0.10
CODEX_THRESHOLDS = {
    "research": 0.30,
    "optimization": 0.20,
    "maintenance": 0.20,
    "review": 0.20,
    "auto_fix": 0.20,
    "incident": 0.10,
}
API_TASK_CLASSES = frozenset(CODEX_THRESHOLDS)


def _now() -> float:
    return time.time()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timestamp(snapshot: dict[str, Any]) -> float | None:
    for key in ("observed_at", "updated_at", "as_of", "timestamp"):
        value = snapshot.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        if value is not None:
            parsed = _number(value, -1)
            if parsed >= 0:
                return parsed
    return None


def _period(now: float, timezone_name: str) -> str:
    # Billing periods are calendar months.  Invalid timezone configuration is
    # not allowed to change the period silently; UTC is the safe fallback.
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(timezone_name or DEFAULT_BILLING_TIMEZONE)
    except Exception:  # noqa: BLE001
        zone = UTC
    return datetime.fromtimestamp(now, zone).strftime("%Y-%m")


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    scope: str
    amount: float
    task_class: str
    created_at: float


class AIBudgetGuard:
    """In-process atomic reservation ledger with a versioned decision contract."""

    def __init__(self, config: dict[str, Any] | None = None, *, clock: Any = _now) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._reservations: dict[str, Reservation] = {}
        self._reserved: dict[str, float] = {}
        self._settled: dict[str, float] = {}
        self._config = config if isinstance(config, dict) else self._load_config()
        self._timezone = str(self._config.get("billing_timezone") or DEFAULT_BILLING_TIMEZONE)
        self._max_age = max(1, int(_number(self._config.get("usage_max_age_seconds"), DEFAULT_MAX_USAGE_AGE_SECONDS)))
        self._settled_period = self.period()

    @staticmethod
    def _load_config() -> dict[str, Any]:
        path = os.environ.get("CODEX_AUDIT_SERVICE_AI_BUDGET_CONFIG", "").strip()
        if not path:
            return {}
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"_config_error": "budget_config_unavailable"}
        return raw if isinstance(raw, dict) else {"_config_error": "budget_config_invalid"}

    @property
    def billing_timezone(self) -> str:
        return self._timezone

    def period(self, now: float | None = None) -> str:
        return _period(self._clock() if now is None else now, self._timezone)

    def _scope(self, provider: str, provider_scope: str, repo: str, task_class: str) -> str:
        return "/".join((provider or "unknown", provider_scope or "default", repo or "unknown", task_class or "maintenance"))

    def _budget_entry(self, provider: str, provider_scope: str, repo: str, task_class: str) -> dict[str, Any] | None:
        budgets = self._config.get("monthly_budgets")
        if not isinstance(budgets, dict):
            return None
        candidates = (
            f"{provider}:{provider_scope}:{repo}:{task_class}",
            f"{provider}:{provider_scope}:{repo}",
            f"{provider}:{provider_scope}",
            provider,
            "default",
        )
        for key in candidates:
            value = budgets.get(key)
            if isinstance(value, (int, float)):
                return {"user_monthly_budget_usd": float(value)}
            if isinstance(value, dict):
                if any(field in value for field in ("user_monthly_budget_usd", "monthly_budget_usd", "provider_project_limit_usd")):
                    return value
        provider_node = budgets.get(provider)
        if isinstance(provider_node, dict):
            scope_node = provider_node.get(provider_scope) or provider_node.get("default")
            if isinstance(scope_node, dict):
                repo_node = scope_node.get(repo) or scope_node.get("default")
                if isinstance(repo_node, dict):
                    task_node = repo_node.get(task_class) or repo_node.get("default")
                    if isinstance(task_node, (int, float)):
                        return {"user_monthly_budget_usd": float(task_node)}
                    if isinstance(task_node, dict):
                        return task_node
        return None

    def _usage(self, snapshot: dict[str, Any] | None) -> tuple[float, bool, str]:
        if not isinstance(snapshot, dict):
            return 0.0, False, "usage_snapshot_missing"
        observed = _timestamp(snapshot)
        if observed is None or self._clock() - observed > self._max_age:
            return 0.0, False, "usage_snapshot_stale"
        value = snapshot.get("used_usd", snapshot.get("cost_usd", snapshot.get("total_cost")))
        if value is None:
            for nested_key in ("organization_costs", "costs", "usage"):
                nested = snapshot.get(nested_key)
                if isinstance(nested, dict):
                    value = nested.get("used_usd", nested.get("cost_usd", nested.get("total_cost")))
                    if value is not None:
                        break
        if value is None:
            return 0.0, False, "usage_cost_missing"
        return max(0.0, _number(value)), True, ""

    def _decision(self, *, task_class: str, provider_scope: str, period: str, observed: Any,
                  reserved: float, hard_limit: Any, remaining: Any, freshness: str,
                  decision: str, reasons: list[str], reset_at: Any = None,
                  auto_fallback_allowed: bool = False, reservation_scope: str = "") -> dict[str, Any]:
        return {
            "schema": DECISION_SCHEMA,
            "task_class": task_class,
            "provider_scope": provider_scope,
            "reservation_scope": reservation_scope or provider_scope,
            "period": period,
            "observed_usage": observed,
            "reserved_usage": round(reserved, 8),
            "hard_limit": hard_limit,
            "remaining_after_reservation": remaining,
            "usage_freshness": freshness,
            "decision": decision,
            "reason_codes": reasons,
            "reset_at": reset_at,
            "billing_timezone": self._timezone,
            "auto_fallback_allowed": auto_fallback_allowed,
        }

    def preflight(
        self,
        *,
        task_class: str,
        provider: str,
        provider_scope: str = "default",
        repo: str = "",
        estimated_cost_usd: float = 0.0,
        usage_snapshot: dict[str, Any] | None = None,
        codex_snapshot: dict[str, Any] | None = None,
        human_approved_fallback: bool = False,
    ) -> dict[str, Any]:
        task = str(task_class or "maintenance").strip().lower()
        provider_name = str(provider or "").strip().lower()
        current_period = self.period()
        scope = self._scope(provider_name, provider_scope, repo, task)
        amount = max(0.0, _number(estimated_cost_usd))
        if provider_name in {"codex", "codex-cli", "codex_account"}:
            return self._preflight_codex(task, provider_scope, current_period, codex_snapshot, scope)

        entry = self._budget_entry(provider_name, provider_scope, repo, task)
        if entry is None:
            return self._decision(
                task_class=task, provider_scope=provider_scope, period=current_period,
                observed=0.0, reserved=0.0, hard_limit=0.0, remaining=0.0,
                freshness="unknown", decision="defer", reasons=["monthly_budget_not_configured"],
            )
        used, fresh, freshness_reason = self._usage(usage_snapshot)
        if not fresh:
            return self._decision(
                task_class=task, provider_scope=provider_scope, period=current_period,
                observed=used, reserved=0.0, hard_limit=0.0, remaining=0.0,
                freshness="stale", decision="block", reasons=[freshness_reason],
            )
        user_limit = max(0.0, _number(entry.get("user_monthly_budget_usd", entry.get("monthly_budget_usd"))))
        provider_limit = _number(entry.get("provider_project_limit_usd"), user_limit)
        hard_limit = min(user_limit, provider_limit * 0.80)
        with self._lock:
            if self._settled_period != current_period:
                self._settled.clear()
                self._settled_period = current_period
            reserved = self._reserved.get(scope, 0.0)
            settled = self._settled.get(scope, 0.0)
        remaining = hard_limit - used - settled - reserved - amount
        if remaining < 0:
            reason = "monthly_hard_limit_reached" if remaining <= 0 else "monthly_budget_insufficient"
            return self._decision(
                task_class=task, provider_scope=provider_scope, period=current_period,
                observed=used, reserved=reserved, hard_limit=round(hard_limit, 8),
                remaining=round(max(0.0, remaining), 8), freshness="fresh", decision="defer",
                reasons=[item for item in (reason, "api_fallback_requires_human_approval" if not human_approved_fallback else "") if item],
            )
        return self._decision(
            task_class=task, provider_scope=provider_scope, period=current_period,
            observed=used, reserved=reserved, hard_limit=round(hard_limit, 8),
            remaining=round(remaining, 8), freshness="fresh", decision="allow", reasons=[],
            auto_fallback_allowed=human_approved_fallback, reservation_scope=scope,
        )

    def _preflight_codex(self, task: str, provider_scope: str, period: str,
                         snapshot: dict[str, Any] | None, scope: str) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            return self._decision(
                task_class=task, provider_scope=provider_scope, period=period,
                observed={}, reserved=0.0, hard_limit=CODEX_RESERVE_RATIO,
                remaining=0.0, freshness="unavailable", decision="defer",
                reasons=["codex_rate_limit_snapshot_unavailable"],
            )
        observed_at = _timestamp(snapshot)
        if observed_at is None or self._clock() - observed_at > self._max_age:
            return self._decision(
                task_class=task, provider_scope=provider_scope, period=period,
                observed=snapshot, reserved=0.0, hard_limit=CODEX_RESERVE_RATIO,
                remaining=0.0, freshness="stale", decision="defer",
                reasons=["codex_rate_limit_snapshot_stale"],
            )
        limits = snapshot.get("rate_limits") if isinstance(snapshot.get("rate_limits"), dict) else snapshot
        ratios: list[float] = []
        reset_at: list[Any] = []
        for name in ("primary", "secondary"):
            window = limits.get(name)
            if not isinstance(window, dict):
                return self._decision(
                    task_class=task, provider_scope=provider_scope, period=period,
                    observed=snapshot, reserved=0.0, hard_limit=CODEX_RESERVE_RATIO,
                    remaining=0.0, freshness="invalid", decision="defer",
                    reasons=["codex_rate_limit_window_missing"],
                )
            remaining = window.get("remaining_percent")
            if remaining is None and window.get("used_percent") is not None:
                remaining = 100 - _number(window.get("used_percent"))
            if remaining is None:
                return self._decision(
                    task_class=task, provider_scope=provider_scope, period=period,
                    observed=snapshot, reserved=0.0, hard_limit=CODEX_RESERVE_RATIO,
                    remaining=0.0, freshness="invalid", decision="defer",
                    reasons=["codex_rate_limit_window_invalid"],
                )
            ratios.append(max(0.0, min(1.0, _number(remaining) / 100.0)))
            reset_at.append(window.get("resets_at"))
        tightest = min(ratios)
        threshold = CODEX_THRESHOLDS.get(task, CODEX_THRESHOLDS["maintenance"])
        allowed = tightest >= threshold and tightest - CODEX_RESERVE_RATIO >= 0
        return self._decision(
            task_class=task, provider_scope=provider_scope, period=period,
            observed={"primary_remaining_ratio": ratios[0], "secondary_remaining_ratio": ratios[1]},
            reserved=0.0, hard_limit=threshold,
            remaining=round(tightest - CODEX_RESERVE_RATIO, 8), freshness="fresh",
            decision="allow" if allowed else "defer",
            reasons=[] if allowed else ["codex_rate_limit_below_task_threshold"],
            reset_at=reset_at,
            reservation_scope=scope,
        )

    def reserve(self, decision: dict[str, Any], amount: float | None = None) -> Reservation | None:
        if not isinstance(decision, dict) or decision.get("decision") != "allow":
            return None
        scope = str(decision.get("reservation_scope") or decision.get("provider_scope") or "default")
        # Provider scope in a decision is intentionally the stable ledger key;
        # callers may pass an explicit amount for API reservations.
        requested = max(0.0, _number(amount, _number(decision.get("remaining_after_reservation"))))
        reservation = Reservation(uuid.uuid4().hex, scope, requested, str(decision.get("task_class") or "maintenance"), self._clock())
        with self._lock:
            hard_limit = _number(decision.get("hard_limit"), float("inf"))
            observed = _number(decision.get("observed_usage"), 0.0)
            if hard_limit != float("inf") and observed + self._settled.get(scope, 0.0) + self._reserved.get(scope, 0.0) + requested > hard_limit + 1e-12:
                return None
            self._reservations[reservation.reservation_id] = reservation
            self._reserved[scope] = self._reserved.get(scope, 0.0) + requested
        return reservation

    def release(self, reservation: Reservation | str) -> bool:
        rid = reservation.reservation_id if isinstance(reservation, Reservation) else str(reservation)
        with self._lock:
            item = self._reservations.pop(rid, None)
            if item is None:
                return False
            self._reserved[item.scope] = max(0.0, self._reserved.get(item.scope, 0.0) - item.amount)
            return True

    def settle(self, reservation: Reservation | str, actual_cost: float) -> bool:
        rid = reservation.reservation_id if isinstance(reservation, Reservation) else str(reservation)
        with self._lock:
            item = self._reservations.pop(rid, None)
            if item is None:
                return False
            self._reserved[item.scope] = max(0.0, self._reserved.get(item.scope, 0.0) - item.amount)
            self._settled[item.scope] = self._settled.get(item.scope, 0.0) + max(0.0, _number(actual_cost))
            return True


_guard = AIBudgetGuard()


def get_ai_budget_guard() -> AIBudgetGuard:
    return _guard


def ai_budget_preflight(**kwargs: Any) -> dict[str, Any]:
    return _guard.preflight(**kwargs)


__all__ = [
    "AIBudgetGuard",
    "API_TASK_CLASSES",
    "CODEX_RESERVE_RATIO",
    "CODEX_THRESHOLDS",
    "DECISION_SCHEMA",
    "Reservation",
    "ai_budget_preflight",
    "get_ai_budget_guard",
]
