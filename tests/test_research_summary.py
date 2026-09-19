"""Focused tests for standalone research_summary advisory callback."""

from __future__ import annotations

import json
import types
from unittest.mock import Mock

import pytest

from scripts.research_summary import make_summary_callback, unavailable


def _runtime(providers, client):
    config = types.SimpleNamespace(research_providers=providers)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(from_env=Mock(return_value=config)),
        client=Mock(return_value=client),
    )


def _result(*, provider="codex", model="gpt-5.6-luna", stage="research_summary",
            output=None, success=True, raw_extra=None):
    raw = {
        "status": "succeeded",
        "provider": provider,
        "model": model,
        "research_stage": stage,
    }
    if raw_extra:
        raw.update(raw_extra)
    return types.SimpleNamespace(
        success=success,
        provider=provider,
        model=model,
        error="",
        note="",
        output=output or json.dumps({"text": "研究结果仅供人工参考。"}, ensure_ascii=False),
        raw=raw,
    )


def test_default_codex_path_still_available():
    client = Mock()
    client.execute.return_value = _result()
    summarize = make_summary_callback(
        runtime=_runtime(("codex",), client),
        revision="abc123",
        repository="QuantStrategyLab/AIAuditBridge",
        local_facts={"limitations": ["research_only"]},
        validate_context=lambda value: dict(value),
        research_stage="research_summary",
    )
    assert summarize({"candidate": "x"}) == {
        "status": "available",
        "text": "研究结果仅供人工参考。",
        "provider": "codex",
        "model": "gpt-5.6-luna",
    }
    kwargs = client.execute.call_args.kwargs
    assert kwargs["allowed_providers"] == ["codex"]
    assert kwargs["research_stage"] == "research_summary"
    assert kwargs["mode"] == "review_only"


def test_explicit_cursor_path_becomes_available():
    client = Mock()
    client.execute.return_value = _result(
        provider="cursor", model="cursor-grok-4.6-low",
    )
    summarize = make_summary_callback(
        runtime=_runtime(("cursor",), client),
        revision="abc123",
        repository="QuantStrategyLab/AIAuditBridge",
        local_facts={"limitations": ["research_only"]},
        validate_context=lambda value: dict(value),
        research_stage="research_summary",
    )
    assert summarize({"candidate": "x"}) == {
        "status": "available",
        "text": "研究结果仅供人工参考。",
        "provider": "cursor",
        "model": "cursor-grok-4.6-low",
    }
    kwargs = client.execute.call_args.kwargs
    assert kwargs["allowed_providers"] == ["cursor"]
    assert kwargs["research_stage"] == "research_summary"
    assert kwargs["mode"] == "review_only"


def test_codex_cursor_chain_stays_unavailable():
    client = Mock()
    summarize = make_summary_callback(
        runtime=_runtime(("codex", "cursor"), client),
        revision="abc123",
        repository="QuantStrategyLab/AIAuditBridge",
        local_facts={},
        validate_context=lambda value: dict(value),
    )
    assert summarize({"candidate": "x"}) == unavailable()
    client.execute.assert_not_called()


def test_cursor_with_non_summary_stage_rejects_without_execute():
    client = Mock()
    summarize = make_summary_callback(
        runtime=_runtime(("cursor",), client),
        revision="abc123",
        repository="QuantStrategyLab/AIAuditBridge",
        local_facts={},
        validate_context=lambda value: dict(value),
        research_stage="optimization",
    )
    assert summarize({"candidate": "x"}) == unavailable()
    client.execute.assert_not_called()


@pytest.mark.parametrize(
    "kind",
    ["provider_mismatch", "model_mismatch", "stage_mismatch", "failed"],
)
def test_identity_mismatch_or_failure_stays_unavailable(kind):
    if kind == "provider_mismatch":
        result = _result(
            provider="cursor", model="cursor-grok-4.6-low",
            raw_extra={"provider": "codex"},
        )
    elif kind == "model_mismatch":
        result = _result(
            provider="cursor", model="cursor-grok-4.6-low",
            raw_extra={"model": "other-model"},
        )
    elif kind == "stage_mismatch":
        result = _result(
            provider="cursor", model="cursor-grok-4.6-low", stage="optimization",
        )
    else:
        result = _result(
            success=False, provider="cursor", model="cursor-grok-4.6-low",
            raw_extra={"status": "failed"},
        )
    client = Mock()
    client.execute.return_value = result
    summarize = make_summary_callback(
        runtime=_runtime(("cursor",), client),
        revision="abc123",
        repository="QuantStrategyLab/AIAuditBridge",
        local_facts={},
        validate_context=lambda value: dict(value),
        research_stage="research_summary",
    )
    assert summarize({"candidate": "x"}) == unavailable()
