from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_research_task_diagnosis import diagnosis_candidates, run_diagnosis
from service.research_diagnosis import (
    MARKER,
    build_research_diagnosis_prompt,
    build_research_diagnosis_request,
    format_research_diagnosis_comment,
)
from service.research_task import ResearchTaskError, build_strategy_diagnosis_task


def _task(*, event_key: str = "a1b2c3d4e5f6") -> dict[str, object]:
    return build_strategy_diagnosis_task(
        event_key=event_key,
        created_at="2026-08-20T00:00:00Z",
        candidate_id="tqqq_core_only_p2_v5",
        candidate_kind="individual",
        domain="us_equity",
        strategy_repository="QuantStrategyLab/UsEquityStrategies",
        evidence={
            "p1_input_digest": "a" * 64,
            "p2_config_digest": "b" * 64,
            "p3_evidence_id": "c" * 64,
            "strategy_revision": "d" * 40,
            "producer_revision": "e" * 40,
        },
    )


def _result(task: dict[str, object] | None = None) -> dict[str, object]:
    active_task = task or _task()
    event_key = str(active_task["task_id"]).removeprefix("watcher-")
    return {
        "research_task_source_snapshot": {"data_status": "ready", "tasks": [active_task]},
        "issues": [
            {
                "repo": "QuantStrategyLab/UsEquitySnapshotPipelines",
                "url": "https://example.test/issues/1",
                "task": {
                    "event_key": event_key,
                    "trigger": {
                        "kind": "strategy_metric_degradation",
                        "severity": "high",
                        "subject": "QuantStrategyLab/UsEquityStrategies:tqqq_core_only_p2_v5",
                        "reason": "sharpe dropped",
                        "signals": [{"metric": "sharpe", "reason": "sharpe dropped 20%"}],
                    },
                },
            }
        ],
    }


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def analyze(self, prompt: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append({"prompt": prompt, **kwargs})
        return SimpleNamespace(success=True, output="## 已验证事实\n已绑定。", provider="openai", model="gpt-test", error="")


class ResearchDiagnosisTests(unittest.TestCase):
    def _run_analysis_result(self, result):
        from unittest.mock import Mock
        comments = []
        client = Mock()
        client.analyze.return_value = result
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://example.test"}, clear=True):
            summary = run_diagnosis(_result(), marker_present=lambda *_args: False,
                    create_comment=lambda _repo, _url, body: comments.append(body) or "https://example.test/comment/1",
                    client_factory=lambda _config: client)
        client.analyze.assert_called_once()
        client.execute.assert_not_called()
        client.review.assert_not_called()
        return summary, comments

    def test_real_client_advisory_and_ok_are_only_research_comments(self):
        from client.config import GatewayConfig
        from client.gateway_client import AiGatewayClient
        from unittest.mock import MagicMock
        for status, output in (
            ("ok", "synthetic research suggestion"), ("advisory", "synthetic research suggestion"),
            ("failed", "synthetic text"), ("invalid", "synthetic text"),
            ("advisory", None), ("advisory", 42), ("ok", None), ("ok", 42),
        ):
            with self.subTest(status=status, output=output):
                response = MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps({
                    "status": status, "output": output,
                    "provider": "openai", "model": "synthetic-model",
                    "policy_verdict": "advisory" if status == "advisory" else "eligible",
                    "prohibited_actions": ["create_pr", "merge", "deploy"],
                }).encode()
                client = AiGatewayClient(GatewayConfig(service_url="https://gateway.invalid"))
                with (patch("client.gateway_client._fetch_oidc_token", return_value=""),
                      patch("client.gateway_client.urllib.request.urlopen", return_value=response) as http):
                    result = client.analyze("synthetic prompt")
                http.assert_called_once()
                self.assertEqual(result.success, status == "ok")
                summary, comments = self._run_analysis_result(result)
                if status not in ("ok", "advisory") or not isinstance(output, str):
                    self.assertNotEqual(summary["status"], "ok")
                    self.assertEqual(comments, [])
                    continue
                self.assertEqual(summary["status"], "ok")
                self.assertEqual(len(comments), 1)
                if status == "advisory":
                    self.assertIn("advisory", comments[0])
                    self.assertIn("不证明执行、晋级或授权", comments[0])
                    self.assertFalse(result.success)
                self.assertIn("synthetic research suggestion", comments[0])

    def test_inconsistent_failed_and_empty_analysis_never_comments(self):
        from client.gateway_client import AiResult
        cases = [
            dict(success=True, note="advisory", raw={"status": "failed"}),
            dict(success=True, note="", raw={"status": "invalid"}),
            dict(success=False, note="advisory", raw={"status": "ok"}),
            dict(success=True, note="advisory", raw={"status": "advisory"}),
            dict(success=False, note="advisory", raw={"status": "advisory", "policy_verdict": "invalid"}),
            dict(success=False, note="advisory", raw=None),
            dict(success=True, note="failed", raw=None),
            dict(success=True, note="", raw={"status": "ok", "output": None}),
            dict(success=True, note="", raw={"status": None}),
            dict(success=True, note="", raw={"status": "ok", "policy_verdict": "failed"}),
            dict(success=True, note="", raw={"status": "ok", "output": "different content"}),
            dict(success=False, note="", raw={"status": "advisory", "output": "synthetic text"}),
            dict(success=False, note="advisory", error="failed", raw={"status": "advisory"}),
        ]
        for output in (None, "", "   ", 42, {"text": "not a string"}):
            cases.append(dict(success=True, output=output))
        for case in cases:
            with self.subTest(case=case):
                fields = dict(provider="openai", model="synthetic-model", output="synthetic text")
                fields.update(case)
                summary, comments = self._run_analysis_result(AiResult(**fields))
                self.assertNotEqual(summary["status"], "ok")
                self.assertEqual(comments, [])

    def test_valid_task_projects_to_read_only_prompt(self) -> None:
        request = build_research_diagnosis_request(_task(), trigger={"signals": [{"metric": "sharpe", "reason": "drop"}]})
        prompt = build_research_diagnosis_prompt(request)

        self.assertEqual(request["allowed_effect"], "read_only_research_diagnosis")
        self.assertFalse(request["human_intervention_required"])
        self.assertIn("不得把历史指标解释为实盘表现", prompt)
        self.assertIn("P6 必须由所有者明确决定", prompt)
        self.assertNotIn("raw bars", request["evidence"])

    def test_tampered_task_cannot_build_diagnosis_request(self) -> None:
        task = _task()
        task["authority"] = {"research_only": True}

        with self.assertRaises(ResearchTaskError):
            build_research_diagnosis_request(task)

    def test_candidate_join_requires_exact_event_key_and_issue_url(self) -> None:
        self.assertEqual(len(diagnosis_candidates(_result())), 1)
        no_match = _result()
        no_match["issues"][0]["task"]["event_key"] = "000000000000"  # type: ignore[index]
        self.assertEqual(diagnosis_candidates(no_match), [])

    def test_run_diagnosis_calls_ai_once_and_writes_marked_comment(self) -> None:
        fake = FakeClient()
        comments: list[tuple[str, str, str]] = []
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://example.test"}, clear=False):
            summary = run_diagnosis(
                _result(),
                marker_present=lambda _repo, _url, _marker: False,
                create_comment=lambda repo, url, body: comments.append((repo, url, body)) or "https://example.test/comment/1",
                client_factory=lambda _config: fake,  # type: ignore[arg-type]
            )

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["diagnoses"][0]["status"], "diagnosed")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["source_repository"], "QuantStrategyLab/UsEquityStrategies")
        self.assertEqual(len(comments), 1)
        self.assertIn("qsl-research-diagnosis:v1", comments[0][2])
        self.assertIn("没有代码、参数、数据、订单、P4/P5/P6", comments[0][2])

    def test_new_verified_task_on_existing_issue_is_not_blocked_by_legacy_marker(self) -> None:
        fake = FakeClient()
        comments: list[tuple[str, str, str]] = []
        next_task = _task(event_key="f1e2d3c4b5a6")
        legacy_comment = MARKER

        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://example.test"}, clear=False):
            summary = run_diagnosis(
                _result(next_task),
                marker_present=lambda _repo, _url, *markers: MARKER in legacy_comment
                if not markers
                else markers[0] in legacy_comment,
                create_comment=lambda repo, url, body: comments.append((repo, url, body)) or "https://example.test/comment/2",
                client_factory=lambda _config: fake,  # type: ignore[arg-type]
            )

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["diagnoses"][0]["status"], "diagnosed")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(comments), 1)
        self.assertIn(str(next_task["task_id"]), comments[0][2])
        self.assertIn(str(next_task["task_sha256"]), comments[0][2])

    def test_existing_task_marker_skips_without_calling_ai(self) -> None:
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://example.test"}, clear=False):
            summary = run_diagnosis(
                _result(),
                marker_present=lambda _repo, _url, _marker: True,
                client_factory=lambda _config: (_ for _ in ()).throw(AssertionError("must not instantiate client")),  # type: ignore[arg-type]
            )

        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["reason"], "no_pending_verified_research_task")

    def test_comment_caps_output_and_keeps_marker(self) -> None:
        request = build_research_diagnosis_request(_task())
        comment = format_research_diagnosis_comment(request, "x" * 13_000)

        self.assertIn("qsl-research-diagnosis:v1", comment)
        self.assertEqual(
            comment.splitlines()[0],
            f"<!-- qsl-research-diagnosis:v1:{request['task_id']}:{request['task_sha256']} -->",
        )
        self.assertIn("输出已按安全上限截断", comment)
        self.assertLess(len(comment), 14_000)


if __name__ == "__main__":
    unittest.main()
