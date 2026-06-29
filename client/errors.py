"""Unified error types for AiGateway client."""

from __future__ import annotations


class AiGatewayError(RuntimeError):
    """Base error for all AiGateway client failures."""


class AuthenticationError(AiGatewayError):
    """OIDC token fetch or validation failed."""


class ServiceUnavailableError(AiGatewayError):
    """AiGateway service returned an error or was unreachable."""


class TimeoutError(AiGatewayError):
    """Request or job polling timed out."""
