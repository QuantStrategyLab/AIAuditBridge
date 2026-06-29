"""Unified configuration for AiGateway client.

Replaces three separate config systems:
- AiServiceConfig / AiProviderConfig (QuantPlatformKit)
- CODEX_AUDIT_PROVIDER env var (run_monthly_codex_audit.py)
- QSP_*_AI_AUDIT_* env vars (ai_audit.py)

Single entry point: ``GatewayConfig.from_env()`` reads ``CODEX_AUDIT_SERVICE_URL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderConfig:
    """Per-provider model configuration."""

    label: str  # "claude" | "gpt" | "codex"
    model: str
    can_execute_code: bool = False
    can_analyze: bool = True

    @classmethod
    def claude(cls, model: str = "claude-sonnet-4-6") -> "ProviderConfig":
        return cls(label="claude", model=model, can_execute_code=False, can_analyze=True)

    @classmethod
    def gpt(cls, model: str = "gpt-5.4-mini") -> "ProviderConfig":
        return cls(label="gpt", model=model, can_execute_code=False, can_analyze=True)

    @classmethod
    def codex(cls, model: str = "") -> "ProviderConfig":
        return cls(label="codex", model=model or "codex-cli", can_execute_code=True, can_analyze=True)


@dataclass(frozen=True)
class GatewayConfig:
    """Configuration for AiGatewayClient.

    All fields can be set explicitly or read from environment variables via ``from_env()``.
    """

    service_url: str
    audience: str = "quant-codex-audit"
    default_analyze_model: str = "claude-sonnet-4-6"
    default_execute_model: str = ""
    reviewers: tuple[ProviderConfig, ...] = field(default_factory=lambda: (ProviderConfig.claude(), ProviderConfig.gpt()))
    verifier: ProviderConfig | None = None
    timeout_analyze: float = 120.0
    timeout_execute: float = 600.0
    timeout_review: float = 600.0
    poll_interval: float = 5.0
    source_repository: str = ""

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        """Build config from environment variables.

        Required: ``CODEX_AUDIT_SERVICE_URL``
        Optional: ``CODEX_AUDIT_SERVICE_AUDIENCE``, ``CODEX_AUDIT_SERVICE_TOKEN``,
                  ``AI_GATEWAY_SOURCE_REPO``, ``CODEX_AUDIT_MODE``
        """
        service_url = os.environ.get("CODEX_AUDIT_SERVICE_URL", "").strip()
        if not service_url:
            raise ValueError("CODEX_AUDIT_SERVICE_URL is required")

        audience = os.environ.get("CODEX_AUDIT_SERVICE_AUDIENCE", "quant-codex-audit").strip()

        # Reviewers: Claude + GPT (always both for adversarial review)
        reviewers = (
            ProviderConfig.claude(os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()),
            ProviderConfig.gpt(os.environ.get("OPENAI_MODEL", "gpt-5.4-mini").strip()),
        )

        # Verifier: Codex VPS (optional, for backtest verification)
        verifier = ProviderConfig.codex(os.environ.get("CODEX_AUDIT_SERVICE_MODEL", "").strip())

        return cls(
            service_url=service_url.rstrip("/"),
            audience=audience,
            default_analyze_model=os.environ.get("DEFAULT_ANALYZE_MODEL", "claude-sonnet-4-6").strip(),
            reviewers=reviewers,
            verifier=verifier,
            source_repository=os.environ.get("AI_GATEWAY_SOURCE_REPO", "").strip(),
        )
