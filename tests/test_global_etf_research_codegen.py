from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import pytest

import scripts.run_global_etf_research_codegen as codegen
import service.ai_gateway_service as gateway
from service.model_resolver import resolve_codex_research_route


def _source() -> dict[str, str]:
    body = (
        "<html><title>Research title</title>"
        "<meta name='citation_title' content='Citation title'>"
        "<meta name='description' content='Organization description'>"
        "<div class='page-header__intro-inner'><p>Actual abstract.</p></div></html>"
    ).encode()
    return {
        "url": codegen.GLOBAL_ETF_RESEARCH_SOURCE_URL,
        "retrieved_at": "2026-09-16T00:00:00+00:00",
        "title": "Research title",
        "abstract": "Actual abstract.",
        "body": body.decode(),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def test_global_codegen_docker_integration_fixture(tmp_path):
    """Opt-in real Docker fixture: fixed UES, synthetic source/model, no network."""
    if os.environ.get("AAB_RUN_GLOBAL_ETF_DOCKER_INTEGRATION") != "1":
        pytest.skip("Global Docker integration is opt-in")
    approved_root = Path(os.environ["AAB_GLOBAL_ETF_APPROVED_REPO"]).resolve()
    original = (approved_root / codegen.GLOBAL_ETF_TARGET_PATH).read_text(encoding="utf-8")
    start = original.index("def _closes_for_symbol")
    end = original.index("\ndef ", start + 1)
    old = original[start:end]
    new = old.replace("frame", "history_frame").replace("subset", "symbol_frame")
    response = SimpleNamespace(
        success=True, provider="codex", model=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_MODEL,
        raw={"status": "succeeded", "provider": "codex", "research_stage": "optimization"},
        output=json.dumps({"final_message": "synthetic Docker fixture", "changes": [{
            "path": codegen.GLOBAL_ETF_TARGET_PATH,
            "base_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "edits": [{"old": old, "new": new}],
        }]}),
    )
    calls = {"model": 0}

    def execute(_prompt):
        calls["model"] += 1
        return response

    with patch.object(codegen, "fetch_global_research_source", return_value=_source()):
        result = codegen.run_global_etf_research_codegen_case(
            ues_repo_root=approved_root,
            run_root=tmp_path / "run",
            source_ref="a" * 40,
            execute=execute,
        )
        recovered = codegen.run_global_etf_research_codegen_case(
            ues_repo_root=approved_root,
            run_root=tmp_path / "run",
            source_ref="a" * 40,
            execute=lambda _prompt: pytest.fail("terminal replay called model"),
            fetch_source=lambda: pytest.fail("terminal replay fetched source"),
        )
    assert result["status"] == "patch_validated"
    assert recovered == result
    assert calls["model"] == 1


class GlobalResearchCodegenTests(TestCase):
    def test_source_parser_uses_nber_intro_not_meta_description(self):
        title, abstract = codegen._source_fields(_source()["body"].encode())
        self.assertEqual((title, abstract), ("Citation title", "Actual abstract."))

    def test_prompt_contains_fixed_file_material_and_patch_contract(self):
        files = {
            codegen.GLOBAL_ETF_TARGET_PATH: "def _closes_for_symbol(symbol):\n    return symbol\n",
            "tests/test_global_etf_rotation.py": "def test_global(): pass\n",
        }
        prompt = codegen._prompt(files, _source())
        for path, value in files.items():
            self.assertIn(path, prompt)
            self.assertIn(hashlib.sha256(value.encode()).hexdigest(), prompt)
            self.assertIn(value, prompt)
        self.assertIn("base_sha256", prompt)
        self.assertIn("changes=[]", prompt)
        self.assertNotIn(_source()["body"], prompt)

    def test_ast_allows_only_fixed_local_renames_or_docstrings(self):
        old = """def _closes_for_symbol(symbol):\n    frame = symbol\n    subset = frame\n    return subset\n"""
        renamed = old.replace("frame", "history_frame").replace("subset", "symbol_frame")
        codegen.validate_global_etf_codegen_change(codegen.GLOBAL_ETF_TARGET_PATH, old, renamed)
        documented = old.replace("def _closes_for_symbol(symbol):", "def _closes_for_symbol(symbol):\n    '''doc'''")
        codegen.validate_global_etf_codegen_change(codegen.GLOBAL_ETF_TARGET_PATH, old, documented)
        mixed = renamed.replace("return symbol_frame", "return frame")
        with self.assertRaises(codegen.GlobalResearchCodegenError):
            codegen.validate_global_etf_codegen_change(codegen.GLOBAL_ETF_TARGET_PATH, old, mixed)
        malicious = old.replace("frame = symbol", "history_frame = symbol")
        with self.assertRaises(codegen.GlobalResearchCodegenError):
            codegen.validate_global_etf_codegen_change(codegen.GLOBAL_ETF_TARGET_PATH, old, malicious)
        sentinel = old.replace("frame = symbol", "__frame = symbol")
        with self.assertRaises(codegen.GlobalResearchCodegenError):
            codegen.validate_global_etf_codegen_change(codegen.GLOBAL_ETF_TARGET_PATH, old, sentinel)
        changed = old.replace("return subset", "return frame + 1")
        with self.assertRaises(codegen.GlobalResearchCodegenError):
            codegen.validate_global_etf_codegen_change(codegen.GLOBAL_ETF_TARGET_PATH, old, changed)

    def test_unknown_claim_stops_before_source_fetch(self):
        source = _source()
        identity = codegen._identity(source=source, source_commit=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT)
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            codegen._claim_or_recover(Path(tmp), identity, source)
            with patch.object(codegen, "fetch_global_research_source", side_effect=AssertionError("must not fetch")):
                with self.assertRaisesRegex(codegen.GlobalResearchCodegenError, "claim_unknown"):
                    codegen.run_global_etf_research_codegen_case(
                        ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40,
                    )

    def test_terminal_result_is_replayed_without_source_or_model(self):
        source = _source()
        response = SimpleNamespace(
            success=True, provider="codex", model=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_MODEL,
            raw={"status": "succeeded", "provider": "codex", "research_stage": "optimization"},
            output=json.dumps({"final_message": "none", "changes": []}),
        )
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {
                codegen.GLOBAL_ETF_TARGET_PATH: "def _closes_for_symbol(symbol):\n    return symbol\n",
                "tests/test_global_etf_rotation.py": "",
            }),
        ):
            calls = {"model": 0, "fetch": 0}

            def fetch():
                calls["fetch"] += 1
                return source

            def execute(_prompt):
                calls["model"] += 1
                return response

            first = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40,
                execute=execute, fetch_source=fetch,
            )
            second = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40,
                execute=lambda _: self.fail("replayed terminal must not call model"),
                fetch_source=lambda: self.fail("replayed terminal must not fetch source"),
            )
        self.assertEqual(first["status"], "no_changes")
        self.assertEqual(second, first)
        self.assertEqual(calls, {"model": 1, "fetch": 1})

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
        self.assertEqual(calls[0][1]["profile"], "global_etf")

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
