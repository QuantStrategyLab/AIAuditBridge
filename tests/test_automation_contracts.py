"""Tests for service/automation_contracts.py — minimal automation data model."""

from __future__ import annotations

import unittest

from service.automation_contracts import (
    AutomationTask,
    EvidenceBundle,
    GateDecision,
    ProposedAction,
    TriggerRecord,
)


class TestAutomationContracts(unittest.TestCase):
    def test_to_dict(self) -> None:
        trigger = TriggerRecord(
            source="watcher",
            kind="strategy_update",
            severity="high",
            reason="threshold exceeded",
            subject="strategy-a",
            metrics={"score": 0.91},
            evidence=["log-1"],
            created_at=123.4,
        )
        evidence = EvidenceBundle(
            summary="summary",
            artifacts=["artifact-1"],
            metrics={"latency": 42},
            risks=["regression"],
        )
        action = ProposedAction(
            action="adjust_threshold",
            lane="automation",
            target="strategy-a",
            rationale="reduce risk",
            requires_human_review=False,
            metadata={"source": "test"},
        )
        decision = GateDecision(
            allowed=True,
            reason="checks passed",
            required_checks=["unit-tests"],
            human_review_required=False,
            metadata={"reviewer": "bot"},
        )
        task = AutomationTask(
            trigger=trigger,
            evidence=evidence,
            proposed_action=action,
            gate_decision=decision,
            status="approved",
            metadata={"owner": "team-a"},
        )

        self.assertEqual(
            task.to_dict(),
            {
                "trigger": {
                    "source": "watcher",
                    "kind": "strategy_update",
                    "severity": "high",
                    "reason": "threshold exceeded",
                    "subject": "strategy-a",
                    "metrics": {"score": 0.91},
                    "evidence": ["log-1"],
                    "created_at": 123.4,
                },
                "evidence": {
                    "summary": "summary",
                    "artifacts": ["artifact-1"],
                    "metrics": {"latency": 42},
                    "risks": ["regression"],
                },
                "proposed_action": {
                    "action": "adjust_threshold",
                    "lane": "automation",
                    "target": "strategy-a",
                    "rationale": "reduce risk",
                    "requires_human_review": False,
                    "metadata": {"source": "test"},
                },
                "gate_decision": {
                    "allowed": True,
                    "reason": "checks passed",
                    "required_checks": ["unit-tests"],
                    "human_review_required": False,
                    "metadata": {"reviewer": "bot"},
                },
                "status": "approved",
                "metadata": {"owner": "team-a"},
            },
        )

    def test_mutable_defaults_are_not_shared(self) -> None:
        first_trigger = TriggerRecord("a", "k", "high", "r", "s")
        second_trigger = TriggerRecord("a", "k", "high", "r", "s")
        first_trigger.metrics["x"] = 1
        first_trigger.evidence.append("e1")
        self.assertEqual(second_trigger.metrics, {})
        self.assertEqual(second_trigger.evidence, [])

        first_bundle = EvidenceBundle("sum")
        second_bundle = EvidenceBundle("sum")
        first_bundle.artifacts.append("a1")
        first_bundle.metrics["m"] = 2
        first_bundle.risks.append("r1")
        self.assertEqual(second_bundle.artifacts, [])
        self.assertEqual(second_bundle.metrics, {})
        self.assertEqual(second_bundle.risks, [])

        first_action = ProposedAction("act", "lane", "target", "why")
        second_action = ProposedAction("act", "lane", "target", "why")
        first_action.metadata["x"] = "y"
        self.assertEqual(second_action.metadata, {})

        first_decision = GateDecision(True, "ok")
        second_decision = GateDecision(True, "ok")
        first_decision.required_checks.append("c1")
        first_decision.metadata["x"] = "y"
        self.assertEqual(second_decision.required_checks, [])
        self.assertEqual(second_decision.metadata, {})

        first_task = AutomationTask(
            trigger=first_trigger,
            evidence=first_bundle,
            proposed_action=first_action,
            gate_decision=first_decision,
        )
        second_task = AutomationTask(
            trigger=second_trigger,
            evidence=second_bundle,
            proposed_action=second_action,
            gate_decision=second_decision,
        )
        first_task.metadata["x"] = 1
        self.assertEqual(second_task.metadata, {})

    def test_empty_field_validation(self) -> None:
        with self.assertRaises(ValueError):
            TriggerRecord("a", "k", "", "r", "s")
        with self.assertRaises(ValueError):
            ProposedAction("", "lane", "target", "why")
        with self.assertRaises(ValueError):
            AutomationTask(
                trigger=TriggerRecord("a", "k", "high", "r", "s"),
                evidence=EvidenceBundle("sum"),
                proposed_action=ProposedAction("act", "lane", "target", "why"),
                gate_decision=GateDecision(True, "ok"),
                status="",
            )

    def test_is_actionable(self) -> None:
        trigger = TriggerRecord("a", "k", "high", "r", "s")
        evidence = EvidenceBundle("sum")
        allowed_task = AutomationTask(
            trigger=trigger,
            evidence=evidence,
            proposed_action=ProposedAction("act", "lane", "target", "why"),
            gate_decision=GateDecision(True, "ok"),
        )
        blocked_task = AutomationTask(
            trigger=trigger,
            evidence=evidence,
            proposed_action=ProposedAction("act", "lane", "target", "why"),
            gate_decision=GateDecision(False, "blocked"),
        )

        self.assertTrue(allowed_task.is_actionable)
        self.assertFalse(blocked_task.is_actionable)


if __name__ == "__main__":
    unittest.main()
