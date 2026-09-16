from __future__ import annotations

import json
from types import SimpleNamespace

from client.config import GatewayConfig
from scripts import run_semantic_quality_acceptance as acceptance


def test_acceptance_calls_four_fixed_cases_in_order_and_caps_output(tmp_path):
    calls = []
    headings = "\n".join(acceptance.SECTION_TITLES) + "\n"
    output = headings + "x" * (acceptance.MAX_OUTPUT_CHARS - len(headings))

    class FakeClient:
        def execute(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return SimpleNamespace(
                success=True,
                output=output,
                provider="codex",
                model="gpt-5.6-terra",
                raw={
                    "job_id": "job-1",
                    "status": "succeeded",
                    "provider": "codex",
                    "research_stage": "drift_analysis",
                    "reasoning_effort": "medium",
                },
            )

    output_path = tmp_path / "summary.json"
    summary = acceptance.run_acceptance(
        output_path,
        config=GatewayConfig(service_url="https://gateway.invalid"),
        client_factory=lambda _config: FakeClient(),
    )

    assert len(calls) == 4
    assert [call[1]["source_ref"] for call in calls] == ["d" * 40] * 4
    for _prompt, kwargs in calls:
        assert kwargs["task"] == "execute"
        assert kwargs["mode"] == "review_only"
        assert kwargs["research_stage"] == "drift_analysis"
        assert kwargs["sandbox"] == "read-only"
        assert kwargs["allowed_providers"] == ["codex"]
        assert kwargs["model"] == "gpt-5.6-terra"
        assert kwargs["reasoning_effort"] == "medium"
        assert kwargs["timeout"] == 600
    assert all(len(case["output"]) == acceptance.MAX_OUTPUT_CHARS for case in summary["cases"])
    assert json.loads(output_path.read_text(encoding="utf-8")) == summary
    assert all("raw" not in case and "error" not in case for case in summary["cases"])
    assert all(case["status"] == "completed" and case["model"] == "gpt-5.6-terra" for case in summary["cases"])


def test_acceptance_stops_after_first_failure_and_defers_remaining(tmp_path):
    calls = []

    class FakeClient:
        def execute(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return SimpleNamespace(success=False, output="", provider="codex", model="", raw={})

    summary = acceptance.run_acceptance(
        tmp_path / "summary.json",
        config=GatewayConfig(service_url="https://gateway.invalid"),
        client_factory=lambda _config: FakeClient(),
    )

    assert len(calls) == 1
    assert [case["status"] for case in summary["cases"]] == ["deferred"] * 4
    assert summary["status"] == "deferred"
    assert all("raw" not in case and "error" not in case for case in summary["cases"])
    assert summary["cases"][0]["reason"] == "call_failed"
    assert all(case["reason"] == "stopped_after_failure" for case in summary["cases"][1:])


def test_acceptance_rejects_oversized_or_malformed_output(tmp_path):
    for output, reason in (("x" * (acceptance.MAX_OUTPUT_CHARS + 1), "output_too_long"), ("missing headings", "format_invalid")):
        calls = []

        class FakeClient:
            def execute(self, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return SimpleNamespace(success=True, output=output, provider="codex", model="gpt-5.6-terra")

        summary = acceptance.run_acceptance(
            tmp_path / f"{reason}.json",
            config=GatewayConfig(service_url="https://gateway.invalid"),
            client_factory=lambda _config: FakeClient(),
        )
        assert len(calls) == 1
        assert summary["cases"][0]["reason"] == reason
        assert summary["cases"][0]["status"] == "deferred"
        if reason == "format_invalid":
            assert summary["cases"][0]["output"] == output
        else:
            assert "output" not in summary["cases"][0]
