"""AiGatewayClient — unified client for all three AI call patterns.

Replaces:
- ``AiServiceClient`` in QuantPlatformKit/ai_provider.py
- Direct HTTP calls in run_monthly_codex_audit.py
- Direct API calls in QuantStrategyPlugins/ai_audit.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from client.config import GatewayConfig
from client.errors import AiGatewayError, AuthenticationError, ServiceUnavailableError, TimeoutError


@dataclass(frozen=True)
class AiResult:
    """Result from a single AI call."""

    provider: str  # "claude" | "gpt" | "codex"
    model: str
    success: bool
    output: str = ""
    error: str = ""
    note: str = ""
    latency_seconds: float = 0.0
    raw: Any = None

    @classmethod
    def unavailable(cls, provider: str, reason: str) -> "AiResult":
        return cls(provider=provider, model="", success=False, error=reason, note=reason)


@dataclass(frozen=True)
class ReviewResult:
    """Aggregated result from a multi-model review."""

    results: list[AiResult] = field(default_factory=list)
    consensus: str = ""  # "approve" | "reject" | "escalate" | "unknown"
    all_success: bool = False
    recommended_action: dict[str, Any] = field(default_factory=dict)
    # ^ e.g. {"action": "auto_merge", "confidence": 0.92, "risk": "low", "reason": "..."}


class AiGatewayClient:
    """Unified client for AiGateway HTTP service.

    Three methods map to the three service endpoints::

        client = AiGatewayClient(GatewayConfig.from_env())

        # Scenario 1: quick analysis (sync, Claude/GPT API)
        result = client.analyze(prompt="Should we optimize this strategy?")

        # Scenario 2: async code execution (Codex on VPS)
        job = client.execute(prompt="Review files...", mode="review_and_fix")

        # Scenario 3: multi-model review (parallel LLMs + Codex verify)
        review = client.review(prompt="Review this proposal...")
    """

    def __init__(self, config: GatewayConfig):
        self.config = config

    # ── analyze ────────────────────────────────────────────────────

    def analyze(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system: str = "",
        max_tokens: int = 4000,
        timeout: float | None = None,
    ) -> AiResult:
        """Sync LLM completion via ``POST /v1/ai/analyze``.

        Uses LlmAdapter on the service side — Claude or GPT API.
        Best for: optimization decisions, shadow audits, text classification.
        """
        selected_model = model or self.config.default_analyze_model
        timeout = timeout or self.config.timeout_analyze
        started = time.time()

        try:
            token = _fetch_oidc_token(self.config.audience)
            payload = json.dumps({
                "task": "analyze",
                "prompt": prompt,
                "model": selected_model,
                "system": system,
                "max_tokens": max_tokens,
                "timeout_seconds": int(timeout),
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self.config.service_url}/v1/ai/analyze",
                data=payload,
                method="POST",
                headers=_headers(token),
            )
            with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data.get("status") == "ok":
                return AiResult(
                    provider=data.get("provider", ""),
                    model=data.get("model", selected_model),
                    success=True,
                    output=str(data.get("output", "")),
                    latency_seconds=time.time() - started,
                    raw=data,
                )
            return AiResult(
                provider="", model=selected_model, success=False,
                error=str(data.get("error", "unknown")), raw=data,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            return AiResult.unavailable("", f"HTTP {exc.code}: {body}")
        except Exception as exc:
            return AiResult.unavailable("", str(exc))

    # ── execute ────────────────────────────────────────────────────

    def execute(
        self,
        prompt: str,
        *,
        mode: str = "review_only",
        model: str | None = None,
        source_repository: str | None = None,
        source_ref: str = "main",
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> AiResult:
        """Async Codex execution via ``POST /v1/ai/execute/jobs`` + polling.

        Uses CodexAdapter on the service side — ``codex exec`` subprocess.
        Best for: monthly audits, auto-fix, backtest verification.
        """
        timeout = timeout or self.config.timeout_execute
        poll_interval = poll_interval or self.config.poll_interval
        started = time.time()

        try:
            token = _fetch_oidc_token(self.config.audience)
            payload = json.dumps({
                "task": "execute",
                "prompt": prompt,
                "mode": mode,
                "model": model or self.config.default_execute_model,
                "source_repository": source_repository or self.config.source_repository,
                "source_ref": source_ref,
                "timeout_seconds": int(timeout),
            }).encode("utf-8")

            # Submit job
            req = urllib.request.Request(
                f"{self.config.service_url}/v1/ai/execute/jobs",
                data=payload,
                method="POST",
                headers=_headers(token),
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                job = json.loads(resp.read().decode("utf-8"))

            job_id = job.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                return AiResult.unavailable("codex", "No job_id from gateway")

            # Poll until completion
            deadline = time.time() + timeout + 60
            while time.time() < deadline:
                time.sleep(poll_interval)
                req2 = urllib.request.Request(
                    f"{self.config.service_url}/v1/ai/execute/jobs/{job_id}",
                    method="GET",
                    headers=_headers(token),
                )
                try:
                    with urllib.request.urlopen(req2, timeout=30) as resp2:
                        status_data = json.loads(resp2.read().decode("utf-8"))
                except urllib.error.HTTPError:
                    continue

                status = status_data.get("status")
                if status == "succeeded":
                    return AiResult(
                        provider="codex", model="codex-cli", success=True,
                        output=str(status_data.get("output", "")),
                        latency_seconds=time.time() - started,
                        raw=status_data,
                    )
                if status == "failed":
                    return AiResult(
                        provider="codex", model="codex-cli", success=False,
                        error=str(status_data.get("error", "")), raw=status_data,
                    )

            return AiResult.unavailable("codex", "Job polling timed out")

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            return AiResult.unavailable("codex", f"HTTP {exc.code}: {body}")
        except Exception as exc:
            return AiResult.unavailable("codex", str(exc))

    # ── review ─────────────────────────────────────────────────────

    def review(
        self,
        prompt: str,
        *,
        reviewers: list[str] | None = None,
        verifier: str | None = "codex",
        model: str | None = None,
        changed_paths: list[str] | None = None,
        timeout: float | None = None,
    ) -> ReviewResult:
        """Multi-model parallel review via ``POST /v1/ai/review``.

        Service-side: parallel LlmAdapter calls + optional CodexAdapter verify.
        Pass ``changed_paths`` for file-risk-aware autonomy recommendations.
        Best for: optimization proposal review, PR review.
        """
        timeout = timeout or self.config.timeout_review

        if reviewers is None:
            reviewers = [r.label for r in self.config.reviewers]

        try:
            token = _fetch_oidc_token(self.config.audience)
            req_payload: dict[str, Any] = {
                "task": "review",
                "prompt": prompt,
                "reviewers": reviewers,
                "verifier": verifier,
                "model": model or "",
                "timeout_seconds": int(timeout),
            }
            if changed_paths:
                req_payload["changed_paths"] = changed_paths

            req = urllib.request.Request(
                f"{self.config.service_url}/v1/ai/review",
                data=json.dumps(req_payload).encode("utf-8"),
                method="POST",
                headers=_headers(token),
            )
            with urllib.request.urlopen(req, timeout=timeout + 60) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            results = []
            for r in data.get("results", []):
                results.append(AiResult(
                    provider=str(r.get("reviewer", "")),
                    model=str(r.get("model", "")),
                    success=bool(r.get("success")),
                    output=str(r.get("output", "")),
                    error=str(r.get("error", "")),
                    latency_seconds=float(r.get("latency_seconds", 0)),
                ))

            return ReviewResult(
                results=results,
                consensus=str(data.get("consensus", "unknown")),
                all_success=bool(data.get("status") == "ok"),
                recommended_action=data.get("recommended_action", {}),
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            return ReviewResult(
                results=[AiResult.unavailable("gateway", f"HTTP {exc.code}: {body}")],
                consensus="unknown",
                all_success=False,
            )
        except Exception as exc:
            return ReviewResult(
                results=[AiResult.unavailable("gateway", str(exc))],
                consensus="unknown",
                all_success=False,
            )

    # ── feedback (Phase 3: closed-loop change tracking) ──────────────

    def register_change(
        self,
        *,
        task: str,
        action: str,
        confidence: float,
        risk: str,
        changed_paths: list[str] | None = None,
        before_metrics: dict[str, float] | None = None,
        source_repository: str = "",
    ) -> str:
        """Register an autonomous change for post-change effect tracking.

        Returns the change_id to use when submitting post-change metrics.
        """
        token = _fetch_oidc_token(self.config.audience)
        payload = json.dumps({
            "task": task,
            "action": action,
            "confidence": confidence,
            "risk": risk,
            "changed_paths": changed_paths or [],
            "before_metrics": before_metrics or {},
            "source_repository": source_repository or self.config.source_repository,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.service_url}/v1/ai/feedback/register",
            data=payload, method="POST", headers=_headers(token),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("change_id", ""))

    def evaluate_change(self, change_id: str, after_metrics: dict[str, float]) -> dict[str, Any]:
        """Submit post-change metrics and get effect evaluation."""
        token = _fetch_oidc_token(self.config.audience)
        payload = json.dumps({
            "change_id": change_id,
            "after_metrics": after_metrics,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.service_url}/v1/ai/feedback/evaluate",
            data=payload, method="POST", headers=_headers(token),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def report_shadow(
        self,
        *,
        plugin: str,
        ai_verdict: str,
        ai_confidence: float,
        deterministic_route: str,
    ) -> dict[str, Any]:
        """Report AI shadow audit vs deterministic logic disagreement."""
        token = _fetch_oidc_token(self.config.audience)
        payload = json.dumps({
            "plugin": plugin,
            "ai_verdict": ai_verdict,
            "ai_confidence": ai_confidence,
            "deterministic_route": deterministic_route,
            "source_repository": self.config.source_repository,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.config.service_url}/v1/ai/feedback/shadow",
            data=payload, method="POST", headers=_headers(token),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_effectiveness(self, repo: str = "", days: int = 90) -> dict[str, Any]:
        """Get autonomous change effectiveness statistics."""
        return self._get_json(f"/v1/ai/changes/effectiveness?repo={repo}&days={days}").get("report", {})

    # ── quota & health (Phase 4) ───────────────────────────────────

    def get_quota(self, repo: str = "") -> dict[str, Any]:
        """Get quota status for a repo or all repos."""
        path = f"/v1/ai/quota" if not repo else f"/v1/ai/quota?repo={repo}"
        return self._get_json(path).get("quota", {})

    def get_health(self) -> dict[str, Any]:
        """Get service health snapshot: status, endpoints, latencies, error rates."""
        data = self._get_json("/v1/ai/health")
        data.pop("status", None)
        return data

    def _get_json(self, path: str) -> dict[str, Any]:
        """Internal GET helper."""
        token = _fetch_oidc_token(self.config.audience)
        req = urllib.request.Request(
            f"{self.config.service_url}{path}",
            method="GET", headers=_headers(token),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ── helpers ────────────────────────────────────────────────────────────


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "AiGatewayClient/2.0",
    }


def _fetch_oidc_token(audience: str = "quant-codex-audit") -> str:
    """Fetch GitHub Actions OIDC token, or fall back to static token."""
    token_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    token_bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if token_url and token_bearer:
        separator = "&" if "?" in token_url else "?"
        url = f"{token_url}{separator}audience={urllib.request.quote(audience, safe='')}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token_bearer}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            token = str(json.loads(resp.read().decode("utf-8")).get("value", ""))
        if not token:
            raise AuthenticationError("GitHub Actions OIDC response did not include a token")
        return token
    # Fallback: static token
    static = os.environ.get("CODEX_AUDIT_SERVICE_TOKEN", "").strip()
    if static:
        return static
    raise AuthenticationError(
        "No OIDC token available. Set workflow permissions: id-token: write, "
        "or set CODEX_AUDIT_SERVICE_TOKEN."
    )
