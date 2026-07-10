from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "codex_audit_service.py"
spec = importlib.util.spec_from_file_location("codex_audit_service_test", SCRIPT_PATH)
if spec is None or spec.loader is None:  # pragma: no cover - defensive guard for env issues
    raise RuntimeError(f"Failed to load module spec from {SCRIPT_PATH}")
_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_service)  # type: ignore[arg-type]

PR_REVIEW_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_codex_pr_review.py"
pr_spec = importlib.util.spec_from_file_location("run_codex_pr_review_test", PR_REVIEW_SCRIPT_PATH)
if pr_spec is None or pr_spec.loader is None:  # pragma: no cover - defensive guard for env issues
    raise RuntimeError(f"Failed to load module spec from {PR_REVIEW_SCRIPT_PATH}")
_pr_review = importlib.util.module_from_spec(pr_spec)
pr_spec.loader.exec_module(_pr_review)  # type: ignore[arg-type]


class TestComplexityModelRouting(unittest.TestCase):
    """Validate complexity-to-model adaptation behavior used in codex-audit service."""

    def setUp(self) -> None:
        self._orig_low = _service.AI_GATEWAY_LLM_DEFAULT_MODEL_LOW
        self._orig_medium = _service.AI_GATEWAY_LLM_DEFAULT_MODEL_MEDIUM
        self._orig_high = _service.AI_GATEWAY_LLM_DEFAULT_MODEL_HIGH
        self._orig_effort_low = _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_LOW
        self._orig_effort_medium = _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_MEDIUM
        self._orig_effort_high = _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_HIGH
        self._orig_low_line = _service.TASK_COMPLEXITY_MEDIUM_LINE_THRESHOLD
        self._orig_low_char = _service.TASK_COMPLEXITY_MEDIUM_PROMPT_THRESHOLD
        self._orig_high_line = _service.TASK_COMPLEXITY_HIGH_LINE_THRESHOLD
        self._orig_high_char = _service.TASK_COMPLEXITY_HIGH_PROMPT_THRESHOLD

        _service.AI_GATEWAY_LLM_DEFAULT_MODEL_LOW = "gpt-test-low"
        _service.AI_GATEWAY_LLM_DEFAULT_MODEL_MEDIUM = "gpt-test-medium"
        _service.AI_GATEWAY_LLM_DEFAULT_MODEL_HIGH = "gpt-test-high"
        _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_LOW = "low"
        _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_MEDIUM = "medium"
        _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_HIGH = "high"
        _service.TASK_COMPLEXITY_MEDIUM_LINE_THRESHOLD = 40
        _service.TASK_COMPLEXITY_HIGH_LINE_THRESHOLD = 80
        _service.TASK_COMPLEXITY_MEDIUM_PROMPT_THRESHOLD = 120
        _service.TASK_COMPLEXITY_HIGH_PROMPT_THRESHOLD = 250

    def tearDown(self) -> None:
        _service.AI_GATEWAY_LLM_DEFAULT_MODEL_LOW = self._orig_low
        _service.AI_GATEWAY_LLM_DEFAULT_MODEL_MEDIUM = self._orig_medium
        _service.AI_GATEWAY_LLM_DEFAULT_MODEL_HIGH = self._orig_high
        _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_LOW = self._orig_effort_low
        _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_MEDIUM = self._orig_effort_medium
        _service.AI_GATEWAY_CODEX_DEFAULT_REASONING_EFFORT_HIGH = self._orig_effort_high
        _service.TASK_COMPLEXITY_MEDIUM_LINE_THRESHOLD = self._orig_low_line
        _service.TASK_COMPLEXITY_MEDIUM_PROMPT_THRESHOLD = self._orig_low_char
        _service.TASK_COMPLEXITY_HIGH_LINE_THRESHOLD = self._orig_high_line
        _service.TASK_COMPLEXITY_HIGH_PROMPT_THRESHOLD = self._orig_high_char

    def test_review_alias_uses_complexity_routing(self) -> None:
        model = _service._resolve_model(
            {
                "model": "auto",
                "complexity": "high",
                "prompt": "review pr",
                "changed_lines": "120",
            },
            "review",
        )
        self.assertEqual(model, "gpt-test-high")

    def test_auto_complexity_uses_prompt_estimation(self) -> None:
        payload = {
            "model": "auto",
            "prompt": "x" * 200,
            "changed_files": 1,
            "changed_lines": 120,
        }
        model = _service._resolve_model(payload, "pr_review")
        self.assertEqual(model, "gpt-test-high")

    def test_reasoning_effort_uses_explicit_or_complexity_routing(self) -> None:
        explicit = _service._resolve_reasoning_effort(
            {"reasoning_effort": "xhigh", "prompt": "review"},
            "pr_review",
        )
        self.assertEqual(explicit, "xhigh")

        inferred = _service._resolve_reasoning_effort(
            {"reasoning_effort": "auto", "prompt": "x" * 200, "changed_lines": 120},
            "pr_review",
        )
        self.assertEqual(inferred, "high")

    def test_codex_command_passes_reasoning_effort(self) -> None:
        with mock.patch.object(_service.shutil, "which", return_value="/usr/bin/codex"):
            command = _service.CodexAdapter()._build_command(
                Path("last-message.md"),
                "gpt-test",
                "high",
            )

        self.assertIn("--model", command)
        self.assertIn("gpt-test", command)
        self.assertIn("-c", command)
        self.assertIn("model_reasoning_effort=high", command)

    def test_task_requires_async_review_and_execute(self) -> None:
        self.assertTrue(_service._task_requires_async("pr_review"))
        self.assertTrue(_service._task_requires_async("review"))
        self.assertTrue(_service._task_requires_async("execute"))
        self.assertTrue(_service._task_requires_async("monthly_snapshot_audit"))
        self.assertEqual(_service._adapter_task("execute", ""), "execute")
        self.assertEqual(_service._adapter_task("pr_review", ""), "execute")
        with self.assertRaisesRegex(ValueError, "Unsupported task='analyze'"):
            _service.resolve_adapter("analyze", "")

    def test_codex_usage_limit_is_classified_as_capacity_failure(self) -> None:
        self.assertEqual(
            _service._classify_codex_exec_failure("You have reached your Codex usage limits for code reviews."),
            "quota_or_capacity_failure",
        )

    def test_direct_api_model_for_complexity_reads_provider_specific_env(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"CODEX_AUDIT_OPENAI_LOW_COMPLEXITY_MODEL": "gpt-low"},
            clear=True,
        ):
            self.assertEqual(
                _pr_review._direct_api_model_for_complexity("openai", "low"),
                "gpt-low",
            )


if __name__ == "__main__":
    unittest.main()
