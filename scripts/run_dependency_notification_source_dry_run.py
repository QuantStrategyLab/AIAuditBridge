#!/usr/bin/env python3
"""Dry-run-only CLI: allowlisted GitHub PR GET → trusted intake schema v1 preview.

Reads open PRs via GitHub REST GET for an explicit repository allowlist, converts
them into the merged trusted intake schema v1, and reuses
``run_trusted_intake_dry_run`` for routing preview.

Does not send Telegram, create issues/comments, merge PRs, call models, or
modify ``dependency_audit.yml`` schedules.
"""

from __future__ import annotations

import argparse
import json
import os

from scripts.run_monthly_codex_audit import BridgeError, env_value
from service.dependency_notification_source import (
    DependencyNotificationSourceError,
    MAX_PRS_PER_REPO,
    parse_repo_allowlist,
    run_source_dry_run,
)


def resolve_source_token() -> str:
    """Reuse existing token env helpers; do not invent new secrets.

    Multi-repo allowlists need ``CODEX_AUDIT_GH_TOKEN`` or ``GH_TOKEN`` with read
    access. Workflow ``GITHUB_TOKEN`` is only valid for ``GITHUB_REPOSITORY`` and
    is therefore insufficient for cross-repo allowlists (documented gap).
    """
    token = env_value("CODEX_AUDIT_GH_TOKEN") or env_value("GH_TOKEN")
    if token:
        return token
    github_token = env_value("GITHUB_TOKEN")
    github_repository = env_value("GITHUB_REPOSITORY")
    if github_token and github_repository:
        # Single-repo only: caller must allowlist exactly that repository.
        return github_token
    raise BridgeError(
        "CODEX_AUDIT_GH_TOKEN or GH_TOKEN with repository read access is required "
        "for dependency notification source dry-run"
    )


def _allowlist_from_args(args: argparse.Namespace) -> tuple[str, ...]:
    chunks: list[str] = []
    if args.repos:
        chunks.append(args.repos)
    if args.repo:
        chunks.extend(args.repo)
    env_raw = os.environ.get("DEPENDENCY_NOTIFICATION_REPO_ALLOWLIST")
    if env_raw:
        chunks.append(env_raw)
    if not chunks:
        raise DependencyNotificationSourceError(
            "repository allowlist required via --repos/--repo or "
            "DEPENDENCY_NOTIFICATION_REPO_ALLOWLIST (empty default is forbidden)"
        )
    return parse_repo_allowlist(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run only: allowlisted GitHub PR GET → trusted intake schema v1 "
            "preview (no send / merge / model)."
        )
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated owner/name allowlist (required unless --repo or env)",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repeatable allowlist entry (owner/name)",
    )
    parser.add_argument(
        "--max-prs-per-repo",
        type=int,
        default=MAX_PRS_PER_REPO,
        help=f"Max open PRs fetched per allowlisted repo (1..{MAX_PRS_PER_REPO})",
    )
    args = parser.parse_args(argv)

    try:
        allowlist = _allowlist_from_args(args)
        # If only GITHUB_TOKEN is present, enforce single-repo match.
        token = resolve_source_token()
        has_org_token = bool(env_value("CODEX_AUDIT_GH_TOKEN") or env_value("GH_TOKEN"))
        github_repository = env_value("GITHUB_REPOSITORY")
        if not has_org_token and github_repository:
            if allowlist != (github_repository,):
                raise DependencyNotificationSourceError(
                    "GITHUB_TOKEN may only read GITHUB_REPOSITORY; "
                    "set CODEX_AUDIT_GH_TOKEN or GH_TOKEN for multi-repo allowlists"
                )
        result = run_source_dry_run(
            token=token,
            allowlist=allowlist,
            max_prs_per_repo=args.max_prs_per_repo,
        )
    except DependencyNotificationSourceError as exc:
        result = {
            "ok": False,
            "action": "telegram",
            "review_required": True,
            "confidence": "unknown",
            "error": "allowlist_or_limits",
            "reasons": [str(exc)],
            "counts": {
                "events": 0,
                "quiet": 0,
                "telegram": 0,
                "github_issue": 0,
                "deduped": 0,
            },
            "events": [],
            "source": {"safe_summary": "allowlist_or_limits"},
        }
    except BridgeError:
        result = {
            "ok": False,
            "action": "telegram",
            "review_required": True,
            "confidence": "unknown",
            "error": "token_unavailable",
            "reasons": ["token_unavailable"],
            "counts": {
                "events": 0,
                "quiet": 0,
                "telegram": 0,
                "github_issue": 0,
                "deduped": 0,
            },
            "events": [],
            "source": {"safe_summary": "token_unavailable"},
        }

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
