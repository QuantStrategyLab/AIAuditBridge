"""Read-only adapter: controlled GitHub PR metadata → trusted intake schema v1.

Fetches only via GitHub REST GET against an explicit repository allowlist.
Never sends notifications, never writes GitHub resources, never calls models.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from scripts.run_dependency_audit import (
    DependencyAuditError,
    github_list_all,
)
from scripts.run_monthly_codex_audit import (
    GitHubRequestError,
    github_request,
)
from service.dependency_notification_triage import (
    DEPENDABOT_LOGINS,
    MAX_TRUSTED_INTAKE_EVENTS,
    TRUSTED_INTAKE_SCHEMA_VERSION,
    is_manifest_or_lock,
    run_trusted_intake_dry_run,
)

# Re-export for tests / CLI without importing Schwab write paths.
__all__ = [
    "DependencyNotificationSourceError",
    "DependencyAuditError",
    "GitHubRequestError",
    "MAX_ALLOWLIST_REPOS",
    "MAX_CHECK_RUN_PAGES",
    "MAX_PRS_PER_REPO",
    "CHECK_RUNS_PER_PAGE",
    "assert_repo_allowed",
    "build_trusted_event",
    "build_trusted_intake_payload",
    "collect_or_fail_closed",
    "collect_trusted_events",
    "github_list_all",
    "github_request",
    "parse_repo_allowlist",
    "resolve_ci_status",
    "run_source_dry_run",
]

MAX_ALLOWLIST_REPOS = 10
MAX_PRS_PER_REPO = 20
MAX_CHECK_RUN_PAGES = 3
CHECK_RUNS_PER_PAGE = 100
_PLACEHOLDER_SHA = "0" * 40
_MISSING_FILES_SENTINEL = "__missing_changed_files__"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_UPDATE_RE = re.compile(
    r"version-update:\s*semver-(patch|minor|major)\b",
    re.IGNORECASE,
)
_QPK_PATH_RE = re.compile(r"(qpk|quantplatformkit|qsl\.toml)", re.IGNORECASE)


class DependencyNotificationSourceError(ValueError):
    """Deterministic gate refused unsafe or incomplete source conversion."""


def parse_repo_allowlist(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    if raw is None:
        raise DependencyNotificationSourceError("repository allowlist is required")
    if isinstance(raw, str):
        parts = raw.replace("\n", ",").split(",")
    else:
        parts = []
        for item in raw:
            parts.extend(str(item).replace("\n", ",").split(","))
    repos: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = str(part or "").strip()
        if not name:
            continue
        if not _REPO_RE.fullmatch(name):
            raise DependencyNotificationSourceError(f"invalid repository allowlist entry: {name}")
        if name in seen:
            continue
        seen.add(name)
        repos.append(name)
    if not repos:
        raise DependencyNotificationSourceError("repository allowlist is required")
    if len(repos) > MAX_ALLOWLIST_REPOS:
        raise DependencyNotificationSourceError(
            f"repository allowlist exceeds limit of {MAX_ALLOWLIST_REPOS}"
        )
    return tuple(repos)


def assert_repo_allowed(repository: str, allowlist: Sequence[str]) -> None:
    name = str(repository or "").strip()
    if name not in set(allowlist):
        raise DependencyNotificationSourceError(
            f"repository not in allowlist: {name or 'missing'}"
        )


def _label_names(pr: Mapping[str, Any]) -> list[str]:
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for item in labels:
        if isinstance(item, Mapping):
            text = str(item.get("name") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            names.append(text)
    return names


def derive_update_class(pr: Mapping[str, Any]) -> str:
    """Populate update_class from Dependabot machine markers / security labels only.

    Free-text title/body prose is never enough for a quiet lane; missing markers
    yield ``unknown`` so the existing triage fail-closes to Telegram.
    """
    labels = [name.lower() for name in _label_names(pr)]
    if any("security" in name for name in labels):
        return "security"
    body = pr.get("body")
    if isinstance(body, str) and body.strip():
        match = _VERSION_UPDATE_RE.search(body)
        if match:
            return match.group(1).lower()
    return "unknown"


def _changed_files(files: Sequence[Mapping[str, Any]] | None) -> list[str]:
    if not isinstance(files, (list, tuple)):
        return []
    names: list[str] = []
    for item in files:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("filename") or "").strip()
        if name:
            names.append(name)
    return names


def _detect_qpk_pin_changed(changed_files: Sequence[str], explicit: bool | None) -> bool:
    if explicit is True:
        return True
    if explicit is False:
        return False
    return any(_QPK_PATH_RE.search(path) for path in changed_files)


def build_trusted_event(
    *,
    repository: str,
    pr: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]] | None,
    ci_status: str,
    qpk_pin_changed: bool | None = None,
) -> dict[str, Any]:
    """Convert one PR snapshot into a trusted intake event (no title/body)."""
    repo = str(repository or "").strip()
    pr_number_raw = pr.get("number")
    pr_number = pr_number_raw if isinstance(pr_number_raw, int) and pr_number_raw > 0 else 0
    author = str((pr.get("user") or {}).get("login") or "").strip() or "unknown"
    update_class = derive_update_class(pr)

    base_sha = str((pr.get("base") or {}).get("sha") or "").strip().lower()
    head_sha = str((pr.get("head") or {}).get("sha") or "").strip().lower()
    if not _SHA_RE.fullmatch(base_sha):
        base_sha = _PLACEHOLDER_SHA
        update_class = "unknown"
    if not _SHA_RE.fullmatch(head_sha):
        head_sha = _PLACEHOLDER_SHA
        update_class = "unknown"

    changed = _changed_files(files)
    if not changed:
        changed = [_MISSING_FILES_SENTINEL]
        dependency_only = False
        update_class = "unknown"
    else:
        dependency_only = all(is_manifest_or_lock(path) for path in changed)

    status = str(ci_status or "").strip().lower() or "unknown"
    if status not in {"success", "failure", "pending", "unknown"}:
        status = "unknown"

    if not repo or pr_number <= 0:
        update_class = "unknown"

    event: dict[str, Any] = {
        "repository": repo or "unknown/unknown",
        "pr_number": pr_number if pr_number > 0 else 1,
        "author_login": author,
        "update_class": update_class or "unknown",
        "changed_files": changed,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "ci_status": status,
        "dependency_only_manifest_change": bool(dependency_only),
        "qpk_pin_changed": _detect_qpk_pin_changed(changed, qpk_pin_changed),
        "event_type": (
            "dependabot_pr" if author in DEPENDABOT_LOGINS else "engineering_review"
        ),
    }
    return event


def build_trusted_intake_payload(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": TRUSTED_INTAKE_SCHEMA_VERSION,
        "events": [dict(event) for event in events],
    }


def resolve_ci_status(token: str, repository: str, head_sha: str) -> str:
    """Map commit check-runs to a coarse ci_status; unknown when inconclusive.

    Uses bounded pagination. If the page cap is hit, the payload is incomplete,
    or total coverage cannot be proven, returns ``unknown`` (never quiet success).
    """
    if not _SHA_RE.fullmatch(str(head_sha or "").strip().lower()):
        return "unknown"

    collected: list[Mapping[str, Any]] = []
    total_count: int | None = None
    for page in range(1, MAX_CHECK_RUN_PAGES + 1):
        try:
            payload = github_request(
                token,
                "GET",
                (
                    f"/repos/{repository}/commits/{head_sha}/check-runs"
                    f"?per_page={CHECK_RUNS_PER_PAGE}&page={page}"
                ),
            )
        except (GitHubRequestError, OSError, ValueError, TypeError):
            return "unknown"
        if not isinstance(payload, Mapping):
            return "unknown"
        if "total_count" in payload:
            raw_total = payload.get("total_count")
            if not isinstance(raw_total, int) or raw_total < 0:
                return "unknown"
            total_count = raw_total
        runs = payload.get("check_runs")
        if not isinstance(runs, list):
            return "unknown"
        for item in runs:
            if isinstance(item, Mapping):
                collected.append(item)
        if len(runs) < CHECK_RUNS_PER_PAGE:
            break
        if page == MAX_CHECK_RUN_PAGES:
            # Full final page at the hard cap — cannot prove full coverage.
            return "unknown"
    else:
        # Loop exhausted without a short page (should be unreachable given break).
        return "unknown"

    if total_count is not None and len(collected) < total_count:
        return "unknown"
    if not collected:
        return "unknown"

    statuses: list[str] = []
    conclusions: list[str] = []
    for item in collected:
        statuses.append(str(item.get("status") or "").strip().lower())
        conclusions.append(str(item.get("conclusion") or "").strip().lower())
    if not statuses:
        return "unknown"
    if any(status and status != "completed" for status in statuses):
        return "pending"
    if any(conclusion in {"failure", "timed_out", "cancelled", "action_required"} for conclusion in conclusions):
        return "failure"
    if conclusions and all(conclusion == "success" for conclusion in conclusions):
        return "success"
    if any(conclusion in {"neutral", "skipped"} for conclusion in conclusions) and all(
        conclusion in {"success", "neutral", "skipped"} for conclusion in conclusions
    ):
        # Incomplete proof of green CI — fail closed.
        return "unknown"
    return "unknown"


def _raise_source_truncated(reason: str) -> None:
    raise DependencyNotificationSourceError(f"source_truncated: {reason}")


def collect_trusted_events(
    *,
    token: str,
    allowlist: Sequence[str],
    max_prs_per_repo: int = MAX_PRS_PER_REPO,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """GET open PRs for allowlisted repos and convert to trusted events.

    Hard PR/event caps are fail-closed: if the cap is reached while open PRs or
    allowlisted repos remain, raises ``DependencyNotificationSourceError`` with a
    ``source_truncated`` reason instead of silently dropping work.
    """
    repos = parse_repo_allowlist(list(allowlist))
    if max_prs_per_repo <= 0 or max_prs_per_repo > MAX_PRS_PER_REPO:
        raise DependencyNotificationSourceError(
            f"max_prs_per_repo must be 1..{MAX_PRS_PER_REPO}"
        )

    events: list[dict[str, Any]] = []
    for repo_index, repository in enumerate(repos):
        assert_repo_allowed(repository, repos)
        # Bound pages: github_list_all default max_pages=20; we only need first page worth.
        pulls = github_list_all(
            token,
            f"/repos/{repository}/pulls?state=open",
            max_pages=1,
        )
        valid_pulls = [pr for pr in pulls if isinstance(pr, dict)]
        if len(valid_pulls) > max_prs_per_repo:
            _raise_source_truncated(
                f"open PRs exceed max_prs_per_repo ({max_prs_per_repo})"
            )
        for pr_index, pr in enumerate(valid_pulls):
            number = pr.get("number")
            if not isinstance(number, int) or number <= 0:
                events.append(
                    build_trusted_event(
                        repository=repository,
                        pr=pr,
                        files=[],
                        ci_status="unknown",
                    )
                )
            else:
                files = github_list_all(
                    token,
                    f"/repos/{repository}/pulls/{number}/files",
                    max_pages=1,
                )
                head_sha = str((pr.get("head") or {}).get("sha") or "").strip().lower()
                ci_status = resolve_ci_status(token, repository, head_sha)
                events.append(
                    build_trusted_event(
                        repository=repository,
                        pr=pr,
                        files=files,
                        ci_status=ci_status,
                    )
                )
            if len(events) >= MAX_TRUSTED_INTAKE_EVENTS:
                more_prs = pr_index + 1 < len(valid_pulls)
                more_repos = repo_index + 1 < len(repos)
                if more_prs or more_repos:
                    _raise_source_truncated(
                        f"events exceed limit of {MAX_TRUSTED_INTAKE_EVENTS}"
                    )
                break
        if len(events) >= MAX_TRUSTED_INTAKE_EVENTS:
            break

    summary = {
        "repositories": len(repos),
        "events": len(events),
        "max_prs_per_repo": max_prs_per_repo,
        "safe_summary": "source_events_collected",
    }
    return events, summary


def _source_fail_closed(error: str, *, reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "action": "telegram",
        "review_required": True,
        "confidence": "unknown",
        "error": error,
        "reasons": list(reasons or [error]),
        "counts": {
            "events": 0,
            "quiet": 0,
            "telegram": 0,
            "github_issue": 0,
            "deduped": 0,
        },
        "events": [],
        "source": {"safe_summary": error},
        "dispatch": {
            "action": "telegram",
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


def collect_or_fail_closed(
    *,
    token: str,
    allowlist: Sequence[str],
    max_prs_per_repo: int = MAX_PRS_PER_REPO,
) -> dict[str, Any]:
    """Collect events or return a redacted fail-closed summary (no raw API bodies)."""
    try:
        events, summary = collect_trusted_events(
            token=token,
            allowlist=allowlist,
            max_prs_per_repo=max_prs_per_repo,
        )
    except DependencyNotificationSourceError as exc:
        message = str(exc)
        if message.startswith("source_truncated"):
            return _source_fail_closed("source_truncated", reasons=[message])
        return _source_fail_closed("allowlist_or_limits", reasons=[message])
    except DependencyAuditError:
        return _source_fail_closed("github_pagination_or_list_failed")
    except GitHubRequestError:
        return _source_fail_closed("github_api_failed")
    except (OSError, TimeoutError, ValueError, TypeError):
        return _source_fail_closed("github_api_failed")

    payload = build_trusted_intake_payload(events)
    preview = run_trusted_intake_dry_run(payload)
    preview = dict(preview)
    preview["source"] = summary
    return preview


def run_source_dry_run(
    *,
    token: str,
    allowlist: Sequence[str],
    max_prs_per_repo: int = MAX_PRS_PER_REPO,
) -> dict[str, Any]:
    """Public entry used by the CLI."""
    return collect_or_fail_closed(
        token=token,
        allowlist=allowlist,
        max_prs_per_repo=max_prs_per_repo,
    )
