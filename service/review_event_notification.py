"""Strict PR review completion event validation and sanitized notification."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

from service.briefing_dispatch import send_telegram_alert, telegram_delivery_config


EVENT_SCHEMA = "qsl.pr_review_event.v1"
EVENT_TASK = "pr_review_completed"
MAX_SAFE_INTEGER = 2**53 - 1
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_HEAD_SHA_RE = re.compile(r"[0-9a-f]{40}")
_RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
_TRUSTED_WORKFLOW_RE = re.compile(
    r"QuantStrategyLab/AIAuditBridge/\.github/workflows/codex_pr_review\.yml@"
    r"(?:refs/heads/main|[0-9a-f]{40})"
)
_REVIEW_OUTCOMES = frozenset({"success", "failure", "cancelled", "skipped"})
_EXACT_KEYS = frozenset(
    {
        "schema",
        "repository",
        "pr_number",
        "head_sha",
        "workflow_run_id",
        "review_outcome",
        "blocked",
        "contract_conflict",
    }
)


class ReviewEventError(ValueError):
    """Raised when review event metadata violates the closed contract."""


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    repository: str
    pr_number: int
    head_sha: str
    workflow_run_id: str
    review_outcome: str
    blocked: bool | None
    contract_conflict: bool | None

    @property
    def workflow_run_url(self) -> str:
        return f"https://github.com/{self.repository}/actions/runs/{self.workflow_run_id}"


def parse_review_event_metadata(owner_repository: str, metadata: Any) -> ReviewEvent:
    if type(metadata) is not dict or frozenset(metadata) != _EXACT_KEYS:
        raise ReviewEventError("review event metadata shape is invalid")
    repository = metadata["repository"]
    if (
        type(repository) is not str
        or _REPOSITORY_RE.fullmatch(repository) is None
        or repository != owner_repository
    ):
        raise ReviewEventError("review event repository is invalid")
    if metadata["schema"] != EVENT_SCHEMA or type(metadata["schema"]) is not str:
        raise ReviewEventError("review event schema is invalid")
    pr_number = metadata["pr_number"]
    if type(pr_number) is not int or not 0 < pr_number <= MAX_SAFE_INTEGER:
        raise ReviewEventError("review event PR number is invalid")
    head_sha = metadata["head_sha"]
    if type(head_sha) is not str or _HEAD_SHA_RE.fullmatch(head_sha) is None:
        raise ReviewEventError("review event head SHA is invalid")
    workflow_run_id = metadata["workflow_run_id"]
    if type(workflow_run_id) is not str or _RUN_ID_RE.fullmatch(workflow_run_id) is None:
        raise ReviewEventError("review event workflow run id is invalid")
    review_outcome = metadata["review_outcome"]
    if type(review_outcome) is not str or review_outcome not in _REVIEW_OUTCOMES:
        raise ReviewEventError("review event outcome is invalid")
    blocked = metadata["blocked"]
    contract_conflict = metadata["contract_conflict"]
    if blocked is not None and type(blocked) is not bool:
        raise ReviewEventError("review event blocked flag is invalid")
    if contract_conflict is not None and type(contract_conflict) is not bool:
        raise ReviewEventError("review event contract conflict flag is invalid")
    return ReviewEvent(
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        workflow_run_id=workflow_run_id,
        review_outcome=review_outcome,
        blocked=blocked,
        contract_conflict=contract_conflict,
    )


def expected_review_event_run_id(event: ReviewEvent) -> str:
    return (
        f"review:{event.repository}:{event.pr_number}:"
        f"{event.head_sha}:{event.workflow_run_id}"
    )


def assert_review_event_provenance(claims: Any, event: ReviewEvent) -> None:
    """Bind a notification to its GitHub run and the trusted review workflow."""
    if type(claims) is not dict:
        raise PermissionError("review event OIDC claims are invalid")
    auth_method = claims.get("auth_method")
    if auth_method == "none" and claims.get("repository") == "local":
        return
    if auth_method != "github_oidc":
        raise PermissionError("review events require GitHub OIDC")
    if claims.get("repository") != event.repository:
        raise PermissionError("review event OIDC repository does not match")
    if claims.get("run_id") != event.workflow_run_id:
        raise PermissionError("review event OIDC run id does not match")
    workflow_ref = claims.get("workflow_ref")
    job_workflow_ref = claims.get("job_workflow_ref")
    trusted_ref = workflow_ref if job_workflow_ref is None or job_workflow_ref == "" else job_workflow_ref
    if type(trusted_ref) is not str or _TRUSTED_WORKFLOW_RE.fullmatch(trusted_ref) is None:
        raise PermissionError("review event workflow identity is not trusted")


def format_review_event_notification(event: ReviewEvent) -> str:
    repository_name = event.repository.split("/", 1)[1]
    blocked = "unknown" if event.blocked is None else str(event.blocked).lower()
    conflict = "unknown" if event.contract_conflict is None else str(event.contract_conflict).lower()
    return "\n".join(
        (
            f"[QSL Review] {repository_name} PR #{event.pr_number}",
            f"head={event.head_sha[:12]} review={event.review_outcome} blocked={blocked} contract_conflict={conflict}",
            event.workflow_run_url,
        )
    )


def dispatch_review_event_notification(event: ReviewEvent) -> dict[str, str]:
    token, chat_ids = telegram_delivery_config(os.environ)
    if not token or not chat_ids:
        return {"status": "skipped", "reason": "telegram_not_configured"}
    sent = send_telegram_alert(
        text=format_review_event_notification(event),
        token=token,
        chat_ids=chat_ids,
    )
    return {"status": "sent" if sent else "failed"}
