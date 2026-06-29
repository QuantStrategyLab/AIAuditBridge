"""Request/response schemas for AiGateway service.

All three endpoints share the same input contract; the ``task`` field
determines which adapter handles the request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── task constants ──────────────────────────────────────────────────────
TASK_ANALYZE = "analyze"
TASK_EXECUTE = "execute"
TASK_REVIEW = "review"

SUPPORTED_TASKS = frozenset({TASK_ANALYZE, TASK_EXECUTE, TASK_REVIEW})

# ── mode constants ──────────────────────────────────────────────────────
MODE_REVIEW_ONLY = "review_only"
MODE_REVIEW_AND_FIX = "review_and_fix"

SUPPORTED_MODES = frozenset({MODE_REVIEW_ONLY, MODE_REVIEW_AND_FIX})

# ── provider constants ──────────────────────────────────────────────────
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_CODEX = "codex"


@dataclass(frozen=True)
class AnalyzeRequest:
    """Payload for ``POST /v1/ai/analyze`` — sync LLM completion."""

    prompt: str
    model: str = "claude-sonnet-4-6"
    system: str = ""
    max_tokens: int = 4000
    timeout_seconds: int = 120

    def validate(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class ExecuteRequest:
    """Payload for ``POST /v1/ai/execute/jobs`` — async Codex execution."""

    prompt: str
    mode: str = MODE_REVIEW_ONLY
    model: str = ""
    timeout_seconds: int = 2700
    source_repository: str = ""
    source_ref: str = ""
    images: list[dict[str, str]] = field(default_factory=list)
    output_schema: dict[str, str] | None = None

    def validate(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if self.mode not in SUPPORTED_MODES:
            raise ValueError(f"mode must be one of {sorted(SUPPORTED_MODES)}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class ReviewRequest:
    """Payload for ``POST /v1/ai/review`` — multi-model parallel review."""

    prompt: str
    reviewers: tuple[str, ...] = ("claude", "gpt")
    verifier: str | None = "codex"
    mode: str = MODE_REVIEW_ONLY
    model: str = ""  # override for all reviewers
    timeout_seconds: int = 600

    def validate(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if not self.reviewers:
            raise ValueError("at least one reviewer is required")
        for reviewer in self.reviewers:
            if reviewer not in {"claude", "gpt"}:
                raise ValueError(f"unsupported reviewer: {reviewer!r}")
        if self.verifier is not None and self.verifier != "codex":
            raise ValueError(f"unsupported verifier: {self.verifier!r}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class AnalyzeResponse:
    status: str  # "ok" | "error"
    output: str = ""
    model: str = ""
    error: str = ""


@dataclass(frozen=True)
class ExecuteJobResponse:
    status: str  # "queued" | "running" | "succeeded" | "failed"
    job_id: str = ""
    output: str = ""
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class ReviewResponse:
    status: str  # "ok" | "error"
    results: list[dict[str, Any]] = field(default_factory=list)
    consensus: str = ""  # "approve" | "reject" | "escalate"
    error: str = ""


def parse_analyze_request(payload: dict[str, Any]) -> AnalyzeRequest:
    req = AnalyzeRequest(
        prompt=str(payload.get("prompt", "")),
        model=str(payload.get("model", "claude-sonnet-4-6")),
        system=str(payload.get("system", "")),
        max_tokens=int(payload.get("max_tokens", 4000)),
        timeout_seconds=int(payload.get("timeout_seconds", 120)),
    )
    req.validate()
    return req


def parse_execute_request(payload: dict[str, Any]) -> ExecuteRequest:
    images = payload.get("images")
    if not isinstance(images, list):
        images = []
    output_schema = payload.get("output_schema")
    if not isinstance(output_schema, dict):
        output_schema = None
    req = ExecuteRequest(
        prompt=str(payload.get("prompt", "")),
        mode=str(payload.get("mode", MODE_REVIEW_ONLY)),
        model=str(payload.get("model", "")),
        timeout_seconds=int(payload.get("timeout_seconds", 2700)),
        source_repository=str(payload.get("source_repository", "")),
        source_ref=str(payload.get("source_ref", "")),
        images=images,
        output_schema=output_schema,
    )
    req.validate()
    return req


def parse_review_request(payload: dict[str, Any]) -> ReviewRequest:
    raw_reviewers = payload.get("reviewers", ["claude", "gpt"])
    if isinstance(raw_reviewers, list):
        reviewers = tuple(str(r) for r in raw_reviewers)
    else:
        reviewers = ("claude", "gpt")
    raw_verifier = payload.get("verifier", "codex")
    verifier = str(raw_verifier) if raw_verifier else None
    req = ReviewRequest(
        prompt=str(payload.get("prompt", "")),
        reviewers=reviewers,
        verifier=verifier,
        mode=str(payload.get("mode", MODE_REVIEW_ONLY)),
        model=str(payload.get("model", "")),
        timeout_seconds=int(payload.get("timeout_seconds", 600)),
    )
    req.validate()
    return req
