"""Autonomy tiers — confidence-driven decision engine for unmanned operation.

Combines AI confidence scores with file risk classifications to produce
a recommended action. This is the policy layer that decides whether the
system should act autonomously or escalate to a human.

Action levels (ascending autonomy):
    escalate          — always require human review
    auto_notify       — apply change, notify human
    auto_pr           — create PR, let human merge at their convenience
    auto_merge        — create PR and auto-merge if CI passes

File risk tiers (from codex_auto_merge_policy.json):
    critical          — secrets, credentials, security-sensitive
    high              — strategy logic, core algorithms
    medium            — report generators, helper scripts, params
    low               — docs, tests, README

Decision matrix::

    Confidence →
Risk ↓   <0.60       0.60-0.79    0.80-0.94    ≥0.95
───────  ─────────── ──────────── ──────────── ───────────
    low      auto_pr     auto_merge   auto_merge   auto_merge
    medium   escalate    auto_pr      auto_pr      auto_pr
    high     escalate    escalate     auto_pr      auto_pr
    critical escalate    escalate     escalate     escalate
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── action constants ────────────────────────────────────────────────────

ACTION_ESCALATE = "escalate"
ACTION_AUTO_NOTIFY = "auto_notify"
ACTION_AUTO_PR = "auto_pr"
ACTION_AUTO_MERGE = "auto_merge"

# ordered by increasing autonomy
ACTION_ORDER = (ACTION_ESCALATE, ACTION_AUTO_NOTIFY, ACTION_AUTO_PR, ACTION_AUTO_MERGE)
ACTION_RANK = {action: index for index, action in enumerate(ACTION_ORDER)}

# ── risk tiers ──────────────────────────────────────────────────────────

RISK_CRITICAL = "critical"
RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"

# ── default decision matrix ─────────────────────────────────────────────

# (risk, min_confidence) → action
DEFAULT_DECISION_MATRIX: list[tuple[str, float, str]] = [
    # risk       min_confidence  action
    (RISK_LOW,      0.60, ACTION_AUTO_MERGE),
    (RISK_LOW,      0.00, ACTION_AUTO_PR),
    (RISK_MEDIUM,   0.70, ACTION_AUTO_PR),
    (RISK_MEDIUM,   0.00, ACTION_ESCALATE),
    (RISK_HIGH,     0.85, ACTION_AUTO_PR),
    (RISK_HIGH,     0.00, ACTION_ESCALATE),
    (RISK_CRITICAL, 0.00, ACTION_ESCALATE),  # never auto
]

# ── file risk classification (from codex_auto_merge_policy.json) ────────

CRITICAL_PATTERNS = (
    r"(^|/)(\.env|.*secret.*|.*credential.*|.*token.*|.*private.*|.*\.pem|.*\.key)$",
)
HIGH_RISK_PREFIXES = (
    "src/quant_",
    "application/",
)
MEDIUM_RISK_PREFIXES = (
    "scripts/",
)
LOW_RISK_PREFIXES = (
    "docs/",
    "tests/",
)
LOW_RISK_EXACT = frozenset({
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    ".gitignore",
})
CRITICAL_EXACT = frozenset({
    ".github/codex_auto_merge_policy.json",
})
REPO_ROOT = Path(__file__).resolve().parents[1]
AUTONOMY_POLICY_PATH_ENV = "CODEX_AUDIT_SERVICE_AUTONOMY_POLICY_PATH"


def load_autonomy_policy(path: Path | None = None) -> dict[str, Any]:
    """Load the shared autonomy policy from a trusted service-owned path.

    The service must not read policy rules from the untrusted PR checkout being
    reviewed. Set CODEX_AUDIT_SERVICE_AUTONOMY_POLICY_PATH to a deployment-owned
    file or pass an explicit path in tests/tools. Missing or malformed files
    fall back to the built-in conservative classifier.
    """
    if path is None:
        env_path = os.environ.get(AUTONOMY_POLICY_PATH_ENV, "").strip()
        if not env_path:
            return {}
        path = Path(env_path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class AutonomyConfig:
    """Configuration for the autonomy decision engine.

    Loaded from CODEX_AUDIT_SERVICE_AUTONOMY_CONFIG (JSON file path) or
    defaults. Override per-repo via the repo's codex_auto_merge_policy.json.
    """

    decision_matrix: list[tuple[str, float, str]] = field(default_factory=lambda: list(DEFAULT_DECISION_MATRIX))
    # per-repo overrides: repo_full_name → custom matrix
    repo_overrides: dict[str, list[tuple[str, float, str]]] = field(default_factory=dict)
    # global minimum confidence to take ANY autonomous action
    global_min_confidence: float = 0.60

    @classmethod
    def from_env(cls) -> "AutonomyConfig":
        config_path = os.environ.get("CODEX_AUDIT_SERVICE_AUTONOMY_CONFIG", "").strip()
        if config_path and Path(config_path).exists():
            raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
            return cls.from_dict(raw)
        return cls()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AutonomyConfig":
        matrix_raw = raw.get("decision_matrix")
        matrix: list[tuple[str, float, str]] = []
        if isinstance(matrix_raw, list):
            for entry in matrix_raw:
                if isinstance(entry, dict):
                    matrix.append((
                        str(entry.get("risk", RISK_LOW)),
                        float(entry.get("min_confidence", 0.0)),
                        str(entry.get("action", ACTION_ESCALATE)),
                    ))
        if not matrix:
            matrix = list(DEFAULT_DECISION_MATRIX)
        # sort by confidence descending so first match wins
        matrix.sort(key=lambda x: x[1], reverse=True)

        overrides: dict[str, list[tuple[str, float, str]]] = {}
        for repo, repo_matrix in raw.get("repo_overrides", {}).items():
            if isinstance(repo_matrix, list):
                overrides[repo] = [
                    (str(e.get("risk", RISK_LOW)), float(e.get("min_confidence", 0.0)), str(e.get("action", ACTION_ESCALATE)))
                    for e in repo_matrix if isinstance(e, dict)
                ]
                overrides[repo].sort(key=lambda x: x[1], reverse=True)

        return cls(
            decision_matrix=matrix,
            repo_overrides=overrides,
            global_min_confidence=float(raw.get("global_min_confidence", 0.60)),
        )

    def get_matrix(self, repo: str | None = None) -> list[tuple[str, float, str]]:
        if repo and repo in self.repo_overrides:
            return self.repo_overrides[repo]
        return self.decision_matrix


def _policy_matches(path: str, rule: dict[str, Any]) -> bool:
    exact = rule.get("exact")
    if isinstance(exact, list) and path in {str(item) for item in exact}:
        return True
    prefixes = rule.get("prefixes")
    if isinstance(prefixes, list) and any(path.startswith(str(prefix)) for prefix in prefixes):
        return True
    return False


def _blocked_by_policy(path: str, policy: dict[str, Any] | None) -> bool:
    if path in CRITICAL_EXACT:
        return True
    patterns = (policy or {}).get("blocked_path_patterns") if isinstance(policy, dict) else None
    raw_patterns = list(CRITICAL_PATTERNS)
    if isinstance(patterns, list):
        raw_patterns.extend(pattern for pattern in patterns if isinstance(pattern, str))
    for pattern in raw_patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        try:
            if re.search(pattern, path, flags=re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def classify_file_risk(path: str, *, policy: dict[str, Any] | None = None) -> str:
    """Classify a changed file path into a risk tier.

    Mirrors ``codex_auto_merge_policy.json`` when present, then falls back to
    the built-in conservative rules.
    """
    if _blocked_by_policy(path, policy):
        return RISK_CRITICAL

    risk_policy = (policy or {}).get("risk_policy") if isinstance(policy, dict) else None
    if isinstance(risk_policy, dict):
        for tier in (RISK_CRITICAL, RISK_HIGH, RISK_MEDIUM, RISK_LOW):
            rule = risk_policy.get(tier)
            if isinstance(rule, dict) and _policy_matches(path, rule):
                return tier

    # low: exact match
    if path in LOW_RISK_EXACT:
        return RISK_LOW

    # low: docs, tests
    for prefix in LOW_RISK_PREFIXES:
        if path.startswith(prefix):
            return RISK_LOW

    # medium: scripts
    for prefix in MEDIUM_RISK_PREFIXES:
        if path.startswith(prefix):
            return RISK_MEDIUM

    # high: strategy/application code
    for prefix in HIGH_RISK_PREFIXES:
        if path.startswith(prefix):
            return RISK_HIGH

    # default: medium (conservative — don't auto unknown file types)
    return RISK_MEDIUM


def classify_changes_risk(changed_paths: list[str], *, policy: dict[str, Any] | None = None) -> str:
    """Classify the overall risk of a set of changed file paths.

    Returns the highest risk tier among all changed files.
    """
    if not changed_paths:
        return RISK_LOW
    tiers = {classify_file_risk(p, policy=policy) for p in changed_paths}
    for tier in (RISK_CRITICAL, RISK_HIGH, RISK_MEDIUM, RISK_LOW):
        if tier in tiers:
            return tier
    return RISK_MEDIUM


def decide_action(
    confidence: float,
    risk: str,
    *,
    config: AutonomyConfig | None = None,
    repo: str | None = None,
) -> str:
    """Determine the recommended action based on AI confidence and file risk.

    Args:
        confidence: Aggregated AI confidence score (0.0–1.0).
        risk: File risk tier (low/medium/high/critical).
        config: AutonomyConfig instance, or None for defaults.
        repo: Optional repo full name for per-repo overrides.

    Returns:
        One of: escalate, auto_notify, auto_pr, auto_merge.
    """
    if config is None:
        config = AutonomyConfig()

    matrix = config.get_matrix(repo)
    clamped_confidence = max(0.0, min(1.0, confidence))

    for tier, min_conf, action in matrix:
        if risk == tier and clamped_confidence >= min_conf:
            return action

    # fallback: escalate
    return ACTION_ESCALATE


def _cap_action(action: str, maximum: str) -> str:
    if ACTION_RANK.get(action, 0) > ACTION_RANK.get(maximum, 0):
        return maximum
    return action


def apply_runtime_guards(
    action: str,
    *,
    health_status: str | None = None,
    quota_status: str | None = None,
    org_health_status: str | None = None,
) -> tuple[str, list[str]]:
    """Downgrade autonomy based on runtime health/quota state."""
    guarded_action = action
    guards: list[str] = []
    health = (health_status or "healthy").strip().lower()
    quota = (quota_status or "ok").strip().lower()

    if health == "unhealthy":
        guarded_action = ACTION_ESCALATE
        guards.append("service health is unhealthy; forcing human review")
    elif health == "degraded":
        capped = _cap_action(guarded_action, ACTION_AUTO_PR)
        if capped != guarded_action:
            guards.append("service health is degraded; auto-merge capped at auto-pr")
        guarded_action = capped

    if quota in {"exhausted", "blocked"}:
        guarded_action = ACTION_ESCALATE
        guards.append(f"quota status is {quota}; forcing human review")
    elif quota in {"low", "constrained"}:
        capped = _cap_action(guarded_action, ACTION_AUTO_PR)
        if capped != guarded_action:
            guards.append(f"quota status is {quota}; auto-merge capped at auto-pr")
        guarded_action = capped

    org_health = str(org_health_status or "").strip().lower()
    if org_health == "unhealthy":
        guarded_action = ACTION_ESCALATE
        guards.append("org health is unhealthy; forcing human review")
    elif org_health and org_health not in {"healthy", "ok"}:
        capped = _cap_action(guarded_action, ACTION_AUTO_PR)
        if capped != guarded_action:
            guards.append(f"org health is {org_health}; auto-merge capped at auto-pr")
        guarded_action = capped

    return guarded_action, guards


def extract_confidence(verdicts: list[dict[str, Any]]) -> float:
    """Extract an aggregated confidence score from a list of reviewer verdicts.

    Each verdict dict may contain a ``confidence`` field (0.0–1.0).
    Returns the weighted average, or 0.5 if no confidence data.
    """
    scores: list[float] = []
    for v in verdicts:
        try:
            c = float(v.get("confidence", 0.5))
            scores.append(max(0.0, min(1.0, c)))
        except (TypeError, ValueError):
            pass
    if not scores:
        return 0.5
    return sum(scores) / len(scores)


def recommended_action(
    verdicts: list[dict[str, Any]],
    changed_paths: list[str] | None = None,
    *,
    config: AutonomyConfig | None = None,
    repo: str | None = None,
    policy: dict[str, Any] | None = None,
    automation_metadata: dict[str, Any] | None = None,
    trusted_automation_metadata: dict[str, Any] | None = None,
    health_status: str | None = None,
    quota_status: str | None = None,
    org_health_status: str | None = None,
) -> dict[str, Any]:
    """Compute the recommended autonomous action from AI verdicts and file risks.

    Returns a dict with:
        action: The recommended action string.
        confidence: Aggregated confidence score.
        risk: Overall file risk tier.
        reason: Human-readable explanation.
    """
    confidence = extract_confidence(verdicts)
    active_policy = policy if policy is not None else load_autonomy_policy()
    risk = classify_changes_risk(changed_paths or [], policy=active_policy)
    initial_action = decide_action(confidence, risk, config=config, repo=repo)
    from service.automation_authority import evaluate_automation_authority

    authority = evaluate_automation_authority(
        changed_paths or [],
        metadata=automation_metadata or {},
        trusted_metadata=trusted_automation_metadata or {},
        proposed_action=initial_action,
    )
    action = str(authority["final_action"])
    action, runtime_guards = apply_runtime_guards(
        action,
        health_status=health_status,
        quota_status=quota_status,
        org_health_status=org_health_status,
    )
    authority["final_action"] = action

    reasons = {
        (ACTION_ESCALATE, RISK_CRITICAL): "Critical files changed — always escalates to human review",
        (ACTION_ESCALATE, RISK_HIGH): f"High-risk files with confidence {confidence:.0%} below auto-pr threshold — escalate",
        (ACTION_ESCALATE, RISK_MEDIUM): f"Medium-risk files with confidence {confidence:.0%} below auto-pr threshold — escalate",
        (ACTION_AUTO_PR, RISK_LOW): f"Low-risk files with moderate confidence {confidence:.0%} — create PR for human approval",
        (ACTION_AUTO_PR, RISK_MEDIUM): f"Medium-risk files with confidence {confidence:.0%} — create PR, do not auto-merge",
        (ACTION_AUTO_PR, RISK_HIGH): f"High-risk files with confidence {confidence:.0%} — create PR, never auto-merge by default",
        (ACTION_AUTO_MERGE, RISK_LOW): f"Low-risk files with high confidence {confidence:.0%} — safe to auto-merge",
        (ACTION_AUTO_NOTIFY, RISK_LOW): f"Low-risk files, confidence {confidence:.0%} — applied with notification",
    }

    reason = reasons.get((action, risk), f"Risk={risk}, confidence={confidence:.0%} → {action}")

    return {
        "action": action,
        "initial_action": initial_action,
        "confidence": confidence,
        "risk": risk,
        "reason": reason,
        "human_review_required": action in {ACTION_ESCALATE, ACTION_AUTO_PR} or bool(authority["human_review_required"]),
        "auto_merge_allowed": action == ACTION_AUTO_MERGE,
        "runtime_guards": runtime_guards,
        "automation_authority": authority,
        "policy_version": active_policy.get("version") if isinstance(active_policy, dict) else None,
    }
