"""Closed-loop feedback — change registry and effect tracking.

Every autonomous change (auto_merge, auto_pr) is registered with its
pre-change metrics. Consumers submit post-change metrics via the feedback
API. The engine compares before/after to classify the effect.

Also consumes shadow audit results from ai_audit.py to detect AI vs
deterministic logic disagreements.

Data flow::

    Autonomous change
        │
        ├─▶ POST /v1/ai/feedback/register   (record before_metrics)
        │
        ▼  (N days later)
    Post-change metrics collected
        │
        ├─▶ POST /v1/ai/feedback/evaluate   (submit after_metrics)
        │
        ▼
    AiGateway computes effect:
        improved  → nothing to do
        neutral   → log for review
        degraded  → auto-create rollback issue

Shadow audit loop::

    ai_audit.py shadow_only
        │
        ├─▶ POST /v1/ai/feedback/shadow     (AI vs deterministic comparison)
        │
        ▼
    AiGateway tracks disagreements over time.
    N consecutive disagreements → auto-issue for deterministic logic review.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from service.task_state import change_requires_human_review, change_task_state

# ── effect constants ────────────────────────────────────────────────────

EFFECT_IMPROVED = "improved"
EFFECT_DEGRADED = "degraded"
EFFECT_NEUTRAL = "neutral"
EFFECT_PENDING = "pending"

# ── registry (file-based, same pattern as job store) ────────────────────


def _registry_dir() -> Path:
    default = Path(os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR", "")) / "changes"
    if not os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR"):
        import tempfile
        default = Path(tempfile.gettempdir()) / "codex-audit-service-jobs" / "changes"
    return default


def _change_path(change_id: str) -> Path:
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,96}", change_id):
        raise ValueError("change_id is invalid")
    return _registry_dir() / f"{change_id}.json"


_REGISTRY_LOCK = threading.Lock()


def _ensure_dir() -> None:
    d = _registry_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass


def _now() -> float:
    return time.time()


def _new_change_id() -> str:
    return secrets.token_urlsafe(24)


# ── data model ──────────────────────────────────────────────────────────


@dataclass
class ChangeRecord:
    change_id: str
    repo: str
    task: str
    action: str  # auto_merge | auto_pr | auto_notify | escalate
    confidence: float
    risk: str
    changed_paths: list[str] = field(default_factory=list)
    before_metrics: dict[str, float] = field(default_factory=dict)
    after_metrics: dict[str, float] | None = None
    effect: str = EFFECT_PENDING
    effect_detail: str = ""
    rollback_issue_required: bool = False
    rollback_intent: str = ""
    rollback_issue_url: str = ""
    external_url: str = ""
    issue_number: int | None = None
    pr_number: int | None = None
    created_at: float = field(default_factory=_now)
    evaluated_at: float | None = None
    source_repo: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "repo": self.repo,
            "task": self.task,
            "action": self.action,
            "confidence": self.confidence,
            "risk": self.risk,
            "changed_paths": self.changed_paths,
            "before_metrics": self.before_metrics,
            "after_metrics": self.after_metrics,
            "effect": self.effect,
            "effect_detail": self.effect_detail,
            "rollback_issue_required": self.rollback_issue_required,
            "rollback_intent": self.rollback_intent,
            "rollback_issue_url": self.rollback_issue_url,
            "external_url": self.external_url,
            "issue_number": self.issue_number,
            "pr_number": self.pr_number,
            "created_at": self.created_at,
            "evaluated_at": self.evaluated_at,
            "source_repo": self.source_repo,
            "state": change_task_state(self),
            "human_review_required": change_requires_human_review(self),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChangeRecord":
        return cls(
            change_id=str(d.get("change_id", "")),
            repo=str(d.get("repo", "")),
            task=str(d.get("task", "")),
            action=str(d.get("action", "")),
            confidence=float(d.get("confidence", 0.0)),
            risk=str(d.get("risk", "")),
            changed_paths=list(d.get("changed_paths", [])),
            before_metrics={k: float(v) for k, v in d.get("before_metrics", {}).items()},
            after_metrics={k: float(v) for k, v in d.get("after_metrics", {}).items()} if d.get("after_metrics") else None,
            effect=str(d.get("effect", EFFECT_PENDING)),
            effect_detail=str(d.get("effect_detail", "")),
            rollback_issue_required=bool(d.get("rollback_issue_required", False)),
            rollback_intent=str(d.get("rollback_intent", "")),
            rollback_issue_url=str(d.get("rollback_issue_url", "")),
            external_url=str(d.get("external_url", "")),
            issue_number=int(d["issue_number"]) if d.get("issue_number") is not None else None,
            pr_number=int(d["pr_number"]) if d.get("pr_number") is not None else None,
            created_at=float(d.get("created_at", _now())),
            evaluated_at=float(d.get("evaluated_at", 0)) if d.get("evaluated_at") else None,
            source_repo=str(d.get("source_repo", "")),
        )


def write_change(record: ChangeRecord) -> None:
    _ensure_dir()
    path = _change_path(record.change_id)
    payload = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    tmp = path.with_suffix(".json.tmp")
    with _REGISTRY_LOCK:
        with open(tmp, "wb") as h:
            h.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def read_change(change_id: str) -> ChangeRecord:
    path = _change_path(change_id)
    if not path.exists():
        raise FileNotFoundError(change_id)
    return ChangeRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_changes(repo: str = "", days: int = 30, limit: int = 50) -> list[ChangeRecord]:
    _ensure_dir()
    now = _now()
    cutoff = now - days * 86400
    records: list[ChangeRecord] = []
    for path in sorted(_registry_dir().glob("*.json"), reverse=True):
        try:
            r = ChangeRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
        if repo and r.repo != repo:
            continue
        if r.created_at < cutoff:
            continue
        records.append(r)
        if len(records) >= limit:
            break
    return records


# ── effect computation ──────────────────────────────────────────────────


def compute_effect(before: dict[str, float], after: dict[str, float]) -> tuple[str, str]:
    """Compare before/after metrics to classify effect.

    Primary metrics examined: sharpe, cagr, max_dd, calmar.
    Returns (effect, detail_reason).
    """
    if not before or not after:
        return EFFECT_PENDING, "missing metrics"

    # Key metrics — higher is better for sharpe/cagr, lower is better for max_dd
    improved_signals = 0
    degraded_signals = 0
    details: list[str] = []

    for metric, higher_better in [("sharpe", True), ("cagr", True), ("calmar", True)]:
        b = before.get(metric, 0)
        a = after.get(metric, 0)
        if b == 0 and a == 0:
            continue
        pct = (a - b) / abs(b) * 100 if b != 0 else 0
        if higher_better:
            if pct > 5:
                improved_signals += 1
                details.append(f"{metric} +{pct:.1f}%")
            elif pct < -5:
                degraded_signals += 1
                details.append(f"{metric} {pct:.1f}%")
        else:
            if pct < -5:
                improved_signals += 1
                details.append(f"{metric} {pct:.1f}%")
            elif pct > 5:
                degraded_signals += 1
                details.append(f"{metric} {pct:+.1f}%")

    # max_dd: lower is better
    b_dd = before.get("max_dd", 0)
    a_dd = after.get("max_dd", 0)
    if b_dd != 0 and a_dd != 0:
        dd_change = a_dd - b_dd  # positive = worse
        if dd_change > 0.02:  # max_dd worsened by >2%
            degraded_signals += 1
            details.append(f"max_dd worsened +{dd_change:.1%}")
        elif dd_change < -0.02:
            improved_signals += 1
            details.append(f"max_dd improved {dd_change:.1%}")

    if degraded_signals > improved_signals:
        return EFFECT_DEGRADED, "; ".join(details) if details else "metrics degraded"
    elif improved_signals > degraded_signals:
        return EFFECT_IMPROVED, "; ".join(details) if details else "metrics improved"
    else:
        return EFFECT_NEUTRAL, "no significant change"


def evaluate_change(change_id: str, after_metrics: dict[str, float]) -> ChangeRecord:
    """Submit post-change metrics and compute effect."""
    record = read_change(change_id)
    record.after_metrics = after_metrics
    effect, detail = compute_effect(record.before_metrics, after_metrics)
    record.effect = effect
    record.effect_detail = detail
    if effect == EFFECT_DEGRADED:
        record.rollback_issue_required = True
        record.rollback_intent = detail or "rollback review required"
    else:
        record.rollback_issue_required = False
        record.rollback_intent = ""
    record.evaluated_at = _now()
    write_change(record)
    return record


# ── shadow audit tracking ───────────────────────────────────────────────


@dataclass
class ShadowDisagreement:
    """Recorded when AI shadow audit disagrees with deterministic logic."""

    repo: str
    plugin: str  # "crisis_response" | "taco_rebound"
    ai_verdict: str
    ai_confidence: float
    deterministic_route: str
    disagreement_count: int  # consecutive disagreements
    recorded_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "plugin": self.plugin,
            "ai_verdict": self.ai_verdict, "ai_confidence": self.ai_confidence,
            "deterministic_route": self.deterministic_route,
            "disagreement_count": self.disagreement_count, "recorded_at": self.recorded_at,
        }


_SHADOW_LOCK = threading.RLock()
SHADOW_ESCALATE_THRESHOLD = 5  # consecutive disagreements before auto-issue


def _shadow_dir() -> Path:
    return _registry_dir() / "shadow"


def _shadow_key(repo: str, plugin: str) -> str:
    return hashlib.sha256(f"{repo}:{plugin}".encode("utf-8")).hexdigest()


def _shadow_path(repo: str, plugin: str) -> Path:
    return _shadow_dir() / f"{_shadow_key(repo, plugin)}.json"


def _load_shadow_disagreement(repo: str, plugin: str) -> ShadowDisagreement | None:
    path = _shadow_path(repo, plugin)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return ShadowDisagreement(
        repo=str(data.get("repo", repo)),
        plugin=str(data.get("plugin", plugin)),
        ai_verdict=str(data.get("ai_verdict", "")),
        ai_confidence=float(data.get("ai_confidence", 0.0)),
        deterministic_route=str(data.get("deterministic_route", "")),
        disagreement_count=int(data.get("disagreement_count", 0)),
        recorded_at=float(data.get("recorded_at", _now())),
    )


def _write_shadow_disagreement(entry: ShadowDisagreement) -> None:
    _ensure_dir()
    path = _shadow_path(entry.repo, entry.plugin)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    tmp = path.with_suffix(".json.tmp")
    with _SHADOW_LOCK:
        with open(tmp, "wb") as h:
            h.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def record_shadow_disagreement(
    repo: str, plugin: str, ai_verdict: str, ai_confidence: float,
    deterministic_route: str,
) -> dict[str, Any]:
    """Record a shadow audit result where AI disagreed with deterministic logic.

    Returns a dict with ``should_escalate`` flag when consecutive disagreements
    exceed the threshold.
    """
    with _SHADOW_LOCK:
        if ai_verdict in {"agree"}:
            # Reset — AI agrees with deterministic
            path = _shadow_path(repo, plugin)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return {"should_escalate": False, "disagreement_count": 0}

        prev = _load_shadow_disagreement(repo, plugin)
        count = (prev.disagreement_count + 1) if prev else 1
        entry = ShadowDisagreement(
            repo=repo, plugin=plugin,
            ai_verdict=ai_verdict, ai_confidence=ai_confidence,
            deterministic_route=deterministic_route, disagreement_count=count,
        )
        _write_shadow_disagreement(entry)

        return {
            "should_escalate": count >= SHADOW_ESCALATE_THRESHOLD,
            "disagreement_count": count,
            "threshold": SHADOW_ESCALATE_THRESHOLD,
            "verdict": ai_verdict,
            "deterministic_route": deterministic_route,
        }


def get_shadow_disagreements() -> list[dict[str, Any]]:
    shadow_dir = _shadow_dir()
    if not shadow_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    with _SHADOW_LOCK:
        for path in sorted(shadow_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(ShadowDisagreement(
                    repo=str(data.get("repo", "")),
                    plugin=str(data.get("plugin", "")),
                    ai_verdict=str(data.get("ai_verdict", "")),
                    ai_confidence=float(data.get("ai_confidence", 0.0)),
                    deterministic_route=str(data.get("deterministic_route", "")),
                    disagreement_count=int(data.get("disagreement_count", 0)),
                    recorded_at=float(data.get("recorded_at", _now())),
                ).to_dict())
            except Exception:
                continue
    records.sort(key=lambda item: float(item.get("recorded_at", 0.0)), reverse=True)
    return records


# ── effectiveness stats ─────────────────────────────────────────────────


def effectiveness_report(repo: str = "", days: int = 90) -> dict[str, Any]:
    """Aggregate stats on autonomous change effectiveness."""
    changes = list_changes(repo=repo, days=days, limit=500)
    evaluated = [c for c in changes if c.effect != EFFECT_PENDING]

    total = len(changes)
    total_evaluated = len(evaluated)
    improved = sum(1 for c in evaluated if c.effect == EFFECT_IMPROVED)
    degraded = sum(1 for c in evaluated if c.effect == EFFECT_DEGRADED)
    neutral = sum(1 for c in evaluated if c.effect == EFFECT_NEUTRAL)

    by_action: dict[str, dict[str, int]] = {}
    by_risk: dict[str, dict[str, int]] = {}
    by_state: dict[str, int] = {}
    for c in changes:
        state = change_task_state(c)
        by_state[state] = by_state.get(state, 0) + 1
    for c in evaluated:
        if c.action not in by_action:
            by_action[c.action] = {"improved": 0, "degraded": 0, "neutral": 0, "total": 0}
        by_action[c.action][c.effect] += 1
        by_action[c.action]["total"] += 1
        if c.risk not in by_risk:
            by_risk[c.risk] = {"improved": 0, "degraded": 0, "neutral": 0, "total": 0}
        by_risk[c.risk][c.effect] += 1
        by_risk[c.risk]["total"] += 1

    auto_actions = {"auto_merge", "auto_pr", "auto_notify"}
    auto_total = sum(1 for c in changes if c.action in auto_actions)
    human_review_required = sum(1 for c in changes if change_requires_human_review(c))
    rollback_required = sum(1 for c in changes if c.rollback_issue_required or c.rollback_issue_url)

    return {
        "period_days": days,
        "total_changes": total,
        "evaluated": total_evaluated,
        "pending": total - total_evaluated,
        "improved": improved,
        "degraded": degraded,
        "neutral": neutral,
        "improvement_rate": improved / total_evaluated if total_evaluated > 0 else 0,
        "auto_actions": auto_total,
        "human_review_required": human_review_required,
        "rollback_required": rollback_required,
        "by_action": by_action,
        "by_risk": by_risk,
        "by_state": by_state,
    }
