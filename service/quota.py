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

from service.codex_account import read_codex_rate_limits

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
    api_key_tokens_input: int = 0
    api_key_tokens_output: int = 0
    api_calls: int = 0
    api_calls_incomplete: bool = False
    codex_calls: int = 0
    total_cost_usd: float = 0.0
    api_key_cost_usd: float = 0.0
    codex_cost_usd: float = 0.0
    last_reset_daily: float = field(default_factory=time.time)
    last_reset_weekly: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "api_key_tokens_input": self.api_key_tokens_input,
            "api_key_tokens_output": self.api_key_tokens_output,
            "api_calls": self.api_calls,
            "api_calls_incomplete": self.api_calls_incomplete,
            "codex_calls": self.codex_calls,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "api_key_cost_usd": round(self.api_key_cost_usd, 4),
            "codex_cost_usd": round(self.codex_cost_usd, 4),
            "last_reset_daily": self.last_reset_daily,
            "last_reset_weekly": self.last_reset_weekly,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QuotaRecord":
        total_cost_usd = float(d.get("total_cost_usd", 0.0))
        codex_cost_usd = float(
            d.get(
                "codex_cost_usd",
                min(total_cost_usd, DEFAULT_MODEL_COSTS.get("codex-cli", {}).get("flat", 0.05) * int(d.get("codex_calls", 0))),
            )
        )
        api_key_cost_usd = float(d.get("api_key_cost_usd", max(0.0, total_cost_usd - codex_cost_usd)))
        has_api_calls = "api_calls" in d
        api_calls = int(d.get("api_calls", 0))
        codex_calls = int(d.get("codex_calls", 0))
        tokens_input = int(d.get("tokens_input", 0))
        tokens_output = int(d.get("tokens_output", 0))
        has_split_api_tokens = "api_key_tokens_input" in d or "api_key_tokens_output" in d
        aggregate_tokens_are_api = not has_split_api_tokens and codex_calls == 0 and (tokens_input > 0 or tokens_output > 0)
        legacy_api_activity = api_key_cost_usd > 0 or aggregate_tokens_are_api
        api_calls_incomplete = bool(d.get("api_calls_incomplete", False) or (not has_api_calls and legacy_api_activity))
        return cls(
            repo=str(d.get("repo", "")),
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            api_key_tokens_input=int(d.get("api_key_tokens_input", tokens_input if aggregate_tokens_are_api else 0)),
            api_key_tokens_output=int(d.get("api_key_tokens_output", tokens_output if aggregate_tokens_are_api else 0)),
            api_calls=api_calls,
            api_calls_incomplete=api_calls_incomplete,
            codex_calls=codex_calls,
            total_cost_usd=total_cost_usd,
            api_key_cost_usd=api_key_cost_usd,
            codex_cost_usd=codex_cost_usd,
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
        self._codex_account_cache: dict[str, Any] | None = None
        self._codex_account_cache_ts = 0.0
        self._codex_account_attempt_ts = 0.0
        self._load_config()
        self._load_records()

    def _store_path(self) -> Path | None:
        path = os.environ.get("CODEX_AUDIT_SERVICE_QUOTA_STORE", "").strip()
        if path:
            return Path(path)
        job_dir = os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR", "").strip()
        if job_dir:
            return Path(job_dir) / "quota.json"
        return None

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

    def _load_records(self) -> None:
        path = self._store_path()
        if path is None:
            return
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        records = raw.get("records") if isinstance(raw, dict) else None
        if not isinstance(records, dict):
            return
        self._records = {
            repo: QuotaRecord.from_dict(item)
            for repo, item in records.items()
            if isinstance(repo, str) and isinstance(item, dict)
        }

    def _save_records_locked(self) -> None:
        path = self._store_path()
        if path is None:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(
            {"records": {repo: record.to_dict() for repo, record in self._records.items()}},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _reset_if_needed(self, record: QuotaRecord) -> QuotaRecord:
        now = time.time()
        if now - record.last_reset_daily > 86400:
            record.tokens_input = 0
            record.tokens_output = 0
            record.api_key_tokens_input = 0
            record.api_key_tokens_output = 0
            record.codex_calls = 0
            record.total_cost_usd = 0.0
            record.api_calls = 0
            record.api_calls_incomplete = False
            record.api_key_cost_usd = 0.0
            record.codex_cost_usd = 0.0
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
                record.codex_cost_usd += cost
            else:
                record.api_calls += 1
                record.api_key_tokens_input += tokens_input
                record.api_key_tokens_output += tokens_output
                record.api_key_cost_usd += cost
            record.total_cost_usd += cost
            self._records[repo] = record
            self._save_records_locked()

    def record_execute(self, repo: str) -> None:
        """Record a codex exec call (flat cost)."""
        cost = DEFAULT_MODEL_COSTS.get("codex-cli", {}).get("flat", 0.05)
        with self._lock:
            if repo not in self._records:
                self._records[repo] = QuotaRecord(repo=repo)
            record = self._records[repo]
            record = self._reset_if_needed(record)
            record.codex_calls += 1
            record.codex_cost_usd += cost
            record.total_cost_usd += cost
            self._records[repo] = record
            self._save_records_locked()

    def _codex_account_snapshot(self) -> dict[str, Any] | None:
        try:
            ttl = max(15, int(os.environ.get("CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_CACHE_SECONDS", "120")))
        except ValueError:
            ttl = 120
        failure_ttl = min(ttl, 60)
        now = time.time()
        if self._codex_account_cache and now - self._codex_account_cache_ts < ttl:
            return self._codex_account_cache
        if now - self._codex_account_attempt_ts < failure_ttl:
            return None
        self._codex_account_attempt_ts = now
        snapshot = read_codex_rate_limits()
        if snapshot:
            self._codex_account_cache = snapshot
            self._codex_account_cache_ts = now
        return snapshot

    def _summary_from_statuses(self, statuses: dict[str, dict[str, Any]], codex_account: dict[str, Any] | None = None) -> dict[str, Any]:
        api_key_cost = sum(float(item.get("api_key_cost_usd", 0.0)) for item in statuses.values())
        codex_cost = sum(float(item.get("codex_cost_usd", 0.0)) for item in statuses.values())
        total_cost = api_key_cost + codex_cost
        summary = {
            "quota_source": "internal_estimate",
            "combined": {
                "label": "API key + Codex",
                "total_cost_usd": round(total_cost, 4),
            },
            "api_key": {
                "label": "API key",
                "calls": sum(int(item.get("api_calls", 0)) for item in statuses.values()),
                "calls_incomplete": any(bool(item.get("api_calls_incomplete", False)) for item in statuses.values()),
                "tokens_input": sum(int(item.get("api_key_tokens_input", 0)) for item in statuses.values()),
                "tokens_output": sum(int(item.get("api_key_tokens_output", 0)) for item in statuses.values()),
                "total_cost_usd": round(api_key_cost, 4),
            },
            "codex": {
                "label": "Codex",
                "calls": sum(int(item.get("codex_calls", 0)) for item in statuses.values()),
                "total_cost_usd": round(codex_cost, 4),
            },
        }
        if codex_account:
            summary["codex_account"] = codex_account
        return summary

    def status(self, repo: str = "") -> dict[str, Any]:
        """Get quota status for a repo or all repos."""
        with self._lock:
            if repo:
                record = self._records.get(repo)
                if not record:
                    empty = QuotaRecord(repo=repo).to_dict()
                    return {**empty, "daily_budget": self.get_daily_budget(repo), "weekly_budget": self.get_weekly_budget(repo)}
                record = self._reset_if_needed(record)
                return {
                    **record.to_dict(),
                    "daily_budget": self.get_daily_budget(repo),
                    "weekly_budget": self.get_weekly_budget(repo),
                    "remaining_daily": self.remaining_daily(repo),
                }
            repo_names = list(self._records)
            default_daily_budget = self._daily_budget
        repos = {r: self.status(r) for r in repo_names}
        return {
            "repos": repos,
            "summary": self._summary_from_statuses(repos, self._codex_account_snapshot()),
            "default_daily_budget": default_daily_budget,
        }


# Singleton
_quota_manager = QuotaManager()


def get_quota_manager() -> QuotaManager:
    return _quota_manager
