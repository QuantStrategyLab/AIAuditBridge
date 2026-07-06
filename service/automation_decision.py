"""Health-driven execution decisions for automation scheduling."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from service.automation_run_ledger import CONTROL_ESCALATE, CONTROL_PAUSE_AUTO_FIX, CONTROL_REVIEW_ONLY
from service.quota import recommend_model
from service.task_state import TERMINAL_STATES

EXECUTION_RUN = "run"
EXECUTION_REVIEW_ONLY = "review_only"
EXECUTION_DEFER = "defer"
EXECUTION_HUMAN_REVIEW = "human_review"

MODE_REVIEW_AND_FIX = "review_and_fix"
MODE_REVIEW_ONLY = "review_only"

AUTONOMY_MANUAL = "manual"
AUTONOMY_REVIEW_ONLY = "review_only"
AUTONOMY_AUTO_PR = "auto_pr"
AUTONOMY_AUTO_MERGE = "auto_merge"
AUTONOMY_ORDER = (AUTONOMY_MANUAL, AUTONOMY_REVIEW_ONLY, AUTONOMY_AUTO_PR, AUTONOMY_AUTO_MERGE)
AUTONOMY_RANK = {level: index for index, level in enumerate(AUTONOMY_ORDER)}

DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_LOW_COST_MODEL = "gpt-5.4-mini"
DEFAULT_LOW_COST_PROVIDER = "openai"
EXECUTION_POLICY_PATH_ENV = "CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH"
POLICY_LOAD_ERROR_KEY = "_load_error"
TRUSTED_FAILURE_ORIGINS = frozenset({"service_job"})
POLICY_ALLOWED_KEYS = frozenset({"max_autonomy", "max_consecutive_failures", "low_cost_model", "low_cost_provider", "quota_low_behavior"})
POLICY_REQUIRED_DEFAULT_KEYS = frozenset({"max_autonomy", "max_consecutive_failures", "low_cost_model", "low_cost_provider"})
POLICY_LOW_QUOTA_BEHAVIORS = frozenset({"low_cost_model", "defer"})
POLICY_PROVIDERS = frozenset({"auto", "openai", "anthropic", "api", "codex"})
QUOTA_STATUS_SEVERITY = {
    "ok": 0,
    "healthy": 0,
    "unknown": 1,
    "unavailable": 1,
    "low": 2,
    "constrained": 2,
    "exhausted": 3,
    "blocked": 3,
}


def _normalize_status(value: Any, default: str = "unknown") -> str:
    if isinstance(value, dict):
        value = value.get("status", default)
    return str(value or default).strip().lower()


def _normalize_quota_status(value: Any, default: str = "unknown") -> str:
    statuses = [_normalize_status(value, "")]
    if isinstance(value, dict) and isinstance(value.get("quota"), dict):
        statuses.append(_normalize_status(value["quota"], ""))
    normalized = [status for status in statuses if status]
    if not normalized:
        return default
    return max(normalized, key=lambda status: QUOTA_STATUS_SEVERITY.get(status, 1))


def _normalize_mode(value: str) -> str:
    return MODE_REVIEW_AND_FIX if str(value or "").strip() == MODE_REVIEW_AND_FIX else MODE_REVIEW_ONLY


def _normalize_repo_id(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_autonomy(value: Any, default: str = AUTONOMY_AUTO_PR) -> tuple[str, str]:
    level = str(value or "").strip().lower()
    if not level:
        return default, ""
    if level in AUTONOMY_RANK:
        return level, ""
    return AUTONOMY_MANUAL, f"invalid max_autonomy {level!r}; forcing manual"


def _safe_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _repo_from_run(run: dict[str, Any]) -> str:
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    return str(metadata.get("source_repository") or metadata.get("repository") or "")


def _fail_closed_policy(reason: str) -> dict[str, Any]:
    return {
        POLICY_LOAD_ERROR_KEY: reason,
        "default": {
            "max_autonomy": AUTONOMY_MANUAL,
            "max_consecutive_failures": 1,
        },
    }


def _validate_execution_policy(payload: dict[str, Any]) -> str:
    default_policy = payload.get("default")
    if not isinstance(default_policy, dict):
        return "execution policy default section is invalid"
    default_error = _validate_policy_section(default_policy, section_name="default", require_defaults=True)
    if default_error:
        return default_error
    repositories = payload.get("repositories")
    if not isinstance(repositories, dict):
        return "execution policy repositories section is invalid"
    for repo, repo_policy in repositories.items():
        if not isinstance(repo_policy, dict):
            return f"execution policy override for {repo!r} is invalid"
        if not repo_policy:
            return f"execution policy override for {repo!r} is empty"
        repo_error = _validate_policy_section(repo_policy, section_name=f"override for {repo!r}", require_defaults=False)
        if repo_error:
            return repo_error
    return ""


def _validate_policy_section(section: dict[str, Any], *, section_name: str, require_defaults: bool) -> str:
    unknown_keys = set(section) - POLICY_ALLOWED_KEYS
    if unknown_keys:
        return f"execution policy {section_name} has unknown keys"
    if require_defaults:
        missing_keys = POLICY_REQUIRED_DEFAULT_KEYS - set(section)
        if missing_keys:
            return f"execution policy {section_name} is missing required keys"
    if "max_autonomy" in section and str(section["max_autonomy"] or "").strip().lower() not in AUTONOMY_RANK:
        return f"execution policy {section_name} has invalid max_autonomy"
    if "max_consecutive_failures" in section and _safe_positive_int(section["max_consecutive_failures"], 0) <= 0:
        return f"execution policy {section_name} has invalid max_consecutive_failures"
    if "low_cost_model" in section and not str(section["low_cost_model"] or "").strip():
        return f"execution policy {section_name} has invalid low_cost_model"
    if "low_cost_provider" in section:
        provider = str(section["low_cost_provider"] or "").strip().lower()
        if provider not in POLICY_PROVIDERS:
            return f"execution policy {section_name} has invalid low_cost_provider"
    if "quota_low_behavior" in section:
        behavior = str(section["quota_low_behavior"] or "").strip().lower()
        if behavior not in POLICY_LOW_QUOTA_BEHAVIORS:
            return f"execution policy {section_name} has invalid quota_low_behavior"
    return ""


def load_execution_policy(path: Path | None = None) -> dict[str, Any]:
    """Load service-owned execution policy for repo autonomy thresholds."""
    if path is None:
        configured = os.environ.get(EXECUTION_POLICY_PATH_ENV, "").strip()
        if not configured:
            return _fail_closed_policy("execution policy path is not configured")
        path = Path(configured).expanduser()
    if not path.exists():
        return _fail_closed_policy("execution policy file is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fail_closed_policy("execution policy file is unreadable")
    if not isinstance(payload, dict):
        return _fail_closed_policy("execution policy file is invalid")
    schema_error = _validate_execution_policy(payload)
    if schema_error:
        return _fail_closed_policy(schema_error)
    return payload


def repo_execution_policy(repo: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge default and repo-specific execution policy without trusting repo checkouts."""
    raw = policy if isinstance(policy, dict) else {}
    defaults = raw.get("default") if isinstance(raw.get("default"), dict) else {}
    repositories = raw.get("repositories") if isinstance(raw.get("repositories"), dict) else {}
    normalized_repo = _normalize_repo_id(repo)
    override = {}
    for configured_repo, configured_policy in repositories.items():
        if _normalize_repo_id(configured_repo) == normalized_repo and isinstance(configured_policy, dict):
            override = configured_policy
            break
    return {**defaults, **override}


