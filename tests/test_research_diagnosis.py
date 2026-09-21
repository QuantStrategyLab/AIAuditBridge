from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from scripts.run_research_task_diagnosis import diagnosis_candidates, run_diagnosis
from service.research_diagnosis import (
    MARKER,
    build_research_diagnosis_prompt,
    build_research_diagnosis_request,
    format_research_diagnosis_comment,
)
from service.research_task import ResearchTaskError, build_strategy_diagnosis_task, calculate_task_sha256

# Bound to the scheduled SOXL watcher consumer (soxl-p1-p3-daily-research.yml).
_SOXL_CANDIDATE = "soxl_soxx_core_only_p2_v3"
_SOXL_ISSUE_REPO = "QuantStrategyLab/UsEquitySnapshotPipelines"
_SOXL_STRATEGY_REPO = "QuantStrategyLab/UsEquityStrategies"


def _task(*, event_key: str = "a1b2c3d4e5f6", candidate_id: str = _SOXL_CANDIDATE) -> dict[str, object]:
    return build_strategy_diagnosis_task(
        event_key=event_key,
        created_at="2026-08-20T00:00:00Z",
        candidate_id=candidate_id,
        candidate_kind="individual",
        domain="us_equity",
        strategy_repository=_SOXL_STRATEGY_REPO,
        evidence={
            "p1_input_digest": "a" * 64,
            "p2_config_digest": "b" * 64,
            "p3_evidence_id": "c" * 64,
            "strategy_revision": "d" * 40,
            "producer_revision": "e" * 40,
        },
    )


