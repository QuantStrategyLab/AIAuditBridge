"""Pure dependency/engineering-review notification triage.

Maps structured evidence onto existing BriefingFinding / BriefingAction lanes.
This batch is fail-closed and side-effect free: no GitHub API, Telegram, or model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from service.briefing_consumer import BriefingAction, BriefingConsumptionResult, BriefingFinding
from service.briefing_dispatch import dispatch_briefing_result

DEPENDABOT_LOGINS = frozenset({"dependabot[bot]", "app/dependabot"})

TRUSTED_INTAKE_SCHEMA_VERSION = 1
MAX_TRUSTED_INTAKE_EVENTS = 50

_REQUIRED_EVENT_FIELDS = (
    "repository",
    "pr_number",
    "author_login",
    "update_class",
    "changed_files",
    "base_sha",
    "head_sha",
    "ci_status",
    "dependency_only_manifest_change",
)

_OPTIONAL_EVENT_FIELDS = frozenset(
    {
        "dependency_names",
        "qpk_pin_changed",
        "notification_kind",
        "event_type",
        "security_advisory_id",
        "security_advisory_ids",
    }
)

_EVIDENCE_PASSTHROUGH_FIELDS = (
    *_REQUIRED_EVENT_FIELDS,
    "dependency_names",
    "qpk_pin_changed",
    "notification_kind",
    "security_advisory_id",
    "security_advisory_ids",
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_MANIFEST_LOCK_EXACT = frozenset(
    {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "Cargo.toml",
        "go.mod",
        "go.sum",
        "Gemfile",
        "Gemfile.lock",
        "composer.json",
        "composer.lock",
        "pyproject.toml",
    }
)

_HIGH_RISK_PATH_MARKERS = (
    ".github/workflows/",
    ".github/actions/",
    "/action.yml",
    "/action.yaml",
    "dockerfile",
    "docker-compose",
    "ibkr",
    "ib-gateway",
    "gateway",
    "/broker",
    "broker/",
    "/execution",
    "execution/",
    "/auth/",
    "credential",
    "secret",
    "/deploy",
    "deploy/",
    "/risk/",
    "risk_",
    "/strategy",
    "strategies/",
    "qpk",
    "quantplatformkit",
    "qsl.toml",
    "security",
    "runtime",
)

_UPDATE_CLASSES_LOW = frozenset({"patch", "minor"})
_UPDATE_CLASSES_HIGH = frozenset({"major", "security"})


@dataclass(frozen=True)
class DependencyTriageResult:
    disposition: str
    action: BriefingAction
    confidence: str
    review_required: bool
    reasons: tuple[str, ...] = ()
    finding: BriefingFinding | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "action": self.action.value,
            "confidence": self.confidence,
            "review_required": self.review_required,
            "reasons": list(self.reasons),
            "finding": None if self.finding is None else self.finding.to_dict(),
            "metrics": dict(self.metrics),
        }


def _basename(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip()
    return normalized.rsplit("/", 1)[-1]


def _normalize_path(path: str) -> str:
    # Strip only "./" prefixes; do not use lstrip("./") (that also strips ".github").
    normalized = str(path or "").replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lower()


def is_manifest_or_lock(path: str) -> bool:
    name = _basename(path)
    if name in _MANIFEST_LOCK_EXACT:
        return True
    if name.startswith("requirements") and name.endswith(".txt"):
        return True
    return False


# Keep private alias for in-module call sites.
_is_manifest_or_lock = is_manifest_or_lock


def _path_high_risk_reasons(path: str) -> list[str]:
    normalized = _normalize_path(path)
    reasons: list[str] = []
    if ".github/workflows/" in normalized or normalized.startswith(".github/workflows/"):
        reasons.append("workflow/action surface")
    if (
        ".github/actions/" in normalized
        or normalized.endswith("action.yml")
        or normalized.endswith("action.yaml")
    ):
        reasons.append("workflow/action surface")
    if "dockerfile" in normalized or "docker-compose" in normalized:
        reasons.append("docker/broker surface")
    if any(token in normalized for token in ("ibkr", "ib-gateway", "/broker", "broker/")):
        reasons.append("docker/broker surface")
    if any(token in normalized for token in ("/execution", "execution/")):
        reasons.append("execution surface")
    if any(token in normalized for token in ("/auth/", "credential", "secret", ".pem", ".key")):
        reasons.append("auth/security surface")
    if any(token in normalized for token in ("/deploy", "deploy/")):
        reasons.append("runtime/deploy surface")
    if "runtime" in normalized and "requirements" not in normalized:
        reasons.append("runtime/deploy surface")
    if any(token in normalized for token in ("/risk/", "risk_engine", "risk-")):
        reasons.append("risk surface")
    if any(token in normalized for token in ("/strategy", "strategies/")):
        reasons.append("strategy surface")
    if any(token in normalized for token in ("qpk", "quantplatformkit", "qsl.toml")):
        reasons.append("QPK pin/surface")
    if "security" in normalized:
        reasons.append("security surface")
    return reasons


def _looks_like_source_code(path: str) -> bool:
    normalized = _normalize_path(path)
    if _is_manifest_or_lock(path):
        return False
    if any(marker in normalized for marker in _HIGH_RISK_PATH_MARKERS):
        return False
    source_suffixes = (
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".cs",
        ".rb",
        ".php",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
    )
    return normalized.endswith(source_suffixes) or "/src/" in f"/{normalized}"


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def _as_file_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return None
    return [str(item).strip() for item in value if str(item).strip()]


def _build_finding(
    *,
    level: BriefingAction,
    reason: str,
    repository: str,
    severity: str,
    metrics: Mapping[str, Any],
    signals: list[dict[str, Any]],
) -> BriefingFinding:
    return BriefingFinding(
        source="dependency_notification_triage",
        level=level,
        reason=reason,
        strategy_profile="",
        domain=repository,
        kind="dependency_notification",
        severity=severity,
        metrics=dict(metrics),
        signals=signals,
    )


def triage_dependency_notification(evidence: Mapping[str, Any] | None) -> DependencyTriageResult:
    """Classify a dependency/engineering review notice from structured evidence only."""
    if not isinstance(evidence, Mapping):
        return _gate_uncertain("evidence mapping missing", confidence="unknown")

    repository = str(evidence.get("repository") or "").strip()
    author = str(evidence.get("author_login") or "").strip()
    update_class = str(evidence.get("update_class") or "").strip().lower()
    ci_status = str(evidence.get("ci_status") or "").strip().lower()
    files = _as_file_list(evidence.get("changed_files"))
    base_sha = evidence.get("base_sha")
    head_sha = evidence.get("head_sha")
    dependency_only = evidence.get("dependency_only_manifest_change")
    qpk_pin_changed = evidence.get("qpk_pin_changed") is True
    dependency_names = evidence.get("dependency_names")
    title_present = bool(str(evidence.get("title") or "").strip())

    metrics: dict[str, Any] = {
        "repository": repository,
        "author_login": author,
        "update_class": update_class or "unknown",
        "ci_status": ci_status or "unknown",
        "changed_files": list(files or []),
        "dependency_names": list(dependency_names)
        if isinstance(dependency_names, (list, tuple))
        else [],
    }
    reasons: list[str] = []
    signals: list[dict[str, Any]] = []

    required_for_quiet = (
        bool(repository),
        bool(author),
        bool(update_class),
        files is not None and len(files) > 0,
        _valid_sha(base_sha),
        _valid_sha(head_sha),
        bool(ci_status),
        dependency_only is True,
    )
    if not all(required_for_quiet):
        if title_present and not all(
            (
                bool(repository),
                bool(author),
                bool(update_class),
                files is not None and len(files) > 0,
                _valid_sha(base_sha),
                _valid_sha(head_sha),
                bool(ci_status),
            )
        ):
            reasons.append("title alone is insufficient; structured evidence missing")
        if not repository:
            reasons.append("repository missing")
        if not author:
            reasons.append("author_login missing")
        if not update_class:
            reasons.append("update_class unknown")
        if files is None:
            reasons.append("changed_files unknown")
        elif not files:
            reasons.append("changed_files empty")
        if not _valid_sha(base_sha) or not _valid_sha(head_sha):
            reasons.append("exact base/head sha missing or invalid")
        if not ci_status:
            reasons.append("ci_status unknown")
        if dependency_only is not True:
            reasons.append("dependency-only manifest evidence missing")

    high_path_reasons: list[str] = []
    unknown_files: list[str] = []
    source_files: list[str] = []
    for path in files or ():
        path_reasons = _path_high_risk_reasons(path)
        if path_reasons:
            high_path_reasons.extend(path_reasons)
            continue
        if _is_manifest_or_lock(path):
            continue
        if _looks_like_source_code(path):
            source_files.append(path)
            continue
        unknown_files.append(path)

    if qpk_pin_changed:
        high_path_reasons.append("QPK pin changed")

    if high_path_reasons:
        reasons.extend(sorted(set(high_path_reasons)))
        signals.append({"metric": "file_surface", "reason": "high_risk_path"})
        return _result(
            disposition="telegram",
            action=BriefingAction.TELEGRAM,
            confidence="known",
            review_required=False,
            reasons=reasons,
            repository=repository,
            severity="high",
            metrics=metrics,
            signals=signals,
        )

    if update_class in _UPDATE_CLASSES_HIGH:
        reasons.append(f"{update_class} update requires admin alert")
        signals.append({"metric": "update_class", "reason": update_class})
        return _result(
            disposition="telegram",
            action=BriefingAction.TELEGRAM,
            confidence="known",
            review_required=False,
            reasons=reasons,
            repository=repository,
            severity="high",
            metrics=metrics,
            signals=signals,
        )

    if ci_status == "failure":
        reasons.append("ci failure requires admin alert")
        signals.append({"metric": "ci_status", "reason": "failure"})
        return _result(
            disposition="telegram",
            action=BriefingAction.TELEGRAM,
            confidence="known",
            review_required=False,
            reasons=reasons,
            repository=repository,
            severity="high",
            metrics=metrics,
            signals=signals,
        )

    if unknown_files:
        reasons.append(f"unknown file surface: {', '.join(unknown_files)}")
        signals.append({"metric": "file_surface", "reason": "unknown"})
        return _result(
            disposition="telegram",
            action=BriefingAction.TELEGRAM,
            confidence="unknown",
            review_required=True,
            reasons=reasons,
            repository=repository,
            severity="high",
            metrics=metrics,
            signals=signals,
        )

    if author and author not in DEPENDABOT_LOGINS:
        reasons.append("author is not Dependabot; escalate to review")
        signals.append({"metric": "author_login", "reason": "non_dependabot"})
        return _result(
            disposition="github_issue",
            action=BriefingAction.GITHUB_ISSUE,
            confidence="known",
            review_required=True,
            reasons=reasons,
            repository=repository,
            severity="medium",
            metrics=metrics,
            signals=signals,
        )

    if source_files:
        reasons.append(f"general source-code review required: {', '.join(source_files)}")
        signals.append({"metric": "file_surface", "reason": "source"})
        return _result(
            disposition="github_issue",
            action=BriefingAction.GITHUB_ISSUE,
            confidence="known",
            review_required=True,
            reasons=reasons,
            repository=repository,
            severity="medium",
            metrics=metrics,
            signals=signals,
        )

    if reasons:
        # Gate evidence gaps that block quiet release must not sink into ordinary
        # GitHub-issue review noise on an unattended engineering chain.
        confidence = (
            "review_required"
            if ("unknown" in " ".join(reasons).lower() or "missing" in " ".join(reasons).lower())
            else "unknown"
        )
        return _gate_uncertain(
            reasons,
            repository=repository,
            confidence=confidence,
            metrics=metrics,
            signals=signals or [{"metric": "evidence", "reason": "incomplete"}],
        )

    if author not in DEPENDABOT_LOGINS:
        return _gate_uncertain(
            "Dependabot identity required for quiet lane",
            repository=repository,
            metrics=metrics,
        )

    if update_class not in _UPDATE_CLASSES_LOW:
        return _gate_uncertain(
            f"update_class={update_class or 'unknown'} not quiet-eligible",
            repository=repository,
            metrics=metrics,
        )

    if ci_status != "success":
        return _gate_uncertain(
            f"ci_status={ci_status or 'unknown'} not quiet-eligible",
            repository=repository,
            metrics=metrics,
        )

    if dependency_only is not True:
        return _gate_uncertain(
            "dependency-only evidence required",
            repository=repository,
            metrics=metrics,
        )

    if not files or any(not _is_manifest_or_lock(path) for path in files):
        return _gate_uncertain(
            "manifest/lockfile-only surface required",
            repository=repository,
            metrics=metrics,
        )

    if not _valid_sha(base_sha) or not _valid_sha(head_sha):
        return _gate_uncertain(
            "exact base/head required",
            repository=repository,
            metrics=metrics,
        )

    quiet_reason = (
        "low-risk Dependabot patch/minor manifest/lockfile update with successful CI"
    )
    reasons = [quiet_reason]
    signals = [{"metric": "lane", "reason": "ai_absorb_or_quiet"}]
    finding = _build_finding(
        level=BriefingAction.QUIET,
        reason=quiet_reason,
        repository=repository,
        severity="low",
        metrics=metrics,
        signals=signals,
    )
    return DependencyTriageResult(
        disposition="quiet",
        action=BriefingAction.QUIET,
        confidence="known",
        review_required=False,
        reasons=tuple(reasons),
        finding=finding,
        metrics=metrics,
    )


def triage_schwab_dependency_audit_decision(
    decision: Mapping[str, Any] | None,
) -> DependencyTriageResult:
    """Map Schwab dependency-audit decisions onto BriefingAction without side effects."""
    if not isinstance(decision, Mapping):
        return _fail_closed("schwab audit decision missing", confidence="unknown")

    repository = str(
        decision.get("repository") or "QuantStrategyLab/SchwabTokenAutoRefresher"
    ).strip()
    verdict = str(decision.get("decision") or "").strip().lower()
    summary = str(decision.get("summary") or "").strip() or "schwab dependency audit"
    reason = str(decision.get("reason") or "").strip() or verdict or "unspecified"
    metrics = {
        "repository": repository,
        "pr": decision.get("pr"),
        "decision": verdict,
    }
    if verdict == "approve":
        text = f"schwab dependency audit approve: {summary}; {reason}"
        return DependencyTriageResult(
            disposition="quiet",
            action=BriefingAction.QUIET,
            confidence="known",
            review_required=False,
            reasons=(text,),
            finding=_build_finding(
                level=BriefingAction.QUIET,
                reason=text,
                repository=repository,
                severity="low",
                metrics=metrics,
                signals=[{"metric": "schwab_lane", "reason": "approve"}],
            ),
            metrics=metrics,
        )
    if verdict in {"defer", "human_required"}:
        text = f"schwab dependency audit {verdict}: {summary}; {reason}"
        return DependencyTriageResult(
            disposition="github_issue",
            action=BriefingAction.GITHUB_ISSUE,
            confidence="known",
            review_required=True,
            reasons=(text,),
            finding=_build_finding(
                level=BriefingAction.GITHUB_ISSUE,
                reason=text,
                repository=repository,
                severity="medium",
                metrics=metrics,
                signals=[{"metric": "schwab_lane", "reason": verdict}],
            ),
            metrics=metrics,
        )
    return _fail_closed(
        f"schwab audit decision unknown: {verdict or 'missing'}",
        repository=repository,
        metrics=metrics,
    )


def triage_to_briefing_result(
    result: DependencyTriageResult,
    *,
    day: str = "",
    report_dir: str = "dependency-notification-triage",
) -> BriefingConsumptionResult:
    """Adapt a triage result for dispatch_briefing_result without performing I/O."""
    findings = [result.finding] if result.finding is not None else []
    return BriefingConsumptionResult(
        day=day or "dependency",
        report_dir=report_dir,
        findings=findings,
    )


def _intake_fail_closed(
    reasons: list[str],
    *,
    error: str,
    confidence: str = "unknown",
) -> dict[str, Any]:
    return {
        "ok": False,
        "action": BriefingAction.TELEGRAM.value,
        "review_required": True,
        "confidence": confidence,
        "error": error,
        "reasons": list(reasons),
        "counts": {
            "events": 0,
            "quiet": 0,
            "telegram": 0,
            "github_issue": 0,
            "deduped": 0,
        },
        "events": [],
        "dispatch": {
            "action": BriefingAction.TELEGRAM.value,
            "telegram_sent": False,
            "github_issue": None,
            "telegram_dry_run": {
                "present": True,
                "safe_summary": "intake_validation_failed",
            },
            "github_dry_run": {"present": False},
            "errors": [],
            "skipped": [],
        },
    }


def _validate_trusted_event(event: Any, *, index: int) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(event, Mapping):
        return None, f"event[{index}] type invalid"
    missing = [name for name in _REQUIRED_EVENT_FIELDS if name not in event]
    if missing:
        return None, f"event[{index}] required fields missing: {', '.join(missing)}"

    repository = str(event.get("repository") or "").strip()
    if not repository:
        return None, f"event[{index}] repository missing"

    pr_number = event.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return None, f"event[{index}] pr_number invalid"

    author_login = str(event.get("author_login") or "").strip()
    if not author_login:
        return None, f"event[{index}] author_login missing"

    update_class = str(event.get("update_class") or "").strip()
    if not update_class:
        return None, f"event[{index}] update_class missing"

    changed_files = event.get("changed_files")
    if not isinstance(changed_files, list) or not changed_files:
        return None, f"event[{index}] changed_files missing"
    if not all(isinstance(item, str) and item.strip() for item in changed_files):
        return None, f"event[{index}] changed_files invalid"

    base_sha = event.get("base_sha")
    head_sha = event.get("head_sha")
    if not _valid_sha(base_sha) or not _valid_sha(head_sha):
        return None, f"event[{index}] base/head sha missing or invalid"

    ci_status = str(event.get("ci_status") or "").strip()
    if not ci_status:
        return None, f"event[{index}] ci_status missing"

    dependency_only = event.get("dependency_only_manifest_change")
    if not isinstance(dependency_only, bool):
        return None, f"event[{index}] dependency_only_manifest_change invalid"

    if "dependency_names" in event:
        names = event.get("dependency_names")
        if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
            return None, f"event[{index}] dependency_names invalid"

    if "qpk_pin_changed" in event and not isinstance(event.get("qpk_pin_changed"), bool):
        return None, f"event[{index}] qpk_pin_changed invalid"

    if "event_type" in event:
        event_type = str(event.get("event_type") or "").strip()
        if event_type and event_type not in {"dependabot_pr", "engineering_review"}:
            return None, f"event[{index}] event type invalid"

    normalized: dict[str, Any] = {
        "repository": repository,
        "pr_number": pr_number,
        "author_login": author_login,
        "update_class": update_class.lower(),
        "changed_files": [str(item).strip() for item in changed_files],
        "base_sha": str(base_sha).strip().lower(),
        "head_sha": str(head_sha).strip().lower(),
        "ci_status": ci_status.lower(),
        "dependency_only_manifest_change": dependency_only,
    }
    for key in _OPTIONAL_EVENT_FIELDS:
        if key in event and key not in normalized:
            normalized[key] = event[key]
    return normalized, None


def _evidence_from_trusted_event(event: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key in _EVIDENCE_PASSTHROUGH_FIELDS:
        if key in event:
            evidence[key] = event[key]
    # Never pass title/body: trusted intake must not use free text as a quiet gate.
    return evidence


def _redact_dispatch_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    known_errors = {
        "optimization_record_failed",
        "github_issue_record_failed",
        "telegram_delivery_failed",
        "telegram_missing_env",
    }
    safe_errors: list[str] = []
    for item in summary.get("errors") or ():
        text = str(item or "").strip()
        if text in known_errors:
            safe_errors.append(text)
        elif text:
            safe_errors.append("dispatch_error")

    redacted: dict[str, Any] = {
        "action": str(summary.get("action") or BriefingAction.QUIET.value),
        "telegram_sent": False,
        "github_issue": None,
        "errors": safe_errors,
        "skipped": [str(item) for item in (summary.get("skipped") or ())],
        "telegram_dry_run": {"present": False},
        "github_dry_run": {"present": False},
    }
    if "telegram_dry_run" in summary:
        redacted["telegram_dry_run"] = {
            "present": True,
            "safe_summary": "telegram_preview_available",
        }
    if "github_dry_run" in summary:
        redacted["github_dry_run"] = {
            "present": True,
            "safe_summary": "github_issue_preview_available",
        }
    return redacted


def run_trusted_intake_dry_run(
    payload: Mapping[str, Any] | None,
    *,
    parse_error: str | None = None,
) -> dict[str, Any]:
    """Validate trusted structured intake and preview triage+dispatch with dry_run=True.

    Reads no GitHub notifications, calls no model, and never sends Telegram/issues.
    """
    if parse_error:
        return _intake_fail_closed(
            [f"input {parse_error}"],
            error=parse_error,
        )
    if not isinstance(payload, Mapping):
        return _intake_fail_closed(
            ["top-level schema invalid"],
            error="schema_invalid",
        )
    if payload.get("schema_version") != TRUSTED_INTAKE_SCHEMA_VERSION:
        return _intake_fail_closed(
            [f"schema_version must be {TRUSTED_INTAKE_SCHEMA_VERSION}"],
            error="schema_version",
        )
    events = payload.get("events")
    if not isinstance(events, list):
        return _intake_fail_closed(
            ["events must be a list"],
            error="events_type",
        )
    if len(events) > MAX_TRUSTED_INTAKE_EVENTS:
        return _intake_fail_closed(
            [f"events exceed limit of {MAX_TRUSTED_INTAKE_EVENTS}"],
            error="events_limit",
        )

    normalized_events: list[dict[str, Any]] = []
    for index, raw_event in enumerate(events):
        normalized, error = _validate_trusted_event(raw_event, index=index)
        if error is not None:
            return _intake_fail_closed([error], error="required_fields")
        assert normalized is not None
        normalized_events.append(normalized)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    duplicate_count = 0
    for event in normalized_events:
        key = (event["repository"], int(event["pr_number"]), str(event["head_sha"]))
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        deduped.append(event)

    event_rows: list[dict[str, Any]] = []
    findings: list[BriefingFinding] = []
    counts = {
        "events": len(deduped),
        "quiet": 0,
        "telegram": 0,
        "github_issue": 0,
        "deduped": duplicate_count,
    }

    for event in deduped:
        triage_result = triage_dependency_notification(_evidence_from_trusted_event(event))
        decision = triage_result.action.value
        counts[decision] = int(counts.get(decision, 0)) + 1
        event_rows.append(
            {
                "repository": event["repository"],
                "pr_number": event["pr_number"],
                "decision": decision,
                "confidence": triage_result.confidence,
                "review_required": triage_result.review_required,
                "reasons": list(triage_result.reasons),
            }
        )
        adapted = triage_to_briefing_result(triage_result)
        findings.extend(adapted.findings)

    briefing = BriefingConsumptionResult(
        day="dependency-notification-dry-run",
        report_dir="dependency-notification-triage",
        findings=findings,
    )
    dispatch = dispatch_briefing_result(briefing, dry_run=True)
    review_required = any(bool(row["review_required"]) for row in event_rows)
    confidence = "known"
    if review_required:
        confidence = "review_required"
    elif any(row["confidence"] != "known" for row in event_rows):
        confidence = "unknown"
    return {
        "ok": True,
        "action": briefing.action.value,
        "review_required": review_required,
        "confidence": confidence,
        "counts": counts,
        "events": event_rows,
        "dispatch": _redact_dispatch_summary(dispatch),
    }


def _result(
    *,
    disposition: str,
    action: BriefingAction,
    confidence: str,
    review_required: bool,
    reasons: list[str],
    repository: str,
    severity: str,
    metrics: dict[str, Any],
    signals: list[dict[str, Any]],
) -> DependencyTriageResult:
    reason_text = "; ".join(reasons) if reasons else disposition
    finding = _build_finding(
        level=action,
        reason=reason_text,
        repository=repository,
        severity=severity,
        metrics=metrics,
        signals=signals,
    )
    return DependencyTriageResult(
        disposition=disposition,
        action=action,
        confidence=confidence,
        review_required=review_required,
        reasons=tuple(reasons),
        finding=finding,
        metrics=metrics,
    )


def _gate_uncertain(
    reason: str | list[str],
    *,
    repository: str = "",
    confidence: str = "unknown",
    metrics: dict[str, Any] | None = None,
    signals: list[dict[str, Any]] | None = None,
) -> DependencyTriageResult:
    """Escalate quiet-gate uncertainty to Telegram so it cannot be buried as a normal issue."""
    reasons = [reason] if isinstance(reason, str) else list(reason)
    return _result(
        disposition="telegram",
        action=BriefingAction.TELEGRAM,
        confidence=confidence,
        review_required=True,
        reasons=reasons,
        repository=repository,
        severity="high",
        metrics=metrics or {},
        signals=signals or [{"metric": "evidence", "reason": "gate_uncertain"}],
    )


def _fail_closed(
    reason: str,
    *,
    repository: str = "",
    confidence: str = "unknown",
    metrics: dict[str, Any] | None = None,
) -> DependencyTriageResult:
    return _result(
        disposition="github_issue",
        action=BriefingAction.GITHUB_ISSUE,
        confidence=confidence,
        review_required=True,
        reasons=[reason],
        repository=repository,
        severity="medium",
        metrics=metrics or {},
        signals=[{"metric": "evidence", "reason": "fail_closed"}],
    )
