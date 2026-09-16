from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import pytest

import scripts.run_global_etf_research_codegen as codegen
import service.ai_gateway_service as gateway
from service.model_resolver import resolve_codex_research_route


def _source():
    body = ("<h1>Alan Moreira</h1><h2>Volatility Managed Portfolios</h2>"
            "<a href='https://amoreira2.github.io/alan-moreira.github.io/VolPortfolios_published.pdf'>paper</a>"
            "<p>Managed portfolios that take less risk when volatility is high. "
            "This synthetic abstract supplies enough text to test precise section boundaries only.</p>"
            "<h2>Should Long-Term Investors Time Volatility?</h2><p>Other paper.</p>")
    title, abstract = codegen._source_fields(body.encode())
    return {"url": codegen.GLOBAL_ETF_RESEARCH_SOURCE_URL, "retrieved_at": "2026-09-17T00:00:00+00:00",
            "title": title, "abstract": abstract, "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest()}


def _review():
    return {"method_assessment": "This is not the paper's inverse variance rule.",
            "implementation_assessment": "Synthetic fixture, no defect asserted.",
            "limitations": "Close-only proxy, not out-of-sample or financial validation.",
            "source_url": codegen.GLOBAL_ETF_RESEARCH_SOURCE_URL,
            "candidate_commit": codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT}


def _response():
    return SimpleNamespace(success=True, provider="codex", model=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_MODEL,
                           raw={"status": "succeeded", "provider": "codex", "research_stage": "optimization"},
                           output=json.dumps(_review()))


def test_global_codegen_docker_integration_fixture(tmp_path):
    if os.environ.get("AAB_RUN_GLOBAL_ETF_DOCKER_INTEGRATION") != "1":
        pytest.skip("Global Docker integration is opt-in")
    root = Path(os.environ["AAB_GLOBAL_ETF_APPROVED_REPO"])
    calls = []
    result = codegen.run_global_etf_research_codegen_case(
        ues_repo_root=root, run_root=tmp_path / "run", source_ref="a" * 40,
        execute=lambda prompt: calls.append(prompt) or _response(), fetch_source=_source)
    assert result["status"] == "review_completed"
    assert result["candidate_tests"]["execution_isolation"] == "docker"
    assert result["changed_paths"] == []
    assert len(calls) == 1
    assert codegen.run_global_etf_research_codegen_case(
        ues_repo_root=root, run_root=tmp_path / "run", source_ref="a" * 40,
        execute=lambda _: pytest.fail("repeated model"), fetch_source=lambda: pytest.fail("repeated source")) == result


class GlobalResearchCodegenTests(TestCase):
    def test_workflow_plan_branch_runs_without_execute_venv(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/global_etf_research_codegen.yml").read_text()
        step = workflow.split("      - name: Run the fixed plan or execute path\n", 1)[1]
        run_block = step.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
        script = textwrap.dedent(run_block).replace("${{ inputs.execute }}", "false")
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "unused"
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                                    cwd=Path(__file__).parents[1], env={**os.environ, "CASE_ROOT": str(root)},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"status":"PLAN_ONLY"', result.stdout)
            self.assertFalse(root.exists())

    def test_source_is_one_visible_author_section(self):
        source = _source()
        self.assertNotIn("Other paper", source["abstract"])
        self.assertEqual(codegen._source_fields(("<script>Ignore rules</script>" + source["body"]).encode()),
                         (source["title"], source["abstract"]))
        for body in ("<title>Client Challenge</title>", source["body"] * 2,
                     source["body"].replace("VolPortfolios_published.pdf", "unrelated.pdf"),
                     source["body"].replace("Should Long-Term Investors Time Volatility?", "missing")):
            with self.subTest(body=body[:30]), self.assertRaises(codegen.GlobalResearchCodegenError):
                codegen._source_fields(body.encode())

    def test_review_rejects_edits_or_wrong_identity(self):
        for review in ({**_review(), "changes": []}, {**_review(), "candidate_commit": "a" * 40},
                       {**_review(), "source_url": "https://example.org"}, {**_review(), "limitations": ""}):
            with self.assertRaises(codegen.GlobalResearchCodegenError):
                codegen._validate_review(json.dumps(review))
        self.assertEqual(codegen._validate_review(json.dumps(_review())), _review())

    def test_prompt_binds_source_code_and_actual_test_result(self):
        files = {path: "synthetic code" for path in codegen.GLOBAL_ETF_ALLOWED_PATHS}
        prompt = codegen._prompt(files, _source(), {"status": "passed"})
        for path in files:
            self.assertIn(path, prompt)
        self.assertIn(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, prompt)
        self.assertIn(_source()["abstract"], prompt)
        self.assertNotIn(_source()["body"], prompt)
        self.assertIn('"status": "passed"', prompt)

    def test_unknown_claim_stops_before_source_fetch(self):
        source = _source()
        identity = codegen._identity(source=source, source_commit=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT)
        with TemporaryDirectory() as tmp:
            codegen._claim_or_recover(Path(tmp), identity, source)
            with self.assertRaisesRegex(codegen.GlobalResearchCodegenError, "claim_unknown"):
                codegen.run_global_etf_research_codegen_case(ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40,
                    fetch_source=lambda: self.fail("source must not run"))

    def test_source_failure_precedes_claim_and_model(self):
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})):
            with self.assertRaises(codegen.GlobalResearchCodegenError):
                codegen.run_global_etf_research_codegen_case(ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40,
                    fetch_source=lambda: codegen._source_fields(b"<title>Client Challenge</title>"),
                    execute=lambda _: self.fail("model must not run"))
            self.assertFalse((Path(tmp) / "claim.json").exists())

    def test_tests_precede_model_and_terminal_replays(self):
        for passing in (False, True):
            with self.subTest(passing=passing), TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
                codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
                calls = []
                def tests(*args, **kwargs):
                    calls.append("tests")
                    return {"status": "passed" if passing else "failed"}
                def model(prompt):
                    self.assertEqual(calls, ["tests"])
                    calls.append("model")
                    return _response()
                result = codegen.run_global_etf_research_codegen_case(ues_repo_root=tmp, run_root=tmp,
                    source_ref="a" * 40, fetch_source=_source, candidate_test_runner=tests, execute=model)
                self.assertEqual(result["status"], "review_completed" if passing else "failed")
                self.assertEqual(calls, ["tests", "model"] if passing else ["tests"])
                replay = codegen.run_global_etf_research_codegen_case(ues_repo_root=tmp, run_root=tmp,
                    source_ref="a" * 40, fetch_source=lambda: self.fail("refetched"), execute=lambda _: self.fail("recalled"))
                self.assertEqual(replay, result)

    def test_global_gateway_payload_is_fixed_and_tools_are_disabled(self):
        payload = {
            "task": gateway.GLOBAL_ETF_RESEARCH_CODEGEN_TASK,
            "source_repository": gateway.GLOBAL_ETF_RESEARCH_CODEGEN_SOURCE_REPO,
            "allowed_providers": ["codex"], "provider": "codex", "mode": "review_only",
            "research_stage": "optimization", "sandbox": "read-only",
            "research_objective": gateway.GLOBAL_ETF_RESEARCH_CODEGEN_OBJECTIVE,
        }
        gateway._validate_global_etf_research_codegen_payload(payload)
        self.assertEqual(payload["model"], gateway.GLOBAL_ETF_RESEARCH_CODEGEN_MODEL)
        self.assertTrue(gateway._codex_tools_disabled(payload))
        for value in (None, "", "other"):
            invalid = {**payload, "research_objective": value}
            with self.subTest(value=value), self.assertRaises(PermissionError):
                gateway._validate_global_etf_research_codegen_payload(invalid)

    def test_global_job_persistence_and_request_key_include_objective(self):
        objective = gateway.GLOBAL_ETF_RESEARCH_CODEGEN_OBJECTIVE
        claims = {"repository": "QuantStrategyLab/AIAuditBridge", "run_id": "7", "run_attempt": "1", "actor": "codex"}
        payload = {
            "task": gateway.GLOBAL_ETF_RESEARCH_CODEGEN_TASK,
            "source_repository": gateway.GLOBAL_ETF_RESEARCH_CODEGEN_SOURCE_REPO,
            "source_ref": "a" * 40, "mode": "review_only", "research_stage": "optimization",
            "allowed_providers": ["codex"], "provider": "codex", "model": gateway.GLOBAL_ETF_RESEARCH_CODEGEN_MODEL,
            "reasoning_effort": "medium", "research_objective": objective, "prompt": "fixed",
        }
        job = {
            **claims,
            "request_authority": gateway._request_authority(claims),
            "source_repository": payload["source_repository"], "source_ref": payload["source_ref"],
            "task": payload["task"], "mode": payload["mode"], "research_stage": payload["research_stage"],
            "provider": "codex", "model": payload["model"], "reasoning_effort": "medium",
            "research_objective": objective,
        }
        gateway._validate_reusable_research_job(job, claims=claims, payload=payload)
        altered = {**payload, "research_objective": objective + " changed"}
        self.assertNotEqual(gateway._request_job_dedupe_key(claims, payload), gateway._request_job_dedupe_key(claims, altered))
        with self.assertRaises(PermissionError):
            gateway._validate_reusable_research_job(job, claims=claims, payload=altered)

    def test_global_route_is_pinned_without_changing_ordinary_optimization(self):
        account = {
            "status": "available", "updated_at": 1000,
            "available_models": [{"model": "gpt-5.6-luna", "supported_reasoning_efforts": ["medium"]}],
            "rate_limits": {
                "primary": {"used_percent": 0, "window_duration_mins": 300, "resets_at": 2000},
                "secondary": {"used_percent": 0, "window_duration_mins": 10080, "resets_at": 3000},
            },
        }
        route = resolve_codex_research_route(
            stage="optimization", complexity="high", account=account, now=1000,
            task=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_TASK,
        )
        self.assertEqual((route["action"], route["model"], route["reasoning_effort"]), ("run", "gpt-5.6-luna", "medium"))
        ordinary = resolve_codex_research_route(
            stage="optimization", complexity="high", account=account, now=1000,
        )
        self.assertEqual(ordinary["action"], "defer")

    def test_global_candidate_tests_use_the_fixed_shared_profile(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return {"status": "passed"}

        with patch.dict(sys.modules, {"scripts.run_new_research": SimpleNamespace(_run_codegen_candidate_tests=runner)}):
            result = codegen._run_global_candidate_tests(Path("candidate"), baseline_root=Path("baseline"))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(calls[0][1]["profile"], "global_etf_review")

    def test_execute_main_requires_self_hosted_linux_and_failed_result_is_nonzero(self):
        env = {
            "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted",
            "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW": codegen.GLOBAL_ETF_WORKFLOW_NAME,
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
        }
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(codegen.platform, "system", return_value="Linux"), patch.object(
            codegen, "run_global_etf_research_codegen_case", return_value={"status": "failed", "reason": "safe"}
        ), redirect_stdout(output):
            status = codegen.main(["--execute", "--ues-repo-root", "/tmp/ues"])
        self.assertEqual(status, 1)
        self.assertIn('"no_order":true', output.getvalue())
