#!/usr/bin/env python3
"""Write one read-only AI diagnosis for a ready portfolio research proposal.

The upstream UsEquitySnapshotPipelines workflow owns the readiness record and
its Issue.  This dispatcher only validates its sanitized artifact, finds that
existing Issue, then adds at most one idempotent text comment.  It cannot
create a portfolio candidate, fetch data, run an experiment, or change any
P1--P6 authority.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.config import GatewayConfig  # noqa: E402
from client.gateway_client import AiGatewayClient  # noqa: E402
from service.portfolio_research_proposal import (  # noqa: E402
    PortfolioResearchProposalError,
    READY_STATUS,
    build_portfolio_research_proposal_diagnosis_request,
    build_portfolio_research_proposal_prompt,
    format_portfolio_research_proposal_comment,
    marker_for_portfolio_research_proposal,
    validate_portfolio_candidate_readiness,
)


SOURCE_REPOSITORY = "QuantStrategyLab/UsEquitySnapshotPipelines"
DISPATCH_SCHEMA = "qsl.portfolio-research-proposal-diagnosis-dispatch.v1"


def _error_summary(value: object) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:300]


def load_portfolio_readiness(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("portfolio readiness artifact must be a JSON object")
    return payload


def find_open_readiness_issue(repository: str, proposal_id: str) -> str:
    """Find only the upstream Issue carrying the exact readiness marker."""
    if repository != SOURCE_REPOSITORY:
        raise ValueError("portfolio readiness source repository is not allowed")
    marker = f"<!-- qsl-portfolio-candidate-readiness:{proposal_id} -->"
    page = 1
    while True:
        completed = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"/repos/{repository}/issues",
                "-f",
                "state=open",
                "-f",
                "per_page=100",
                "-f",
                f"page={page}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            issues = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("unable to parse readiness Issue list") from exc
        if not isinstance(issues, list) or not issues:
            return ""
        for issue in issues:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            if marker in str(issue.get("body") or ""):
                return str(issue.get("html_url") or issue.get("url") or "").strip()
        if len(issues) < 100:
            return ""
        page += 1


def issue_has_diagnosis_marker(repository: str, issue_url: str, marker: str) -> bool:
    """Treat unreadable comments as already diagnosed, preventing repeats."""
    try:
        completed = subprocess.run(
            ["gh", "issue", "view", issue_url, "--repo", repository, "--json", "comments", "--jq", ".comments[].body"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return True
    return marker in completed.stdout


def comment_issue(repository: str, issue_url: str, body: str) -> str:
    completed = subprocess.run(
        ["gh", "issue", "comment", issue_url, "--repo", repository, "--body", body],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def run_portfolio_research_proposal_diagnosis(
    readiness: Mapping[str, Any],
    *,
    repository: str = SOURCE_REPOSITORY,
    dry_run: bool = False,
    find_issue: Callable[[str, str], str] = find_open_readiness_issue,
    marker_present: Callable[[str, str, str], bool] = issue_has_diagnosis_marker,
    create_comment: Callable[[str, str, str], str] = comment_issue,
    client_factory: Callable[[GatewayConfig], AiGatewayClient] = AiGatewayClient,
) -> dict[str, Any]:
    """Diagnose one ready proposal at most; all failures remain no-side-effect."""
    verified = validate_portfolio_candidate_readiness(readiness)
    proposal_id = str(verified["proposal"]["proposal_id"])
    summary: dict[str, Any] = {
        "schema_version": DISPATCH_SCHEMA,
        "status": "ok",
        "proposal_id": proposal_id,
        "readiness_sha256": verified["readiness_sha256"],
        "dry_run": dry_run,
        "diagnoses": [],
    }
    if verified["status"] != READY_STATUS:
        summary["status"] = "skipped"
        summary["reason"] = "portfolio_readiness_not_ready"
        return summary
    if repository != SOURCE_REPOSITORY:
        raise ValueError("portfolio readiness source repository is not allowed")
    request = build_portfolio_research_proposal_diagnosis_request(verified)
    marker = marker_for_portfolio_research_proposal(request)
    try:
        issue_url = find_issue(repository, proposal_id)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        summary["status"] = "unavailable"
        summary["reason"] = "source_issue_lookup_unavailable"
        summary["error"] = _error_summary(exc)
        return summary
    if not issue_url:
        summary["status"] = "skipped"
        summary["reason"] = "source_readiness_issue_not_found"
        return summary
    if marker_present(repository, issue_url, marker):
        summary["status"] = "skipped"
        summary["reason"] = "proposal_already_diagnosed_or_comments_unavailable"
        return summary
    prompt = build_portfolio_research_proposal_prompt(request)
    if dry_run:
        summary["diagnoses"].append(
            {
                "status": "dry_run",
                "proposal_id": proposal_id,
                "repository": repository,
                "issue_url": issue_url,
                "prompt": prompt,
            }
        )
        return summary
    try:
        config = GatewayConfig.from_env()
    except ValueError:
        summary["status"] = "not_configured"
        summary["reason"] = "ai_gateway_not_configured"
        return summary
    client = client_factory(config)
    try:
        ai_result = client.analyze(
            prompt,
            system="You provide bounded, read-only portfolio research proposal diagnosis only.",
            max_tokens=1_600,
            timeout=120,
            source_repository=repository,
        )
    except Exception as exc:  # noqa: BLE001 - gateway failures must stay non-operative
        summary["status"] = "unavailable"
        summary["reason"] = "ai_gateway_unavailable"
        summary["error"] = _error_summary(exc)
        return summary
    # Content availability is separate from decision authority; never change success.
    output = ai_result.output
    raw = getattr(ai_result, "raw", None)
    note = getattr(ai_result, "note", "")
    status = raw.get("status", "ok") if isinstance(raw, dict) else "ok"
    policy = raw.get("policy_verdict", status) if isinstance(raw, dict) else status
    advisory = (ai_result.success is False and note == "advisory"
                and status == "advisory" and policy == "advisory" and isinstance(raw, dict))
    ok = (ai_result.success is True and note == ""
          and status == "ok" and policy in ("ok", "eligible"))
    content_available = (
        isinstance(output, str) and bool(output.strip())
        and not ai_result.error and (raw is None or isinstance(raw, dict))
        and (not isinstance(raw, dict) or raw.get("output", output) == output)
        and (ok or advisory)
    )
    if not content_available:
        summary["status"] = "unavailable"
        summary["reason"] = "ai_gateway_unavailable"
        summary["error"] = _error_summary(ai_result.error)
        return summary
    if advisory:
        output = "advisory：仅供研究讨论，不证明执行、晋级或授权。\n\n" + output
    try:
        body = format_portfolio_research_proposal_comment(
            request, output, provider=ai_result.provider, model=ai_result.model
        )
        comment_url = create_comment(repository, issue_url, body)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        summary["status"] = "unavailable"
        summary["reason"] = "source_issue_comment_unavailable"
        summary["error"] = _error_summary(exc)
        return summary
    summary["diagnoses"].append(
        {
            "status": "diagnosed",
            "proposal_id": proposal_id,
            "repository": repository,
            "issue_url": issue_url,
            "comment_url": comment_url,
        }
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one read-only portfolio research proposal diagnosis.")
    parser.add_argument("--input", required=True, help="Sanitized portfolio readiness artifact")
    parser.add_argument("--dry-run", action="store_true", help="Build a prompt without calling AI or commenting")
    args = parser.parse_args(argv)
    try:
        result = run_portfolio_research_proposal_diagnosis(
            load_portfolio_readiness(args.input), dry_run=args.dry_run
        )
    except (OSError, ValueError, json.JSONDecodeError, PortfolioResearchProposalError) as exc:
        print(json.dumps({"schema_version": DISPATCH_SCHEMA, "status": "rejected", "error": _error_summary(exc)}, sort_keys=True))
        return 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
