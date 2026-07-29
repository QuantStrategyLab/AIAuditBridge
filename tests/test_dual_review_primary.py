from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from client.config import GatewayConfig
from client.errors import CircuitBreakerOpenError
from client.gateway_client import AiGatewayClient, AiResult
from service.dual_review import VERDICT_INVALID, VERDICT_UNAVAILABLE
from service.dual_review_primary import build_primary_prompt, parse_primary_review_output, run_codex_primary_review


class DualReviewPrimaryTests(unittest.TestCase):
    def test_gateway_execute_preserves_task_and_complexity(self) -> None:
        submit_response = MagicMock()
        submit_response.__enter__.return_value.read.return_value = b'{"job_id":"job-1"}'
        poll_response = MagicMock()
        poll_response.__enter__.return_value.read.return_value = (
            b'{"status":"succeeded","output":"ok"}'
        )
        config = GatewayConfig(
            service_url="https://service.invalid",
            source_repository="QuantStrategyLab/AIAuditBridge",
        )

        with (
            patch(
                "client.gateway_client._fetch_oidc_token",
                side_effect=["submit-oidc", "poll-oidc"],
            ) as fetch_oidc_token,
            patch(
                "client.gateway_client.urllib.request.urlopen",
                side_effect=[submit_response, poll_response],
            ) as urlopen,
            patch("client.gateway_client.time.sleep"),
        ):
            result = AiGatewayClient(config).execute(
                "review",
                task="promotion_review",
                complexity="high",
                timeout=1,
            )

        self.assertTrue(result.success)
        request = urlopen.call_args_list[0].args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["task"], "promotion_review")
        self.assertEqual(payload["complexity"], "high")
        self.assertEqual(fetch_oidc_token.call_count, 2)
        self.assertEqual(request.get_header("Authorization"), "Bearer submit-oidc")
        poll_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(poll_request.get_header("Authorization"), "Bearer poll-oidc")

    def test_gateway_execute_labels_network_failure(self) -> None:
        config = GatewayConfig(
            service_url="https://service.invalid",
            source_repository="QuantStrategyLab/AIAuditBridge",
        )
        with (
            patch("client.gateway_client._fetch_oidc_token", return_value="oidc"),
            patch(
                "client.gateway_client.urllib.request.urlopen",
                side_effect=urllib.error.URLError("temporary DNS failure"),
            ),
        ):
            result = AiGatewayClient(config).execute("review")

        self.assertFalse(result.success)
        self.assertEqual(result.raw, {"failure_category": "transient_service_failure"})

    def test_gateway_execute_labels_open_circuit(self) -> None:
        client = AiGatewayClient(
            GatewayConfig(
                service_url="https://service.invalid",
                source_repository="QuantStrategyLab/AIAuditBridge",
            )
        )
        with patch.object(
            client._breaker,
            "before_call",
            side_effect=CircuitBreakerOpenError("circuit open"),
        ):
            result = client.execute("review")

        self.assertFalse(result.success)
        self.assertEqual(result.raw, {"failure_category": "transient_service_failure"})

    def test_build_primary_prompt_includes_evidence_summary(self) -> None:
        from pathlib import Path
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.json"
            path.write_text(
                json.dumps({"strategy_profile": "demo", "oos_sharpe": 1.2, "status": "shadow_candidate"}),
                encoding="utf-8",
            )
            prompt = build_primary_prompt(
                trigger="promotion",
                strategy_profile="demo",
                context={"old_status": "shadow_candidate", "new_status": "live_candidate"},
                evidence_path=path,
            )
            self.assertIn("demo", prompt)
            self.assertIn("oos_sharpe", prompt)

    def test_parse_primary_review_output(self) -> None:
        review = parse_primary_review_output('{"verdict":"approve","confidence":0.77,"summary":"ok"}')
        self.assertEqual(review["verdict"], "approve")
        self.assertEqual(review["source"], "codex_primary")

    @patch.dict(
        "os.environ",
        {
            "CODEX_AUDIT_SERVICE_URL": "https://service.invalid",
            "GITHUB_REPOSITORY": "",
        },
    )
    @patch("service.dual_review_primary.AiGatewayClient.execute")
    def test_budget_error_is_unavailable(self, review) -> None:
        review.return_value = AiResult.unavailable("codex", "Daily budget exceeded")
        result = run_codex_primary_review(prompt="review")
        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)
        review.assert_called_once_with(
            "review",
            task="dual_review",
            mode="review_only",
            complexity="high",
            source_repository=None,
            timeout=900,
        )

    @patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_URL": "https://service.invalid"})
    @patch("service.dual_review_primary.AiGatewayClient.execute")
    def test_capacity_error_is_unavailable(self, review) -> None:
        review.return_value = AiResult.unavailable(
            "codex",
            "Codex service request failed: 401 too many active jobs: max 10",
        )
        result = run_codex_primary_review(prompt="review")
        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)

    @patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_URL": "https://service.invalid"})
    @patch("service.dual_review_primary.AiGatewayClient.execute")
    def test_structured_capacity_failure_is_unavailable(self, review) -> None:
        review.return_value = AiResult(
            provider="codex",
            model="codex-cli",
            success=False,
            error="rate limit exceeded",
            raw={"failure_category": "quota_or_capacity_failure"},
        )

        result = run_codex_primary_review(prompt="review")

        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)

    @patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_URL": "https://service.invalid"})
    @patch("service.dual_review_primary.AiGatewayClient.execute")
    def test_structured_network_failure_is_unavailable(self, review) -> None:
        review.return_value = AiResult(
            provider="codex",
            model="codex-cli",
            success=False,
            error="temporary DNS failure",
            raw={"failure_category": "transient_service_failure"},
        )

        result = run_codex_primary_review(prompt="review")

        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)

    @patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_URL": "https://service.invalid"})
    @patch("service.dual_review_primary.AiGatewayClient.execute")
    def test_structured_service_outages_are_unavailable(self, review) -> None:
        for category in (
            "auth_or_config_failure",
            "service_restart",
            "stale_job_timeout",
        ):
            with self.subTest(category=category):
                review.return_value = AiResult(
                    provider="codex",
                    model="codex-cli",
                    success=False,
                    error=category,
                    raw={"failure_category": category},
                )

                result = run_codex_primary_review(prompt="review")

                self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)

    @patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_URL": "https://service.invalid"})
    @patch("service.dual_review_primary.AiGatewayClient.execute")
    def test_protocol_error_is_invalid(self, review) -> None:
        review.return_value = AiResult.unavailable(
            "codex",
            "response did not contain review JSON",
        )
        result = run_codex_primary_review(prompt="review")
        self.assertEqual(result["verdict"], VERDICT_INVALID)


if __name__ == "__main__":
    unittest.main()
