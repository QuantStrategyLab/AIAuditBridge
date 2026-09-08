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

    def execute(self, prompt: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append({"prompt": prompt, **kwargs})
        return SimpleNamespace(success=True, output="## 已验证事实\n已绑定。", provider="codex", model="codex-cli", error="", raw={"status": "succeeded"})


class ResearchDiagnosisTests(unittest.TestCase):
    def _run_codex_result(self, result):
        from unittest.mock import Mock
        comments = []
        client = Mock()
        client.execute.return_value = result
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://example.test"}, clear=True):
            summary = run_diagnosis(_result(), marker_present=lambda *_args: False,
                    create_comment=lambda _repo, _url, body: comments.append(body) or "https://example.test/comment/1",
                    client_factory=lambda _config: client)
        client.execute.assert_called_once()
        client.analyze.assert_not_called()
        client.review.assert_not_called()
        return summary, comments

    def test_real_execute_client_uses_codex_job_only_and_binds_source_revision(self):
        from client.config import GatewayConfig
        from client.gateway_client import AiGatewayClient
        from unittest.mock import MagicMock
        responses = []
        route = {"job_id": "synthetic-job", "provider": "codex", "research_stage": "drift_analysis",
                 "model": "gpt-5.6-terra", "reasoning_effort": "medium"}
        for payload in ({"codex_research_routing": "v1"}, route,
                        {**route, "status": "succeeded", "output": "synthetic research suggestion"}):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            responses.append(response)
        client = AiGatewayClient(GatewayConfig(service_url="https://gateway.invalid"))
        comments = []
        with (patch("client.gateway_client._fetch_oidc_token", return_value=""),
              patch("client.gateway_client.time.sleep"),
              patch("client.gateway_client.urllib.request.urlopen", side_effect=responses) as http,
              patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://gateway.invalid"}, clear=True)):
            summary = run_diagnosis(_result(), marker_present=lambda *_: False,
                create_comment=lambda _repo, _url, body: comments.append(body) or "comment-1",
                client_factory=lambda _: client)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(len(comments), 1)
        requests = [call.args[0] for call in http.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["GET", "POST", "GET"])
        self.assertTrue(requests[0].full_url.endswith("/healthz"))
        self.assertTrue(all("/v1/ai/execute/jobs" in request.full_url for request in requests[1:]))
        payload = json.loads(requests[1].data)
        self.assertEqual(payload["source_ref"], "d" * 40)
        self.assertEqual(payload["mode"], "review_only")
        self.assertEqual(payload["research_stage"], "drift_analysis")
        self.assertEqual(payload["timeout_seconds"], 600)

    def test_quota_deferral_keeps_issue_pending_without_comment_or_error(self):
        from client.gateway_client import AiResult
        result = AiResult(provider="codex", model="", success=False, note="deferred",
            raw={"status": "deferred", "retry_at": 9000})
        summary, comments = self._run_codex_result(result)
        self.assertEqual(comments, [])
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["diagnoses"][0]["status"], "deferred")
        self.assertEqual(summary["diagnoses"][0]["retry_at"], 9000)

    def test_codex_failure_or_other_provider_never_uses_api_fallback(self):
        from client.gateway_client import AiResult
        for result in (
            AiResult(provider="codex", model="", success=False, error="sensitive failure"),
            AiResult(provider="openai", model="", success=True, output="API response"),
            AiResult(provider="codex", model="", success=True, output="partial",
                     raw={"status": "running"}),
        ):
            with self.subTest(result=result):
                summary, comments = self._run_codex_result(result)
                self.assertEqual(comments, [])
                self.assertEqual(summary["status"], "partial_error")
                self.assertNotIn("sensitive", json.dumps(summary))

    def test_inconsistent_failed_and_empty_codex_results_never_comment(self):
        from client.gateway_client import AiResult
        cases = [
            dict(success=True, note="advisory", raw={"status": "succeeded"}),
            dict(success=True, raw={"status": "failed"}),
            dict(success=True, raw={"status": "invalid"}),
            dict(success=False, raw={"status": "succeeded"}),
            dict(success=True, raw=None),
            dict(success=True, raw={"status": "succeeded", "output": None}),
            dict(success=True, raw={"status": "succeeded", "output": "different content"}),
            dict(success=True, error="failed", raw={"status": "succeeded"}),
        ]
        for output in (None, "", "   ", 42, {"text": "not a string"}):
            cases.append(dict(success=True, output=output, raw={"status": "succeeded"}))
        for case in cases:
            with self.subTest(case=case):
                fields = dict(provider="codex", model="synthetic-model", output="synthetic text")
                fields.update(case)
                summary, comments = self._run_codex_result(AiResult(**fields))
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
