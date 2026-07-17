#!/usr/bin/env python3
"""Run Codex review on a PR diff and block merge when serious issues are found.

Uses the existing Codex audit service backend (same as monthly reviews).
Evaluates findings against the repo's codex_auto_merge_policy.json.
Exits non-zero when blocked, which fails the GitHub Actions check run.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from string import Template
from typing import Any

# ---------------------------------------------------------------------------
# Configuration (aligned with CodexAuditBridge)
# ---------------------------------------------------------------------------

API_BASE = "https://api.github.com"
BRIDGE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CODEX_PR_REVIEW_REPO_ROOT") or os.environ.get("GITHUB_WORKSPACE") or Path.cwd()).resolve()
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

POLICY_PATH = ROOT / ".github" / "codex_auto_merge_policy.json"
PROMPT_TEMPLATE_PATH = BRIDGE_ROOT / "prompts" / "pr_review.md"
DEFAULT_SERVICE_AUDIENCE = "quant-codex-audit"
DEFAULT_TIMEOUT_MINUTES = 20
DEFAULT_MAX_CONTEXT_LINES = 800
TASK_COMPLEXITY_LOW = "low"
TASK_COMPLEXITY_MEDIUM = "medium"
TASK_COMPLEXITY_HIGH = "high"
TASK_COMPLEXITY_LEVELS = (TASK_COMPLEXITY_LOW, TASK_COMPLEXITY_MEDIUM, TASK_COMPLEXITY_HIGH)
CODEX_SERVICE_FALLBACK_SIGNALS = (
    "429",
    "too many requests",
    "rate limit",
    "quota",
    "codex exec failed",
)
NO_REVIEW_BACKEND_CONFIGURED = (
    "No Codex service URL or API key configured. "
    "Set CODEX_AUDIT_SERVICE_URL, ANTHROPIC_API_KEY, or OPENAI_API_KEY."
)

# Risk → block mapping
BLOCK_SEVERITIES = frozenset({"critical", "high"})
COMMENT_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
STREAK_MARKER_PREFIX = "<!-- codex-pr-review-streak:"
STREAK_MARKER_SUFFIX = " -->"
FINGERPRINT_MARKER_PREFIX = "<!-- codex-pr-review-fingerprint:"
FINGERPRINT_MARKER_SUFFIX = " -->"
FINGERPRINTS_MARKER_PREFIX = "<!-- codex-pr-review-fingerprints:"
FINGERPRINTS_MARKER_SUFFIX = " -->"
HEAD_SHA_MARKER_PREFIX = "<!-- codex-pr-review-head-sha:"
HEAD_SHA_MARKER_SUFFIX = " -->"
FINDING_HISTORY_MARKER_PREFIX = "<!-- codex-pr-review-history:v1:"
FINDING_HISTORY_MARKER_SUFFIX = " -->"
CONTRACT_CONFLICT_MARKER_PREFIX = "<!-- codex-pr-review-contract-conflict:"
AUTO_FIX_ALLOWED_MARKER_PREFIX = "<!-- codex-pr-review-auto-fix-allowed:"
NEXT_ACTION_MARKER_PREFIX = "<!-- codex-pr-review-next-action:"
IMPLEMENTATION_MARKER_PREFIX = "<!-- codex-pr-review-implementation:v1:"
IMPLEMENTATION_MARKER_SUFFIX = " -->"
REVIEW_STATE_MARKER_PREFIX = "<!-- codex-pr-review-state:v2:"
REVIEW_STATE_MARKER_SUFFIX = " -->"
REVIEW_DISPOSITION_MARKER_PREFIX = "<!-- codex-pr-review-disposition:v1:"
REVIEW_DISPOSITION_MARKER_SUFFIX = " -->"
DECISION_MARKER_SUFFIX = " -->"
FINDING_HISTORY_MAX_ROUNDS = 4
FINDING_HISTORY_MAX_BYTES = 8192
FINDING_HISTORY_MAX_ENCODED_BYTES = ((FINDING_HISTORY_MAX_BYTES + 2) // 3) * 4
FINDING_HISTORY_TEXT_LIMIT = 500
REVIEW_STATE_MAX_FINDINGS = 32
REVIEW_STATE_MAX_BYTES = 16 * 1024
REVIEW_STATE_MAX_ENCODED_BYTES = ((REVIEW_STATE_MAX_BYTES + 2) // 3) * 4
REVIEW_DISPOSITION_MAX_BYTES = 1024
REVIEW_DISPOSITION_MAX_ENCODED_BYTES = ((REVIEW_DISPOSITION_MAX_BYTES + 2) // 3) * 4
REVIEW_DISPOSITION_MAX_PAGES = 5
REVIEW_AUTHORITY_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
REVIEW_FINDING_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
FULL_HEAD_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
HEAD_SHA_PATTERN = re.compile(r"[0-9a-f]{7,64}")
FINDING_CATEGORIES = frozenset(
    {
        "security",
        "bug",
        "performance",
        "logic",
        "reliability",
        "correctness",
        "data_integrity",
        "api_compatibility",
        "race_condition",
    }
)
EVIDENCE_SYMBOL_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,127}")
EVIDENCE_KINDS = frozenset({"current_caller", "public_untrusted_boundary"})
ARBITRATION_REPEAT_THRESHOLD = 2


class ReviewError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def github_request(
    token: str, method: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    url = path if path.startswith("https://") else f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-pr-review",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"GitHub API {method} {url} failed: {exc.code} {body[:600]}") from exc
    return json.loads(body) if body else {}


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


# ---------------------------------------------------------------------------
# Policy loading (reuses evaluate_codex_pr_merge.py logic)
# ---------------------------------------------------------------------------


def load_policy(token: str = "", repo: str = "", base_ref: str = "") -> dict[str, Any]:
    """Load the risk policy, falling back to safe defaults."""
    if token and repo and base_ref:
        return _load_policy_from_trusted_ref(token, repo, base_ref)

    if not POLICY_PATH.exists():
        return _default_policy()

    try:
        payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fail_closed("invalid auto-merge policy JSON")

    return _validate_policy_payload(payload)


def _load_policy_from_trusted_ref(token: str, repo: str, ref: str) -> dict[str, Any]:
    path = urllib.parse.quote(".github/codex_auto_merge_policy.json", safe="")
    ref_query = urllib.parse.quote(ref, safe="")
    try:
        payload = github_request(token, "GET", f"/repos/{repo}/contents/{path}?ref={ref_query}")
    except ReviewError as exc:
        if "failed: 404" in str(exc):
            return _default_policy()
        return _fail_closed("could not load trusted auto-merge policy")
    if not isinstance(payload, dict):
        return _fail_closed("invalid trusted auto-merge policy response")
    try:
        encoded = str(payload.get("content") or "")
        raw = base64.b64decode(encoded).decode("utf-8")
        policy = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return _fail_closed("invalid trusted auto-merge policy JSON")
    return _validate_policy_payload(policy)


def _validate_policy_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _fail_closed("invalid auto-merge policy format")
    if payload.get("version") != 1:
        return _fail_closed("unsupported policy version")
    return payload


def _default_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "blocked_path_patterns": [
            r"(^|/)(\.env|.*secret.*|.*credential.*|.*token.*|.*private.*|.*\.pem|.*\.key)$",
        ],
        "risk_policy": {
            "low": {
                "prefixes": ["docs/", "tests/"],
                "exact": ["README.md", "README.zh-CN.md"],
                "reason": "docs/tests/readme-only changes",
            },
            "high": {"reason": "source code changes require review"},
        },
        "max_changed_files": 30,
        "max_changed_lines": 2000,
        "pr_review": {},
    }


def _fail_closed(reason: str) -> dict[str, Any]:
    return {
        "policy_errors": [reason],
        "blocked_path_patterns": [r".*"],
        "risk_policy": {
            "low": {"prefixes": [], "exact": [], "reason": reason},
            "high": {"reason": reason},
        },
    }


# ---------------------------------------------------------------------------
# File risk classification
# ---------------------------------------------------------------------------


def classify_file_risk(
    file_path: str, policy: dict[str, Any]
) -> tuple[str, str]:
    """Return (risk_level, reason) for a single file path."""
    policy.get("policy_errors", [])

    # Blocked patterns (secrets, credentials, etc.)
    blocked_patterns = policy.get("blocked_path_patterns", [])
    for pattern in blocked_patterns:
        try:
            if re.search(pattern, file_path, re.IGNORECASE):
                return ("high", f"matches blocked path pattern: {pattern}")
        except re.error:
            continue

    risk_policy = policy.get("risk_policy", {})
    low = risk_policy.get("low", {})
    low_prefixes = low.get("prefixes", [])
    low_exact = set(low.get("exact", []))
    medium_exact = set(
        risk_policy.get("medium", {}).get("exact", [])
    )

    # Normalize path
    normalized = file_path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]

    if not normalized:
        return ("high", "empty path")

    if normalized in low_exact or any(
        normalized.startswith(prefix) for prefix in low_prefixes
    ):
        return ("low", "docs/test/readme change")

    if normalized in medium_exact:
        return ("medium", "monthly-review helper changed")

    return ("high", "source code change")


def changed_files_are_low_risk(paths: list[str], policy: dict[str, Any]) -> bool:
    """Return True when every changed path is low-risk under the policy."""
    return bool(paths) and all(classify_file_risk(path, policy)[0] == TASK_COMPLEXITY_LOW for path in paths)


# ---------------------------------------------------------------------------
# PR diff fetching
# ---------------------------------------------------------------------------


def _verify_pr_head_sha(token: str, repo: str, pr_number: int, expected_head_sha: str) -> None:
    if not FULL_HEAD_SHA_PATTERN.fullmatch(expected_head_sha):
        raise ReviewError("reviewed head SHA is invalid")
    payload = github_request(token, "GET", f"/repos/{repo}/pulls/{pr_number}")
    head = payload.get("head") if isinstance(payload, dict) else None
    actual_head_sha = str(head.get("sha") or "").strip().lower() if isinstance(head, dict) else ""
    if actual_head_sha != expected_head_sha:
        raise ReviewError("pull request head changed while review context was fetched")


def fetch_pr_diff(
    token: str, repo: str, pr_number: int, expected_head_sha: str = ""
) -> str:
    """Fetch the unified diff for a PR."""
    diff_url = f"{API_BASE}/repos/{repo}/pulls/{pr_number}"
    req = urllib.request.Request(
        diff_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-pr-review",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            diff = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"Failed to fetch PR diff: {exc.code} {body[:600]}") from exc
    if expected_head_sha:
        _verify_pr_head_sha(token, repo, pr_number, expected_head_sha)
    return diff


def fetch_pr_files(token: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Fetch the list of changed files in a PR."""
    files: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = github_request(
            token,
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}",
        )
        if not isinstance(payload, list) or not payload:
            break
        files.extend(payload)
        if len(payload) < 100:
            break
        page += 1
    return files


# ---------------------------------------------------------------------------
# Review prompt
# ---------------------------------------------------------------------------


