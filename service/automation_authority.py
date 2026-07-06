"""Machine-readable automation authority policy for QuantStrategyLab."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from service.autonomy import (
    ACTION_AUTO_MERGE,
    ACTION_AUTO_NOTIFY,
    ACTION_AUTO_PR,
    ACTION_ESCALATE,
    ACTION_ORDER,
    ACTION_RANK,
)

POLICY_SCHEMA_VERSION = "qsl_automation_authority_policy.v1"
AUTOMATION_AUTHORITY_POLICY_PATH_ENV = "CODEX_AUDIT_SERVICE_AUTOMATION_AUTHORITY_POLICY_PATH"

CLASS_ROUTINE_LOW_RISK = "routine_low_risk"
CLASS_LIVE_EQUIVALENT_OPTIMIZATION = "live_equivalent_optimization"
CLASS_LIVE_CANDIDATE_PROMOTION = "live_candidate_promotion"
CLASS_NEW_OR_RECONSTRUCTED_STRATEGY = "new_or_reconstructed_strategy"
CLASS_PLUGIN_POSITION_CONTROL = "plugin_position_control"
CLASS_SECURITY_PERMISSION_BOUNDARY = "security_permission_or_secret"
CLASS_BROKER_OR_ORDER_EXECUTION = "broker_or_order_execution"
CLASS_UNKNOWN = "unknown_change"

AUTHORITY_AUTO = "auto_allowed"
AUTHORITY_REVIEW = "human_review_required"

LIVE_EQUIVALENT_REQUIRED_EVIDENCE = (
    "baseline_profile_runtime_enabled",
    "strategy_family_unchanged",
    "public_contract_unchanged",
    "broker_permission_unchanged",
    "risk_limits_not_increased",
    "backtest_passed",
    "shadow_or_regression_passed",
    "rollback_ready",
)

DEFAULT_AUTOMATION_AUTHORITY_POLICY: dict[str, Any] = {
    "schema_version": POLICY_SCHEMA_VERSION,
    "default_class": CLASS_UNKNOWN,
    "classes": {
        CLASS_ROUTINE_LOW_RISK: {
            "authority": AUTHORITY_AUTO,
            "max_action": ACTION_AUTO_MERGE,
            "description": "Docs, tests, generated assets, and low-risk config repairs.",
        },
        CLASS_LIVE_EQUIVALENT_OPTIMIZATION: {
            "authority": AUTHORITY_AUTO,
            "max_action": ACTION_AUTO_MERGE,
            "required_evidence": list(LIVE_EQUIVALENT_REQUIRED_EVIDENCE),
            "description": "Same-family optimization for an already live strategy with no wider risk or permission surface.",
        },
        CLASS_LIVE_CANDIDATE_PROMOTION: {
            "authority": AUTHORITY_REVIEW,
            "max_action": ACTION_AUTO_PR,
            "description": "Shadow/live-candidate strategy promotion requires operator review.",
        },
        CLASS_NEW_OR_RECONSTRUCTED_STRATEGY: {
            "authority": AUTHORITY_REVIEW,
            "max_action": ACTION_AUTO_PR,
            "description": "New or reconstructed strategy design requires operator review before live impact.",
        },
        CLASS_PLUGIN_POSITION_CONTROL: {
            "authority": AUTHORITY_REVIEW,
            "max_action": ACTION_AUTO_PR,
            "description": "Plugin changes that can affect position control require operator review.",
        },
        CLASS_SECURITY_PERMISSION_BOUNDARY: {
            "authority": AUTHORITY_REVIEW,
            "max_action": ACTION_ESCALATE,
            "description": "Security, permissions, workflows, secrets, and credentials are never auto-applied.",
        },
        CLASS_BROKER_OR_ORDER_EXECUTION: {
            "authority": AUTHORITY_REVIEW,
            "max_action": ACTION_ESCALATE,
            "description": "Broker/order execution permission changes are never auto-applied.",
        },
        CLASS_UNKNOWN: {
            "authority": AUTHORITY_REVIEW,
            "max_action": ACTION_AUTO_PR,
            "description": "Unknown changes may open a PR but require human review before live impact.",
        },
    },
    "path_overrides": [
        {"pattern": r"^\.github/", "class": CLASS_SECURITY_PERMISSION_BOUNDARY},
        {"pattern": r"(^|/)(.*secret.*|.*credential.*|.*token.*|.*\.pem|.*\.key)$", "class": CLASS_SECURITY_PERMISSION_BOUNDARY},
        {"pattern": r"(^|/)SECURITY\.md$", "class": CLASS_SECURITY_PERMISSION_BOUNDARY},
        {"pattern": r"(^|/)plugin_policies\.py$", "class": CLASS_PLUGIN_POSITION_CONTROL},
        {"pattern": r"(^|/)strategy-profiles\.example\.json$", "class": CLASS_LIVE_CANDIDATE_PROMOTION},
        {"pattern": r"(^|/)(broker[^/]*|orders?[^/]*|execution[^/]*|positions?[^/]*)(/|_|\.|$)", "class": CLASS_BROKER_OR_ORDER_EXECUTION},
    ],
}


def load_automation_authority_policy(path: Path | None = None) -> dict[str, Any]:
    """Load service-owned automation authority policy, falling back to defaults."""
    policy = deepcopy(DEFAULT_AUTOMATION_AUTHORITY_POLICY)
    if path is None:
        env_path = os.environ.get(AUTOMATION_AUTHORITY_POLICY_PATH_ENV, "").strip()
        if not env_path:
            return policy
        path = Path(env_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return policy
    if not isinstance(raw, dict) or raw.get("schema_version") != POLICY_SCHEMA_VERSION:
        return policy
    merged = deepcopy(policy)
    if "default_class" in raw:
        merged["default_class"] = raw["default_class"]
    if isinstance(raw.get("classes"), dict):
        for name, override in raw["classes"].items():
            if not isinstance(override, dict):
                continue
            base = dict(merged["classes"].get(str(name), {}))
            base.update(override)
            merged["classes"][str(name)] = base
    if isinstance(raw.get("path_overrides"), list):
        merged["path_overrides"] = list(policy["path_overrides"]) + [
            rule for rule in raw["path_overrides"] if isinstance(rule, dict)
        ]
    return merged


def _cap_action(action: str, maximum: str) -> str:
    if ACTION_RANK.get(action, 0) > ACTION_RANK.get(maximum, 0):
        return maximum
    return action


def _normalize_action(value: Any, default: str = ACTION_AUTO_PR) -> str:
    action = str(value or default).strip().lower()
    return action if action in ACTION_ORDER else default


def _normalize_class(value: Any, policy: dict[str, Any]) -> str:
    change_class = str(value or "").strip().lower()
    classes = policy.get("classes") if isinstance(policy.get("classes"), dict) else {}
    if change_class in classes:
        return change_class
    default_class = str(policy.get("default_class") or CLASS_UNKNOWN).strip().lower()
    return default_class if default_class in classes else CLASS_UNKNOWN


def _metadata_flag(metadata: dict[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "passed", "ok"}
    return False


def _class_authority(policy: dict[str, Any], change_class: str) -> str:
    class_policy = (policy.get("classes") or {}).get(change_class)
    if not isinstance(class_policy, dict):
        return AUTHORITY_REVIEW
    return str(class_policy.get("authority") or AUTHORITY_REVIEW).strip().lower()


def _class_max_action(policy: dict[str, Any], change_class: str) -> str:
    class_policy = (policy.get("classes") or {}).get(change_class)
    if not isinstance(class_policy, dict):
        return ACTION_AUTO_PR
    return _normalize_action(class_policy.get("max_action"), ACTION_AUTO_PR)


def _class_is_no_more_permissive(candidate: str, current: str, policy: dict[str, Any]) -> bool:
    candidate_review = _class_authority(policy, candidate) != AUTHORITY_AUTO
    current_review = _class_authority(policy, current) != AUTHORITY_AUTO
    if candidate_review != current_review:
        return candidate_review
    return ACTION_RANK.get(_class_max_action(policy, candidate), 0) <= ACTION_RANK.get(_class_max_action(policy, current), 0)


def _infer_class_from_paths(changed_paths: list[str], policy: dict[str, Any]) -> str:
    matched_classes: list[str] = []
    for path in changed_paths:
        for rule in policy.get("path_overrides", []):
            if not isinstance(rule, dict):
                continue
            pattern = str(rule.get("pattern") or "")
            target_class = str(rule.get("class") or "")
            if not pattern or not target_class:
                continue
            try:
                if re.search(pattern, path, flags=re.IGNORECASE):
                    matched_classes.append(_normalize_class(target_class, policy))
            except re.error:
                continue
    if matched_classes:
        selected = matched_classes[0]
        for candidate in matched_classes[1:]:
            if _class_is_no_more_permissive(candidate, selected, policy):
                selected = candidate
        return selected
    if changed_paths and all(path.startswith(("docs/", "tests/")) or path in {"README.md", "README.zh-CN.md"} for path in changed_paths):
        return CLASS_ROUTINE_LOW_RISK
    return _normalize_class("", policy)


def evaluate_automation_authority(
    changed_paths: list[str] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    trusted_metadata: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    proposed_action: str = ACTION_AUTO_PR,
) -> dict[str, Any]:
    """Evaluate maximum autonomy; only service-owned trusted_metadata can relax policy."""
    active_policy = policy if isinstance(policy, dict) else load_automation_authority_policy()
    clean_paths = [str(path) for path in (changed_paths or []) if isinstance(path, str)]
    clean_metadata = dict(metadata or {})
    clean_trusted_metadata = dict(trusted_metadata or {})
    path_class = _infer_class_from_paths(clean_paths, active_policy)
    change_class = path_class
    trusted_class = _normalize_class(
        clean_trusted_metadata.get("change_class") or clean_trusted_metadata.get("automation_change_type"),
        active_policy,
    )
    if trusted_class != CLASS_UNKNOWN and (
        _class_is_no_more_permissive(trusted_class, change_class, active_policy)
        or path_class == CLASS_UNKNOWN
        or _class_authority(active_policy, path_class) == AUTHORITY_AUTO
    ):
        change_class = trusted_class
    untrusted_class = _normalize_class(
        clean_metadata.get("change_class") or clean_metadata.get("automation_change_type"),
        active_policy,
    )
    if untrusted_class != CLASS_UNKNOWN and _class_is_no_more_permissive(untrusted_class, change_class, active_policy):
        change_class = untrusted_class

    class_policy = dict((active_policy.get("classes") or {}).get(change_class) or {})
    max_action = _normalize_action(class_policy.get("max_action"), ACTION_AUTO_PR)
    authority = str(class_policy.get("authority") or AUTHORITY_REVIEW).strip().lower()
    base_authority = authority
    reasons = [str(class_policy.get("description") or change_class)]
    missing_evidence: list[str] = []

    required_evidence = class_policy.get("required_evidence")
    if isinstance(required_evidence, list):
        for key in required_evidence:
            normalized_key = str(key)
            if not _metadata_flag(clean_trusted_metadata, normalized_key):
                missing_evidence.append(normalized_key)

    human_review_required = authority != AUTHORITY_AUTO
    if missing_evidence:
        human_review_required = True
        authority = AUTHORITY_REVIEW
        max_action = _cap_action(max_action, ACTION_AUTO_PR)
        reasons.append("missing required evidence: " + ", ".join(missing_evidence))

    final_action = _cap_action(_normalize_action(proposed_action), max_action)
    if human_review_required and final_action in {ACTION_AUTO_NOTIFY, ACTION_AUTO_MERGE}:
        final_action = ACTION_AUTO_PR if ACTION_RANK.get(max_action, 0) >= ACTION_RANK[ACTION_AUTO_PR] else ACTION_ESCALATE

    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "change_class": change_class,
        "authority": authority,
        "base_authority": base_authority,
        "max_action": max_action,
        "proposed_action": _normalize_action(proposed_action),
        "final_action": final_action,
        "human_review_required": human_review_required,
        "missing_evidence": missing_evidence,
        "reasons": reasons,
    }
