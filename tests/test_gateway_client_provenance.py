from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from client.config import GatewayConfig
from client.gateway_client import AiGatewayClient


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class GatewayClientProvenanceTests(unittest.TestCase):
    def test_installed_sdk_exports_consumer_api_without_source_checkout(self) -> None:
        script = """
import sys
def deny_network(event, args):
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}:
        raise PermissionError("network disabled")
sys.addaudithook(deny_network)
from importlib.metadata import distribution
from ai_gateway_client import AiGatewayClient, GatewayConfig, AiResult
client = AiGatewayClient(GatewayConfig(service_url="https://gateway.invalid"))
result = AiResult.unavailable("codex", "synthetic_unavailable")
assert client.config.service_url == "https://gateway.invalid"
assert result.success is False
assert AiGatewayClient.__module__ == "ai_gateway_client.gateway_client"
assert GatewayConfig.__module__ == "ai_gateway_client.config"
assert AiResult.__module__ == "ai_gateway_client.gateway_client"
dist = distribution("ai-gateway-client")
assert not dist.requires
assert not any(str(path).startswith(("service/", "client/")) for path in dist.files)
"""
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", script],
                cwd=cwd,
                env={"PATH": os.defpath},
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_analyze_advisory_preserves_output_without_opening_breaker(self) -> None:
        response = _FakeResponse({
            "status": "advisory",
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "output": "analysis output",
            "provenance_receipt": {"policy_verdict": "advisory"},
        })
        client = AiGatewayClient(GatewayConfig(service_url="https://gateway.invalid"))

        with patch("client.gateway_client._fetch_oidc_token", return_value="test-token"), patch(
            "client.gateway_client.urllib.request.urlopen",
            return_value=response,
        ):
            result = client.analyze("analyze", model="gpt-5.4-mini")

        self.assertFalse(result.success)
        self.assertEqual(result.output, "analysis output")
        self.assertEqual(result.note, "advisory")
        self.assertEqual(result.raw["provenance_receipt"]["policy_verdict"], "advisory")
        self.assertEqual(client._breaker.state, "closed")
        self.assertEqual(client._breaker.failures, 0)

    def test_review_advisory_is_not_counted_as_service_failure(self) -> None:
        response = _FakeResponse({
            "status": "advisory",
            "results": [{
                "reviewer": "openai",
                "model": "gpt-5.4-mini",
                "success": True,
                "output": '{"verdict":"approve","confidence":0.9}',
            }],
            "consensus": "approve",
            "recommended_action": {"action": "escalate", "auto_merge_allowed": False},
        })
        client = AiGatewayClient(GatewayConfig(service_url="https://gateway.invalid"))

        with patch("client.gateway_client._fetch_oidc_token", return_value="test-token"), patch(
            "client.gateway_client.urllib.request.urlopen",
            return_value=response,
        ):
            result = client.review("review", reviewers=["gpt"], verifier=None)

        self.assertFalse(result.all_success)
        self.assertTrue(result.results[0].success)
        self.assertEqual(result.recommended_action["action"], "escalate")
        self.assertEqual(client._breaker.state, "closed")
        self.assertEqual(client._breaker.failures, 0)


if __name__ == "__main__":
    unittest.main()
