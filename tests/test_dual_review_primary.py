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
        self.assertEqual(result.raw, {
            "failure_category": "transient_service_failure",
            "request_phase": "submit",
        })
        self.assertNotIn("http_status", result.raw)

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
        expected_prompt = (
            "You are the primary Codex reviewer for quantitative strategy promotion, risk, and recovery decisions. "
            "Respond with JSON only: "
            '{"verdict":"approve"|"reject","confidence":0.0-1.0,"summary":"..."}'
            "\n\nreview"
        )
        review.assert_called_once_with(
            expected_prompt,
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

    def _research_env(self):
        return patch.dict("os.environ", {
            "CODEX_AUDIT_SERVICE_URL": "https://service.invalid",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-token",
            "GITHUB_REPOSITORY": "QuantStrategyLab/CryptoStrategies",
        })

    def test_research_failure_keeps_allowlisted_diagnostics(self) -> None:
        sentinel = "sk-secret https://user:pass@service.invalid private-job"
        cases = (
            {"failure_category": "auth_or_config_failure", "request_phase": "health", "http_status": 401},
            {"failure_category": "quota_or_capacity_failure", "request_phase": "submit", "http_status": 429},
            {"failure_category": "transient_service_failure", "request_phase": "submit", "http_status": 503},
            {"failure_category": "patch_contract_failure", "request_phase": "submit"},
            {"failure_category": "transient_service_failure", "request_phase": "oidc"},
        )
        for raw in cases:
            with self.subTest(raw=raw), self._research_env(), patch(
                "service.dual_review_primary.AiGatewayClient.execute",
            ) as execute:
                execute.return_value = AiResult(
                    provider="codex", model="", success=False,
                    error="subscription_research_http_failure",
                    raw={**raw, "error": sentinel, "token": sentinel, "job": {"prompt": sentinel}},
                )
                result = run_codex_primary_review(prompt="review", research_stage="promotion_review")
            self.assertEqual(result["error"], "research_primary_result_unavailable")
            self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)
            self.assertEqual(result["provider"], "codex")
            self.assertEqual(result["research_stage"], "promotion_review")
            self.assertEqual(result["failure_category"], raw["failure_category"])
            self.assertEqual(result["request_phase"], raw["request_phase"])
            if "http_status" in raw:
                self.assertEqual(result["http_status"], raw["http_status"])
            else:
                self.assertNotIn("http_status", result)
            self.assertNotIn(sentinel, repr(result))
            execute.assert_called_once()

    def test_research_failure_drops_forged_diagnostics(self) -> None:
        sentinel = "bearer-sentinel https://token:secret@service.invalid"
        forged = {
            "failure_category": "auth_or_config_failure\ninjected",
            "request_phase": "submit",
            "http_status": True,
            "token": sentinel,
            "url": sentinel,
            "job": sentinel,
            "output": sentinel,
        }
        with self._research_env(), patch("service.dual_review_primary.AiGatewayClient.execute") as execute:
            execute.return_value = AiResult(
                provider="codex", model="", success=False, error="subscription_research_http_failure", raw=forged,
            )
            result = run_codex_primary_review(prompt="review", research_stage="promotion_review")
        self.assertEqual(result["error"], "research_primary_result_unavailable")
        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)
        self.assertNotIn("failure_category", result)
        self.assertEqual(result["request_phase"], "submit")
        self.assertNotIn("http_status", result)
        self.assertNotIn(sentinel, repr(result))
        for bad_status in ("401", 99, 600, 401.5, False):
            with self.subTest(http_status=bad_status), self._research_env(), patch(
                "service.dual_review_primary.AiGatewayClient.execute",
            ) as execute:
                execute.return_value = AiResult(
                    provider="codex", model="", success=False, error="subscription_research_http_failure",
                    raw={"failure_category": "not-a-category", "request_phase": "later", "http_status": bad_status},
                )
                result = run_codex_primary_review(prompt="review", research_stage="promotion_review")
            self.assertNotIn("failure_category", result)
            self.assertNotIn("request_phase", result)
            self.assertNotIn("http_status", result)

    def test_research_primary_discards_list_failure_category(self) -> None:
        with self._research_env(), patch("service.dual_review_primary.AiGatewayClient.execute") as execute:
            execute.return_value = AiResult(
                provider="codex", model="", success=False,
                error="subscription_research_http_failure",
                raw={"failure_category": []},
            )
            result = run_codex_primary_review(prompt="review", research_stage="promotion_review")
        self.assertEqual(result["error"], "research_primary_result_unavailable")
        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["research_stage"], "promotion_review")
        self.assertNotIn("failure_category", result)
        execute.assert_called_once()

    def test_research_primary_discards_dict_request_phase(self) -> None:
        with self._research_env(), patch("service.dual_review_primary.AiGatewayClient.execute") as execute:
            execute.return_value = AiResult(
                provider="codex", model="", success=False,
                error="subscription_research_http_failure",
                raw={"request_phase": {}},
            )
            result = run_codex_primary_review(prompt="review", research_stage="promotion_review")
        self.assertEqual(result["error"], "research_primary_result_unavailable")
        self.assertEqual(result["verdict"], VERDICT_UNAVAILABLE)
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["research_stage"], "promotion_review")
        self.assertNotIn("request_phase", result)
        execute.assert_called_once()

    def test_research_success_ignores_diagnostic_fields(self) -> None:
        output = '{"verdict":"approve","confidence":0.8,"summary":"ok"}'
        with self._research_env(), patch("service.dual_review_primary.AiGatewayClient.execute") as execute:
            execute.return_value = AiResult(
                provider="codex", model="gpt-6-astra", success=True, output=output,
                raw={
                    "status": "succeeded", "provider": "codex", "research_stage": "promotion_review",
                    "reasoning_effort": "xhigh", "model": "gpt-6-astra", "output": output,
                    "failure_category": "auth_or_config_failure", "request_phase": "poll", "http_status": 401,
                },
            )
            result = run_codex_primary_review(prompt="review", research_stage="promotion_review")
        self.assertEqual(result["verdict"], "approve")
        self.assertNotIn("failure_category", result)
        self.assertNotIn("http_status", result)
        self.assertNotIn("request_phase", result)
        execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