def _result(
    task: dict[str, object] | None = None,
    *,
    data_status: str = "ready",
    event_key: str | None = None,
    repo: str = _SOXL_ISSUE_REPO,
    issue_url: str = "https://example.test/issues/1",
    include_issue: bool = True,
) -> dict[str, object]:
    active_task = task or _task()
    resolved_event_key = event_key
    if resolved_event_key is None:
        resolved_event_key = str(active_task["task_id"]).removeprefix("watcher-")
    payload: dict[str, object] = {
        "research_task_source_snapshot": {
            "schema_version": "qsl_research_task_source_snapshot.v1",
            "data_status": data_status,
            "tasks": [active_task] if data_status == "ready" else [],
            "errors": [] if data_status == "ready" else ["comparable_metrics_unavailable"],
        },
        "issues": [],
    }
    if include_issue:
        payload["issues"] = [
            {
                "repo": repo,
                "url": issue_url,
                "task": {
                    "event_key": resolved_event_key,
                    "trigger": {
                        "kind": "strategy_metric_degradation",
                        "severity": "high",
                        "subject": f"{_SOXL_STRATEGY_REPO}:{_SOXL_CANDIDATE}",
                        "reason": "sharpe dropped",
                        "signals": [{"metric": "sharpe", "reason": "sharpe dropped 20%"}],
                    },
                },
            }
        ]
    return payload


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def analyze(self, prompt: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append({"prompt": prompt, **kwargs})
        return SimpleNamespace(
            success=True,
            output="## 已验证事实\n已绑定 SOXL P3。",
            provider="openai",
            model="gpt-test",
            error="",
            note="",
            raw={"status": "ok", "output": "## 已验证事实\n已绑定 SOXL P3。", "policy_verdict": "eligible"},
        )

    def execute(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("diagnosis must not call execute")

    def review(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("diagnosis must not call review")


class ResearchDiagnosisTests(unittest.TestCase):
    def _run_isolated(
        self,
        result: dict[str, object],
        *,
        marker_present=None,
        create_comment=None,
        client_factory=None,
        client: FakeClient | None = None,
    ):
        comments: list[tuple[str, str, str]] = []
        fake = client or FakeClient()
        factory = client_factory or (lambda _config: fake)

        def _default_marker(_repo: str, _url: str, _marker: str) -> bool:
            return False

        def _default_comment(repo: str, url: str, body: str) -> str:
            comments.append((repo, url, body))
            return "https://example.test/comment/1"

        with (
            patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://example.test"}, clear=False),
            patch("scripts.run_research_task_diagnosis.subprocess.run", side_effect=AssertionError("gh must stay stubbed")),
            patch("scripts.run_research_task_diagnosis.AiGatewayClient", side_effect=AssertionError("real gateway blocked")),
        ):
            summary = run_diagnosis(
                result,
                marker_present=marker_present or _default_marker,
                create_comment=create_comment or _default_comment,
                client_factory=factory,
            )
        return summary, comments, fake

    def _run_analysis_result(self, result):
        comments = []
        client = Mock()
        client.analyze.return_value = result
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_URL": "https://example.test"}, clear=True):
            summary = run_diagnosis(
                _result(),
                marker_present=lambda *_args: False,
                create_comment=lambda _repo, _url, body: comments.append(body) or "https://example.test/comment/1",
                client_factory=lambda _config: client,
            )
        client.analyze.assert_called_once()
        client.execute.assert_not_called()
        client.review.assert_not_called()
        return summary, comments

    def test_real_client_advisory_and_ok_are_only_research_comments(self):
        from client.config import GatewayConfig
        from client.gateway_client import AiGatewayClient

        for status, output in (
            ("ok", "synthetic research suggestion"),
            ("advisory", "synthetic research suggestion"),
            ("failed", "synthetic text"),
            ("invalid", "synthetic text"),
            ("advisory", None),
            ("advisory", 42),
            ("ok", None),
            ("ok", 42),
        ):
            with self.subTest(status=status, output=output):
                response = MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(
                    {
                        "status": status,
                        "output": output,
                        "provider": "openai",
                        "model": "synthetic-model",
                        "policy_verdict": "advisory" if status == "advisory" else "eligible",
                        "prohibited_actions": ["create_pr", "merge", "deploy"],
                    }
                ).encode()
                client = AiGatewayClient(GatewayConfig(service_url="https://gateway.invalid"))
                with (
                    patch("client.gateway_client._fetch_oidc_token", return_value=""),
                    patch("client.gateway_client.urllib.request.urlopen", return_value=response) as http,
                ):
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

    def test_valid_soxl_task_projects_to_read_only_advisory_request(self) -> None:
        request = build_research_diagnosis_request(
            _task(),
            trigger={"signals": [{"metric": "sharpe", "reason": "drop"}]},
        )
        prompt = build_research_diagnosis_prompt(request)

        self.assertEqual(request["allowed_effect"], "read_only_research_diagnosis")
        self.assertFalse(request["human_intervention_required"])
        self.assertEqual(request["notification"], "none")
        self.assertEqual(
            request["authority"],
            {
                "research_only": True,
                "no_order": True,
                "size_zero_required": True,
                "p4_p5_p6_authorized": False,
            },
        )
        self.assertEqual(request["target"]["candidate_id"], _SOXL_CANDIDATE)
        self.assertEqual(request["target"]["repository"], _SOXL_STRATEGY_REPO)
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

    def test_missing_incomparable_or_identity_mismatch_inputs_never_call_ai(self) -> None:
        cases = {
            "missing_snapshot": {"research_task_source_snapshot": {}, "issues": _result()["issues"]},
            "incomparable_unavailable": _result(data_status="unavailable"),
            "missing_tasks_ready": {
                "research_task_source_snapshot": {"data_status": "ready", "tasks": []},
                "issues": _result()["issues"],
            },
            "identity_event_key_mismatch": _result(event_key="000000000000"),
            "identity_missing_issue_url": _result(issue_url=""),
            "identity_non_qsl_repo": _result(repo="OtherOrg/UsEquitySnapshotPipelines"),
            "identity_issue_without_task_binding": _result(include_issue=False),
        }
        tampered = _result()
        tampered_task = dict(tampered["research_task_source_snapshot"]["tasks"][0])  # type: ignore[index]
        tampered_task["task_sha256"] = "f" * 64
        tampered["research_task_source_snapshot"]["tasks"] = [tampered_task]  # type: ignore[index]
        cases["identity_digest_mismatch"] = tampered

        for name, payload in cases.items():
            with self.subTest(case=name):
                summary, comments, fake = self._run_isolated(
                    payload,
                    client_factory=lambda _config: (_ for _ in ()).throw(
                        AssertionError(f"{name}: must not instantiate client")
                    ),
                )
                self.assertEqual(comments, [])
                self.assertEqual(fake.calls, [])
                self.assertIn(summary["status"], {"skipped", "partial_error"})
                if name == "identity_digest_mismatch":
                    self.assertEqual(summary["status"], "partial_error")
                    self.assertEqual(summary["diagnoses"][0]["status"], "rejected")
                else:
                    self.assertEqual(summary["status"], "skipped")
                    self.assertEqual(summary["reason"], "no_pending_verified_research_task")

    def test_expired_or_unverified_marker_state_parks_without_ai(self) -> None:
        # Production marker lookup fail-closes to "present" so unknown/expired
        # comment state never re-triggers diagnosis.
        summary, comments, fake = self._run_isolated(
            _result(),
            marker_present=lambda *_args: True,
            client_factory=lambda _config: (_ for _ in ()).throw(
                AssertionError("unverified marker state must not call AI")
            ),
        )
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["reason"], "no_pending_verified_research_task")
        self.assertEqual(comments, [])
        self.assertEqual(fake.calls, [])

    def test_run_diagnosis_calls_ai_once_and_writes_marked_comment(self) -> None:
        summary, comments, fake = self._run_isolated(_result())

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["diagnoses"][0]["status"], "diagnosed")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["source_repository"], _SOXL_STRATEGY_REPO)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][0], _SOXL_ISSUE_REPO)
        self.assertIn("qsl-research-diagnosis:v1", comments[0][2])
        self.assertIn("没有代码、参数、数据、订单、P4/P5/P6", comments[0][2])
        self.assertIn(_SOXL_CANDIDATE, fake.calls[0]["prompt"])

    def test_new_verified_task_on_existing_issue_is_not_blocked_by_legacy_marker(self) -> None:
        next_task = _task(event_key="f1e2d3c4b5a6")
        legacy_comment = MARKER

        summary, comments, fake = self._run_isolated(
            _result(next_task),
            marker_present=lambda _repo, _url, *markers: MARKER in legacy_comment
            if not markers
            else markers[0] in legacy_comment,
        )

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["diagnoses"][0]["status"], "diagnosed")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(comments), 1)
        self.assertIn(str(next_task["task_id"]), comments[0][2])
        self.assertIn(str(next_task["task_sha256"]), comments[0][2])

    def test_existing_task_marker_skips_without_calling_ai(self) -> None:
        summary, comments, fake = self._run_isolated(
            _result(),
            marker_present=lambda _repo, _url, _marker: True,
            client_factory=lambda _config: (_ for _ in ()).throw(AssertionError("must not instantiate client")),
        )

        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["reason"], "no_pending_verified_research_task")
        self.assertEqual(comments, [])
        self.assertEqual(fake.calls, [])

    def test_unknown_ai_result_parks_without_issue_write(self) -> None:
        from client.gateway_client import AiResult

        unknown = AiResult(
            provider="openai",
            model="synthetic-model",
            success=False,
            output="",
            error="provider returned unknown failure",
            note="unknown",
            raw={"status": "unknown", "failure_category": "unknown_failure"},
        )
        summary, comments, fake = self._run_isolated(
            _result(),
            client_factory=lambda _config: SimpleNamespace(
                analyze=lambda *args, **kwargs: unknown,
                execute=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no execute")),
                review=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no review")),
            ),
        )

        self.assertEqual(summary["status"], "partial_error")
        self.assertEqual(summary["diagnoses"][0]["status"], "unavailable")
        self.assertEqual(comments, [])
        self.assertEqual(fake.calls, [])

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

    def test_soxl_task_digest_stays_canonical(self) -> None:
        task = _task()
        self.assertEqual(task["task_sha256"], calculate_task_sha256(task))
        self.assertEqual(task["target"]["candidate_id"], _SOXL_CANDIDATE)


if __name__ == "__main__":
    unittest.main()
