from service.ai_gateway_service import _resolve_analyze_model


# Direct handler invocation: no HTTP server, provider, account lookup or disk quota.
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from service import ai_gateway_service as gateway
from service.adapters.llm_adapter import LlmResult
from service.quota import QuotaManager, QuotaRecord, estimate_cost, estimate_tokens


def test_analyze_model_resolves_codex_cli_to_api_backed_model() -> None:
    assert _resolve_analyze_model("codex-cli") == "claude-sonnet-4-6"
    assert _resolve_analyze_model("gpt-5.4-mini") == "gpt-5.4-mini"


class ReviewQuotaTests(TestCase):
    repo = "Synthetic/caller"

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict("os.environ", {}, clear=True))
        self.quota = QuotaManager()
        self.llm = Mock()
        self.llm.parallel_review.return_value = []
        self.codex = Mock()
        self.codex.execute.return_value = SimpleNamespace(output="synthetic", success=True, error="")
        self.response = Mock()
        mocks = {
            "_check_rate_limit": Mock(), "_audit_log": Mock(),
            "LlmAdapter": Mock(return_value=self.llm),
            "CodexAdapter": Mock(return_value=self.codex),
            "_trusted_automation_proof_for_review": Mock(return_value=None),
            "get_quota_manager": Mock(return_value=self.quota),
            "read_org_health": Mock(return_value={"status": "ok"}),
            "get_health_monitor": Mock(return_value=SimpleNamespace(status="ok")),
            "load_autonomy_policy": Mock(return_value={}),
            "compute_recommended_action": Mock(return_value={"action": "manual_review", "confidence": 0, "risk": "low"}),
            "_json_response": self.response,
        }
        for name, mock in mocks.items():
            self.stack.enter_context(patch.object(gateway, name, mock))
        self.stack.enter_context(patch("service.quota.recommend_model", return_value="gpt-5.4-mini"))

    def review(self, *, claims=None, **overrides):
        payload = {"prompt": "synthetic review", "reviewers": ["gpt"], "model": "gpt-5.4-mini", "verifier": None}
        payload.update(overrides)
        gateway.AiGatewayRequestHandler._handle_review(object(), claims if claims is not None else {"repository": self.repo}, payload)
        return self.response.call_args.args[1:]

    def test_exhausted_budget_blocks_all_review_providers(self) -> None:
        self.quota._daily_budget = 0
        status, _ = self.review(verifier="codex")
        self.assertEqual(status, 429)
        self.llm.parallel_review.assert_not_called()
        self.codex.execute.assert_not_called()

    def test_parallel_models_share_the_request_budget(self) -> None:
        self.quota._daily_budget = 0.003
        # Each 4000-token output estimate fits; their sum does not.
        status, _ = self.review(reviewers=["gpt", "claude"])
        self.assertEqual(status, 429)
        self.llm.parallel_review.assert_not_called()

    def test_non_finite_estimate_blocks_review(self) -> None:
        with patch.object(self.quota, "check", return_value={"allowed": True, "cost_estimate_usd": float("nan")}):
            status, _ = self.review()
        self.assertEqual(status, 429)
        self.llm.parallel_review.assert_not_called()

    def test_reported_usage_survives_partial_failure_and_uses_authenticated_repo(self) -> None:
        self.llm.parallel_review.return_value = [
            LlmResult(provider="openai", model="gpt-5.4-mini", output="ok", tokens_input=10, tokens_output=2, usage_complete=True),
            LlmResult(provider="anthropic", model="claude-sonnet-4-6", output="", success=False,
                      error="synthetic parse failure", tokens_input=20, tokens_output=3, usage_complete=True),
        ]
        status, body = self.review(reviewers=["gpt", "claude"], source_repository="Synthetic/other")
        self.assertEqual(status, 200)
        record = self.quota._records[self.repo]
        self.assertEqual(record.api_calls, 2)
        self.assertEqual((record.reported_tokens_input, record.reported_tokens_output), (30, 5))
        self.assertFalse(record.reported_usage_incomplete)
        self.assertNotIn("Synthetic/other", self.quota._records)
        self.assertEqual(body["results"][1]["usage"]["tokens_input"], 20)
        self.assertEqual(body["quota"]["cost_basis"], "estimate")

    def test_missing_usage_remains_unknown_with_nonzero_estimated_cost(self) -> None:
        self.llm.parallel_review.return_value = [LlmResult(provider="openai", model="gpt-5.4-mini", output="", success=False)]
        _, body = self.review()
        record = self.quota._records[self.repo]
        self.assertIsNone(record.reported_tokens_input)
        self.assertIsNone(record.reported_tokens_output)
        self.assertTrue(record.reported_usage_incomplete)
        self.assertGreater(record.api_key_cost_usd, 0)
        self.assertIsNone(body["results"][0]["usage"]["tokens_input"])
        self.assertFalse(body["results"][0]["usage"]["complete"])

    def test_usage_is_recorded_before_codex_failure(self) -> None:
        self.llm.parallel_review.return_value = [LlmResult(provider="openai", model="gpt-5.4-mini", output="ok",
                                                         tokens_input=12, tokens_output=1, usage_complete=True)]
        self.codex.execute.side_effect = RuntimeError("synthetic verifier failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic verifier failure"):
            self.review(verifier="codex")
        record = self.quota._records[self.repo]
        self.assertEqual(record.reported_tokens_input, 12)
        self.assertEqual(record.api_calls, 1)
        self.assertEqual(record.codex_calls, 1)
        self.assertGreater(record.codex_cost_usd, 0)
        self.assertGreater(record.api_key_cost_usd, 0)
        self.assertLess(record.api_key_cost_usd, record.total_cost_usd)

    def test_unknown_model_keeps_explicit_existing_fallback_estimate(self) -> None:
        self.llm.parallel_review.return_value = [LlmResult(provider="openai", model="gpt-synthetic-unknown", output="ok")]
        status, body = self.review(model="gpt-synthetic-unknown")
        self.assertEqual(status, 200)
        self.assertEqual(body["quota"]["cost_basis"], "estimate")
        self.assertEqual(body["quota"]["model_estimates"][0]["cost_estimate_source"], "fallback")
        self.assertGreater(self.quota._records[self.repo].api_key_cost_usd, 0)

    def test_reported_counters_roundtrip_partial_and_zero_without_relabeling_legacy_estimates(self) -> None:
        self.quota.record(self.repo, "gpt-5.4-mini", "old prompt", "old output")
        self.quota.record(self.repo, "gpt-5.4-mini", "prompt", "", reported_tokens_input=9,
                          reported_tokens_output=0, reported_usage_complete=True)
        self.quota.record(self.repo, "gpt-5.4-mini", "prompt", "", reported_tokens_input=None,
                          reported_tokens_output=None, reported_usage_complete=False)
        record = QuotaRecord.from_dict(self.quota._records[self.repo].to_dict())
        self.assertEqual((record.reported_tokens_input, record.reported_tokens_output), (9, 0))
        self.assertTrue(record.reported_usage_incomplete)
        self.assertEqual(record.api_calls, 3)
        old = QuotaRecord.from_dict({"repo": self.repo, "tokens_input": 50, "tokens_output": 20})
        self.assertIsNone(old.reported_tokens_input)
        self.assertIsNone(old.reported_tokens_output)
        # Existing legacy string-based estimator remains unchanged.
        expected = estimate_cost("gpt-5.4-mini", estimate_tokens("old prompt"), estimate_tokens("old output"))
        self.assertGreater(self.quota._records[self.repo].api_key_cost_usd, expected)
        record.last_reset_daily = 0
        self.quota._reset_if_needed(record)
        self.assertIsNone(record.reported_tokens_input)
        self.assertFalse(record.reported_usage_incomplete)

    def test_verified_local_auth_preserves_source_allowlist_and_local_quota_bucket(self) -> None:
        self.llm.parallel_review.return_value = [LlmResult(provider="openai", model="gpt-5.4-mini", output="ok")]
        with patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_AUTH": "none",
                                       "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                                       "CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES": "Synthetic/source"}):
            claims = gateway.authenticate({})
            status, _ = self.review(claims=claims, source_repository="Synthetic/source")
        self.assertEqual(status, 200)
        self.assertEqual(self.quota._records["local"].api_calls, 1)
        self.assertNotIn("Synthetic/source", self.quota._records)

    def test_oidc_cross_organization_source_still_rejected_before_model(self) -> None:
        claims = {"repository": self.repo, "auth_method": "github_oidc"}
        with self.assertRaises(PermissionError):
            self.review(claims=claims, source_repository="OtherOrg/source")
        self.llm.parallel_review.assert_not_called()

    def test_local_auth_still_enforces_source_allowlist(self) -> None:
        claims = {"repository": "local", "auth_method": "none"}
        with patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES": "Synthetic/allowed"}), self.assertRaises(PermissionError):
            self.review(claims=claims, source_repository="Synthetic/denied")
        self.llm.parallel_review.assert_not_called()