def build_review_prompt(diff: str, pr_title: str, pr_body: str, repo: str) -> str:
    """Build the Codex review prompt with the PR diff and structured output instructions."""
    diff_limited = _truncate_lines(diff, DEFAULT_MAX_CONTEXT_LINES * 3)

    template = Template(
        """You are reviewing a pull request for a production codebase. Your job is to find bugs, security issues, and logic errors that could cause real problems.

## PR Context

- Repository: ${REPO}
- PR Title: ${TITLE}

${BODY}

## Review Instructions

1. Focus on **security vulnerabilities, logic errors, data corruption, crash bugs, race conditions, and API compatibility breaks**.
2. Do NOT flag: code style, formatting, naming suggestions, minor refactoring preferences, or documentation issues.
3. Do not emit a finding that concludes no code change is needed. For OIDC, `job_workflow_ref` is absent for explicit direct callers; flag a bypass only when a non-direct repository can reach the direct-caller path despite the allowlists.
4. Assign **critical** or **high** only when the supplied PR context proves all of the following: an exact changed RIGHT-side patch path and line; a current repository caller of the changed enclosing callable, or an explicitly declared public untrusted boundary on that callable; reachability under the current configuration and inputs; and a concrete correctness, security, or data-integrity impact. Put the concrete impact in `impact` and identify the repository evidence with the exact `kind`, `path`, `line`, and callable `symbol` fields. If any element is missing, downgrade it to medium or low or omit it.
5. Do not block on a hypothetical future consumer, forged internal object state, or generic defense-in-depth concern. Do not request a new parser, store, registry, or event-persistence layer unless the changed code already exposes that current boundary and the defect is reachable through it.
6. Never provide `review_finding_id`, advisory provenance, or disposition authority. The trusted bridge computes identity and reads dispositions independently of model output.
7. Review the entire diff holistically and report all independent actionable findings in one response. Do not stop after the first blocking issue.
8. Do not invent backward-compatibility requirements that are absent from the repository and PR contract. When the repository and PR explicitly define a clean-slate namespace with legacy compatibility out of scope, review that boundary for accidental fallback instead of requesting dual-read or migration. This never overrides security or data-integrity findings.
9. Only for a public JSON/wire contract proven by rule 4, check optional-key presence versus explicit null, recursive JSON-safe types, every identity-bearing integer range, one canonical timestamp representation, deterministic round-trips and digests, immutability, and identifier/path safety.
10. For each finding, classify its severity:
   - **critical**: security vulnerability, data loss, production crash
   - **high**: logic error that produces wrong results, API break, memory/connection leak
   - **medium**: missing error handling, performance degradation, race condition
   - **low**: misleading comment, unclear variable name, redundant code

## Output Format

Return exactly one JSON object and no surrounding prose:

```json
{
  "summary": "Brief summary of the review (1-3 sentences)",
  "findings": [
    {
      "severity": "critical|high|medium|low",
      "category": "security|bug|performance|logic|reliability",
      "file": "relative/path/to/file.py",
      "line": 42,
      "description": "What the problem is",
      "impact": "Concrete reachable correctness, security, or data-integrity impact",
      "evidence": {
        "kind": "current_caller|public_untrusted_boundary",
        "path": "relative/path/to/caller.py",
        "line": 84,
        "symbol": "changed_callable"
      },
      "suggestion": "How to fix it"
    }
  ]
}
```

If there are no findings, return an empty `findings` array.

## PR Diff

```diff
${DIFF}
```"""
    )

    return template.safe_substitute(
        REPO=repo,
        TITLE=pr_title,
        BODY=f"### PR Description\n\n{pr_body}" if pr_body.strip() else "",
        DIFF=diff_limited,
    )


def review_implementation_digest() -> str:
    """Return the identity of the trusted bridge implementation that reviews a PR."""
    digest = hashlib.sha256()
    for path in (Path(__file__), PROMPT_TEMPLATE_PATH):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:24]


def _truncate_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    half = max_lines // 2
    return (
        "\n".join(lines[:half])
        + f"\n\n... [{len(lines) - max_lines} lines truncated] ...\n\n"
        + "\n".join(lines[-half:])
    )


def _normalize_complexity(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in TASK_COMPLEXITY_LEVELS:
        return normalized
    return ""


def _estimate_review_complexity(
    diff: str,
    changed_files: list[str],
    *,
    title: str = "",
    body: str = "",
) -> str:
    diff_lines = len((diff or "").splitlines())
    file_count = len([f for f in changed_files if f])
    prompt_chars = len((diff or "")) + len((title or "")) + len((body or ""))

    if diff_lines >= 1800 or file_count >= 15 or prompt_chars >= 18000:
        return TASK_COMPLEXITY_HIGH
    if diff_lines >= 600 or file_count >= 6 or prompt_chars >= 7000:
        return TASK_COMPLEXITY_MEDIUM
    return TASK_COMPLEXITY_LOW


def _direct_api_model_for_complexity(provider: str, complexity: str) -> str:
    level = _normalize_complexity(complexity)
    if not level:
        return ""
    prefix = "ANTHROPIC" if provider == "anthropic" else "OPENAI"
    for name in (
        f"CODEX_AUDIT_{prefix}_{level.upper()}_COMPLEXITY_MODEL",
        f"{prefix}_{level.upper()}_COMPLEXITY_MODEL",
        f"{prefix}_MODEL_{level.upper()}",
    ):
        value = env_value(name)
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Codex service integration
# ---------------------------------------------------------------------------


def request_github_oidc_token(audience: str) -> str:
    request_url = env_value("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = env_value("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not request_url or not request_token:
        raise ReviewError(
            "GitHub OIDC environment unavailable. Set permissions: id-token: write."
        )
    separator = "&" if "?" in request_url else "?"
    url = f"{request_url}{separator}audience={urllib.parse.quote(audience)}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {request_token}",
            "Accept": "application/json",
            "User-Agent": "codex-pr-review-oidc",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ReviewError("GitHub OIDC token response missing token value")
    return token


def run_codex_service_review(prompt: str, timeout_minutes: int, complexity: str = "", changed_file_count: int = 0, changed_line_count: int = 0) -> str:
    """Submit a review job to the Codex audit service and wait for completion."""
    service_url = env_value("CODEX_AUDIT_SERVICE_URL")
    if not service_url:
        raise ReviewError("CODEX_AUDIT_SERVICE_URL is not configured")

    service_url = service_url.strip().rstrip("/")
    audience = env_value("CODEX_AUDIT_SERVICE_AUDIENCE", DEFAULT_SERVICE_AUDIENCE)

    # Submit job
    oidc_token = request_github_oidc_token(audience)
    payload = {
        "source_repository": env_value("GITHUB_REPOSITORY"),
        "source_ref": env_value("GITHUB_REF_NAME", "main"),
        "task": "pr_review",
        "mode": "review_only",
        "prompt": prompt,
        # The VPS owns the Codex CLI model configuration.  Its configured
        # model is validated at deployment time; forwarding an API catalog
        # model here can select one unavailable to the CLI account.
        "complexity": _normalize_complexity(complexity) or "auto",
        "changed_files": int(changed_file_count),
        "changed_lines": int(changed_line_count),
        "timeout_seconds": timeout_minutes * 60,
    }
    submit_resp = _service_request(
        "POST",
        f"{service_url}/v1/codex-audit/jobs",
        oidc_token,
        payload,
    )
    job_id = submit_resp.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ReviewError("Codex service did not return a job id")

    # Poll for completion
    deadline = time.time() + timeout_minutes * 60 + 120
    poll_interval = 5
    job_url = f"{service_url}/v1/codex-audit/jobs/{job_id}"
    while time.time() < deadline:
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 2, 30)
        job_payload = _service_request("GET", job_url, request_github_oidc_token(audience), None)
        status = job_payload.get("status")
        if status == "succeeded":
            output = job_payload.get("output")
            if not isinstance(output, str):
                raise ReviewError("Codex service response missing text output")
            return output.strip()
        if status == "failed":
            error = str(job_payload.get("error") or "unknown failure")
            failure_category = str(job_payload.get("failure_category") or "").strip()
            category_suffix = f" [{failure_category}]" if failure_category else ""
            raise ReviewError(f"Codex service job failed{category_suffix}: {error[:600]}")
        if status not in {"queued", "running"}:
            raise ReviewError(f"Unexpected Codex service status: {status!r}")

    raise ReviewError("Codex service job timed out")


def _service_review_should_fallback(exc: ReviewError) -> bool:
    message = str(exc).lower()
    return any(signal in message for signal in CODEX_SERVICE_FALLBACK_SIGNALS)


def _review_backend_is_unconfigured(exc: ReviewError) -> bool:
    message = str(exc).strip()
    normalized = message.lower()
    return message == NO_REVIEW_BACKEND_CONFIGURED or "oidc repository is not allowed" in normalized


def _review_capacity_is_unavailable(exc: ReviewError) -> bool:
    message = str(exc).lower()
    return (
        "[quota_or_capacity_failure]" in message
        or "usage limit" in message
        or "daily budget exceeded" in message
        or "codex service job failed [unknown_failure]: codex exec failed" in message
    )


def _api_fallback_enabled() -> bool:
    return parse_bool(env_value("CODEX_PR_REVIEW_API_FALLBACK_ENABLED", "true"))


def _direct_api_primary_enabled() -> bool:
    return parse_bool(env_value("CODEX_PR_REVIEW_DIRECT_API_PRIMARY_ENABLED", "true"))


def run_codex_review_with_fallback(
    prompt: str,
    timeout_minutes: int,
    complexity: str = "",
    changed_file_count: int = 0,
    changed_line_count: int = 0,
) -> str:
    service_url = env_value("CODEX_AUDIT_SERVICE_URL")
    service_failure: Exception | None = None
    if service_url:
        try:
            print(f"Running Codex review via service: {service_url}")
            return run_codex_service_review(
                prompt,
                timeout_minutes,
                complexity=complexity,
                changed_file_count=changed_file_count,
                changed_line_count=changed_line_count,
            )
        except ReviewError as exc:
            if not _service_review_should_fallback(exc):
                raise
            service_failure = exc
            print(f"::warning::Codex service review failed: {exc}")
        except (json.JSONDecodeError, OSError, urllib.error.URLError) as exc:
            service_failure = exc
            print(f"::error::Codex service review failed: {exc}")

    if service_failure is not None and not _api_fallback_enabled():
        raise ReviewError(f"Codex service review failed and direct API fallback is disabled: {service_failure}")
    if not service_url and not _direct_api_primary_enabled():
        raise ReviewError(NO_REVIEW_BACKEND_CONFIGURED)

    print("Running Codex review via direct API")
    try:
        return run_direct_api_review(prompt, complexity=complexity)
    except ReviewError as exc:
        if service_failure is not None and _review_backend_is_unconfigured(exc):
            raise ReviewError(
                f"Codex service review failed and no direct API fallback is configured: {service_failure}"
            ) from exc
        raise


def _service_request(
    method: str, url: str, oidc_token: str, payload: dict[str, Any] | None
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {oidc_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "codex-pr-review-client",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"Codex service request failed: {exc.code} {detail[:600]}") from exc
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ReviewError("Codex service returned invalid JSON")
    return result


# ---------------------------------------------------------------------------
# Direct API review (fallback when service is unavailable)
# ---------------------------------------------------------------------------


def run_direct_api_review(prompt: str, complexity: str = "") -> str:
    """Run review directly via Anthropic or OpenAI API."""
    anthropic_key = env_value("ANTHROPIC_API_KEY")
    openai_key = env_value("OPENAI_API_KEY")

    provider_order = [
        "openai",
        "anthropic",
    ]
    normalized = _normalize_complexity(complexity)
    if normalized in (TASK_COMPLEXITY_HIGH, TASK_COMPLEXITY_MEDIUM):
        provider_order = ["anthropic", "openai"]

    for provider in provider_order:
        if provider == "anthropic" and anthropic_key:
            return _run_anthropic_review(
                prompt,
                anthropic_key,
                model=_direct_api_model_for_complexity(provider, normalized),
            )
        if provider == "openai" and openai_key:
            return _run_openai_review(
                prompt,
                openai_key,
                model=_direct_api_model_for_complexity(provider, normalized),
            )

    raise ReviewError(NO_REVIEW_BACKEND_CONFIGURED)


def _run_anthropic_review(prompt: str, api_key: str, model: str = "") -> str:
    model = env_value("ANTHROPIC_MODEL", "claude-sonnet-4-6") if not model else model
    system = "You are a careful code reviewer. Return only the JSON object as specified."
    payload = {
        "model": model,
        "max_tokens": 4000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "codex-pr-review",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"Anthropic API failed: {exc.code} {detail[:600]}") from exc

    content = body.get("content", [])
    if not isinstance(content, list):
        raise ReviewError("Unexpected Anthropic response format")
    text_parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n\n".join(text_parts)


def _run_openai_review(prompt: str, api_key: str, model: str = "") -> str:
    model = env_value("OPENAI_MODEL", "gpt-5.4-mini") if not model else model
    system = "You are a careful code reviewer. Return only the JSON object as specified."
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        f"{env_value('OPENAI_API_BASE_URL', 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-pr-review",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"OpenAI API failed: {exc.code} {detail[:600]}") from exc

    choices = body.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise ReviewError("Unexpected OpenAI response format")
    message = choices[0].get("message", {})
    return str(message.get("content", ""))


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_review_output(
    text: str,
    *,
    require_findings: bool = True,
    required_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Extract the JSON review result from Codex/API output."""
    stripped = text.strip()

    # Try to extract from markdown code fence
    fence_match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE
    )
    if fence_match:
        stripped = fence_match.group(1).strip()

    candidates: list[dict[str, Any]] = []
    try:
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            candidates.append(payload)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(stripped):
            if char != "{":
                continue
            try:
                payload, _end = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                candidates.append(payload)

    for payload in candidates:
        if require_findings and not isinstance(payload.get("findings"), list):
            continue
        if any(key not in payload for key in required_keys):
            continue
        return payload

    if require_findings:
        raise ReviewError("Failed to parse Codex review output with a findings array")
    if required_keys:
        raise ReviewError(
            "Failed to parse Codex review output with required keys: "
            + ", ".join(required_keys)
        )
    raise ReviewError("Failed to parse Codex review output as JSON")


def parse_arbitration_output(
    text: str, *, require_contract_conflict: bool = False
) -> dict[str, Any]:
    """Parse the independent arbiter's constrained verdict."""
    payload = parse_review_output(text, require_findings=False, required_keys=("verdict", "reason"))
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"clear", "block", "ambiguous"}:
        raise ReviewError("Arbitration output verdict must be clear, block, or ambiguous")
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        raise ReviewError("Arbitration output reason is required")
    contract_conflict = payload.get("contract_conflict", False)
    if require_contract_conflict and "contract_conflict" not in payload:
        raise ReviewError("Arbitration output contract_conflict is required")
    if not isinstance(contract_conflict, bool):
        raise ReviewError("Arbitration output contract_conflict must be a boolean")
    result: dict[str, Any] = {
        "verdict": verdict,
        "reason": reason,
    }
    if "contract_conflict" in payload:
        result["contract_conflict"] = contract_conflict
    return result


def blocking_finding_fingerprint(findings: list[dict[str, Any]]) -> str:
    """Return a stable arbitration-candidate identifier despite wording drift."""
    normalized: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        normalized.append(
            {
                "category": str(finding.get("category") or "").strip().lower(),
                "file": str(finding.get("file") or "").strip(),
                "severity": str(finding.get("severity") or "").strip().lower(),
            }
        )
    if not normalized:
        return ""
    payload = json.dumps(sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True)), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def blocking_finding_fingerprints(findings: list[dict[str, Any]]) -> tuple[str, ...]:
    """Return per-finding keys so unrelated findings do not reset arbitration state."""
    return tuple(
        sorted(
            {
                fingerprint
                for finding in findings
                if isinstance(finding, dict)
                for fingerprint in [blocking_finding_fingerprint([finding])]
                if fingerprint
            }
        )
    )


