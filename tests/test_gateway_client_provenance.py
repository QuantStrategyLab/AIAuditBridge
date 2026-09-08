from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
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
    def test_codex_research_freezes_admitted_identity_on_every_poll(self):
        route = {"job_id": "submitted-job", "provider": "codex", "research_stage": "promotion_review",
                 "model": "gpt-6-astra", "reasoning_effort": "xhigh"}
        mutations = {"job_id": "different-job", "provider": "cursor", "research_stage": "optimization",
                     "model": "gpt-5.6-sol", "reasoning_effort": "high"}
        for status in ("running", "succeeded"):
            for field, value in mutations.items():
                first_poll = {**route, "status": status, "output": "private-marker", field: value}
                replies = [_FakeResponse({"codex_research_routing": "v1"}), _FakeResponse(route),
                           _FakeResponse(first_poll), _FakeResponse({**route, "status": "succeeded", "output": "synthetic"})]
                client = AiGatewayClient(GatewayConfig(service_url="https://synthetic.invalid"))
                with self.subTest(status=status, field=field), patch(
                    "client.gateway_client._fetch_oidc_token", return_value="synthetic",
                ), patch("client.gateway_client.time.sleep"), patch(
                    "client.gateway_client.urllib.request.urlopen", side_effect=replies,
                ) as http:
                    result = client.execute("synthetic", research_stage="promotion_review")
                    self.assertFalse(result.success)
                    self.assertEqual(http.call_count, 3)
                    self.assertNotIn("private-marker", repr(result))

    def test_research_refuses_incomplete_or_conflicting_admission_before_polling(self):
        route = {"job_id": "submitted-job", "provider": "codex", "research_stage": "promotion_review",
                 "model": "gpt-6-astra", "reasoning_effort": "xhigh"}
        mutations = ({"provider": "cursor"}, {"research_stage": "optimization"}, {"model": ""},
                     {"model": "gpt-5.6-sol"}, {"reasoning_effort": "high"}, {"reasoning_effort": None})
        for mutation in mutations:
            replies = [_FakeResponse({"codex_research_routing": "v1"}), _FakeResponse({**route, **mutation}),
                       _FakeResponse({**route, "status": "succeeded", "output": "synthetic"})]
            client = AiGatewayClient(GatewayConfig(service_url="https://synthetic.invalid"))
            with self.subTest(mutation=mutation), patch("client.gateway_client._fetch_oidc_token", return_value="synthetic"), patch(
                "client.gateway_client.time.sleep",
            ), patch("client.gateway_client.urllib.request.urlopen", side_effect=replies) as http:
                result = client.execute("synthetic", research_stage="promotion_review", model="gpt-6-astra", reasoning_effort="xhigh")
                self.assertFalse(result.success)
                self.assertEqual(http.call_count, 2)

    def test_codex_research_accepts_same_running_and_completed_job(self):
        route = {"job_id": "submitted-job", "provider": "codex", "research_stage": "promotion_review",
                 "model": "gpt-6-astra", "reasoning_effort": "xhigh"}
        replies = [_FakeResponse({"codex_research_routing": "v1"}), _FakeResponse(route),
                   _FakeResponse({**route, "status": "running"}),
                   _FakeResponse({**route, "status": "succeeded", "output": "synthetic"})]
        client = AiGatewayClient(GatewayConfig(service_url="https://synthetic.invalid"))
        with patch("client.gateway_client._fetch_oidc_token", return_value="synthetic"), patch(
            "client.gateway_client.time.sleep",
        ), patch("client.gateway_client.urllib.request.urlopen", side_effect=replies):
            result = client.execute("synthetic", research_stage="promotion_review")
        self.assertTrue(result.success)
        self.assertEqual(result.raw["job_id"], "submitted-job")

    def test_nonresearch_execute_keeps_legacy_receipt_compatibility(self):
        replies = [_FakeResponse({"job_id": "legacy-job"}), _FakeResponse({"status": "succeeded", "output": "legacy"})]
        client = AiGatewayClient(GatewayConfig(service_url="https://synthetic.invalid"))
        with patch("client.gateway_client._fetch_oidc_token", return_value="synthetic"), patch(
            "client.gateway_client.time.sleep",
        ), patch("client.gateway_client.urllib.request.urlopen", side_effect=replies) as http:
            result = client.execute("synthetic")
        self.assertTrue(result.success)
        self.assertEqual(http.call_count, 2)

    def test_research_deferral_preserves_retry_without_polling_or_breaker_failure(self) -> None:
        client = AiGatewayClient(GatewayConfig(service_url="https://gateway.invalid"))
        error = urllib.error.HTTPError("https://gateway.invalid", 429, "deferred", {}, io.BytesIO(json.dumps({
            "status": "deferred", "error": "codex_quota_reserved", "retry_at": 9000,
            "private": "must-not-propagate",
        }).encode()))
        with patch("client.gateway_client._fetch_oidc_token", return_value="test-token"), patch(
            "client.gateway_client.urllib.request.urlopen", side_effect=[_FakeResponse({"codex_research_routing": "v1"}), error]
        ) as http:
            result = client.execute("synthetic", research_stage="optimization", reasoning_effort="high")
        self.assertFalse(result.success)
        self.assertEqual(result.raw["status"], "deferred")
        self.assertEqual(result.raw["retry_at"], 9000)
        self.assertNotIn("must-not-propagate", repr(result))
        self.assertEqual(client._breaker.failures, 0)
        self.assertEqual(http.call_count, 2)
        payload = json.loads(http.call_args.args[0].data)
        self.assertEqual(payload["research_stage"], "optimization")
        self.assertEqual(payload["reasoning_effort"], "high")

    def test_research_refuses_old_service_before_submitting_and_checks_completion_route(self):
        route = {"job_id": "synthetic", "provider": "codex", "research_stage": "optimization",
                 "model": "gpt-5.6-sol", "reasoning_effort": "high"}
        cases = [
            [_FakeResponse({"status": "ok"})],
            [_FakeResponse({"codex_research_routing": "v1"}), _FakeResponse({"job_id": "synthetic"})],
            [_FakeResponse({"codex_research_routing": "v1"}), _FakeResponse(route),
             _FakeResponse({"status": "succeeded", "output": "old response"})],
        ]
        for replies in cases:
            client = AiGatewayClient(GatewayConfig(service_url="https://gateway.invalid"))
            with self.subTest(replies=len(replies)), patch("client.gateway_client._fetch_oidc_token", return_value="synthetic"), patch(
                "client.gateway_client.urllib.request.urlopen", side_effect=replies
            ) as http, patch("client.gateway_client.time.sleep"):
                result = client.execute("synthetic", research_stage="optimization")
            self.assertFalse(result.success)
            self.assertEqual(result.output, "")
            self.assertEqual(http.call_count, len(replies))
            if len(replies) == 1:
                self.assertEqual(http.call_args.args[0].get_method(), "GET")

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
