"""Quota management — per-repo API budgets and model cost tracking.

Prevents a single repo or workflow from exhausting the shared API budget.
Tracks token consumption, enforces daily limits, and supports model
tier escalation (try cheap model first, upgrade only when needed).

Configuration via environment::

    CODEX_AUDIT_SERVICE_QUOTA_CONFIG=/path/to/quota.json

Example quota.json::

    {
      "default_daily_budget_usd": 5.0,
      "default_weekly_budget_usd": 25.0,
      "repo_budgets": {
        "QuantStrategyLab/CryptoLivePoolPipelines": {"daily": 10.0, "weekly": 50.0}
      },
      "model_costs_per_1k_tokens": {
        "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
        "gpt-5.4-mini": {"input": 0.00015, "output": 0.0006},
        "codex-cli": {"flat": 0.05}
      }
    }
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── constants ───────────────────────────────────────────────────────────

DEFAULT_DAILY_BUDGET_USD = 5.0
DEFAULT_WEEKLY_BUDGET_USD = 25.0

# Model cost estimates (USD per 1K tokens) — rough estimates, tune for actual pricing
DEFAULT_MODEL_COSTS: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-fable-5": {"input": 0.003, "output": 0.015},
    "gpt-5.4-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-5.4": {"input": 0.0025, "output": 0.01},
    "codex-cli": {"flat": 0.05},  # flat per-execution cost
}

# Model tier for escalation: cheaper → more capable
MODEL_TIERS: list[list[str]] = [
    ["gpt-5.4-mini"],              # tier 0: cheapest
    ["claude-sonnet-4-6"],         # tier 1: standard
    ["claude-fable-5", "gpt-5.4"], # tier 2: capable
    ["codex-cli"],                 # tier 3: most expensive
]

# ── data model ──────────────────────────────────────────────────────────


@dataclass
class QuotaRecord:
    repo: str
    tokens_input: int = 0
    tokens_output: int = 0
    codex_calls: int = 0
    total_cost_usd: float = 0.0
    last_reset_daily: float = field(default_factory=time.time)
    last_reset_weekly: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "codex_calls": self.codex_calls,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "last_reset_daily": self.last_reset_daily,
            "last_reset_weekly": self.last_reset_weekly,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QuotaRecord":
        return cls(
            repo=str(d.get("repo", "")),
            tokens_input=int(d.get("tokens_input", 0)),
            tokens_output=int(d.get("tokens_output", 0)),
            codex_calls=int(d.get("codex_calls", 0)),
            total_cost_usd=float(d.get("total_cost_usd", 0.0)),
            last_reset_daily=float(d.get("last_reset_daily", time.time())),
            last_reset_weekly=float(d.get("last_reset_weekly", time.time())),
        )


# ── cost estimation ────────────────────────────────────────────────────


def estimate_tokens(prompt: str) -> int:
    """Rough token count — ~4 chars per token for English text."""
    return max(1, len(prompt) // 4)


def estimate_cost(model: str, tokens_input: int, tokens_output: int = 0) -> float:
    """Estimate USD cost for a model call."""
    costs = DEFAULT_MODEL_COSTS.get(model, {})
    if "flat" in costs:
        return costs["flat"]
    input_cost = costs.get("input", 0.001) * tokens_input / 1000
    output_cost = costs.get("output", 0.005) * (tokens_output or tokens_input // 2) / 1000
    return input_cost + output_cost


def recommend_model(budget_remaining: float, min_confidence: float = 0.0) -> str:
    """Recommend the best model that fits within the remaining budget.

    Escalation logic:
    - budget < $0.01 → gpt-5.4-mini (cheapest)
    - budget < $0.05 → claude-sonnet-4-6
    - budget ≥ $0.05 → claude-fable-5 / gpt-5.4
    - codex-cli always requires explicit budget
    """
    if budget_remaining < 0.01:
        return "gpt-5.4-mini"
    if budget_remaining < 0.05:
        return "claude-sonnet-4-6"
    return "claude-sonnet-4-6"  # default


# ── quota store ─────────────────────────────────────────────────────────


class QuotaManager:
    """Thread-safe per-repo quota tracker with daily/weekly reset."""

    def __init__(self):
        self._records: dict[str, QuotaRecord] = {}
        self._lock = threading.RLock()
        self._model_costs = dict(DEFAULT_MODEL_COSTS)
        self._daily_budget = DEFAULT_DAILY_BUDGET_USD
        self._weekly_budget = DEFAULT_WEEKLY_BUDGET_USD
        self._repo_budgets: dict[str, dict[str, float]] = {}
        self._load_config()

    def _load_config(self) -> None:
        config_path = os.environ.get("CODEX_AUDIT_SERVICE_QUOTA_CONFIG", "").strip()
        if config_path and Path(config_path).exists():
            try:
                raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            if isinstance(raw.get("model_costs_per_1k_tokens"), dict):
                self._model_costs.update(raw["model_costs_per_1k_tokens"])
            self._daily_budget = float(raw.get("default_daily_budget_usd", DEFAULT_DAILY_BUDGET_USD))
            self._weekly_budget = float(raw.get("default_weekly_budget_usd", DEFAULT_WEEKLY_BUDGET_USD))
            if isinstance(raw.get("repo_budgets"), dict):
                self._repo_budgets = raw["repo_budgets"]

    def _reset_if_needed(self, record: QuotaRecord) -> QuotaRecord:
        now = time.time()
        if now - record.last_reset_daily > 86400:
            record.tokens_input = 0
            record.tokens_output = 0
            record.codex_calls = 0
            record.total_cost_usd = 0.0
            record.last_reset_daily = now
        if now - record.last_reset_weekly > 604800:
            record.last_reset_weekly = now
        return record

    def get_daily_budget(self, repo: str) -> float:
        return self._repo_budgets.get(repo, {}).get("daily", self._daily_budget)

    def get_weekly_budget(self, repo: str) -> float:
        return self._repo_budgets.get(repo, {}).get("weekly", self._weekly_budget)

    def remaining_daily(self, repo: str) -> float:
        with self._lock:
            record = self._records.get(repo)
            if not record:
                return self.get_daily_budget(repo)
            record = self._reset_if_needed(record)
            return max(0, self.get_daily_budget(repo) - record.total_cost_usd)

    def remaining_weekly(self, repo: str) -> float:
        with self._lock:
            record = self._records.get(repo)
            if not record:
                return self.get_weekly_budget(repo)
            return max(0, self.get_weekly_budget(repo) - record.total_cost_usd)

    def check(self, repo: str, model: str, prompt: str = "", estimated_output_tokens: int = 0) -> dict[str, Any]:
        """Check if the repo has enough quota remaining. Returns approval dict."""
        tokens_input = estimate_tokens(prompt)
        cost = estimate_cost(model, tokens_input, estimated_output_tokens)
        remaining = self.remaining_daily(repo)

        if remaining < cost:
            recommended = recommend_model(remaining)
            return {
                "allowed": False,
                "reason": f"Daily budget exceeded: ${remaining:.4f} remaining, ${cost:.4f} needed",
                "recommended_model": recommended,
                "remaining_usd": remaining,
                "cost_estimate_usd": cost,
            }
        return {
            "allowed": True,
            "cost_estimate_usd": cost,
            "remaining_usd": remaining - cost,
        }

    def record(self, repo: str, model: str, prompt: str, output: str = "") -> None:
        """Record a completed API call for quota tracking."""
        tokens_input = estimate_tokens(prompt)
        tokens_output = estimate_tokens(output) if output else tokens_input // 2
        cost = estimate_cost(model, tokens_input, tokens_output if output else tokens_input // 2)

        with self._lock:
            if repo not in self._records:
                self._records[repo] = QuotaRecord(repo=repo)
            record = self._records[repo]
            record = self._reset_if_needed(record)
            record.tokens_input += tokens_input
            record.tokens_output += tokens_output
            if model == "codex-cli":
                record.codex_calls += 1
            record.total_cost_usd += cost
            self._records[repo] = record

    def record_execute(self, repo: str) -> None:
        """Record a codex exec call (flat cost)."""
        cost = DEFAULT_MODEL_COSTS.get("codex-cli", {}).get("flat", 0.05)
        with self._lock:
            if repo not in self._records:
                self._records[repo] = QuotaRecord(repo=repo)
            record = self._records[repo]
            record = self._reset_if_needed(record)
            record.codex_calls += 1
            record.total_cost_usd += cost
            self._records[repo] = record

    def status(self, repo: str = "") -> dict[str, Any]:
        """Get quota status for a repo or all repos."""
        with self._lock:
            if repo:
                record = self._records.get(repo)
                if not record:
                    return {"repo": repo, "total_cost_usd": 0.0, "daily_budget": self.get_daily_budget(repo)}
                record = self._reset_if_needed(record)
                return {
                    **record.to_dict(),
                    "daily_budget": self.get_daily_budget(repo),
                    "weekly_budget": self.get_weekly_budget(repo),
                    "remaining_daily": self.remaining_daily(repo),
                }
            return {
                "repos": {r: self.status(r) for r in self._records},
                "default_daily_budget": self._daily_budget,
            }


# Singleton
_quota_manager = QuotaManager()


def get_quota_manager() -> QuotaManager:
    return _quota_manager