def consecutive_failure_count(
    runs: list[dict[str, Any]],
    *,
    repo: str,
    task_name: str = "",
) -> int:
    """Count latest consecutive trusted failed runs for one repo from newest-first runs."""
    count = 0
    normalized_repo = _normalize_repo_id(repo)
    for run in runs:
        if not isinstance(run, dict):
            continue
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        if str(metadata.get("origin") or "") not in TRUSTED_FAILURE_ORIGINS:
            continue
        if normalized_repo and _normalize_repo_id(_repo_from_run(run)) != normalized_repo:
            continue
        state = str(run.get("task_state") or "").strip().lower()
        if state == "failed":
            count += 1
            continue
        if state in TERMINAL_STATES:
            break
    return count


def decide_automation_execution(
    *,
    repo: str,
    task_name: str = "",
    requested_mode: str = MODE_REVIEW_AND_FIX,
    requested_provider: str = "auto",
    requested_model: str = "",
    control_action: str = CONTROL_REVIEW_ONLY,
    service_health: Any = "",
    quota_status: Any = "",
    org_health_status: Any = "",
    recent_runs: list[dict[str, Any]] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a safe execution decision from health, quota, failures, and repo policy."""
    repo_policy = repo_execution_policy(repo, policy)
    policy_load_error = str((policy or {}).get(POLICY_LOAD_ERROR_KEY) or "") if isinstance(policy, dict) else ""
    max_autonomy, autonomy_config_error = _parse_autonomy(repo_policy.get("max_autonomy"), AUTONOMY_AUTO_PR)
    max_failures = _safe_positive_int(repo_policy.get("max_consecutive_failures"), DEFAULT_MAX_CONSECUTIVE_FAILURES)
    low_cost_model = str(repo_policy.get("low_cost_model") or DEFAULT_LOW_COST_MODEL)
    low_cost_provider = str(repo_policy.get("low_cost_provider") or DEFAULT_LOW_COST_PROVIDER).strip().lower()
    quota_low_behavior = str(repo_policy.get("quota_low_behavior") or "low_cost_model").strip().lower()

    effective_mode = _normalize_mode(requested_mode)
    effective_provider = str(requested_provider or "auto").strip().lower() or "auto"
    effective_model = str(requested_model or "").strip()
    action = EXECUTION_RUN
    reasons: list[str] = []
    human_review_required = False
    defer = False

    service = _normalize_status(service_health)
    quota = _normalize_quota_status(quota_status)
    org_health = _normalize_status(org_health_status)
    failures = consecutive_failure_count(recent_runs or [], repo=repo, task_name=task_name)

    if AUTONOMY_RANK[max_autonomy] <= AUTONOMY_RANK[AUTONOMY_REVIEW_ONLY]:
        effective_mode = MODE_REVIEW_ONLY
        human_review_required = True
        reasons.append(f"repo max autonomy is {max_autonomy}")
    if autonomy_config_error:
        reasons.append(autonomy_config_error)
    if policy_load_error:
        reasons.append(policy_load_error)
    if max_autonomy == AUTONOMY_MANUAL:
        action = EXECUTION_HUMAN_REVIEW

    if failures >= max_failures:
        action = EXECUTION_HUMAN_REVIEW
        effective_mode = MODE_REVIEW_ONLY
        human_review_required = True
        reasons.append(f"consecutive failures reached {failures}/{max_failures}")

    if control_action in {CONTROL_REVIEW_ONLY, CONTROL_PAUSE_AUTO_FIX, CONTROL_ESCALATE}:
        effective_mode = MODE_REVIEW_ONLY
        human_review_required = True
        reasons.append(f"runtime control action is {control_action}")
    if control_action == CONTROL_ESCALATE:
        action = EXECUTION_HUMAN_REVIEW

    if service == "degraded" or org_health == "degraded":
        effective_mode = MODE_REVIEW_ONLY
        human_review_required = True
        reasons.append("health degraded; forcing review_only")
    if service == "unhealthy" or org_health == "unhealthy":
        action = EXECUTION_HUMAN_REVIEW
        effective_mode = MODE_REVIEW_ONLY
        human_review_required = True
        reasons.append("health unhealthy; forcing human review")

    if quota in {"low", "constrained"}:
        effective_model = low_cost_model or recommend_model(0.0)
        effective_provider = low_cost_provider or "auto"
        if quota_low_behavior == "defer":
            if action != EXECUTION_HUMAN_REVIEW:
                action = EXECUTION_DEFER
                defer = True
            effective_mode = MODE_REVIEW_ONLY
            human_review_required = True
            reasons.append(f"quota status is {quota}; deferring automation")
        else:
            reasons.append(f"quota status is {quota}; recommending low-cost model")
    elif quota in {"exhausted", "blocked"}:
        if action != EXECUTION_HUMAN_REVIEW:
            action = EXECUTION_DEFER
            defer = True
        effective_mode = MODE_REVIEW_ONLY
        human_review_required = True
        reasons.append(f"quota status is {quota}; deferring automation")

    return {
        "action": action,
        "repo": repo,
        "task_name": task_name,
        "requested_mode": _normalize_mode(requested_mode),
        "effective_mode": effective_mode,
        "requested_provider": requested_provider,
        "effective_provider": effective_provider,
        "requested_model": requested_model,
        "effective_model": effective_model,
        "max_autonomy": max_autonomy,
        "consecutive_failures": failures,
        "max_consecutive_failures": max_failures,
        "human_review_required": human_review_required,
        "auto_fix_allowed": action == EXECUTION_RUN and effective_mode == MODE_REVIEW_AND_FIX and not human_review_required,
        "defer": defer,
        "reasons": reasons or ["execution allowed"],
    }