def build_arbitration_prompt(
    *,
    repo: str,
    pr_title: str,
    diff: str,
    findings: list[dict[str, Any]],
    previous_findings: list[dict[str, Any]] | None = None,
    previous_head_sha: str = "",
    history_state: str = "",
) -> str:
    """Ask an independent Codex pass to adjudicate repeated or conflicting findings."""
    diff_limited = _truncate_lines(diff, DEFAULT_MAX_CONTEXT_LINES * 3)
    findings_json = json.dumps(findings, ensure_ascii=False, indent=2)
    previous_findings_json = json.dumps(previous_findings or [], ensure_ascii=False, indent=2)
    return f"""You are the independent Codex review arbiter for a production quantitative codebase.

The primary reviewer raised the current blocking findings below. Compare them with the prior blocking findings when present, then decide whether every current finding remains valid against the cumulative PR diff. Do not defer to either review round.

A contract conflict means that following the current suggestion would reverse or contradict the behavior required by a prior finding for the same file/category/severity. Wording drift that preserves the same required behavior is not a conflict. Unrelated findings are not conflicts.

Treat public interfaces, schemas, tests, and documentation in the base/current source as contract evidence. Use `clear` only when that source of truth proves every current finding false, obsolete, or fixed. Use `block` when a current finding is proven valid. Use `ambiguous` when the source of truth is insufficient. Never clear solely because two reviewers disagree.

When current blocking findings are empty but the trusted history state is `active_blocking_history`, decide whether the prior blocking findings are demonstrably fixed by the current source-of-truth diff. A clean primary review alone is not evidence.

Repository: {repo}
PR title: {pr_title}

## Prior reviewed head
{previous_head_sha or "not available"}

## Trusted history state
{history_state or "normal"}

## Prior blocking findings
{previous_findings_json}

## Current blocking findings
{findings_json}

## Current PR diff
{diff_limited}

Return exactly one JSON object:
{{
  "verdict": "clear" | "block" | "ambiguous",
  "contract_conflict": true | false,
  "reason": "Concrete evidence for the verdict."
}}

Do not discuss style or generic test coverage.
"""


def apply_arbitration_result(
    decision: dict[str, Any], arbitration: dict[str, Any]
) -> dict[str, Any]:
    """Apply an arbiter verdict without allowing contract-conflict remediation churn."""
    result = dict(decision)
    contract_conflict = bool(arbitration.get("contract_conflict"))
    if arbitration.get("verdict") == "clear":
        result["blocked"] = False
        result["cleared_blocking_findings"] = list(
            result.get("blocking_findings") or []
        )
        result["blocking_findings"] = []
        result["summary"] = (
            "✅ **Merge allowed**: blocking findings were cleared by independent Codex arbitration"
        )
    result["contract_conflict"] = contract_conflict
    result["auto_fix_allowed"] = not contract_conflict
    result["next_action"] = (
        "contract_arbitration"
        if contract_conflict
        else "auto_remediation" if result.get("blocked") else "none"
    )
    return result


def apply_arbitration_failure(
    decision: dict[str, Any], error: ReviewError
) -> dict[str, Any]:
    """Fail closed when history-aware arbitration cannot establish the contract."""
    result = dict(decision)
    result.update(
        {
            "blocked": True,
            "contract_conflict": True,
            "auto_fix_allowed": False,
            "next_action": "contract_arbitration",
            "summary": (
                "🚫 **Merge blocked**: contract arbitration failed closed; "
                "automatic remediation is disabled"
            ),
        }
    )
    return result


# ---------------------------------------------------------------------------
# Deterministic blocking-finding identity
# ---------------------------------------------------------------------------


def _normalize_identity_text(value: Any, field: str) -> str:
    if type(value) is not str:
        raise ReviewError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or len(normalized) > FINDING_HISTORY_TEXT_LIMIT:
        raise ReviewError(f"{field} is empty or exceeds its safe bound")
    return normalized


def _canonical_repo_path(value: Any) -> str:
    if type(value) is not str:
        raise ReviewError("repository path must be a string")
    path = value.strip()
    if (
        not path
        or path != value
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or re.search(r"[\x00-\x1f\x7f]", path)
        or len(path) > 300
    ):
        raise ReviewError("repository path is not canonical")
    return path


def _repository_file_lines(repo_root: Path, path: str) -> list[str]:
    root = repo_root.resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReviewError("repository evidence escapes the review root") from exc
    if not candidate.is_file():
        raise ReviewError("repository evidence file does not exist")
    try:
        return candidate.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReviewError("repository evidence cannot be read as UTF-8") from exc


def _repository_line(repo_root: Path, path: str, line: int) -> str:
    if type(line) is not int or line <= 0:
        raise ReviewError("repository evidence line must be a positive integer")
    lines = _repository_file_lines(repo_root, path)
    if line > len(lines):
        raise ReviewError("repository evidence line does not exist")
    return lines[line - 1]


def _changed_right_side_lines(
    changed_files: list[dict[str, Any]],
) -> tuple[dict[str, dict[int, str]], set[str]]:
    changed: dict[str, dict[int, str]] = {}
    invalid_paths: set[str] = set()
    hunk_pattern = re.compile(
        r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
    )
    for file_record in changed_files:
        if not isinstance(file_record, dict):
            continue
        try:
            path = _canonical_repo_path(file_record.get("filename"))
        except ReviewError:
            continue
        patch = file_record.get("patch")
        if type(patch) is not str or not patch:
            invalid_paths.add(path)
            continue
        right_lines: dict[int, str] = {}
        right_line: int | None = None
        expected_old = expected_new = seen_old = seen_new = 0
        valid_hunk = False
        invalid_patch = False
        for patch_line in patch.splitlines():
            hunk_match = hunk_pattern.match(patch_line)
            if hunk_match:
                if valid_hunk and (seen_old != expected_old or seen_new != expected_new):
                    invalid_patch = True
                    break
                expected_old = int(hunk_match.group(1) or "1")
                right_line = int(hunk_match.group(2))
                expected_new = int(hunk_match.group(3) or "1")
                seen_old = seen_new = 0
                valid_hunk = True
                continue
            if right_line is None:
                continue
            if patch_line.startswith("+"):
                right_lines[right_line] = patch_line[1:]
                right_line += 1
                seen_new += 1
            elif patch_line.startswith("-"):
                seen_old += 1
            elif patch_line.startswith("\\"):
                continue
            elif patch_line.startswith(" "):
                right_line += 1
                seen_old += 1
                seen_new += 1
            else:
                invalid_patch = True
                break
        if valid_hunk and (seen_old != expected_old or seen_new != expected_new):
            invalid_patch = True
        if not valid_hunk or invalid_patch:
            invalid_paths.add(path)
            continue
        changed[path] = right_lines
    return changed, invalid_paths


def _python_enclosing_callable(
    repo_root: Path, path: str, line: int
) -> tuple[str, set[int]] | None:
    if not path.endswith(".py"):
        return None
    try:
        source = "\n".join(_repository_file_lines(repo_root, path)) + "\n"
        tree = ast.parse(source)
    except (ReviewError, SyntaxError):
        return None
    candidates: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end_line = getattr(node, "end_lineno", None)
        if isinstance(end_line, int) and node.lineno <= line <= end_line:
            candidates.append(node)
    if not candidates:
        return None
    node = min(candidates, key=lambda item: (int(item.end_lineno or item.lineno) - item.lineno, -item.lineno))
    decorator_lines = {
        decorator_line
        for decorator in node.decorator_list
        for decorator_line in range(
            decorator.lineno, int(getattr(decorator, "end_lineno", decorator.lineno)) + 1
        )
    }
    return node.name, decorator_lines


def _canonical_finding_evidence(
    value: Any,
    *,
    repo_root: Path,
    finding_path: str,
    finding_line: int,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"kind", "path", "line", "symbol"}:
        raise ReviewError("finding evidence must use the exact bounded schema")
    kind = str(value.get("kind") or "").strip()
    if kind not in EVIDENCE_KINDS:
        raise ReviewError("finding evidence kind is unsupported")
    path = _canonical_repo_path(value.get("path"))
    line = value.get("line")
    if type(line) is not int or line <= 0:
        raise ReviewError("finding evidence line must be a positive integer")
    symbol = value.get("symbol")
    if type(symbol) is not str or not EVIDENCE_SYMBOL_PATTERN.fullmatch(symbol):
        raise ReviewError("finding evidence symbol is invalid")

    enclosing = _python_enclosing_callable(repo_root, finding_path, finding_line)
    if enclosing is None or enclosing[0] != symbol:
        raise ReviewError("finding evidence is not linked to the changed callable")
    source_line = _repository_line(repo_root, path, line)
    if kind == "current_caller":
        if path.startswith(("tests/", "docs/")) or path.endswith((".md", ".rst")):
            raise ReviewError("finding evidence is not a production caller")
        if re.match(r"^\s*(?:async\s+)?def\s+", source_line) or not re.search(
            rf"\b{re.escape(symbol)}\s*\(", source_line
        ):
            raise ReviewError("finding evidence is not a repository caller")
    else:
        boundary_tokens = re.compile(
            r"\b(route|get|post|put|patch|delete|websocket|endpoint|webhook|command)\b",
            flags=re.IGNORECASE,
        )
        explicitly_declared = "public-untrusted-boundary" in source_line.casefold()
        if not (
            path == finding_path
            and line in enclosing[1]
            and (boundary_tokens.search(source_line) or explicitly_declared)
        ):
            raise ReviewError("finding evidence is not an explicit public boundary")

    normalized_source = unicodedata.normalize("NFKC", source_line).strip()
    return {
        "kind": kind,
        "path": path,
        "line": line,
        "symbol": symbol,
        "source_sha256": hashlib.sha256(normalized_source.encode("utf-8")).hexdigest(),
    }


def _review_finding_identity_payload(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "review_finding_id.v1",
        "reviewed_head_sha": finding["reviewed_head_sha"],
        "severity": finding["severity"],
        "category": finding["category"],
        "file": finding["file"],
        "line": finding["line"],
        "evidence": finding["evidence"],
        "description_sha256": finding["description_sha256"],
        "impact_sha256": finding["impact_sha256"],
    }


