from service.ai_gateway_service import _resolve_analyze_model


# Direct handler invocation: no HTTP server, provider, account lookup or disk quota.
from contextlib import ExitStack
import threading
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
        self.stack.enter_context(patch.object(self.quota, "_codex_account_snapshot", return_value={
            "status": "available", "rate_limits": {"primary": {"used_percent": 74}},
        }))
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
            "get_health_monitor": Mock(return_value=SimpleNamespace(status="ok", record=Mock())),
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

    def test_weekly_budget_limits_combined_review_cost(self):
        # Each reviewer fits separately; their total exceeds the weekly window.
        self.quota._weekly_budget = 0.003
        status, _ = self.review(reviewers=["gpt", "claude"])
        self.assertEqual(status, 429)
        self.llm.parallel_review.assert_not_called()

    def test_corrupt_store_blocks_analyze_and_review_before_provider(self):
        self.quota._store_available = False
        self.assertEqual(self.analyze()[0], 429)
        self.assertEqual(self.review()[0], 429)
        self.llm.complete.assert_not_called()
        self.llm.parallel_review.assert_not_called()

    def test_loaded_invalid_usage_blocks_handlers_before_provider(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "quota.json"
            store.write_text(json.dumps({"records": {self.repo: {"reported_tokens_input": "invalid"}}}))
            with patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_QUOTA_STORE": str(store)}):
                quota = QuotaManager()
            with patch.object(gateway, "get_quota_manager", return_value=quota):
                self.assertEqual(self.analyze()[0], 429)
                self.assertEqual(self.review()[0], 429)
        self.llm.complete.assert_not_called()
        self.llm.parallel_review.assert_not_called()

    def test_nonfinite_windows_block_both_handlers(self):
        for name in ("_daily_budget", "_weekly_budget"):
            for value in (float("nan"), float("inf")):
                with self.subTest(window=name, value=value):
                    with patch.object(self.quota, name, value):
                        self.assertEqual(self.analyze()[0], 429)
                        self.assertEqual(self.review()[0], 429)
        self.llm.complete.assert_not_called()
        self.llm.parallel_review.assert_not_called()

    def test_exhausted_budget_blocks_all_review_providers(self) -> None:
        self.quota._daily_budget = 0
        status, _ = self.review(verifier="codex")
        self.assertEqual(status, 429)
        self.llm.parallel_review.assert_not_called()
        self.codex.execute.assert_not_called()

    def test_exhausted_auth_budget_blocks_analyze_despite_spoofed_source_repositories(self) -> None:
        """Auth bucket exhausted: payload cannot pick another repo to bypass quota."""
        self.quota._repo_budgets[self.repo] = {"daily": 0.0}
        self.llm.complete.return_value = LlmResult(
            provider="openai", model="gpt-5.4-mini", output="should-not-run",
        )
        for source in ("Synthetic/other-a", "Synthetic/other-b", "Synthetic/other-c"):
            with self.subTest(source_repository=source):
                self.llm.complete.reset_mock()
                status, _ = self.analyze(source_repository=source)
                self.assertEqual(status, 429)
                self.llm.complete.assert_not_called()
                self.assertNotIn(source, self.quota._records)

    def test_analyze_and_review_charge_same_authenticated_quota_bucket(self) -> None:
        """Limited-budget analyze + review must land in the authenticated identity bucket."""
        self.llm.complete.return_value = LlmResult(
            provider="openai", model="gpt-5.4-mini", output="ok",
            tokens_input=10, tokens_output=2, usage_complete=True,
        )
        self.llm.parallel_review.return_value = [
            LlmResult(
                provider="openai", model="gpt-5.4-mini", output="ok",
                tokens_input=10, tokens_output=2, usage_complete=True,
            ),
        ]
        display = "Synthetic/display-only"
        status_analyze, _ = self.analyze(source_repository=display)
        status_review, _ = self.review(source_repository=display)
        self.assertEqual(status_analyze, 200)
        self.assertEqual(status_review, 200)
        record = self.quota._records[self.repo]
        self.assertGreaterEqual(record.api_calls, 2)
        self.assertGreater(record.api_key_cost_usd, 0)
        self.assertNotIn(display, self.quota._records)

    def analyze(self, *, claims=None, **overrides):
        payload = {"prompt": "synthetic review", "model": "gpt-5.4-mini"}
        payload.update(overrides)
        gateway.AiGatewayRequestHandler._handle_analyze(
            object(),
            claims if claims is not None else {"repository": self.repo},
            payload,
        )
        return self.response.call_args.args[1:]

    def test_analyze_admission_includes_system_and_output_limit(self) -> None:
        self.llm.complete.return_value = LlmResult(provider="openai", model="gpt-5.4-mini", output="ok")
        for system, max_tokens in (("synthetic " * 2000, 10), ("", 8192)):
            with self.subTest(system_present=bool(system), max_tokens=max_tokens):
                quota_prompt = system + "\nsynthetic review"
                full_cost = estimate_cost("gpt-5.4-mini", estimate_tokens(quota_prompt), max_tokens)
                self.quota._daily_budget = full_cost / 2
                status, _ = self.analyze(system=system, max_tokens=max_tokens)
                self.assertEqual(status, 429)
                self.llm.complete.assert_not_called()
                self.assertNotIn(self.repo, self.quota._records)

    def test_analyze_records_complete_reported_usage_once(self) -> None:
        self.llm.complete.return_value = LlmResult(
            provider="openai", model="gpt-5.4-mini", output="ok",
            tokens_input=1000, tokens_output=8000, usage_complete=True,
        )
        with patch.object(self.quota, "check", wraps=self.quota.check) as check, \
                patch.object(self.quota, "record", wraps=self.quota.record) as record_usage:
            status, _ = self.analyze(system="synthetic system", max_tokens=8192,
                                     source_repository="Synthetic/display-only")
        self.assertEqual(status, 200)
        check.assert_called_once_with(self.repo, "gpt-5.4-mini", "synthetic system\nsynthetic review",
                                      estimated_output_tokens=8192)
        record_usage.assert_called_once()
        record = self.quota._records[self.repo]
        self.assertEqual(record.api_calls, 1)
        self.assertEqual((record.reported_tokens_input, record.reported_tokens_output), (1000, 8000))
        self.assertFalse(record.reported_usage_incomplete)
        self.assertAlmostEqual(record.api_key_cost_usd, estimate_cost("gpt-5.4-mini", 1000, 8000))
        self.assertNotIn("Synthetic/display-only", self.quota._records)

    def test_analyze_failed_results_retain_partial_or_missing_usage(self) -> None:
        for tokens_input, tokens_output, complete in ((20, 3, True), (20, None, False), (None, None, False)):
            with self.subTest(tokens_input=tokens_input, tokens_output=tokens_output, complete=complete):
                self.quota._records.clear()
                self.llm.complete.return_value = LlmResult(
                    provider="openai", model="gpt-5.4-mini", output="", success=False,
                    error="synthetic parse failure", tokens_input=tokens_input,
                    tokens_output=tokens_output, usage_complete=complete,
                )
                with patch.object(self.quota, "record", wraps=self.quota.record) as record_usage:
                    status, _ = self.analyze(system="synthetic context")
                self.assertEqual(status, 502)
                record_usage.assert_called_once()
                record = self.quota._records[self.repo]
                self.assertEqual(record.api_calls, 1)
                self.assertEqual((record.reported_tokens_input, record.reported_tokens_output),
                                 (tokens_input, tokens_output))
                self.assertEqual(record.reported_usage_incomplete, not complete)
                self.assertGreater(record.api_key_cost_usd, 0)

    def test_analyze_zero_reported_usage_is_not_unknown(self) -> None:
        self.llm.complete.return_value = LlmResult(
            provider="openai", model="gpt-5.4-mini", output="",
            tokens_input=0, tokens_output=0, usage_complete=True,
        )
        self.analyze()
        record = self.quota._records[self.repo]
        self.assertEqual((record.reported_tokens_input, record.reported_tokens_output), (0, 0))
        self.assertFalse(record.reported_usage_incomplete)
        self.assertEqual(record.api_calls, 1)
        self.assertEqual(record.api_key_cost_usd, 0)

    def test_analyze_records_usage_before_receipt_assembly_failure(self) -> None:
        self.llm.complete.return_value = LlmResult(
            provider="openai", model="gpt-5.4-mini", output="ok",
            tokens_input=12, tokens_output=1, usage_complete=True,
        )
        with patch.object(gateway, "build_provenance_receipt", side_effect=ValueError("synthetic receipt failure")), \
                patch.object(self.quota, "record", wraps=self.quota.record) as record_usage:
            with self.assertRaisesRegex(ValueError, "synthetic receipt failure"):
                self.analyze()
        record_usage.assert_called_once()
        self.llm.complete.assert_called_once()
        record = self.quota._records[self.repo]
        self.assertEqual(record.api_calls, 1)
        self.assertEqual((record.reported_tokens_input, record.reported_tokens_output), (12, 1))

    def _invoke_api(self, endpoint):
        payload = {"prompt": "synthetic review", "model": "gpt-5.4-mini", "source_repository": self.repo}
        claims = {"repository": self.repo}
        if endpoint == "review":
            payload.update(reviewers=["gpt"], verifier=None)
            gateway.AiGatewayRequestHandler._handle_review(object(), claims, payload)
        else:
            gateway.AiGatewayRequestHandler._handle_analyze(object(), claims, payload)

    def test_concurrent_api_requests_share_admission_before_spending(self) -> None:
        for first, second in (("review", "review"), ("review", "analyze"),
                              ("analyze", "review"), ("analyze", "analyze")):
            with self.subTest(first=first, second=second):
                self._assert_serial_budget(first, second)

    def _assert_serial_budget(self, first, second):
        self.quota = QuotaManager()
        entered = threading.Event()
        release = threading.Event()
        second_at_quota = threading.Event()
        second_provider = threading.Event()
        errors = []
        statuses = []
        provider_calls = []
        counts = {
            "review": (estimate_tokens(gateway.REVIEW_SYSTEM_PROMPT + "\nsynthetic review"), gateway.DEFAULT_MAX_TOKENS),
            "analyze": (estimate_tokens("\nsynthetic review"), gateway.DEFAULT_MAX_TOKENS),
        }
        costs = {name: estimate_cost("gpt-5.4-mini", *tokens) for name, tokens in counts.items()}
        self.quota._daily_budget = max(costs[first], costs[second]) + min(costs[first], costs[second]) / 2

        def get_quota():
            if threading.current_thread().name == "second-budget-request":
                second_at_quota.set()
            return self.quota

        def provider(endpoint):
            provider_calls.append(endpoint)
            if threading.current_thread().name == "first-budget-request":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("synthetic provider release timed out")
            else:
                second_provider.set()
            tokens_input, tokens_output = counts[endpoint]
            return LlmResult(provider="openai", model="gpt-5.4-mini", output="x" * (tokens_output * 4),
                             tokens_input=tokens_input, tokens_output=tokens_output, usage_complete=True)

        def invoke(endpoint):
            try:
                self._invoke_api(endpoint)
            except Exception as exc:
                errors.append(exc)

        with patch.object(gateway, "get_quota_manager", side_effect=get_quota), \
                patch.object(gateway, "_json_response", side_effect=lambda _handler, status, _body: statuses.append(status)):
            self.llm.parallel_review.side_effect = lambda **kwargs: [provider("review")]
            self.llm.complete.side_effect = lambda **kwargs: provider("analyze")
            workers = [threading.Thread(target=invoke, args=(endpoint,), name=name, daemon=True)
                       for endpoint, name in ((first, "first-budget-request"), (second, "second-budget-request"))]
            workers[0].start()
            try:
                self.assertTrue(entered.wait(2))
                workers[1].start()
                self.assertTrue(second_at_quota.wait(2))
                self.assertFalse(second_provider.wait(0.1), "second provider entered before first usage was recorded")
            finally:
                release.set()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(3)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(statuses), [200, 429])
        self.assertEqual(provider_calls, [first])
        self.assertLessEqual(self.quota._records[self.repo].api_key_cost_usd, self.quota._daily_budget)

    def test_provider_exception_releases_admission_for_next_request(self) -> None:
        for endpoint in ("review", "analyze"):
            with self.subTest(endpoint=endpoint):
                provider = self.llm.parallel_review if endpoint == "review" else self.llm.complete
                provider.side_effect = RuntimeError("synthetic provider failure")
                with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
                    self._invoke_api(endpoint)
                provider.side_effect = None
                result = LlmResult(provider="openai", model="gpt-5.4-mini", output="synthetic")
                provider.return_value = [result] if endpoint == "review" else result
                finished = threading.Event()
                errors = []

                def next_request():
                    try:
                        self._invoke_api(endpoint)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        finished.set()

                worker = threading.Thread(target=next_request, daemon=True)
                worker.start()
                self.assertTrue(finished.wait(3), "admission remained locked after exception")
                worker.join(1)
                self.assertEqual(errors, [])

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
