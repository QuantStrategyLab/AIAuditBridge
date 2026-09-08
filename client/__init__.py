"""AiGateway client library — single dependency for Quant repos to call AI.

Usage::

    from ai_gateway_client import AiGatewayClient, GatewayConfig

    config = GatewayConfig.from_env()
    client = AiGatewayClient(config)

    # Scenario 1: quick analysis (optimization decision, shadow audit)
    result = client.analyze(prompt="Should we optimize?", model="claude-sonnet-4-6")

    # Scenario 2: async code execution (monthly audit, auto-fix)
    job = client.execute(prompt="Review and fix...", mode="review_and_fix")

    # Scenario 3: multi-model review (proposal review with consensus)
    results = client.review(prompt="Review this proposal...")
"""
from .gateway_client import AiGatewayClient, AiResult
from .config import GatewayConfig, ProviderConfig
from .errors import AiGatewayError, AuthenticationError, ServiceUnavailableError

__all__ = [
    "AiGatewayClient",
    "AiResult",
    "GatewayConfig",
    "ProviderConfig",
    "AiGatewayError",
    "AuthenticationError",
    "ServiceUnavailableError",
]
