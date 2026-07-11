"""Fail-closed monthly AI budget and Codex rate-limit gate.

The guard is deliberately independent from model selection: a low budget never
silently selects another paid provider.  Callers must reserve before starting
work and settle or release the reservation when the work finishes.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
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
CODEX_RESERVATION_RATIO = 0.10
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


def _reset_timestamp(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    parsed = _number(value, -1.0)
    return parsed if parsed >= 0 else None


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
    baseline_usage: float | None = None
    period: str = ""
    aggregate_scope: str = ""


class AIBudgetGuard:
    """In-process atomic reservation ledger with a versioned decision contract."""

    def __init__(self, config: dict[str, Any] | None = None, *, clock: Any = _now) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._reservations: dict[str, Reservation] = {}
        self._reserved: dict[str, float] = {}
        self._settled: dict[str, float] = {}
        self._settled_baseline: dict[str, float] = {}
        self._config = config if isinstance(config, dict) else self._load_config()
        self._timezone = str(self._config.get("billing_timezone") or DEFAULT_BILLING_TIMEZONE)
        self._max_age = max(1, int(_number(self._config.get("usage_max_age_seconds"), DEFAULT_MAX_USAGE_AGE_SECONDS)))
        self._settled_period = self.period()
        ledger_path = str(self._config.get("ledger_path") or os.environ.get("CODEX_AUDIT_SERVICE_AI_BUDGET_LEDGER_PATH", "")).strip()
        if not ledger_path:
            job_dir = os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR", "").strip()
            ledger_path = str(Path(job_dir) / "ai_budget_ledger.sqlite3") if job_dir else ""
        self._ledger_path = ledger_path
        self._ledger_error = False
        self._init_ledger()

    def _init_ledger(self) -> None:
        if not self._ledger_path:
            return
        try:
            path = Path(self._ledger_path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with sqlite3.connect(path, timeout=30) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ai_budget_ledger (scope TEXT NOT NULL, period TEXT NOT NULL, reserved REAL NOT NULL, settled REAL NOT NULL, baseline REAL, updated_at REAL NOT NULL, PRIMARY KEY(scope, period))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ai_budget_reservations (reservation_id TEXT PRIMARY KEY, scope TEXT NOT NULL, aggregate_scope TEXT NOT NULL, period TEXT NOT NULL, amount REAL NOT NULL, task_class TEXT NOT NULL, created_at REAL NOT NULL, baseline_usage REAL)"
                )
                columns = {
                    row[1]
                    for row in db.execute("PRAGMA table_info(ai_budget_reservations)").fetchall()
                }
                if "aggregate_scope" not in columns:
                    db.execute(
                        "ALTER TABLE ai_budget_reservations ADD COLUMN aggregate_scope TEXT NOT NULL DEFAULT ''"
                    )
                    db.execute(
                        "UPDATE ai_budget_reservations SET aggregate_scope=scope WHERE aggregate_scope=''"
                    )
        except (OSError, sqlite3.Error):
            self._ledger_error = True

    def _load_scope_locked(self, scope: str) -> None:
        if not self._ledger_path:
            return
        try:
            with sqlite3.connect(self._ledger_path, timeout=30) as db:
                row = db.execute(
                    "SELECT period,reserved,settled,baseline FROM ai_budget_ledger WHERE scope=? AND period=?",
                    (scope, self._settled_period),
                ).fetchone()
            if row is None:
                return
            period, reserved, settled, baseline = row
            if period != self._settled_period:
                return
            self._reserved[scope] = max(0.0, float(reserved))
            self._settled[scope] = max(0.0, float(settled))
            if baseline is not None:
                self._settled_baseline[scope] = float(baseline)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self._ledger_error = True
            return

    def _save_scope_locked(self, scope: str) -> None:
        if not self._ledger_path:
            return
        try:
            with sqlite3.connect(self._ledger_path, timeout=30) as db:
                db.execute(
                    "INSERT INTO ai_budget_ledger(scope,period,reserved,settled,baseline,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(scope,period) DO UPDATE SET reserved=excluded.reserved,settled=excluded.settled,baseline=excluded.baseline,updated_at=excluded.updated_at",
                    (
                        scope,
                        self._settled_period,
                        self._reserved.get(scope, 0.0),
                        self._settled.get(scope, 0.0),
                        self._settled_baseline.get(scope),
                        self._clock(),
                    ),
                )
        except (OSError, sqlite3.Error):
            self._ledger_error = True
            return

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

    def _scope(self, provider: str, provider_scope: str, repo: str, task_class: str, period: str) -> str:
        return "/".join((provider or "unknown", provider_scope or "default", repo or "unknown", task_class or "maintenance", period))

    @staticmethod
    def _aggregate_scope(provider: str, provider_scope: str, period: str) -> str:
        # Aggregate reservations share one provider/project cap across all
        # task classes; task-specific thresholds stay in the decision only.
        return "/".join((provider or "unknown", provider_scope or "default", "_aggregate", period))

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
        if isinstance(value, bool):
            return 0.0, False, "usage_cost_invalid"
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0, False, "usage_cost_invalid"
        if not math.isfinite(parsed) or parsed < 0:
            return 0.0, False, "usage_cost_invalid"
        return parsed, True, ""

    def _reconcile_settled_locked(self, scope: str, observed: float) -> float:
        settled = self._settled.get(scope, 0.0)
        baseline = self._settled_baseline.get(scope)
        if settled <= 0 or baseline is None or observed <= baseline:
            return settled
        observed_advance = observed - baseline
        if observed_advance >= settled:
            self._settled.pop(scope, None)
            self._settled_baseline.pop(scope, None)
            return 0.0
        remaining = settled - observed_advance
        self._settled[scope] = remaining
        self._settled_baseline[scope] = observed
        return remaining

    def _decision(self, *, task_class: str, provider_scope: str, period: str, observed: Any,
                  reserved: float, hard_limit: Any, remaining: Any, freshness: str,
                  decision: str, reasons: list[str], reset_at: Any = None,
                  aggregate_observed: Any = None, aggregate_hard_limit: Any = None,
                  aggregate_reserved: float = 0.0,
                  auto_fallback_allowed: bool = False, reservation_scope: str = "", aggregate_scope: str = "") -> dict[str, Any]:
        return {
            "schema": DECISION_SCHEMA,
            "task_class": task_class,
            "provider_scope": provider_scope,
            "reservation_scope": reservation_scope or provider_scope,
            "aggregate_scope": aggregate_scope,
            "period": period,
            "observed_usage": observed,
            "aggregate_observed_usage": aggregate_observed if aggregate_observed is not None else observed,
            "reserved_usage": round(reserved, 8),
            "hard_limit": hard_limit,
            "aggregate_hard_limit": aggregate_hard_limit if aggregate_hard_limit is not None else hard_limit,
            "aggregate_reserved_usage": round(aggregate_reserved, 8),
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
        if self._ledger_error:
            return self._decision(
                task_class=task, provider_scope=provider_scope, period=current_period,
                observed={}, reserved=0.0, hard_limit=0.0, remaining=0.0,
                freshness="unavailable", decision="block", reasons=["shared_budget_ledger_unavailable"],
            )
        scope = self._scope(provider_name, provider_scope, repo, task, current_period)
        aggregate_scope = self._aggregate_scope(provider_name, provider_scope, current_period)
        amount = max(0.0, _number(estimated_cost_usd))
        if provider_name in {"codex", "codex-cli", "codex_account"}:
            return self._preflight_codex(task, provider_scope, current_period, codex_snapshot, scope, aggregate_scope)

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
        aggregate_hard_limit = max(0.0, provider_limit * 0.80)
        with self._lock:
            if self._settled_period != current_period:
                self._reserved.clear()
                self._settled.clear()
                self._settled_baseline.clear()
                self._reservations.clear()
                self._settled_period = current_period
            self._load_scope_locked(scope)
            if aggregate_scope != scope:
                self._load_scope_locked(aggregate_scope)
            reserved = self._reserved.get(scope, 0.0)
            settled = self._reconcile_settled_locked(scope, 0.0)
            aggregate_reserved = self._reserved.get(aggregate_scope, 0.0)
            aggregate_settled = self._reconcile_settled_locked(aggregate_scope, used)
        repo_remaining = hard_limit - settled - reserved - amount
        aggregate_remaining = aggregate_hard_limit - used - aggregate_settled - aggregate_reserved - amount
        remaining = min(repo_remaining, aggregate_remaining)
        if remaining < 0:
            reason = "monthly_hard_limit_reached" if remaining <= 0 else "monthly_budget_insufficient"
            return self._decision(
                task_class=task, provider_scope=provider_scope, period=current_period,
                observed=0.0, reserved=reserved, hard_limit=round(hard_limit, 8),
                remaining=round(max(0.0, remaining), 8), freshness="fresh", decision="defer",
                reasons=[item for item in (reason, "api_fallback_requires_human_approval" if not human_approved_fallback else "") if item],
                aggregate_observed=used, aggregate_hard_limit=round(aggregate_hard_limit, 8),
                aggregate_reserved=aggregate_reserved,
            )
        return self._decision(
            task_class=task, provider_scope=provider_scope, period=current_period,
            observed=0.0, reserved=reserved, hard_limit=round(hard_limit, 8),
            remaining=round(remaining, 8), freshness="fresh", decision="allow", reasons=[],
            aggregate_observed=used, aggregate_hard_limit=round(aggregate_hard_limit, 8),
            aggregate_reserved=aggregate_reserved,
            auto_fallback_allowed=human_approved_fallback,
            reservation_scope=scope, aggregate_scope=self._aggregate_scope(provider_name, provider_scope, current_period),
        )

    def _preflight_codex(self, task: str, provider_scope: str, period: str,
                         snapshot: dict[str, Any] | None, scope: str, aggregate_scope: str) -> dict[str, Any]:
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
                raw_used = window.get("used_percent")
                try:
                    parsed_used = float(raw_used)
                except (TypeError, ValueError):
                    parsed_used = float("nan")
                if not math.isfinite(parsed_used):
                    return self._decision(
                        task_class=task, provider_scope=provider_scope, period=period,
                        observed=snapshot, reserved=0.0, hard_limit=CODEX_RESERVE_RATIO,
                        remaining=0.0, freshness="invalid", decision="defer",
                        reasons=["codex_rate_limit_window_invalid"],
                    )
                remaining = 100 - parsed_used
            if remaining is None:
                return self._decision(
                    task_class=task, provider_scope=provider_scope, period=period,
                    observed=snapshot, reserved=0.0, hard_limit=CODEX_RESERVE_RATIO,
                    remaining=0.0, freshness="invalid", decision="defer",
                    reasons=["codex_rate_limit_window_invalid"],
                )
            try:
                parsed_remaining = float(remaining)
            except (TypeError, ValueError):
                parsed_remaining = float("nan")
            if not math.isfinite(parsed_remaining):
                return self._decision(
                    task_class=task, provider_scope=provider_scope, period=period,
                    observed=snapshot, reserved=0.0, hard_limit=CODEX_RESERVE_RATIO,
                    remaining=0.0, freshness="invalid", decision="defer",
                    reasons=["codex_rate_limit_window_invalid"],
                )
            ratios.append(max(0.0, min(1.0, parsed_remaining / 100.0)))
            reset_at.append(window.get("resets_at"))
        tightest = min(ratios)
        with self._lock:
            if self._settled_period != period:
                self._reserved.clear()
                self._settled.clear()
                self._settled_baseline.clear()
                self._reservations.clear()
                self._settled_period = period
            self._load_scope_locked(scope)
            if any(
                (parsed := _reset_timestamp(value)) is not None and parsed <= self._clock()
                for value in reset_at
            ):
                for key in list(self._reserved):
                    if key.startswith("codex/"):
                        self._reserved.pop(key, None)
                for key in list(self._settled):
                    if key.startswith("codex/"):
                        self._settled.pop(key, None)
                for key in list(self._settled_baseline):
                    if key.startswith("codex/"):
                        self._settled_baseline.pop(key, None)
                for reservation_id, reservation in list(self._reservations.items()):
                    if reservation.scope.startswith("codex/") or reservation.aggregate_scope.startswith("codex/"):
                        self._reservations.pop(reservation_id, None)
                if self._ledger_path:
                    try:
                        with sqlite3.connect(self._ledger_path, timeout=30) as db:
                            db.execute(
                                "DELETE FROM ai_budget_reservations WHERE scope LIKE 'codex/%' OR aggregate_scope LIKE 'codex/%'"
                            )
                            db.execute("DELETE FROM ai_budget_ledger WHERE scope LIKE 'codex/%'")
                    except (OSError, sqlite3.Error):
                        pass
            settled = self._settled.get(scope, 0.0)
            reserved = self._reserved.get(scope, 0.0)
        threshold = CODEX_THRESHOLDS.get(task, CODEX_THRESHOLDS["maintenance"])
        headroom = max(0.0, tightest - CODEX_RESERVE_RATIO)
        remaining = max(0.0, headroom - settled - reserved)
        allowed = tightest >= threshold and remaining >= CODEX_RESERVATION_RATIO
        return self._decision(
            task_class=task, provider_scope=provider_scope, period=period,
            observed={"primary_remaining_ratio": ratios[0], "secondary_remaining_ratio": ratios[1]},
            reserved=round(settled + reserved, 8), hard_limit=round(headroom, 8),
            remaining=round(remaining, 8), freshness="fresh",
            decision="allow" if allowed else "defer",
            reasons=[] if allowed else [
                "codex_rate_limit_below_task_threshold"
                if tightest < threshold
                else "codex_reservation_headroom_insufficient"
            ],
            reset_at=reset_at,
            reservation_scope=scope,
            aggregate_scope=aggregate_scope,
        )

    def reserve(self, decision: dict[str, Any], amount: float | None = None) -> Reservation | None:
        if not isinstance(decision, dict) or decision.get("decision") != "allow":
            return None
        if amount is None:
            return None
        scope = str(decision.get("reservation_scope") or decision.get("provider_scope") or "default")
        aggregate_scope = str(decision.get("aggregate_scope") or scope)
        # Provider scope in a decision is intentionally the stable ledger key;
        # callers may pass an explicit amount for API reservations.
        requested = max(0.0, _number(amount, _number(decision.get("remaining_after_reservation"))))
        observed_value = decision.get("observed_usage")
        aggregate_observed_value = decision.get("aggregate_observed_usage")
        baseline_usage = _number(aggregate_observed_value, _number(observed_value, -1.0))
        reservation = Reservation(
            uuid.uuid4().hex,
            scope,
            requested,
            str(decision.get("task_class") or "maintenance"),
            self._clock(),
            baseline_usage if baseline_usage >= 0 else None,
            self.period(),
            aggregate_scope,
        )
        with self._lock:
            current_period = self.period()
            decision_period = str(decision.get("period") or current_period)
            if decision_period != current_period:
                return None
            if self._settled_period != current_period:
                self._reserved.clear()
                self._settled.clear()
                self._settled_baseline.clear()
                self._reservations.clear()
                self._settled_period = current_period
            if self._ledger_path:
                try:
                    with sqlite3.connect(self._ledger_path, timeout=30) as db:
                        db.execute("BEGIN IMMEDIATE")
                        row = db.execute(
                            "SELECT reserved,settled,baseline FROM ai_budget_ledger WHERE scope=? AND period=?",
                            (scope, self._settled_period),
                        ).fetchone()
                        aggregate_row = db.execute(
                            "SELECT reserved,settled,baseline FROM ai_budget_ledger WHERE scope=? AND period=?",
                            (aggregate_scope, self._settled_period),
                        ).fetchone()
                        stored_reserved = max(0.0, float(row[0])) if row else 0.0
                        stored_settled = max(0.0, float(row[1])) if row else 0.0
                        stored_baseline = float(row[2]) if row and row[2] is not None else None
                        aggregate_reserved = max(0.0, float(aggregate_row[0])) if aggregate_row else 0.0
                        aggregate_settled = max(0.0, float(aggregate_row[1])) if aggregate_row else 0.0
                        aggregate_baseline = float(aggregate_row[2]) if aggregate_row and aggregate_row[2] is not None else None
                        observed_value = decision.get("observed_usage")
                        observed = float(observed_value) if isinstance(observed_value, (int, float)) and not isinstance(observed_value, bool) else 0.0
                        aggregate_observed_value = decision.get("aggregate_observed_usage")
                        aggregate_observed = float(aggregate_observed_value) if isinstance(aggregate_observed_value, (int, float)) and not isinstance(aggregate_observed_value, bool) else observed
                        if aggregate_baseline is not None and aggregate_observed >= aggregate_baseline + aggregate_settled:
                            aggregate_settled = 0.0
                            aggregate_baseline = None
                        if stored_baseline is not None and observed >= stored_baseline + stored_settled:
                            stored_settled = 0.0
                            stored_baseline = None
                        hard_limit = _number(decision.get("hard_limit"), float("inf"))
                        aggregate_hard_limit = _number(decision.get("aggregate_hard_limit"), hard_limit)
                        if hard_limit != float("inf") and (
                            observed + stored_settled + stored_reserved + requested > hard_limit + 1e-12
                            or aggregate_observed + aggregate_settled + aggregate_reserved + requested > aggregate_hard_limit + 1e-12
                        ):
                            return None
                        new_reserved = stored_reserved + requested
                        db.execute(
                            "INSERT INTO ai_budget_ledger(scope,period,reserved,settled,baseline,updated_at) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(scope,period) DO UPDATE SET reserved=excluded.reserved,settled=excluded.settled,baseline=excluded.baseline,updated_at=excluded.updated_at",
                            (scope, self._settled_period, new_reserved, stored_settled, stored_baseline, self._clock()),
                        )
                        if aggregate_scope != scope:
                            db.execute(
                                "INSERT INTO ai_budget_ledger(scope,period,reserved,settled,baseline,updated_at) VALUES(?,?,?,?,?,?) "
                                "ON CONFLICT(scope,period) DO UPDATE SET reserved=excluded.reserved,settled=excluded.settled,baseline=excluded.baseline,updated_at=excluded.updated_at",
                                (aggregate_scope, self._settled_period, aggregate_reserved + requested, aggregate_settled, aggregate_baseline, self._clock()),
                            )
                        db.execute(
                            "INSERT INTO ai_budget_reservations(reservation_id,scope,aggregate_scope,period,amount,task_class,created_at,baseline_usage) VALUES(?,?,?,?,?,?,?,?)",
                            (reservation.reservation_id, reservation.scope, reservation.aggregate_scope, reservation.period, reservation.amount, reservation.task_class, reservation.created_at, reservation.baseline_usage),
                        )
                        db.commit()
                    self._reserved[scope] = new_reserved
                    self._settled[scope] = stored_settled
                    if aggregate_scope != scope:
                        self._reserved[aggregate_scope] = aggregate_reserved + requested
                        self._settled[aggregate_scope] = aggregate_settled
                    if stored_baseline is not None:
                        self._settled_baseline[scope] = stored_baseline
                    if aggregate_scope != scope and aggregate_baseline is not None:
                        self._settled_baseline[aggregate_scope] = aggregate_baseline
                    self._reservations[reservation.reservation_id] = reservation
                    return reservation
                except (OSError, sqlite3.Error, TypeError, ValueError):
                    return None
            self._load_scope_locked(scope)
            hard_limit = _number(decision.get("hard_limit"), float("inf"))
            aggregate_hard_limit = _number(decision.get("aggregate_hard_limit"), hard_limit)
            observed_value = decision.get("observed_usage")
            if isinstance(observed_value, (int, float)) and not isinstance(observed_value, bool):
                observed = float(observed_value)
                settled = self._reconcile_settled_locked(scope, observed)
            else:
                observed = 0.0
                settled = self._settled.get(scope, 0.0)
            aggregate_observed = _number(decision.get("aggregate_observed_usage"), observed)
            aggregate_settled = self._reconcile_settled_locked(aggregate_scope, aggregate_observed)
            aggregate_reserved = self._reserved.get(aggregate_scope, 0.0)
            if hard_limit != float("inf") and (
                observed + settled + self._reserved.get(scope, 0.0) + requested > hard_limit + 1e-12
                or aggregate_observed + aggregate_settled + aggregate_reserved + requested > aggregate_hard_limit + 1e-12
            ):
                return None
            self._reservations[reservation.reservation_id] = reservation
            self._reserved[scope] = self._reserved.get(scope, 0.0) + requested
            if aggregate_scope != scope:
                self._reserved[aggregate_scope] = aggregate_reserved + requested
            self._save_scope_locked(scope)
            if aggregate_scope != scope:
                self._save_scope_locked(aggregate_scope)
        return reservation

    def release(self, reservation: Reservation | str) -> bool:
        rid = reservation.reservation_id if isinstance(reservation, Reservation) else str(reservation)
        with self._lock:
            if self._ledger_path:
                try:
                    with sqlite3.connect(self._ledger_path, timeout=30) as db:
                        db.execute("BEGIN IMMEDIATE")
                        row = db.execute(
                            "SELECT scope,aggregate_scope,period,amount FROM ai_budget_reservations WHERE reservation_id=?",
                            (rid,),
                        ).fetchone()
                        if row is None:
                            db.rollback()
                            return False
                        scope, aggregate_scope, period, amount = row
                        db.execute(
                            "UPDATE ai_budget_ledger SET reserved=MAX(0,reserved-?),updated_at=? WHERE scope=? AND period=?",
                            (float(amount), self._clock(), scope, period),
                        )
                        if aggregate_scope != scope:
                            db.execute(
                                "UPDATE ai_budget_ledger SET reserved=MAX(0,reserved-?),updated_at=? WHERE scope=? AND period=?",
                                (float(amount), self._clock(), aggregate_scope, period),
                            )
                        db.execute("DELETE FROM ai_budget_reservations WHERE reservation_id=?", (rid,))
                    self._reservations.pop(rid, None)
                    self._load_scope_locked(scope)
                    if aggregate_scope != scope:
                        self._load_scope_locked(aggregate_scope)
                    return True
                except (OSError, sqlite3.Error, TypeError, ValueError):
                    return False
            item = self._reservations.pop(rid, None)
            if item is None:
                return False
            if item.period != self.period():
                return True
            self._reserved[item.scope] = max(0.0, self._reserved.get(item.scope, 0.0) - item.amount)
            if item.aggregate_scope != item.scope:
                self._reserved[item.aggregate_scope] = max(0.0, self._reserved.get(item.aggregate_scope, 0.0) - item.amount)
            self._save_scope_locked(item.scope)
            if item.aggregate_scope != item.scope:
                self._save_scope_locked(item.aggregate_scope)
            return True

    def settle(self, reservation: Reservation | str, actual_cost: float) -> bool:
        rid = reservation.reservation_id if isinstance(reservation, Reservation) else str(reservation)
        with self._lock:
            if self._ledger_path:
                try:
                    amount = max(0.0, _number(actual_cost))
                    with sqlite3.connect(self._ledger_path, timeout=30) as db:
                        db.execute("BEGIN IMMEDIATE")
                        row = db.execute(
                            "SELECT scope,aggregate_scope,period,amount,baseline_usage FROM ai_budget_reservations WHERE reservation_id=?",
                            (rid,),
                        ).fetchone()
                        if row is None:
                            db.rollback()
                            return False
                        scope, aggregate_scope, period, reserved_amount, baseline_usage = row
                        ledger = db.execute(
                            "SELECT period,settled,baseline FROM ai_budget_ledger WHERE scope=? AND period=?",
                            (scope, period),
                        ).fetchone()
                        period = ledger[0] if ledger else period
                        settled = float(ledger[1]) if ledger else 0.0
                        baseline = ledger[2] if ledger else None
                        if baseline is None:
                            baseline = baseline_usage
                        db.execute(
                            "INSERT INTO ai_budget_ledger(scope,period,reserved,settled,baseline,updated_at) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(scope,period) DO UPDATE SET reserved=MAX(0,ai_budget_ledger.reserved-?),settled=ai_budget_ledger.settled+?,baseline=COALESCE(ai_budget_ledger.baseline,excluded.baseline),updated_at=excluded.updated_at",
                            (scope, period, max(0.0, float(reserved_amount)), settled + amount, baseline, self._clock(), float(reserved_amount), amount),
                        )
                        if aggregate_scope != scope:
                            aggregate_row = db.execute(
                                "SELECT settled,baseline FROM ai_budget_ledger WHERE scope=? AND period=?",
                                (aggregate_scope, period),
                            ).fetchone()
                            aggregate_baseline = aggregate_row[1] if aggregate_row and aggregate_row[1] is not None else baseline_usage
                            db.execute(
                                "UPDATE ai_budget_ledger SET reserved=MAX(0,reserved-?),settled=settled+?,baseline=COALESCE(baseline,?),updated_at=? WHERE scope=? AND period=?",
                                (float(reserved_amount), amount, aggregate_baseline, self._clock(), aggregate_scope, period),
                            )
                        db.execute("DELETE FROM ai_budget_reservations WHERE reservation_id=?", (rid,))
                    self._reservations.pop(rid, None)
                    self._load_scope_locked(scope)
                    if aggregate_scope != scope:
                        self._load_scope_locked(aggregate_scope)
                    return True
                except (OSError, sqlite3.Error, TypeError, ValueError):
                    return False
            item = self._reservations.pop(rid, None)
            if item is None:
                return False
            if item.period != self.period():
                return True
            self._reserved[item.scope] = max(0.0, self._reserved.get(item.scope, 0.0) - item.amount)
            if item.aggregate_scope != item.scope:
                self._reserved[item.aggregate_scope] = max(0.0, self._reserved.get(item.aggregate_scope, 0.0) - item.amount)
            amount = max(0.0, _number(actual_cost))
            if amount:
                self._settled.setdefault(item.scope, 0.0)
                if item.baseline_usage is not None:
                    self._settled_baseline.setdefault(item.scope, item.baseline_usage)
                self._settled[item.scope] += amount
                if item.aggregate_scope != item.scope:
                    if item.baseline_usage is not None:
                        self._settled_baseline.setdefault(item.aggregate_scope, item.baseline_usage)
                    self._settled[item.aggregate_scope] = self._settled.get(item.aggregate_scope, 0.0) + amount
            self._save_scope_locked(item.scope)
            if item.aggregate_scope != item.scope:
                self._save_scope_locked(item.aggregate_scope)
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
    "CODEX_RESERVATION_RATIO",
    "CODEX_THRESHOLDS",
    "DECISION_SCHEMA",
    "Reservation",
    "ai_budget_preflight",
    "get_ai_budget_guard",
]
