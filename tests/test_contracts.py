"""Tests for service/contracts.py — request schema validation."""

from __future__ import annotations

import unittest

from service.contracts import (
    MODE_REVIEW_AND_FIX,
    MODE_REVIEW_ONLY,
    TASK_ANALYZE,
    TASK_EXECUTE,
    TASK_REVIEW,
    AnalyzeRequest,
    AnalyzeResponse,
    ExecuteJobResponse,
    ExecuteRequest,
    ReviewRequest,
    ReviewResponse,
    parse_analyze_request,
    parse_execute_request,
    parse_review_request,
)


class TestConstants(unittest.TestCase):
    """Task and mode constants are correctly defined."""

    def test_task_constants(self) -> None:
        self.assertEqual(TASK_ANALYZE, "analyze")
        self.assertEqual(TASK_EXECUTE, "execute")
        self.assertEqual(TASK_REVIEW, "review")

    def test_mode_constants(self) -> None:
        self.assertEqual(MODE_REVIEW_ONLY, "review_only")
        self.assertEqual(MODE_REVIEW_AND_FIX, "review_and_fix")


class TestAnalyzeRequest(unittest.TestCase):
    """AnalyzeRequest schema validation."""

    def test_valid_request(self) -> None:
        req = AnalyzeRequest(prompt="Analyze this strategy")
        req.validate()  # should not raise

    def test_empty_prompt_raises(self) -> None:
        req = AnalyzeRequest(prompt="")
        with self.assertRaises(ValueError):
            req.validate()

    def test_blank_prompt_raises(self) -> None:
        req = AnalyzeRequest(prompt="   ")
        with self.assertRaises(ValueError):
            req.validate()

    def test_negative_timeout_raises(self) -> None:
        req = AnalyzeRequest(prompt="test", timeout_seconds=-1)
        with self.assertRaises(ValueError):
            req.validate()

    def test_zero_timeout_raises(self) -> None:
        req = AnalyzeRequest(prompt="test", timeout_seconds=0)
        with self.assertRaises(ValueError):
            req.validate()

    def test_default_values(self) -> None:
        req = AnalyzeRequest(prompt="test")
        self.assertEqual(req.model, "claude-sonnet-4-6")
        self.assertEqual(req.system, "")
        self.assertEqual(req.max_tokens, 4000)
        self.assertEqual(req.timeout_seconds, 120)


class TestExecuteRequest(unittest.TestCase):
    """ExecuteRequest schema validation."""

    def test_valid_request(self) -> None:
        req = ExecuteRequest(prompt="Execute this task")
        req.validate()

    def test_empty_prompt_raises(self) -> None:
        req = ExecuteRequest(prompt="")
        with self.assertRaises(ValueError):
            req.validate()

    def test_invalid_mode_raises(self) -> None:
        req = ExecuteRequest(prompt="test", mode="invalid_mode")
        with self.assertRaises(ValueError):
            req.validate()

    def test_negative_timeout_raises(self) -> None:
        req = ExecuteRequest(prompt="test", timeout_seconds=-5)
        with self.assertRaises(ValueError):
            req.validate()

    def test_default_values(self) -> None:
        req = ExecuteRequest(prompt="test")
        self.assertEqual(req.mode, MODE_REVIEW_ONLY)
        self.assertEqual(req.model, "")
        self.assertEqual(req.timeout_seconds, 2700)
        self.assertEqual(req.source_repository, "")
        self.assertEqual(req.source_ref, "")
        self.assertEqual(req.images, [])
        self.assertIsNone(req.output_schema)

    def test_with_images_and_schema(self) -> None:
        req = ExecuteRequest(
            prompt="test",
            images=[{"path": "chart.png", "description": "Performance chart"}],
            output_schema={"type": "object"},
        )
        self.assertEqual(len(req.images), 1)
        self.assertEqual(req.output_schema, {"type": "object"})