def _compute_review_finding_id(finding: dict[str, Any]) -> str:
    payload = json.dumps(
        _review_finding_identity_payload(finding),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_blocking_finding(
    finding: dict[str, Any],
    *,
    reviewed_head_sha: str,
    repo_root: Path,
    changed_lines: dict[str, dict[int, str]],
    invalid_patch_paths: set[str],
) -> dict[str, Any]:
    if not FULL_HEAD_SHA_PATTERN.fullmatch(reviewed_head_sha):
        raise ReviewError("reviewed head SHA is invalid")
    severity = str(finding.get("severity") or "").strip().lower()
    if severity not in BLOCK_SEVERITIES:
        raise ReviewError("blocking finding severity is invalid")
    category = str(finding.get("category") or "").strip().lower()
    if category not in FINDING_CATEGORIES:
        raise ReviewError("blocking finding category is invalid")
    path = _canonical_repo_path(finding.get("file"))
    line = finding.get("line")
    if type(line) is not int or line <= 0:
        raise ReviewError("blocking finding line must be a positive integer")
    if path in invalid_patch_paths or line not in changed_lines.get(path, {}):
        raise ReviewError("blocking finding line is not a changed right-side patch line")
    if _repository_line(repo_root, path, line) != changed_lines[path][line]:
        raise ReviewError("blocking finding line does not match the reviewed checkout")
    description = _normalize_identity_text(finding.get("description"), "description")
    impact = _normalize_identity_text(finding.get("impact"), "impact")
    evidence = _canonical_finding_evidence(
        finding.get("evidence"),
        repo_root=repo_root,
        finding_path=path,
        finding_line=line,
    )
    canonical = {
        "reviewed_head_sha": reviewed_head_sha,
        "severity": severity,
        "category": category,
        "file": path,
        "line": line,
        "description": _sanitize_history_text(description),
        "impact": _sanitize_history_text(impact),
        "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
        "impact_sha256": hashlib.sha256(impact.encode("utf-8")).hexdigest(),
        "evidence": evidence,
    }
    canonical["review_finding_id"] = _compute_review_finding_id(canonical)
    return {
        **canonical,
        "suggestion": _sanitize_history_text(finding.get("suggestion")),
    }


def _safe_reported_finding(finding: dict[str, Any]) -> dict[str, Any]:
    line = finding.get("line")
    return {
        "severity": _sanitize_history_text(finding.get("severity"), 20).lower(),
        "category": _sanitize_history_text(finding.get("category"), 80).lower(),
        "file": _sanitize_history_path(finding.get("file")),
        "line": line if type(line) is int and line > 0 else None,
        "description": _sanitize_history_text(finding.get("description")),
        "impact": _sanitize_history_text(finding.get("impact")),
        "suggestion": _sanitize_history_text(finding.get("suggestion")),
    }


def _active_review_finding(finding: dict[str, Any]) -> dict[str, Any]:
    required = {
        "review_finding_id",
        "reviewed_head_sha",
        "severity",
        "category",
        "file",
        "line",
        "description",
        "impact",
        "description_sha256",
        "impact_sha256",
        "evidence",
    }
    if not isinstance(finding, dict) or not required.issubset(finding):
        raise ReviewError("active review finding is incomplete")
    canonical = {key: finding[key] for key in required}
    finding_id = canonical["review_finding_id"]
    if type(finding_id) is not str or not REVIEW_FINDING_ID_PATTERN.fullmatch(finding_id):
        raise ReviewError("active review finding ID is invalid")
    if not FULL_HEAD_SHA_PATTERN.fullmatch(str(canonical["reviewed_head_sha"])):
        raise ReviewError("active review finding head SHA is invalid")
    if canonical["severity"] not in BLOCK_SEVERITIES:
        raise ReviewError("active review finding severity is invalid")
    if canonical["category"] not in FINDING_CATEGORIES:
        raise ReviewError("active review finding category is invalid")
    _canonical_repo_path(canonical["file"])
    if type(canonical["line"]) is not int or canonical["line"] <= 0:
        raise ReviewError("active review finding line is invalid")
    for text_field in ("description", "impact"):
        if type(canonical[text_field]) is not str or len(canonical[text_field]) > FINDING_HISTORY_TEXT_LIMIT:
            raise ReviewError("active review finding text is invalid")
    for digest_field in ("description_sha256", "impact_sha256"):
        if type(canonical[digest_field]) is not str or not REVIEW_FINDING_ID_PATTERN.fullmatch(
            canonical[digest_field]
        ):
            raise ReviewError("active review finding digest is invalid")
    evidence = canonical["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "kind",
        "path",
        "line",
        "symbol",
        "source_sha256",
    }:
        raise ReviewError("active review finding evidence is invalid")
    if evidence["kind"] not in EVIDENCE_KINDS:
        raise ReviewError("active review finding evidence kind is invalid")
    _canonical_repo_path(evidence["path"])
    if type(evidence["line"]) is not int or evidence["line"] <= 0:
        raise ReviewError("active review finding evidence line is invalid")
    if type(evidence["symbol"]) is not str or not EVIDENCE_SYMBOL_PATTERN.fullmatch(
        evidence["symbol"]
    ):
        raise ReviewError("active review finding evidence symbol is invalid")
    if type(evidence["source_sha256"]) is not str or not REVIEW_FINDING_ID_PATTERN.fullmatch(
        evidence["source_sha256"]
    ):
        raise ReviewError("active review finding evidence digest is invalid")
    if _compute_review_finding_id(canonical) != finding_id:
        raise ReviewError("active review finding identity does not match its payload")
    return canonical


def _validated_disposition_record(record: Any) -> dict[str, str]:
    if not isinstance(record, dict) or set(record) != {"head_sha", "review_finding_id"}:
        raise ReviewError("review disposition record is invalid")
    head_sha = record.get("head_sha")
    finding_id = record.get("review_finding_id")
    if type(head_sha) is not str or not FULL_HEAD_SHA_PATTERN.fullmatch(head_sha):
        raise ReviewError("review disposition head SHA is invalid")
    if type(finding_id) is not str or not REVIEW_FINDING_ID_PATTERN.fullmatch(finding_id):
        raise ReviewError("review disposition finding ID is invalid")
    return {"head_sha": head_sha, "review_finding_id": finding_id}


def reconcile_active_review_findings(
    previous_findings: list[dict[str, Any]],
    current_findings: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge exact active IDs and remove only authenticated exact dispositions."""
    active: dict[str, dict[str, Any]] = {}
    for finding in [*previous_findings, *current_findings]:
        canonical = _active_review_finding(finding)
        active[canonical["review_finding_id"]] = canonical
    disposed = {
        (record["head_sha"], record["review_finding_id"])
        for record in (_validated_disposition_record(item) for item in dispositions)
    }
    return [
        active[finding_id]
        for finding_id in sorted(active)
        if (active[finding_id]["reviewed_head_sha"], finding_id) not in disposed
    ]


# ---------------------------------------------------------------------------
# Findings evaluation
# ---------------------------------------------------------------------------


def evaluate_findings(
    findings: list[dict[str, Any]],
    changed_files: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    reviewed_head_sha: str = "",
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate Codex findings against the risk policy.

    Returns a decision dict with:
    - blocked: whether merge should be blocked
    - blocking_findings: findings that cause blocking
    - non_blocking_findings: findings that are reported but don't block
    - risk_summary: human-readable summary
    """
    blocking: list[dict[str, Any]] = []
    non_blocking: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    file_risk_cache: dict[str, tuple[str, str]] = {}
    deterministic_validation = bool(reviewed_head_sha or repo_root is not None)
    changed_lines: dict[str, dict[int, str]] = {}
    invalid_patch_paths: set[str] = set()
    if deterministic_validation:
        changed_lines, invalid_patch_paths = _changed_right_side_lines(changed_files)

    # Build a set of changed file paths
    changed_paths: set[str] = set()
    file_statuses: dict[str, str] = {}
    for f in changed_files:
        path = f.get("filename", "").strip()
        if path:
            changed_paths.add(path)
            file_statuses[path] = f.get("status", "")

    for finding in findings:
        if not isinstance(finding, dict):
            continue

        severity = str(finding.get("severity", "")).strip().lower()
        file_path = str(finding.get("file", "")).strip()

        # Classify the file's risk level
        if file_path not in file_risk_cache:
            file_risk_cache[file_path] = classify_file_risk(file_path, policy)
        file_risk, file_risk_reason = file_risk_cache[file_path]

        # Determine if this finding should block
        should_block = (
            severity in BLOCK_SEVERITIES
            and file_risk == "high"
            and file_path in changed_paths  # only block on actually changed files
        )

        enriched = {
            **_safe_reported_finding(finding),
            "file_risk": file_risk,
            "file_risk_reason": file_risk_reason,
        }

        if should_block and deterministic_validation:
            try:
                validated = _validated_blocking_finding(
                    finding,
                    reviewed_head_sha=reviewed_head_sha,
                    repo_root=(repo_root or ROOT).resolve(),
                    changed_lines=changed_lines,
                    invalid_patch_paths=invalid_patch_paths,
                )
            except ReviewError as exc:
                invalid.append(
                    {
                        **enriched,
                        "validation_error": str(exc),
                    }
                )
            else:
                blocking.append(
                    {
                        **validated,
                        "file_risk": file_risk,
                        "file_risk_reason": file_risk_reason,
                    }
                )
        elif should_block:
            blocking.append(enriched)
        else:
            non_blocking.append(enriched)

    blocked = bool(blocking or invalid)

    # Build summary
    all_findings = blocking + non_blocking
    summary_parts = []
    if invalid:
        summary_parts.append(
            "🚫 **Merge blocked**: high/critical finding metadata could not be "
            "validated deterministically"
        )
    elif blocked:
        summary_parts.append(
            f"🚫 **Merge blocked**: {len(blocking)} serious issue(s) found in high-risk files"
        )
    elif all_findings:
        total = len(all_findings)
        summary_parts.append(
            f"✅ **Merge allowed**: {total} finding(s) reported but none are blocking"
        )
    else:
        summary_parts.append("✅ **Merge allowed**: No issues found")

    return {
        "blocked": blocked,
        "blocking_findings": blocking,
        "non_blocking_findings": non_blocking,
        "invalid_findings": invalid,
        "review_validation_failed": bool(invalid),
        "total_findings": len(all_findings) + len(invalid),
        "summary": "\n\n".join(summary_parts),
        "contract_conflict": bool(invalid),
        "auto_fix_allowed": not invalid,
        "next_action": "contract_arbitration" if invalid else "auto_remediation" if blocked else "none",
    }


# ---------------------------------------------------------------------------
# PR comment
# ---------------------------------------------------------------------------


def _sanitize_history_text(value: Any, limit: int = FINDING_HISTORY_TEXT_LIMIT) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    text = re.sub(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", "[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b", "[REDACTED]", text)
    text = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "[REDACTED]", text)
    text = re.sub(r"\bglpat-[A-Za-z0-9_-]{10,}\b", "[REDACTED]", text)
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[REDACTED]",
        text,
    )
    text = re.sub(
        r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@[^\s]+",
        "[REDACTED_DSN]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?i)\b(token|secret|password|api[ _-]?key|authorization|cookie)\b\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b", "[REDACTED]", text)
    text = re.sub(
        r"(?<!\S)(?=\S*[A-Za-z])(?=\S*\d)\S{8,39}(?!\S)",
        "[REDACTED]",
        text,
    )
    text = re.sub(r"\b[A-Za-z0-9_+/=-]{40,}\b", "[REDACTED]", text)
    return text[:limit]


def _history_finding(finding: dict[str, Any]) -> dict[str, str]:
    return {
        "severity": _sanitize_history_text(finding.get("severity"), 20).lower(),
        "category": _sanitize_history_text(finding.get("category"), 80).lower(),
        "file": _sanitize_history_path(finding.get("file")),
        "description": _sanitize_history_text(finding.get("description")),
        "suggestion": _sanitize_history_text(finding.get("suggestion")),
    }


def _sanitize_history_path(value: Any) -> str:
    """Preserve stable PR paths while excluding control characters and excess length."""
    return re.sub(r"[\x00-\x1f\x7f]+", "", str(value or "")).strip()[:300]


def _encode_bounded_marker(payload: dict[str, Any], max_bytes: int) -> str:
    raw = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(raw) > max_bytes:
        raise ReviewError("machine metadata exceeds its safe bound")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_bounded_marker(encoded: str, max_bytes: int, max_encoded_bytes: int) -> Any:
    if not encoded or len(encoded) > max_encoded_bytes:
        raise ReviewError("machine metadata exceeds its safe bound")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        if len(raw) > max_bytes:
            raise ReviewError("machine metadata exceeds its safe bound")
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ReviewError("machine metadata is malformed") from exc


def build_review_state_marker(
    decision: dict[str, Any],
    *,
    reviewed_head_sha: str,
    active_findings: list[dict[str, Any]],
    applied_dispositions: list[dict[str, Any]] | None = None,
    blocking_streak: int = 0,
    finding_fingerprint: str = "",
    finding_fingerprints: tuple[str, ...] = (),
    state_status: str = "normal",
) -> str:
    """Build the sole bounded machine-state block for new review comments."""
    if state_status not in {"normal", "invalid"}:
        raise ReviewError("review state status is invalid")
    if not FULL_HEAD_SHA_PATTERN.fullmatch(reviewed_head_sha):
        raise ReviewError("review state head SHA is invalid")
    if type(blocking_streak) is not int or blocking_streak < 0:
        raise ReviewError("review state blocking streak is invalid")
    if len(active_findings) > REVIEW_STATE_MAX_FINDINGS:
        raise ReviewError("review state has too many active findings")
    active = [_active_review_finding(finding) for finding in active_findings]
    dispositions = [
        _validated_disposition_record(record)
        for record in (applied_dispositions or [])
    ]
    if len(dispositions) > REVIEW_STATE_MAX_FINDINGS:
        raise ReviewError("review state has too many dispositions")
    if len({item["review_finding_id"] for item in active}) != len(active):
        raise ReviewError("review state has duplicate active findings")
    if len({(item["head_sha"], item["review_finding_id"]) for item in dispositions}) != len(
        dispositions
    ):
        raise ReviewError("review state has duplicate dispositions")
    if type(finding_fingerprint) is not str or (
        finding_fingerprint
        and not re.fullmatch(r"[0-9a-f]{20}|[0-9a-f]{64}", finding_fingerprint)
    ):
        raise ReviewError("review state fingerprint is invalid")
    fingerprints = list(finding_fingerprints)
    if any(
        type(item) is not str
        or not re.fullmatch(r"[0-9a-f]{20}|[0-9a-f]{64}", item)
        for item in fingerprints
    ):
        raise ReviewError("review state fingerprints are invalid")
    contract_conflict = decision.get("contract_conflict", False)
    auto_fix_allowed = decision.get("auto_fix_allowed", False)
    next_action = decision.get("next_action", "none")
    if type(contract_conflict) is not bool or type(auto_fix_allowed) is not bool:
        raise ReviewError("review state decision flags are invalid")
    if type(next_action) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", next_action):
        raise ReviewError("review state next action is invalid")
    payload = {
        "version": 2,
        "status": state_status,
        "reviewed_head_sha": reviewed_head_sha,
        "blocking_streak": blocking_streak,
        "finding_fingerprint": finding_fingerprint,
        "finding_fingerprints": sorted(set(fingerprints)),
        "active_findings": sorted(active, key=lambda item: item["review_finding_id"]),
        "applied_dispositions": sorted(
            dispositions, key=lambda item: (item["head_sha"], item["review_finding_id"])
        ),
        "contract_conflict": contract_conflict,
        "auto_fix_allowed": auto_fix_allowed,
        "next_action": next_action,
        "implementation_digest": review_implementation_digest(),
    }
    encoded = _encode_bounded_marker(payload, REVIEW_STATE_MAX_BYTES)
    return f"{REVIEW_STATE_MARKER_PREFIX}{encoded}{REVIEW_STATE_MARKER_SUFFIX}"


def parse_review_machine_state(body: str) -> tuple[dict[str, Any] | None, bool]:
    """Parse only the dedicated second-line state block; prose is never control data."""
    lines = (body or "").splitlines()
    if len(lines) < 2 or lines[0] != "<!-- codex-pr-review -->":
        return None, REVIEW_STATE_MARKER_PREFIX not in (body or "")
    match = re.fullmatch(
        rf"{re.escape(REVIEW_STATE_MARKER_PREFIX)}([A-Za-z0-9_-]+)"
        rf"{re.escape(REVIEW_STATE_MARKER_SUFFIX)}",
        lines[1],
    )
    if not match:
        return None, not lines[1].startswith(REVIEW_STATE_MARKER_PREFIX)
    try:
        payload = _decode_bounded_marker(
            match.group(1), REVIEW_STATE_MAX_BYTES, REVIEW_STATE_MAX_ENCODED_BYTES
        )
        required_keys = {
            "version",
            "status",
            "reviewed_head_sha",
            "blocking_streak",
            "finding_fingerprint",
            "finding_fingerprints",
            "active_findings",
            "applied_dispositions",
            "contract_conflict",
            "auto_fix_allowed",
            "next_action",
            "implementation_digest",
        }
        if not isinstance(payload, dict) or set(payload) != required_keys:
            raise ReviewError("review state schema is invalid")
        if payload["version"] != 2 or payload["status"] not in {"normal", "invalid"}:
            raise ReviewError("review state version or status is invalid")
        if type(payload["reviewed_head_sha"]) is not str or not FULL_HEAD_SHA_PATTERN.fullmatch(
            payload["reviewed_head_sha"]
        ):
            raise ReviewError("review state head SHA is invalid")
        if type(payload["blocking_streak"]) is not int or payload["blocking_streak"] < 0:
            raise ReviewError("review state blocking streak is invalid")
        if type(payload["finding_fingerprint"]) is not str or (
            payload["finding_fingerprint"]
            and not re.fullmatch(r"[0-9a-f]{20}|[0-9a-f]{64}", payload["finding_fingerprint"])
        ):
            raise ReviewError("review state fingerprint is invalid")
        fingerprints = payload["finding_fingerprints"]
        if not isinstance(fingerprints, list) or len(fingerprints) > REVIEW_STATE_MAX_FINDINGS:
            raise ReviewError("review state fingerprints are invalid")
        if any(
            type(item) is not str
            or not re.fullmatch(r"[0-9a-f]{20}|[0-9a-f]{64}", item)
            for item in fingerprints
        ):
            raise ReviewError("review state fingerprints are invalid")
        if fingerprints != sorted(set(fingerprints)):
            raise ReviewError("review state fingerprints are not canonical")
        active = payload["active_findings"]
        if not isinstance(active, list) or len(active) > REVIEW_STATE_MAX_FINDINGS:
            raise ReviewError("review state active findings are invalid")
        payload["active_findings"] = [_active_review_finding(item) for item in active]
        active_ids = [item["review_finding_id"] for item in payload["active_findings"]]
        if active_ids != sorted(set(active_ids)):
            raise ReviewError("review state active findings are not canonical")
        dispositions = payload["applied_dispositions"]
        if not isinstance(dispositions, list) or len(dispositions) > REVIEW_STATE_MAX_FINDINGS:
            raise ReviewError("review state dispositions are invalid")
        payload["applied_dispositions"] = [
            _validated_disposition_record(item) for item in dispositions
        ]
        disposition_ids = [
            (item["head_sha"], item["review_finding_id"])
            for item in payload["applied_dispositions"]
        ]
        if disposition_ids != sorted(set(disposition_ids)):
            raise ReviewError("review state dispositions are not canonical")
        if type(payload["contract_conflict"]) is not bool or type(
            payload["auto_fix_allowed"]
        ) is not bool:
            raise ReviewError("review state decision flags are invalid")
        if type(payload["next_action"]) is not str or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", payload["next_action"]
        ):
            raise ReviewError("review state next action is invalid")
        if type(payload["implementation_digest"]) is not str or not re.fullmatch(
            r"[0-9a-f]{24}", payload["implementation_digest"]
        ):
            raise ReviewError("review state implementation digest is invalid")
    except ReviewError:
        return None, False
    return payload, True


def build_review_disposition_marker(head_sha: str, review_finding_id: str) -> str:
    record = _validated_disposition_record(
        {"head_sha": head_sha, "review_finding_id": review_finding_id}
    )
    payload = {
        "schema": "review_finding_disposition.v1",
        **record,
        "disposition": "clear",
    }
    encoded = _encode_bounded_marker(payload, REVIEW_DISPOSITION_MAX_BYTES)
    return (
        f"{REVIEW_DISPOSITION_MARKER_PREFIX}{encoded}"
        f"{REVIEW_DISPOSITION_MARKER_SUFFIX}"
    )


def _parse_review_disposition_marker(body: str) -> dict[str, str]:
    match = re.fullmatch(
        rf"{re.escape(REVIEW_DISPOSITION_MARKER_PREFIX)}([A-Za-z0-9_-]+)"
        rf"{re.escape(REVIEW_DISPOSITION_MARKER_SUFFIX)}",
        body or "",
    )
    if not match:
        raise ReviewError("review disposition must be a dedicated anchored record")
    payload = _decode_bounded_marker(
        match.group(1),
        REVIEW_DISPOSITION_MAX_BYTES,
        REVIEW_DISPOSITION_MAX_ENCODED_BYTES,
    )
    expected_keys = {"schema", "head_sha", "review_finding_id", "disposition"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ReviewError("review disposition schema is invalid")
    if (
        payload.get("schema") != "review_finding_disposition.v1"
        or payload.get("disposition") != "clear"
    ):
        raise ReviewError("review disposition version or action is invalid")
    return _validated_disposition_record(
        {
            "head_sha": payload["head_sha"],
            "review_finding_id": payload["review_finding_id"],
        }
    )


def extract_trusted_review_dispositions(
    reviews: Any, active_findings: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Accept exact records only from authenticated repository-authority reviews."""
    if not isinstance(reviews, list):
        raise ReviewError("repository review authority metadata is malformed")
    active = {
        item["review_finding_id"]: item
        for item in (_active_review_finding(finding) for finding in active_findings)
    }
    dispositions: dict[tuple[str, str], dict[str, str]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise ReviewError("repository review authority metadata is malformed")
        body = review.get("body")
        if type(body) is not str or REVIEW_DISPOSITION_MARKER_PREFIX not in body:
            continue
        association = str(review.get("author_association") or "").strip().upper()
        if association not in REVIEW_AUTHORITY_ASSOCIATIONS:
            continue
        user = review.get("user")
        if (
            not isinstance(user, dict)
            or type(user.get("id")) is not int
            or user["id"] <= 0
            or type(user.get("login")) is not str
            or not user["login"].strip()
            or str(user.get("type") or "").strip() != "User"
            or type(review.get("id")) is not int
            or review["id"] <= 0
            or type(review.get("submitted_at")) is not str
            or not review["submitted_at"].strip()
            or str(review.get("state") or "").upper() not in {"APPROVED", "COMMENTED"}
        ):
            raise ReviewError("repository review authority metadata is unverifiable")
        record = _parse_review_disposition_marker(body)
        commit_id = str(review.get("commit_id") or "").strip().lower()
        finding = active.get(record["review_finding_id"])
        if (
            not FULL_HEAD_SHA_PATTERN.fullmatch(commit_id)
            or commit_id != record["head_sha"]
            or finding is None
            or finding["reviewed_head_sha"] != record["head_sha"]
        ):
            raise ReviewError("review disposition is not anchored to an exact active finding")
        dispositions[(record["head_sha"], record["review_finding_id"])] = record
    return [dispositions[key] for key in sorted(dispositions)]


def fetch_trusted_review_dispositions(
    token: str,
    repo: str,
    pr_number: int,
    active_findings: list[dict[str, Any]],
) -> list[dict[str, str]]:
    reviews: list[dict[str, Any]] = []
    for page in range(1, REVIEW_DISPOSITION_MAX_PAGES + 1):
        payload = github_request(
            token,
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/reviews?per_page=100&page={page}",
        )
        if not isinstance(payload, list):
            raise ReviewError("repository review authority metadata is malformed")
        reviews.extend(payload)
        if len(payload) < 100:
            return extract_trusted_review_dispositions(reviews, active_findings)
    raise ReviewError("repository review authority metadata exceeds its safe bound")


def build_finding_history_marker(
    history: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    head_sha: str,
    *,
    status: str | None = None,
) -> str:
    """Serialize a bounded, sanitized blocking-finding history into a bot marker."""
    sanitized_findings = [_history_finding(finding) for finding in findings if isinstance(finding, dict)]
    will_append = bool(sanitized_findings or status)
    history_limit = FINDING_HISTORY_MAX_ROUNDS - 1 if will_append else FINDING_HISTORY_MAX_ROUNDS
    rounds = list(history[-history_limit:])
    normalized_head_sha = str(head_sha or "").strip().lower()
    if will_append:
        if not re.fullmatch(r"[0-9a-f]{7,64}", normalized_head_sha):
            return build_invalid_finding_history_marker(normalized_head_sha)
        rounds.append(
            {
                "head_sha": normalized_head_sha,
                "findings": sanitized_findings,
                "status": status or "blocking",
            }
        )
    payload = {"version": 1, "rounds": rounds[-FINDING_HISTORY_MAX_ROUNDS:]}
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    while len(raw) > FINDING_HISTORY_MAX_BYTES and len(rounds) > 1:
        rounds.pop(0)
        payload["rounds"] = rounds
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > FINDING_HISTORY_MAX_BYTES:
        overflow_head_sha = normalized_head_sha or str(rounds[-1].get("head_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{7,64}", overflow_head_sha):
            return build_invalid_finding_history_marker(overflow_head_sha)
        overflow_payload = {
            "version": 1,
            "rounds": [
                {
                    "head_sha": overflow_head_sha,
                    "findings": [],
                    "status": "overflow",
                }
            ],
        }
        raw = json.dumps(overflow_payload, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{FINDING_HISTORY_MARKER_PREFIX}{encoded}{FINDING_HISTORY_MARKER_SUFFIX}"


def build_invalid_finding_history_marker(head_sha: str = "") -> str:
    normalized_head_sha = str(head_sha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{7,64}", normalized_head_sha):
        payload = {
            "version": 1,
            "rounds": [
                {
                    "head_sha": normalized_head_sha,
                    "findings": [],
                    "status": "invalid_history",
                }
            ],
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    else:
        raw = b'{"version":1,"invalid":true}'
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{FINDING_HISTORY_MARKER_PREFIX}{encoded}{FINDING_HISTORY_MARKER_SUFFIX}"


def _legacy_machine_metadata(body: str) -> str:
    stripped = (body or "").strip()
    if "\n" not in stripped:
        return stripped
    lines = stripped.splitlines()
    if not lines or lines[0] != "<!-- codex-pr-review -->":
        return ""
    metadata: list[str] = []
    for line in lines[1:]:
        if line.startswith("## "):
            break
        metadata.append(line)
    return "\n".join(metadata)


def parse_finding_history(body: str) -> tuple[list[dict[str, Any]], bool]:
    """Recover trusted history; legacy absence is valid, malformed state fails closed."""
    metadata = _legacy_machine_metadata(body)
    if FINDING_HISTORY_MARKER_PREFIX not in metadata:
        return [], True
    match = re.search(
        rf"(?m)^{re.escape(FINDING_HISTORY_MARKER_PREFIX)}([A-Za-z0-9_-]+)"
        rf"{re.escape(FINDING_HISTORY_MARKER_SUFFIX)}$",
        metadata,
    )
    if not match:
        return [], False
    encoded = match.group(1)
    if len(encoded) > FINDING_HISTORY_MAX_ENCODED_BYTES:
        return [], False
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        if len(raw) > FINDING_HISTORY_MAX_BYTES:
            return [], False
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return [], False
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return [], False
    rounds = payload.get("rounds")
    if not isinstance(rounds, list) or len(rounds) > FINDING_HISTORY_MAX_ROUNDS:
        return [], False
    validated: list[dict[str, Any]] = []
    field_limits = {
        "severity": 20,
        "category": 80,
        "file": 300,
        "description": FINDING_HISTORY_TEXT_LIMIT,
        "suggestion": FINDING_HISTORY_TEXT_LIMIT,
    }
    for round_state in rounds:
        if not isinstance(round_state, dict):
            return [], False
        head_sha = round_state.get("head_sha")
        findings = round_state.get("findings")
        status = round_state.get("status", "blocking")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{7,64}", head_sha):
            return [], False
        if not isinstance(findings, list):
            return [], False
        if status not in {"blocking", "clear", "cleared", "overflow", "invalid_history"}:
            return [], False
        if set(round_state) not in ({"head_sha", "findings"}, {"head_sha", "findings", "status"}):
            return [], False
        checked_findings: list[dict[str, str]] = []
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != set(field_limits):
                return [], False
            if any(
                not isinstance(finding[field], str) or len(finding[field]) > limit
                for field, limit in field_limits.items()
            ):
                return [], False
            checked_findings.append(dict(finding))
        validated.append(
            {"head_sha": head_sha, "findings": checked_findings, "status": status}
        )
    return validated, True


def previous_matching_findings(
    history: list[dict[str, Any]], current_findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return the latest unresolved historical finding for every current key."""
    current_keys = set(blocking_finding_fingerprints(current_findings))
    matched: dict[str, dict[str, Any]] = {}
    resolved: set[str] = set()
    for round_state in reversed(history):
        prior = round_state.get("findings")
        if not isinstance(prior, list):
            continue
        status = round_state.get("status", "blocking")
        if status in {"clear", "cleared"} and not prior:
            break
        for finding in prior:
            if not isinstance(finding, dict):
                continue
            key = blocking_finding_fingerprint([finding])
            if key not in current_keys or key in resolved or key in matched:
                continue
            if status in {"clear", "cleared"}:
                resolved.add(key)
                continue
            matched[key] = {
                **finding,
                "history_head_sha": str(round_state.get("head_sha") or ""),
            }
        if current_keys.issubset(resolved | set(matched)):
            break
    return [matched[key] for key in sorted(matched)]


def previous_matching_round(
    history: list[dict[str, Any]], current_findings: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Compatibility wrapper returning aggregated matches and their newest head."""
    findings = previous_matching_findings(history, current_findings)
    if not findings:
        return None
    heads = [str(finding.get("history_head_sha") or "") for finding in findings]
    return {"head_sha": next((head for head in heads if head), ""), "findings": findings}


def unresolved_history_findings(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate every latest unresolved finding key in the bounded history."""
    all_findings = [
        finding
        for round_state in history
        for finding in (round_state.get("findings") or [])
        if isinstance(finding, dict)
    ]
    return previous_matching_findings(history, all_findings)


def has_active_blocking_history(history: list[dict[str, Any]]) -> bool:
    if not history:
        return False
    latest = history[-1]
    status = latest.get("status", "blocking")
    return finding_history_requires_confirmation(history) or (
        status == "blocking" and bool(latest.get("findings"))
    )


def finding_history_requires_confirmation(history: list[dict[str, Any]]) -> bool:
    return bool(
        history
        and history[-1].get("status") in {"overflow", "invalid_history"}
    )


def _sanitize_comment_text(value: Any, limit: int = FINDING_HISTORY_TEXT_LIMIT) -> str:
    return (
        _sanitize_history_text(value, limit)
        .replace("<!--", "&lt;!--")
        .replace("-->", "--&gt;")
    )


def build_pr_comment(
    decision: dict[str, Any],
    pr_url: str,
    *,
    blocking_streak: int = 0,
    finding_fingerprint: str = "",
    finding_fingerprints: tuple[str, ...] = (),
    reviewed_head_sha: str = "",
    arbitration: dict[str, Any] | None = None,
    finding_history_marker: str = "",
    review_state_marker: str = "",
) -> str:
    """Build a markdown comment to post on the PR."""
    if review_state_marker:
        lines = ["<!-- codex-pr-review -->", review_state_marker]
    else:
        lines = [
            "<!-- codex-pr-review -->",
            f"{STREAK_MARKER_PREFIX}{int(blocking_streak)}{STREAK_MARKER_SUFFIX}",
            f"{FINGERPRINT_MARKER_PREFIX}{finding_fingerprint}{FINGERPRINT_MARKER_SUFFIX}",
            f"{FINGERPRINTS_MARKER_PREFIX}{','.join(finding_fingerprints)}{FINGERPRINTS_MARKER_SUFFIX}",
            f"{HEAD_SHA_MARKER_PREFIX}{reviewed_head_sha}{HEAD_SHA_MARKER_SUFFIX}",
            finding_history_marker,
            f"{CONTRACT_CONFLICT_MARKER_PREFIX}{str(bool(decision.get('contract_conflict'))).lower()}{DECISION_MARKER_SUFFIX}",
            f"{AUTO_FIX_ALLOWED_MARKER_PREFIX}{str(bool(decision.get('auto_fix_allowed', True))).lower()}{DECISION_MARKER_SUFFIX}",
            f"{NEXT_ACTION_MARKER_PREFIX}{decision.get('next_action', 'none')}{DECISION_MARKER_SUFFIX}",
            f"{IMPLEMENTATION_MARKER_PREFIX}{review_implementation_digest()}{IMPLEMENTATION_MARKER_SUFFIX}",
        ]
    lines.extend(
        [
            "## 🤖 Codex PR Review",
            "",
            _sanitize_comment_text(decision["summary"], 1000),
            "",
        ]
    )

    if arbitration:
        verdict = arbitration.get("verdict", "")
        reason = _sanitize_comment_text(arbitration.get("reason", ""))
        emoji = "✅" if verdict == "clear" else "🚫" if verdict == "block" else "⚠️"
        lines.extend(
            [
                "### ⚖️ Codex Review Arbitration",
                "",
                f"{emoji} **{verdict or 'error'}**: {reason}",
                "",
            ]
        )
    blocking = decision["blocking_findings"]
    if blocking:
        lines.extend([
            "### 🚫 Blocking Issues",
            "",
            (
                "These primary findings were cleared by independent Codex arbitration:"
                if arbitration and arbitration.get("verdict") == "clear"
                else "These issues must be fixed before this PR can be merged:"
            ),
            "",
        ])
        for i, f in enumerate(blocking, 1):
            lines.extend(_format_finding(i, f))

    non_blocking = decision["non_blocking_findings"]
    if non_blocking:
        lines.extend([
            "### ℹ️ Other Findings",
            "",
        ])
        for i, f in enumerate(non_blocking, 1):
            lines.extend(_format_finding(i, f))

    lines.extend([
        "---",
        f"*Review by Codex PR Review bot • [PR]({pr_url})*",
    ])

    return "\n".join(lines)


def _format_finding(index: int, finding: dict[str, Any]) -> list[str]:
    severity = _sanitize_comment_text(finding.get("severity", "unknown"), 20)
    category = _sanitize_comment_text(finding.get("category", "general"), 80)
    file_path = _sanitize_comment_text(finding.get("file", "?"), 300)
    line = finding.get("line")
    description = _sanitize_comment_text(finding.get("description", "No description"))
    suggestion = _sanitize_comment_text(finding.get("suggestion", ""))

    emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(severity, "⚪")

    lines = [
        f"#### {index}. {emoji} [{severity.upper()}] {category.title()} in `{file_path}`",
        "",
        f"> {description}",
    ]
    if line:
        lines[-1] += f" (line {line})"
    if suggestion:
        lines.extend(["", f"**Suggestion:** {suggestion}"])
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Existing comment management
# ---------------------------------------------------------------------------


def find_existing_review_comment(
    token: str, repo: str, pr_number: int
) -> tuple[int | None, str]:
    """Find an existing Codex review comment on the PR.

    Returns ``(comment_id, body)``. ``comment_id`` is ``None`` when absent.
    """
    marker = "<!-- codex-pr-review -->"
    page = 1
    while True:
        comments = github_request(
            token,
            "GET",
            f"/repos/{repo}/issues/{pr_number}/comments?per_page=100&page={page}&sort=created&direction=desc",
        )
        if not isinstance(comments, list):
            break
        for comment in comments:
            if _is_trusted_review_comment(comment) and marker in str(comment.get("body", "")):
                return comment.get("id"), str(comment.get("body") or "")
        if len(comments) < 100:
            break
        page += 1
    return None, ""


def _is_trusted_review_comment(comment: Any) -> bool:
    """Accept state only from a complete trusted GitHub comment record."""
    if not isinstance(comment, dict):
        return False
    user = comment.get("user")
    if not isinstance(user, dict):
        return False
    expected_login = env_value("CODEX_PR_REVIEW_COMMENT_AUTHOR", "github-actions[bot]").strip().casefold()
    actual_login = str(user.get("login") or "").strip().casefold()
    if not expected_login or actual_login != expected_login:
        return False
    if str(user.get("type") or "").strip().casefold() != "bot":
        return False
    if not isinstance(comment.get("id"), int) or comment["id"] <= 0:
        return False
    if not isinstance(comment.get("created_at"), str) or not comment["created_at"].strip():
        return False
    app = comment.get("performed_via_github_app")
    if app is not None and (
        not isinstance(app, dict)
        or str(app.get("slug") or "").strip().casefold() != "github-actions"
    ):
        return False
    return True


def trusted_review_comment_provenance(comment: Any) -> str:
    """Derive provenance from API record fields, never from comment markdown."""
    if not _is_trusted_review_comment(comment):
        return ""
    user = comment["user"]
    record = {
        "comment_id": comment["id"],
        "author_id": user.get("id"),
        "author_login": str(user.get("login") or "").casefold(),
        "created_at": comment["created_at"],
        "updated_at": comment.get("updated_at"),
    }
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def parse_review_implementation_digest(body: str) -> str:
    metadata = _legacy_machine_metadata(body)
    match = re.search(
        rf"(?m)^{re.escape(IMPLEMENTATION_MARKER_PREFIX)}([0-9a-f]{{24}})"
        rf"{re.escape(IMPLEMENTATION_MARKER_SUFFIX)}$",
        metadata,
    )
    return match.group(1) if match else ""


def parse_blocking_streak(body: str) -> int:
    """Read consecutive blocking-round counter from a prior review comment."""
    metadata = _legacy_machine_metadata(body)
    match = re.search(
        rf"(?m)^{re.escape(STREAK_MARKER_PREFIX)}(\d+)"
        rf"{re.escape(STREAK_MARKER_SUFFIX)}$",
        metadata,
    )
    if not match:
        return 0
    try:
        return max(0, int(match.group(1)))
    except ValueError:
        return 0


def parse_blocking_fingerprint(body: str) -> str:
    """Read the primary finding fingerprint from a prior review comment."""
    metadata = _legacy_machine_metadata(body)
    match = re.search(
        rf"(?m)^{re.escape(FINGERPRINT_MARKER_PREFIX)}([0-9a-f]*)"
        rf"{re.escape(FINGERPRINT_MARKER_SUFFIX)}$",
        metadata,
    )
    return match.group(1) if match else ""


def parse_blocking_fingerprints(body: str) -> tuple[str, ...]:
    """Read per-finding keys, including comments written before the new marker."""
    metadata = _legacy_machine_metadata(body)
    match = re.search(
        rf"(?m)^{re.escape(FINGERPRINTS_MARKER_PREFIX)}([0-9a-f,]*)"
        rf"{re.escape(FINGERPRINTS_MARKER_SUFFIX)}$",
        metadata,
    )
    if match:
        return tuple(sorted({item for item in match.group(1).split(",") if re.fullmatch(r"[0-9a-f]{20}", item)}))

    legacy_findings: list[dict[str, str]] = []
    for severity, category, file_path in re.findall(
        r"^#### \d+\. .*?\[([A-Z]+)\] (.+?) in `([^`]+)`$",
        metadata,
        flags=re.MULTILINE,
    ):
        legacy_findings.append(
            {"severity": severity.lower(), "category": category.lower(), "file": file_path}
        )
    return blocking_finding_fingerprints(legacy_findings)


def parse_reviewed_head_sha(body: str) -> str:
    """Read the reviewed pull-request head SHA from a prior trusted comment."""
    metadata = _legacy_machine_metadata(body)
    match = re.search(
        rf"(?m)^{re.escape(HEAD_SHA_MARKER_PREFIX)}([0-9a-f]{{7,64}})"
        rf"{re.escape(HEAD_SHA_MARKER_SUFFIX)}$",
        metadata,
    )
    return match.group(1) if match else ""


def next_blocking_streak(
    previous_streak: int,
    *,
    blocked: bool,
    previous_fingerprint: str = "",
    current_fingerprint: str = "",
    previous_head_sha: str = "",
    current_head_sha: str = "",
) -> int:
    """Advance only when the same finding is reviewed on a new PR head."""
    if not blocked:
        return 0
    if previous_fingerprint and current_fingerprint and previous_fingerprint == current_fingerprint:
        if previous_head_sha and current_head_sha and previous_head_sha != current_head_sha:
            return previous_streak + 1
        return max(1, previous_streak)
    return 1


def should_arbitrate(*, blocked: bool, streak: int, repeated: bool, new_head: bool) -> bool:
    """Arbitrate only after the same finding survives a new author commit."""
    return bool(blocked and repeated and new_head and streak >= ARBITRATION_REPEAT_THRESHOLD)


def upsert_pr_comment(
    token: str,
    repo: str,
    pr_number: int,
    body: str,
    expected_head_sha: str = "",
) -> None:
    """Create or update the Codex review comment on the PR."""
    if expected_head_sha:
        _verify_pr_head_sha(token, repo, pr_number, expected_head_sha)
    existing_id, _existing_body = find_existing_review_comment(token, repo, pr_number)
    if existing_id:
        github_request(
            token,
            "PATCH",
            f"/repos/{repo}/issues/comments/{existing_id}",
            {"body": body},
        )
        print(f"Updated existing review comment #{existing_id}")
    else:
        github_request(
            token,
            "POST",
            f"/repos/{repo}/issues/{pr_number}/comments",
            {"body": body},
        )
        print("Posted new review comment")


def write_decision_outputs(decision_payload: dict[str, Any]) -> None:
    """Persist the decision and publish the same contract fields to GitHub Actions."""
    output_dir = Path("data/output/codex_pr_review")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "decision.json").write_text(
        json.dumps(decision_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        arbitration = decision_payload.get("arbitration")
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"blocked={'true' if decision_payload['blocked'] else 'false'}\n")
            f.write(f"total_findings={decision_payload['total_findings']}\n")
            f.write(f"blocking_count={len(decision_payload['blocking_findings'])}\n")
            f.write(f"blocking_streak={decision_payload.get('blocking_streak', 0)}\n")
            f.write(f"repeated_finding={'true' if decision_payload.get('repeated_finding') else 'false'}\n")
            f.write(f"new_head={'true' if decision_payload.get('new_head') else 'false'}\n")
            f.write(f"arbitration_verdict={arbitration.get('verdict', '') if arbitration else ''}\n")
            f.write(f"contract_conflict={'true' if decision_payload.get('contract_conflict') else 'false'}\n")
            f.write(f"auto_fix_allowed={'true' if decision_payload.get('auto_fix_allowed') else 'false'}\n")
            f.write(f"next_action={decision_payload.get('next_action', 'none')}\n")


def publish_review_decision(
    token: str,
    repo: str,
    pr_number: int,
    pr_url: str,
    decision: dict[str, Any],
    *,
    exit_code: int,
    blocking_streak: int = 0,
    finding_fingerprint: str = "",
    finding_fingerprints: tuple[str, ...] = (),
    repeated_fingerprints: tuple[str, ...] = (),
    repeated_finding: bool = False,
    previous_head_sha: str = "",
    current_head_sha: str = "",
    reviewed_head_sha: str = "",
    new_head: bool = False,
    arbitration: dict[str, Any] | None = None,
    finding_history_marker: str = "",
    history_valid: bool = True,
    active_review_findings: list[dict[str, Any]] | None = None,
    applied_dispositions: list[dict[str, Any]] | None = None,
    review_state_status: str = "normal",
) -> int:
    """Publish one consistent comment, artifact, and GitHub step-output decision."""
    review_state_marker = ""
    if active_review_findings is not None:
        state_head_sha = reviewed_head_sha or current_head_sha or previous_head_sha
        try:
            review_state_marker = build_review_state_marker(
                decision,
                reviewed_head_sha=state_head_sha,
                active_findings=active_review_findings,
                applied_dispositions=applied_dispositions,
                blocking_streak=blocking_streak,
                finding_fingerprint=finding_fingerprint,
                finding_fingerprints=finding_fingerprints,
                state_status=review_state_status,
            )
        except ReviewError as exc:
            decision = apply_arbitration_failure(decision, exc)
            exit_code = 1
            history_valid = False
            review_state_status = "invalid"
            review_state_marker = build_review_state_marker(
                decision,
                reviewed_head_sha=state_head_sha,
                active_findings=[],
                applied_dispositions=[],
                blocking_streak=blocking_streak,
                state_status="invalid",
            )
    upsert_pr_comment(
        token,
        repo,
        pr_number,
        build_pr_comment(
            decision,
            pr_url,
            blocking_streak=blocking_streak,
            finding_fingerprint=finding_fingerprint,
            finding_fingerprints=finding_fingerprints,
            reviewed_head_sha=reviewed_head_sha,
            arbitration=arbitration,
            finding_history_marker=finding_history_marker,
            review_state_marker=review_state_marker,
        ),
        current_head_sha or reviewed_head_sha,
    )
    write_decision_outputs(
        {
            **decision,
            "blocking_streak": blocking_streak,
            "finding_fingerprint": finding_fingerprint,
            "finding_fingerprints": finding_fingerprints,
            "repeated_fingerprints": repeated_fingerprints,
            "repeated_finding": repeated_finding,
            "previous_head_sha": previous_head_sha,
            "current_head_sha": current_head_sha,
            "new_head": new_head,
            "arbitration": arbitration,
            "history_valid": history_valid,
            "review_state_status": review_state_status,
            "active_review_findings": active_review_findings,
            "applied_dispositions": applied_dispositions or [],
        }
    )
    return exit_code


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    token = env_value("GH_TOKEN") or env_value("GITHUB_TOKEN")
    if not token:
        print("::error::GH_TOKEN or GITHUB_TOKEN is required", file=sys.stderr)
        return 1

    repo = env_value("GITHUB_REPOSITORY")
    if not repo:
        print("::error::GITHUB_REPOSITORY is not set", file=sys.stderr)
        return 1

    # Get PR context from the event
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path.exists():
        print("::error::GITHUB_EVENT_PATH not found", file=sys.stderr)
        return 1

    event = json.loads(event_path.read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    pr_number = pr.get("number")
    if not pr_number:
        print("::error::No pull request number in event", file=sys.stderr)
        return 1

    pr_title = str(pr.get("title", ""))
    pr_body = str(pr.get("body", ""))
    pr_url = str(pr.get("html_url", ""))
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    current_head_sha = str(head.get("sha") or "").strip().lower()
    if not FULL_HEAD_SHA_PATTERN.fullmatch(current_head_sha):
        print("::error::Pull request head SHA is invalid", file=sys.stderr)
        return 1

    print(f"Reviewing PR #{pr_number}: {pr_title}")

    # Fetch changed files for risk classification
    changed_files = fetch_pr_files(token, repo, pr_number)
    changed_paths = [f.get("filename", "") for f in changed_files]
    print(f"Changed files ({len(changed_paths)}): {', '.join(changed_paths[:10])}"
          + (f" and {len(changed_paths) - 10} more..." if len(changed_paths) > 10 else ""))

    # Load policy from the trusted base ref. The PR head checkout is untrusted
    # and may include policy changes that should be reviewed as data, not used
    # as live guardrail configuration for this same review.
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    policy = load_policy(
        token,
        str(base_repo.get("full_name") or repo),
        str(base.get("sha") or ""),
    )
    if policy.get("policy_errors"):
        print(f"::warning::Policy errors: {policy['policy_errors']}")

    history_source_valid = True
    try:
        _existing_id, previous_comment = find_existing_review_comment(token, repo, pr_number)
    except ReviewError as exc:
        print(f"::warning::Failed to fetch prior review comment: {exc}")
        previous_comment = ""
        history_source_valid = False
    previous_state, review_state_valid = parse_review_machine_state(previous_comment)
    machine_state_declared = bool(
        len(previous_comment.splitlines()) >= 2
        and previous_comment.splitlines()[1].startswith(REVIEW_STATE_MARKER_PREFIX)
    )
    active_review_findings: list[dict[str, Any]] = []
    applied_dispositions: list[dict[str, Any]] = []
    if previous_state is not None:
        previous_streak = int(previous_state["blocking_streak"])
        previous_fingerprint = str(previous_state["finding_fingerprint"])
        previous_fingerprints = tuple(previous_state["finding_fingerprints"])
        previous_head_sha = str(previous_state["reviewed_head_sha"])
        active_review_findings = list(previous_state["active_findings"])
        finding_history: list[dict[str, Any]] = []
        history_valid = bool(
            history_source_valid
            and review_state_valid
            and previous_state["status"] == "normal"
        )
        active_blocking_history = bool(active_review_findings)
        legacy_blocking_state = False
    else:
        previous_streak = parse_blocking_streak(previous_comment)
        previous_fingerprint = parse_blocking_fingerprint(previous_comment)
        previous_fingerprints = parse_blocking_fingerprints(previous_comment)
        previous_head_sha = parse_reviewed_head_sha(previous_comment)
        finding_history, history_valid = parse_finding_history(previous_comment)
        history_valid = history_source_valid and history_valid and not (
            machine_state_declared and not review_state_valid
        )
        if history_valid and finding_history:
            latest_history = finding_history[-1]
            if not previous_head_sha:
                previous_head_sha = str(latest_history.get("head_sha") or "")
            if not previous_fingerprints:
                previous_fingerprints = blocking_finding_fingerprints(
                    unresolved_history_findings(finding_history)
                )
        active_blocking_history = has_active_blocking_history(finding_history)
        legacy_blocking_state = bool(
            not finding_history and (previous_streak > 0 or previous_fingerprints)
        )
        if active_blocking_history or legacy_blocking_state:
            # Legacy history lacks exact line, evidence, impact and bridge-issued
            # IDs, so it cannot be dispositioned or migrated safely.
            history_valid = False
    if not history_valid:
        decision = {
            "blocked": True,
            "blocking_findings": [],
            "non_blocking_findings": [],
            "total_findings": 0,
            "summary": (
                "🚫 **Merge blocked**: trusted review history is malformed or oversized; "
                "automatic remediation is disabled pending contract arbitration"
            ),
            "contract_conflict": True,
            "auto_fix_allowed": False,
            "next_action": "contract_arbitration",
        }
        if not history_source_valid:
            write_decision_outputs(
                {
                    **decision,
                    "blocking_streak": previous_streak,
                    "previous_head_sha": previous_head_sha,
                    "current_head_sha": current_head_sha,
                    "new_head": False,
                    "history_valid": False,
                    "history_source_valid": False,
                }
            )
            return 1
        return publish_review_decision(
            token,
            repo,
            pr_number,
            pr_url,
            decision,
            exit_code=1,
            blocking_streak=previous_streak,
            previous_head_sha=previous_head_sha,
            current_head_sha=current_head_sha,
            reviewed_head_sha=current_head_sha,
            new_head=bool(previous_head_sha and current_head_sha != previous_head_sha),
            finding_history_marker=build_invalid_finding_history_marker(
                current_head_sha
            ),
            history_valid=False,
            active_review_findings=[],
            review_state_status="invalid",
        )

    if active_review_findings:
        try:
            applied_dispositions = fetch_trusted_review_dispositions(
                token, repo, pr_number, active_review_findings
            )
            active_review_findings = reconcile_active_review_findings(
                active_review_findings, [], applied_dispositions
            )
        except ReviewError as exc:
            print(f"::error::Trusted review disposition validation failed: {exc}", file=sys.stderr)
            decision = {
                "blocked": True,
                "blocking_findings": active_review_findings,
                "non_blocking_findings": [],
                "invalid_findings": [],
                "review_validation_failed": True,
                "total_findings": len(active_review_findings),
                "summary": (
                    "🚫 **Merge blocked**: authenticated review disposition metadata "
                    "could not be validated"
                ),
                "contract_conflict": True,
                "auto_fix_allowed": False,
                "next_action": "contract_arbitration",
            }
            return publish_review_decision(
                token,
                repo,
                pr_number,
                pr_url,
                decision,
                exit_code=1,
                blocking_streak=previous_streak,
                previous_head_sha=previous_head_sha,
                current_head_sha=current_head_sha,
                reviewed_head_sha=current_head_sha,
                new_head=bool(previous_head_sha and current_head_sha != previous_head_sha),
                active_review_findings=active_review_findings,
                applied_dispositions=[],
            )
        active_blocking_history = bool(active_review_findings)

    # First pass: classify files. If all files are low-risk, skip review.
    all_low_risk = changed_files_are_low_risk(changed_paths, policy)
    if (
        all_low_risk
        and changed_paths
        and not active_blocking_history
        and not legacy_blocking_state
    ):
        print("All changed files are low-risk (docs/tests). Skipping Codex review.")
        decision = {
            "blocked": False,
            "blocking_findings": [],
            "non_blocking_findings": [],
            "total_findings": 0,
            "summary": "✅ **Merge allowed**: All changes are in docs/tests — Codex review skipped.",
            "contract_conflict": False,
            "auto_fix_allowed": True,
            "next_action": "none",
        }
        return publish_review_decision(
            token,
            repo,
            pr_number,
            pr_url,
            decision,
            exit_code=0,
            current_head_sha=current_head_sha,
            reviewed_head_sha=current_head_sha,
            finding_history_marker=build_finding_history_marker(
                finding_history, [], current_head_sha, status="clear"
            ),
            active_review_findings=[],
            applied_dispositions=applied_dispositions,
        )

    # Fetch PR diff
    try:
        diff = fetch_pr_diff(token, repo, pr_number, current_head_sha)
    except ReviewError as exc:
        print(f"::error::PR review context validation failed: {exc}", file=sys.stderr)
        write_decision_outputs(
            {
                "blocked": True,
                "blocking_findings": active_review_findings,
                "non_blocking_findings": [],
                "invalid_findings": [],
                "review_validation_failed": True,
                "total_findings": len(active_review_findings),
                "summary": "PR review context validation failed closed.",
                "contract_conflict": True,
                "auto_fix_allowed": False,
                "next_action": "contract_arbitration",
                "blocking_streak": previous_streak,
                "previous_head_sha": previous_head_sha,
                "current_head_sha": current_head_sha,
                "new_head": False,
                "history_valid": True,
            }
        )
        return 1
    print(f"Fetched diff: {len(diff)} chars, {len(diff.splitlines())} lines")

    # Build review prompt
    prompt = build_review_prompt(diff, pr_title, pr_body, repo)
    print(f"Built review prompt: {len(prompt)} chars")

    # Run Codex review
    try:
        complexity = _estimate_review_complexity(diff, changed_paths, title=pr_title, body=pr_body)
        output = run_codex_review_with_fallback(
            prompt,
            DEFAULT_TIMEOUT_MINUTES,
            complexity=complexity,
            changed_file_count=len(changed_paths),
            changed_line_count=len(diff.splitlines()),
        )
    except ReviewError as exc:
        if _review_capacity_is_unavailable(exc):
            print(f"::warning::Codex review unavailable due to quota or capacity: {exc}")
            if active_blocking_history or legacy_blocking_state:
                decision = {
                    "blocked": True,
                    "blocking_findings": active_review_findings,
                    "non_blocking_findings": [],
                    "total_findings": len(active_review_findings),
                    "summary": (
                        "🚫 **Merge blocked**: review capacity is unavailable while "
                        "blocking contract history is active"
                    ),
                    "contract_conflict": True,
                    "auto_fix_allowed": False,
                    "next_action": "contract_arbitration",
                }
                return publish_review_decision(
                    token,
                    repo,
                    pr_number,
                    pr_url,
                    decision,
                    exit_code=1,
                    blocking_streak=previous_streak,
                    finding_fingerprints=previous_fingerprints,
                    previous_head_sha=previous_head_sha,
                    current_head_sha=current_head_sha,
                    reviewed_head_sha=current_head_sha,
                    new_head=bool(
                        previous_head_sha and current_head_sha != previous_head_sha
                    ),
                    finding_history_marker=build_finding_history_marker(
                        finding_history, [], current_head_sha
                    ),
                    active_review_findings=active_review_findings,
                    applied_dispositions=applied_dispositions,
                )
            decision = {
                "blocked": False,
                "blocking_findings": [],
                "non_blocking_findings": [],
                "total_findings": 0,
                "summary": (
                    "⚠️ **Review unavailable**: Codex review quota or capacity is unavailable. "
                    "Required CI checks remain the merge gate."
                ),
                "contract_conflict": False,
                "auto_fix_allowed": False,
                "next_action": "review_retry",
            }
            return publish_review_decision(
                token,
                repo,
                pr_number,
                pr_url,
                decision,
                exit_code=0,
                previous_head_sha=previous_head_sha,
                current_head_sha=current_head_sha,
                finding_history_marker=build_finding_history_marker(
                    finding_history, [], current_head_sha
                ),
                reviewed_head_sha=current_head_sha,
                active_review_findings=[],
                applied_dispositions=applied_dispositions,
            )
        print(f"::error::Codex review failed: {exc}", file=sys.stderr)
        decision = {
            "blocked": True,
            "blocking_findings": active_review_findings,
            "non_blocking_findings": [],
            "total_findings": len(active_review_findings),
            "summary": "🚫 **Merge blocked**: The Codex review could not be completed.",
            "contract_conflict": active_blocking_history,
            "auto_fix_allowed": False,
            "next_action": "contract_arbitration" if active_blocking_history else "review_retry",
        }
        return publish_review_decision(
            token,
            repo,
            pr_number,
            pr_url,
            decision,
            exit_code=1,
            blocking_streak=previous_streak,
            finding_fingerprints=previous_fingerprints,
            previous_head_sha=previous_head_sha,
            current_head_sha=current_head_sha,
            reviewed_head_sha=current_head_sha,
            new_head=bool(previous_head_sha and current_head_sha != previous_head_sha),
            finding_history_marker=build_finding_history_marker(
                finding_history, [], current_head_sha
            ),
            active_review_findings=active_review_findings,
            applied_dispositions=applied_dispositions,
        )

    print(f"Codex output: {len(output)} chars")

    # Parse review output
    try:
        review = parse_review_output(output)
    except ReviewError as exc:
        print(f"::warning::Failed to parse review output: {exc}")
        decision = {
            "blocked": True,
            "blocking_findings": active_review_findings,
            "non_blocking_findings": [],
            "total_findings": len(active_review_findings),
            "summary": "🚫 **Merge blocked**: review output could not be parsed safely.",
            "contract_conflict": active_blocking_history,
            "auto_fix_allowed": False,
            "next_action": "contract_arbitration" if active_blocking_history else "review_retry",
        }
        return publish_review_decision(
            token,
            repo,
            pr_number,
            pr_url,
            decision,
            exit_code=1,
            blocking_streak=previous_streak,
            finding_fingerprints=previous_fingerprints,
            previous_head_sha=previous_head_sha,
            current_head_sha=current_head_sha,
            reviewed_head_sha=current_head_sha,
            new_head=bool(previous_head_sha and current_head_sha != previous_head_sha),
            finding_history_marker=build_finding_history_marker(
                finding_history, [], current_head_sha
            ),
            active_review_findings=active_review_findings,
            applied_dispositions=applied_dispositions,
        )

    findings = review.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    print(f"Found {len(findings)} issue(s)")

    # Evaluate findings
    decision = evaluate_findings(
        findings,
        changed_files,
        policy,
        reviewed_head_sha=current_head_sha,
        repo_root=ROOT,
    )
    current_blocking = list(decision["blocking_findings"])
    active_review_findings = reconcile_active_review_findings(
        active_review_findings,
        current_blocking,
        applied_dispositions,
    )
    current_ids = {
        finding["review_finding_id"] for finding in current_blocking
    }
    active_current_ids = {
        finding["review_finding_id"]
        for finding in active_review_findings
        if finding["review_finding_id"] in current_ids
    }
    exact_finding_ids = tuple(
        finding["review_finding_id"] for finding in active_review_findings
    )
    finding_fingerprint = (
        hashlib.sha256(",".join(exact_finding_ids).encode("ascii")).hexdigest()[:20]
        if exact_finding_ids
        else ""
    )
    repeated_fingerprints = tuple(
        sorted(set(exact_finding_ids).intersection(previous_fingerprints))
    )
    repeated_finding = bool(repeated_fingerprints)
    new_head = bool(
        previous_head_sha
        and current_head_sha
        and previous_head_sha != current_head_sha
    )
    blocking_streak = next_blocking_streak(
        previous_streak,
        blocked=bool(active_review_findings or decision["review_validation_failed"]),
        previous_fingerprint=(
            repeated_fingerprints[0]
            if repeated_fingerprints
            else previous_fingerprint
        ),
        current_fingerprint=(
            repeated_fingerprints[0]
            if repeated_fingerprints
            else finding_fingerprint
        ),
        previous_head_sha=previous_head_sha,
        current_head_sha=current_head_sha,
    )
    decision["blocking_findings"] = active_review_findings
    decision["blocked"] = bool(
        active_review_findings or decision["review_validation_failed"]
    )
    decision["total_findings"] = (
        len(active_review_findings)
        + len(decision["non_blocking_findings"])
        + len(decision["invalid_findings"])
    )
    if decision["review_validation_failed"]:
        decision.update(
            {
                "contract_conflict": True,
                "auto_fix_allowed": False,
                "next_action": "contract_arbitration",
            }
        )
    elif active_review_findings:
        all_active_are_current = active_current_ids == set(exact_finding_ids)
        decision.update(
            {
                "contract_conflict": False,
                "auto_fix_allowed": all_active_are_current,
                "next_action": (
                    "auto_remediation"
                    if all_active_are_current
                    else "authenticated_disposition"
                ),
                "summary": (
                    f"🚫 **Merge blocked**: {len(active_review_findings)} exact "
                    "active finding(s) remain"
                ),
            }
        )
    else:
        decision.update(
            {
                "contract_conflict": False,
                "auto_fix_allowed": True,
                "next_action": "none",
                "summary": "✅ **Merge allowed**: No active blocking findings remain",
            }
        )
    if decision["blocked"]:
        print(
            f"::error::Merge blocked: deterministic review state is active "
            f"(streak {blocking_streak})"
        )
    else:
        print("Review passed: no blocking issues")
    return publish_review_decision(
        token,
        repo,
        pr_number,
        pr_url,
        decision,
        exit_code=1 if decision["blocked"] else 0,
        blocking_streak=blocking_streak,
        finding_fingerprint=finding_fingerprint if decision["blocked"] else "",
        finding_fingerprints=exact_finding_ids if decision["blocked"] else (),
        repeated_fingerprints=repeated_fingerprints,
        repeated_finding=repeated_finding,
        previous_head_sha=previous_head_sha,
        current_head_sha=current_head_sha,
        new_head=new_head,
        reviewed_head_sha=current_head_sha,
        history_valid=True,
        active_review_findings=active_review_findings,
        applied_dispositions=applied_dispositions,
    )

if __name__ == "__main__":
    raise SystemExit(main())
