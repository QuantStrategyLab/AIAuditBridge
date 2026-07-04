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


def classify_file_risk(path: str) -> str:
    """Classify a changed file path into a risk tier.

    Mirrors the logic in codex_auto_merge_policy.json risk_policy.
    """
    import re as _re

    # critical: secrets, credentials, keys
    for pattern in CRITICAL_PATTERNS:
        if _re.search(pattern, path):
            return RISK_CRITICAL

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


def classify_changes_risk(changed_paths: list[str]) -> str:
    """Classify the overall risk of a set of changed file paths.

    Returns the highest risk tier among all changed files.
    """
    if not changed_paths:
        return RISK_LOW
    tiers = {classify_file_risk(p) for p in changed_paths}
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
) -> dict[str, Any]:
    """Compute the recommended autonomous action from AI verdicts and file risks.

    Returns a dict with:
        action: The recommended action string.
        confidence: Aggregated confidence score.
        risk: Overall file risk tier.
        reason: Human-readable explanation.
    """
    confidence = extract_confidence(verdicts)
    risk = classify_changes_risk(changed_paths or [])
    action = decide_action(confidence, risk, config=config, repo=repo)

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
        "confidence": confidence,
        "risk": risk,
        "reason": reason,
    }