class TestReviewRequest(unittest.TestCase):
    """ReviewRequest schema validation."""

    def test_valid_request(self) -> None:
        req = ReviewRequest(prompt="Review this PR")
        req.validate()

    def test_empty_prompt_raises(self) -> None:
        req = ReviewRequest(prompt="")
        with self.assertRaises(ValueError):
            req.validate()

    def test_empty_reviewers_raises(self) -> None:
        req = ReviewRequest(prompt="test", reviewers=())
        with self.assertRaises(ValueError):
            req.validate()

    def test_unsupported_reviewer_raises(self) -> None:
        req = ReviewRequest(prompt="test", reviewers=("unknown",))
        with self.assertRaises(ValueError):
            req.validate()

    def test_unsupported_verifier_raises(self) -> None:
        req = ReviewRequest(prompt="test", verifier="unsupported")
        with self.assertRaises(ValueError):
            req.validate()

    def test_none_verifier_is_allowed(self) -> None:
        req = ReviewRequest(prompt="test", verifier=None)
        req.validate()

    def test_zero_timeout_raises(self) -> None:
        req = ReviewRequest(prompt="test", timeout_seconds=0)
        with self.assertRaises(ValueError):
            req.validate()

    def test_default_values(self) -> None:
        req = ReviewRequest(prompt="test")
        self.assertEqual(req.reviewers, ("claude", "gpt"))
        self.assertEqual(req.verifier, "codex")
        self.assertEqual(req.mode, MODE_REVIEW_ONLY)
        self.assertEqual(req.model, "")
        self.assertEqual(req.timeout_seconds, 600)

    def test_single_reviewer(self) -> None:
        req = ReviewRequest(prompt="test", reviewers=("claude",))
        req.validate()
        self.assertEqual(req.reviewers, ("claude",))


class TestResponseSchemas(unittest.TestCase):
    """Response dataclass structure."""

    def test_analyze_response_defaults(self) -> None:
        resp = AnalyzeResponse(status="ok")
        self.assertEqual(resp.status, "ok")
        self.assertEqual(resp.output, "")
        self.assertEqual(resp.model, "")
        self.assertEqual(resp.error, "")

    def test_execute_job_response(self) -> None:
        resp = ExecuteJobResponse(status="queued", job_id="abc123")
        self.assertEqual(resp.status, "queued")
        self.assertEqual(resp.job_id, "abc123")

    def test_review_response(self) -> None:
        resp = ReviewResponse(status="ok", consensus="approve")
        self.assertEqual(resp.status, "ok")
        self.assertEqual(resp.consensus, "approve")
        self.assertEqual(resp.results, [])


class TestParseFunctions(unittest.TestCase):
    """Parsing functions correctly build schema objects from dict payloads."""

    def test_parse_analyze_request(self) -> None:
        payload = {
            "prompt": "Analyze this",
            "model": "claude-sonnet-4-6",
            "system": "You are a helper",
            "max_tokens": 2000,
            "timeout_seconds": 60,
        }
        req = parse_analyze_request(payload)
        self.assertIsInstance(req, AnalyzeRequest)
        self.assertEqual(req.prompt, "Analyze this")
        self.assertEqual(req.max_tokens, 2000)
        self.assertEqual(req.timeout_seconds, 60)

    def test_parse_analyze_request_defaults(self) -> None:
        req = parse_analyze_request({"prompt": "test"})
        self.assertEqual(req.model, "claude-sonnet-4-6")
        self.assertEqual(req.system, "")

    def test_parse_execute_request(self) -> None:
        payload = {
            "prompt": "Execute this",
            "mode": MODE_REVIEW_AND_FIX,
            "timeout_seconds": 3600,
            "source_repository": "owner/repo",
            "source_ref": "main",
        }
        req = parse_execute_request(payload)
        self.assertIsInstance(req, ExecuteRequest)
        self.assertEqual(req.mode, MODE_REVIEW_AND_FIX)
        self.assertEqual(req.timeout_seconds, 3600)
        self.assertEqual(req.source_repository, "owner/repo")

    def test_parse_execute_request_handles_invalid_images(self) -> None:
        req = parse_execute_request({"prompt": "test", "images": "not_a_list"})
        self.assertEqual(req.images, [])

    def test_parse_execute_request_handles_invalid_schema(self) -> None:
        req = parse_execute_request({"prompt": "test", "output_schema": "not_a_dict"})
        self.assertIsNone(req.output_schema)

    def test_parse_review_request(self) -> None:
        payload = {
            "prompt": "Review this",
            "reviewers": ["claude"],
            "verifier": None,
            "timeout_seconds": 300,
        }
        req = parse_review_request(payload)
        self.assertIsInstance(req, ReviewRequest)
        self.assertEqual(req.reviewers, ("claude",))
        self.assertIsNone(req.verifier)
        self.assertEqual(req.timeout_seconds, 300)

    def test_parse_review_request_defaults(self) -> None:
        req = parse_review_request({"prompt": "test"})
        self.assertEqual(req.reviewers, ("claude", "gpt"))
        self.assertEqual(req.verifier, "codex")
