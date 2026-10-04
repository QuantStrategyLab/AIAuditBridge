"""Save and reread one bilingual summary through the installed QPK callback."""

from __future__ import annotations

import json
import types
from unittest.mock import Mock

from quant_platform_kit.strategy_lifecycle.research_promotion_cycle import (
    ResearchPromotionState,
    ResearchPromotionTicket,
    _attach_research_summary,
    load_research_promotion_ticket,
    save_research_promotion_ticket,
)

from scripts.research_summary import make_summary_callback


def _locales():
    return {
        "zh-CN": {
            "question": "要决定是否继续观察这个研究候选。",
            "basis": "现有材料只说明它仍是研究候选。",
            "limits": "没有比较或证据时不能判断结果。",
            "suggestion": "只把它当作待人工考虑的候选。",
        },
        "en": {
            "question": "Decide whether to keep watching this research candidate.",
            "basis": "The materials only show an existing research candidate.",
            "limits": "Missing comparison or evidence means the case is incomplete.",
            "suggestion": "Treat it as a candidate for human consideration.",
        },
    }


def test_callback_persists_locales_binding_and_skips_cached_call(tmp_path):
    client = Mock()
    client.execute.return_value = types.SimpleNamespace(
        success=True, provider="codex", model="gpt-5.6-luna", error="", note="",
        output=json.dumps({"locales": _locales()}, ensure_ascii=False),
        raw={
            "status": "succeeded", "provider": "codex", "model": "gpt-5.6-luna",
            "research_stage": "research_summary",
        },
    )
    config = types.SimpleNamespace(research_providers=("codex",))
    runtime = types.SimpleNamespace(
        config=types.SimpleNamespace(from_env=Mock(return_value=config)),
        client=Mock(return_value=client),
    )
    summarize = make_summary_callback(
        runtime=runtime,
        revision="abc123",
        repository="QuantStrategyLab/AIAuditBridge",
        local_facts={"limitations": ["research_only"]},
        validate_context=dict,
        research_stage="research_summary",
    )
    ticket = ResearchPromotionTicket(
        ticket_id="rpt_bilingualprobe",
        strategy_profile="synthetic_profile",
        domain="synthetic_domain",
        state=ResearchPromotionState.AWAITING_HUMAN,
        drift_status="review",
        drift_score=0.0,
        created_at="2026-09-28T00:00:00+00:00",
        updated_at="2026-09-28T00:00:00+00:00",
        proposed_params={"candidate": "synthetic"},
        notes=("research_only",),
    )
    path = tmp_path / "ticket.json"

    def persist():
        save_research_promotion_ticket(ticket, path)

    _attach_research_summary(ticket, None, summarize, persist)
    _attach_research_summary(ticket, None, summarize, persist)
    loaded = load_research_promotion_ticket(path)
    _attach_research_summary(loaded, None, summarize, lambda: save_research_promotion_ticket(loaded, path))

    assert client.execute.call_count == 1
    explanation = loaded.research_summary["ai_explanation"]
    assert set(explanation) == {"status", "provider", "model", "scope", "locales", "binding"}
    assert explanation["status"] == "available"
    assert explanation["provider"] == "codex"
    assert explanation["model"] == "gpt-5.6-luna"
    assert explanation["scope"] == "candidate"
    assert explanation["locales"] == _locales()
    assert explanation["binding"] == {
        "ticket_id": "rpt_bilingualprobe",
        "strategy_profile": "synthetic_profile",
        "domain": "synthetic_domain",
        "proposed_params": {"candidate": "synthetic"},
        "comparison": {
            "baseline": None,
            "candidate": None,
            "cost_model": "",
            "end_date": None,
            "start_date": None,
            "status": "unavailable",
        },
        "shadow_evidence_kind": "",
        "shadow_passed": None,
        "notes": ["research_only"],
    }
