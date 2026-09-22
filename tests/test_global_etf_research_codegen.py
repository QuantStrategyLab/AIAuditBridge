from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import urllib.error
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import pytest

import scripts.run_global_etf_research_codegen as codegen
import service.ai_gateway_service as gateway
from service.model_resolver import resolve_codex_research_route

# Offline claim binding used by assertions. Clear ambient Actions vars so CI does
# not make `_resolve_actions_binding()` inherit the live workflow run/job.
_OFFLINE_ACTIONS = {"run_id": "1", "job_id": "offline"}
_AMBIENT_ACTIONS_ENV = ("GITHUB_ACTIONS", "GITHUB_RUN_ID", "GITHUB_JOB")


@pytest.fixture(autouse=True)
def _isolate_ambient_github_actions_env(monkeypatch):
    for key in _AMBIENT_ACTIONS_ENV:
        monkeypatch.delenv(key, raising=False)


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


def _core_result(result: dict) -> dict:
    return {key: value for key, value in result.items() if key not in codegen._PUBLIC_META_KEYS}


def _assert_fresh_projection(result: dict, *, run_id: str = _OFFLINE_ACTIONS["run_id"], job_id: str = _OFFLINE_ACTIONS["job_id"]) -> None:
    assert result["execution_id"] == codegen.GLOBAL_ETF_EXECUTION_ID
    assert result["replay"] is False
    assert result["run_id"] == run_id
    assert result["job_id"] == job_id


def _assert_replay_projection(result: dict, *, run_id: str = _OFFLINE_ACTIONS["run_id"], job_id: str = _OFFLINE_ACTIONS["job_id"]) -> None:
    assert result["execution_id"] == codegen.GLOBAL_ETF_EXECUTION_ID
    assert result["replay"] is True
    assert result["run_id"] == run_id
    assert result["job_id"] == job_id


def test_global_codegen_docker_integration_fixture(tmp_path):
    if os.environ.get("AAB_RUN_GLOBAL_ETF_DOCKER_INTEGRATION") != "1":
        pytest.skip("Global Docker integration is opt-in")
    root = Path(os.environ["AAB_GLOBAL_ETF_APPROVED_REPO"])
    calls = []
    result = codegen.run_global_etf_research_codegen_case(
        ues_repo_root=root, run_root=tmp_path / "run", source_ref="a" * 40,
        execute=lambda prompt: calls.append(prompt) or _response(), fetch_source=_source,
        actions=_OFFLINE_ACTIONS)
    assert result["status"] == "review_completed"
    assert result["candidate_tests"]["execution_isolation"] == "docker"
    assert result["changed_paths"] == []
    assert len(calls) == 1
    _assert_fresh_projection(result)
    replay = codegen.run_global_etf_research_codegen_case(
        ues_repo_root=root, run_root=tmp_path / "run", source_ref="a" * 40,
        execute=lambda _: pytest.fail("repeated model"), fetch_source=lambda: pytest.fail("repeated source"))
    assert _core_result(replay) == _core_result(result)
    _assert_replay_projection(replay)


class GlobalResearchCodegenTests(TestCase):
    def _write_legacy_deferred(self, root: Path, *, retry_at: int) -> dict[str, object]:
        source = _source()
        identity = codegen._identity(source=source, source_commit=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT)
        codegen._claim_or_recover(root, identity, source, actions=_OFFLINE_ACTIONS)
        claim = json.loads((root / "claim.json").read_text(encoding="utf-8"))
        claim["claimed_at"] = (codegen.datetime.now(codegen.timezone.utc) - timedelta(seconds=120)).isoformat()
        (root / "claim.json").write_text(json.dumps(claim), encoding="utf-8")
        codegen._write_terminal(root, {
            "status": "failed", "reason": "global_codegen_gateway_failed", "identity": identity,
        })
        (root / "response.json").write_text(json.dumps({
            "success": False, "provider": "codex", "model": "", "output": "", "raw": {
                "status": "deferred", "execution_started": False, "retry_at": retry_at,
                "error": "codex_quota_reserved", "failure_category": "quota_or_capacity_failure",
            },
        }), encoding="utf-8")
        return identity

    def test_auth_preflight_is_a_bounded_execute_or_auth_only_gate_before_research_entry(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/global_etf_research_codegen.yml").read_text()
        preflight = "      - name: Verify audit-service authentication before research entry\n"
        entry = "      - name: Run the fixed plan or execute path\n"

        self.assertIn(preflight, workflow)
        self.assertIn("      auth_only:\n", workflow)
        self.assertIn("        default: false\n", workflow.split("      auth_only:\n", 1)[1].split("\n\npermissions:", 1)[0])
        self.assertIn("        if: inputs.execute == true || inputs.auth_only == true\n", workflow.split(preflight, 1)[1].split(entry, 1)[0])
        self.assertIn("        if: inputs.auth_only != true\n", workflow.split(entry, 1)[1].split("\n      - name:", 1)[0])
        self.assertLess(workflow.index(preflight), workflow.index(entry))

    def test_workflow_rejects_execute_and_auth_only_together(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/global_etf_research_codegen.yml").read_text()
        step = workflow.split("      - name: Reject conflicting execution modes\n", 1)[1]
        run_block = step.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
        script = textwrap.dedent(run_block).replace("${{ inputs.execute }}", "true").replace(
            "${{ inputs.auth_only }}", "true")
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("execute_and_auth_only_are_mutually_exclusive", result.stderr)

    def test_workflow_exposes_explicit_resume_only_with_execute(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/global_etf_research_codegen.yml").read_text()
        inputs = workflow.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("      resume_deferred:\n", inputs)
        self.assertIn("        default: false\n", inputs.split("      resume_deferred:\n", 1)[1])
        self.assertIn("      allow_early_resume:\n", inputs)
        self.assertIn("        default: false\n", inputs.split("      allow_early_resume:\n", 1)[1])
        guard = workflow.split("      - name: Reject conflicting execution modes\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("resume_deferred_requires_execute", guard)
        self.assertIn("resume_deferred_and_auth_only_are_mutually_exclusive", guard)
        self.assertIn("allow_early_resume_requires_execute", guard)
        self.assertIn("allow_early_resume_requires_resume_deferred", guard)
        execute = workflow.split("      - name: Run the fixed plan or execute path\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("resume_arg+=(--resume-deferred)", execute)
        self.assertIn("early_resume_arg+=(--allow-early-resume)", execute)

    def test_workflow_rejects_early_resume_without_deferred_resume(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/global_etf_research_codegen.yml").read_text()
        step = workflow.split("      - name: Reject conflicting execution modes\n", 1)[1]
        run_block = step.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
        script = textwrap.dedent(run_block)
        for name, value in (("execute", "true"), ("auth_only", "false"), ("resume_deferred", "false"), ("allow_early_resume", "true")):
            script = script.replace("${{ inputs." + name + " }}", value)
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allow_early_resume_requires_resume_deferred", result.stderr)

    def test_auth_preflight_hides_exception_and_stops_before_research(self):
        class FakeAuthenticationError(Exception):
            pass

        client = SimpleNamespace(get_health=lambda: (_ for _ in ()).throw(RuntimeError("sensitive detail")))
        output = io.StringIO()
        with patch.dict(sys.modules, {"ai_gateway_client": SimpleNamespace(
            AiGatewayClient=lambda config: client, AuthenticationError=FakeAuthenticationError,
            GatewayConfig=SimpleNamespace(from_env=lambda: object()),
        )}), patch.object(codegen, "run_global_etf_research_codegen_case") as run_research, redirect_stdout(output):
            status = codegen.main(["--auth-preflight"])

        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue().strip(), "auth_preflight_unknown")
        self.assertNotIn("sensitive detail", output.getvalue())
        run_research.assert_not_called()

    def test_auth_preflight_only_reports_passed_after_health_check(self):
        class FakeAuthenticationError(Exception):
            pass

        calls = []
        client = SimpleNamespace(get_health=lambda: calls.append("health") or {})
        output = io.StringIO()
        with patch.dict(sys.modules, {"ai_gateway_client": SimpleNamespace(
            AiGatewayClient=lambda config: client, AuthenticationError=FakeAuthenticationError,
            GatewayConfig=SimpleNamespace(from_env=lambda: object()),
        )}), redirect_stdout(output):
            status = codegen.main(["--auth-preflight"])

        self.assertEqual(status, 0)
        self.assertEqual(calls, ["health"])
        self.assertEqual(output.getvalue().strip(), "auth_preflight_passed")

    def test_auth_preflight_classifies_failures_without_leaking_exception_content(self):
        class FakeAuthenticationError(Exception):
            pass

        failures = (
            (FakeAuthenticationError("canary"), "oidc"),
            (urllib.error.URLError("canary"), "transport"),
        )
        for failure, category in failures:
            with self.subTest(category=category):
                client = SimpleNamespace(get_health=lambda failure=failure: (_ for _ in ()).throw(failure))
                output = io.StringIO()
                with patch.dict(sys.modules, {"ai_gateway_client": SimpleNamespace(
                    AiGatewayClient=lambda config: client, AuthenticationError=FakeAuthenticationError,
                    GatewayConfig=SimpleNamespace(from_env=lambda: object()),
                )}), redirect_stdout(output):
                    status = codegen.main(["--auth-preflight"])
                self.assertEqual(status, 1)
                self.assertEqual(output.getvalue().strip(), f"auth_preflight_{category}")
                self.assertNotIn("canary", output.getvalue())

    def test_auth_preflight_classifies_http_service_and_oidc_stages_without_urls(self):
        class FakeAuthenticationError(Exception):
            pass

        config = SimpleNamespace(service_url="https://audit.example")
        failures = (
            (urllib.error.HTTPError("https://audit.example/v1/ai/health", 401, "canary", {}, io.BytesIO(b'{"error":"canary"}')), "service_http_401_unknown"),
            (urllib.error.HTTPError("https://oidc.example/token?canary", 403, "canary", None, None), "oidc_http_403"),
        )
        for failure, category in failures:
            with self.subTest(category=category):
                client = SimpleNamespace(get_health=lambda failure=failure: (_ for _ in ()).throw(failure))
                output = io.StringIO()
                with patch.dict(os.environ, {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token?secret=canary"}), patch.dict(
                    sys.modules, {"ai_gateway_client": SimpleNamespace(
                        AiGatewayClient=lambda _: client, AuthenticationError=FakeAuthenticationError,
                        GatewayConfig=SimpleNamespace(from_env=lambda: config),
                    )}
                ), redirect_stdout(output):
                    status = codegen.main(["--auth-preflight"])
                self.assertEqual(status, 1)
                self.assertEqual(output.getvalue().strip(), f"auth_preflight_{category}")
                self.assertNotIn("canary", output.getvalue())

    def test_auth_preflight_exactly_classifies_known_service_claim_error(self):
        class FakeAuthenticationError(Exception):
            pass

        error = urllib.error.HTTPError(
            "https://audit.example/v1/ai/health", 401, "canary", {},
            io.BytesIO(b'{"error":"OIDC job workflow ref is not allowed","detail":"canary"}'),
        )
        client = SimpleNamespace(get_health=lambda: (_ for _ in ()).throw(error))
        output = io.StringIO()
        config = SimpleNamespace(service_url="https://audit.example")
        with patch.dict(sys.modules, {"ai_gateway_client": SimpleNamespace(
            AiGatewayClient=lambda _: client, AuthenticationError=FakeAuthenticationError,
            GatewayConfig=SimpleNamespace(from_env=lambda: config),
        )}), redirect_stdout(output):
            status = codegen.main(["--auth-preflight"])

        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue().strip(), "auth_preflight_service_http_401_job_workflow_ref_not_allowed")
        self.assertNotIn("canary", output.getvalue())

    def test_auth_preflight_classifies_config_and_invalid_response(self):
        class FakeAuthenticationError(Exception):
            pass

        cases = (
            (SimpleNamespace(from_env=lambda: (_ for _ in ()).throw(ValueError("canary"))), None, "config"),
            (SimpleNamespace(from_env=lambda: object()), SimpleNamespace(get_health=lambda: []), "invalid_response"),
        )
        for config, client, category in cases:
            with self.subTest(category=category):
                output = io.StringIO()
                with patch.dict(sys.modules, {"ai_gateway_client": SimpleNamespace(
                    AiGatewayClient=lambda _: client, AuthenticationError=FakeAuthenticationError, GatewayConfig=config,
                )}), redirect_stdout(output):
                    status = codegen.main(["--auth-preflight"])
                self.assertEqual(status, 1)
                self.assertEqual(output.getvalue().strip(), f"auth_preflight_{category}")

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
            codegen._claim_or_recover(Path(tmp), identity, source, actions=_OFFLINE_ACTIONS)
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
                _assert_fresh_projection(result)
                replay = codegen.run_global_etf_research_codegen_case(ues_repo_root=tmp, run_root=tmp,
                    source_ref="a" * 40, fetch_source=lambda: self.fail("refetched"), execute=lambda _: self.fail("recalled"))
                self.assertEqual(_core_result(replay), _core_result(result))
                _assert_replay_projection(replay)

    def test_trusted_quota_deferral_preserves_completed_test_summary_and_replays_without_model(self):
        retry_at = int(codegen.datetime.now(codegen.timezone.utc).timestamp()) + 60
        response = SimpleNamespace(
            success=False, provider="codex", model="", output="",
            raw={
                "status": "deferred", "execution_started": False,
                "retry_at": retry_at, "error": "codex_quota_reserved",
                "failure_category": "quota_or_capacity_failure",
            },
        )
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            calls = []
            result = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40, fetch_source=_source,
                candidate_test_runner=lambda *args, **kwargs: {"status": "passed", "profile": "fixed"},
                execute=lambda prompt: calls.append(prompt) or response,
            )
            self.assertEqual(result["status"], "deferred")
            self.assertEqual(result["reason"], "global_codegen_quota_deferred")
            self.assertEqual(result["retry_at"], retry_at)
            self.assertEqual(result["candidate_tests"], {"status": "passed", "profile": "fixed"})
            self.assertEqual(len(calls), 1)
            _assert_fresh_projection(result)
            replay = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40,
                fetch_source=lambda: self.fail("deferred source must not run"),
                execute=lambda _: self.fail("deferred model must not run"),
            )
            self.assertEqual(_core_result(replay), _core_result(result))
            _assert_replay_projection(replay)

    def test_service_shape_quota_deferral_without_failure_category_is_deferred(self):
        retry_at = int(codegen.datetime.now(codegen.timezone.utc).timestamp()) + 60
        response = SimpleNamespace(
            success=False, provider="codex", model="", output="",
            raw={
                "status": "deferred", "execution_started": False,
                "retry_at": retry_at, "error": "codex_quota_reserved",
            },
        )
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            result = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40, fetch_source=_source,
                candidate_test_runner=lambda *args, **kwargs: {"status": "passed"},
                execute=lambda prompt: response,
            )
            self.assertEqual(result["status"], "deferred")
            self.assertEqual(result["reason"], "global_codegen_quota_deferred")
            self.assertEqual(result["retry_at"], retry_at)

    def test_gateway_failure_records_allowlisted_failure_category_without_retry(self):
        secret = "sk-live-secret-token"
        provider_text = "provider said OPENAI_API_KEY=super-secret"
        cases = {
            "quota_or_capacity_failure": {"failure_category": "quota_or_capacity_failure"},
            "auth_or_config_failure": {"failure_category": "auth_or_config_failure"},
            "patch_contract_failure": {"failure_category": "patch_contract_failure"},
            "transient_service_failure": {"failure_category": "transient_service_failure"},
            "missing": {},
            "empty": {"failure_category": ""},
            "none": {"failure_category": None},
            "unknown": {"failure_category": provider_text},
            "list": {"failure_category": ["quota_or_capacity_failure", secret]},
            "non_mapping": secret,
        }
        self.assertNotIn("failure_category", codegen._public_result({"status": "review_completed"}))
        for name, raw in cases.items():
            with self.subTest(name=name), TemporaryDirectory() as tmp, patch.object(
                codegen, "_docker_preflight"), patch.object(
                codegen, "_read_global_base",
                return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(
                    _archive_codegen_base=lambda *args, **kwargs: None)}):
                calls = []
                response = SimpleNamespace(
                    success=False, provider="codex", model=secret, output=provider_text,
                    error=secret, note=provider_text, raw=raw,
                )
                result = codegen.run_global_etf_research_codegen_case(
                    ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40, fetch_source=_source,
                    candidate_test_runner=lambda *args, **kwargs: {"status": "passed"},
                    execute=lambda prompt: calls.append("model") or response,
                )
                allowlisted = {
                    "quota_or_capacity_failure", "auth_or_config_failure",
                    "patch_contract_failure", "transient_service_failure",
                }
                expected = name if name in allowlisted else "unknown_failure"
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["reason"], "global_codegen_gateway_failed")
                self.assertEqual(result["failure_category"], expected)
                self.assertNotIn("retry_at", result)
                self.assertEqual(result["candidate_tests"], {"status": "passed"})
                self.assertEqual(result["identity"]["task"], codegen.GLOBAL_ETF_RESEARCH_CODEGEN_TASK)
                self.assertEqual(
                    result["identity"]["source_commit"], codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT)
                self.assertTrue(result["no_order"])
                self.assertTrue(result["research_only"])
                self.assertFalse(result["promotion_eligible"])
                self.assertFalse(result["live_authority_granted"])
                rendered = json.dumps(result)
                self.assertNotIn(secret, rendered)
                self.assertNotIn(provider_text, rendered)
                stored = (Path(tmp) / "result.json").read_text(encoding="utf-8")
                self.assertNotIn(secret, stored)
                self.assertNotIn(provider_text, stored)
                public = codegen._public_result(result)
                self.assertEqual(public["failure_category"], expected)
                self.assertEqual(public["status"], "failed")
                self.assertEqual(public["reason"], "global_codegen_gateway_failed")
                self.assertNotIn("identity", public)
                self.assertTrue(public["no_order"])
                self.assertFalse(public["promotion_eligible"])
                self.assertFalse(public["live_authority_granted"])
                self.assertNotIn(secret, json.dumps(public))
                self.assertNotIn(provider_text, json.dumps(public))
                replay = codegen.run_global_etf_research_codegen_case(
                    ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40,
                    fetch_source=lambda: self.fail("must not refetch"),
                    execute=lambda _: self.fail("failure category must not retry"),
                )
                self.assertEqual(replay["status"], "failed")
                self.assertEqual(replay["reason"], "global_codegen_gateway_failed")
                self.assertEqual(replay["failure_category"], expected)
                self.assertEqual(calls, ["model"])
                _assert_replay_projection(replay)
                poisoned = codegen._public_result({**public, "failure_category": secret})
                self.assertEqual(poisoned["failure_category"], "unknown_failure")
                self.assertNotIn(secret, json.dumps(poisoned))

    def test_legacy_gateway_failure_is_deferred_only_for_the_exact_saved_quota_shape(self):
        source = _source()
        identity = codegen._identity(source=source, source_commit=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT)
        retry_at = int(codegen.datetime.now(codegen.timezone.utc).timestamp()) + 60
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            codegen._claim_or_recover(root, identity, source, actions=_OFFLINE_ACTIONS)
            codegen._write_terminal(root, {
                "status": "failed", "reason": "global_codegen_gateway_failed", "identity": identity,
            })
            (root / "response.json").write_text(json.dumps({"success": False, "provider": "codex", "output": "", "raw": {
                "status": "deferred", "execution_started": False, "retry_at": retry_at,
                "error": "codex_quota_reserved", "failure_category": "quota_or_capacity_failure",
            }}), encoding="utf-8")
            recovered = codegen._recover_existing(root)
            self.assertEqual(recovered["status"], "deferred")
            self.assertEqual(recovered["retry_at"], retry_at)
            self.assertNotIn("candidate_tests", recovered)

            (root / "response.json").write_text(json.dumps({"success": False, "provider": "codex", "output": "", "raw": {
                "status": "deferred", "execution_started": None, "retry_at": retry_at,
                "error": "codex_quota_reserved", "failure_category": "quota_or_capacity_failure",
            }}), encoding="utf-8")
            self.assertEqual(codegen._recover_existing(root)["status"], "failed")

    def test_due_legacy_deferral_resumes_once_with_cached_source_and_preserves_old_evidence(self):
        retry_at = int(codegen.datetime.now(codegen.timezone.utc).timestamp()) - 1
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            root = Path(tmp)
            self._write_legacy_deferred(root, retry_at=retry_at)
            result = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                fetch_source=lambda: self.fail("resume must use the verified claim source"),
                candidate_test_runner=lambda *args, **kwargs: {"status": "passed", "profile": "fixed"},
                execute=lambda _: _response(),
            )
            self.assertEqual(result["status"], "review_completed")
            self.assertEqual(json.loads((root / "result.json").read_text())["status"], "failed")
            self.assertTrue((root / "resume.lock").exists())
            self.assertTrue((root / "resume-response.json").exists())
            _assert_fresh_projection(result)
            recovered = codegen._with_projection(codegen._recover_existing(root), root, replay=True)
            self.assertEqual(_core_result(recovered), _core_result(result))
            _assert_replay_projection(recovered)
            replayed = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                execute=lambda _: self.fail("a completed recovery must not run twice"),
            )
            self.assertEqual(_core_result(replayed), _core_result(result))
            _assert_replay_projection(replayed)

    def test_resume_without_an_existing_deferred_record_stops_before_docker_or_model(self):
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight") as docker:
            with self.assertRaisesRegex(codegen.GlobalResearchCodegenError, "resume_unavailable"):
                codegen.run_global_etf_research_codegen_case(
                    ues_repo_root=tmp, run_root=tmp, source_ref="a" * 40, resume_deferred=True,
                    fetch_source=lambda: self.fail("no source"), execute=lambda _: self.fail("no model"),
                )
            docker.assert_not_called()

    def test_deferred_resume_rejects_not_due_or_locked_without_a_model_call(self):
        now = int(codegen.datetime.now(codegen.timezone.utc).timestamp())
        for state in ("not_due", "locked"):
            with self.subTest(state=state), TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight") as docker, patch.object(
                codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})):
                root = Path(tmp)
                self._write_legacy_deferred(root, retry_at=now + 60)
                if state == "locked":
                    codegen._acquire_resume_lock(root, json.loads((root / "claim.json").read_text())["identity"])
                with self.assertRaises(codegen.GlobalResearchCodegenError):
                    codegen.run_global_etf_research_codegen_case(
                        ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                        fetch_source=lambda: self.fail("no source"), execute=lambda _: self.fail("no model"),
                    )
                if state == "not_due":
                    self.assertTrue(docker.called)
                else:
                    docker.assert_not_called()

    def test_explicit_early_resume_allows_one_trusted_quota_deferral(self):
        retry_at = int(codegen.datetime.now(codegen.timezone.utc).timestamp()) + 60
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            root = Path(tmp)
            self._write_legacy_deferred(root, retry_at=retry_at)
            calls = []
            result = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                allow_early_resume=True,
                candidate_test_runner=lambda *args, **kwargs: {"status": "passed"},
                execute=lambda _: calls.append("model") or _response(),
            )
            self.assertEqual(result["status"], "review_completed")
            self.assertEqual(calls, ["model"])
            self.assertTrue((root / "resume.lock").exists())
            self.assertTrue((root / "resume-result.json").exists())

    def test_resume_unknown_response_leaves_permanent_lock_and_no_retry_result(self):
        retry_at = int(codegen.datetime.now(codegen.timezone.utc).timestamp()) - 1
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            root = Path(tmp)
            self._write_legacy_deferred(root, retry_at=retry_at)
            with self.assertRaisesRegex(codegen.GlobalResearchCodegenError, "response_unknown"):
                codegen.run_global_etf_research_codegen_case(
                    ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                    candidate_test_runner=lambda *args, **kwargs: {"status": "passed"},
                    execute=lambda _: (_ for _ in ()).throw(RuntimeError("unknown")),
                )
            self.assertTrue((root / "resume.lock").exists())
            self.assertFalse((root / "resume-result.json").exists())
            with self.assertRaisesRegex(codegen.GlobalResearchCodegenError, "resume_blocked"):
                codegen.run_global_etf_research_codegen_case(
                    ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                    execute=lambda _: self.fail("must not retry"),
                )

    def test_concurrent_resumes_make_one_model_call_and_preserve_original_evidence(self):
        retry_at = int(codegen.datetime.now(codegen.timezone.utc).timestamp()) - 1
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            root = Path(tmp)
            self._write_legacy_deferred(root, retry_at=retry_at)
            original = {name: (root / name).read_bytes() for name in ("claim.json", "response.json", "result.json")}
            barrier = threading.Barrier(2)
            original_loader = codegen._load_resumable_deferred
            calls, outcomes = [], []

            def synchronized_loader(*args, **kwargs):
                value = original_loader(*args, **kwargs)
                barrier.wait(timeout=5)
                return value

            def resume():
                try:
                    outcomes.append(codegen.run_global_etf_research_codegen_case(
                        ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                        candidate_test_runner=lambda *args, **kwargs: {"status": "passed"},
                        execute=lambda _: calls.append("model") or _response(),
                    ))
                except codegen.GlobalResearchCodegenError as exc:
                    outcomes.append(str(exc))

            with patch.object(codegen, "_load_resumable_deferred", side_effect=synchronized_loader):
                threads = [threading.Thread(target=resume), threading.Thread(target=resume)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(calls, ["model"])
            self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
            self.assertIn("global_codegen_resume_blocked", outcomes)
            self.assertEqual({name: (root / name).read_bytes() for name in original}, original)

    def test_resume_quota_deferral_blocks_a_third_model_call(self):
        retry_at = int(codegen.datetime.now(codegen.timezone.utc).timestamp()) - 1
        response = SimpleNamespace(
            success=False, provider="codex", model="", output="",
            raw={
                "status": "deferred", "execution_started": False,
                "retry_at": retry_at + 120, "error": "codex_quota_reserved",
                "failure_category": "quota_or_capacity_failure",
            },
        )
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            root = Path(tmp)
            self._write_legacy_deferred(root, retry_at=retry_at)
            calls = []
            resumed = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                candidate_test_runner=lambda *args, **kwargs: {"status": "passed"},
                execute=lambda _: calls.append("model") or response,
            )
            self.assertEqual(resumed["status"], "deferred")
            self.assertEqual(codegen.run_global_etf_research_codegen_case(
                ues_repo_root=root, run_root=root, source_ref="a" * 40,
                execute=lambda _: self.fail("default must not call"),
            )["status"], "deferred")
            with self.assertRaisesRegex(codegen.GlobalResearchCodegenError, "resume_used"):
                codegen.run_global_etf_research_codegen_case(
                    ues_repo_root=root, run_root=root, source_ref="a" * 40, resume_deferred=True,
                    execute=lambda _: self.fail("third model call"),
                )
            self.assertEqual(calls, ["model"])

    def test_project_result_projects_deferred_resume_and_unknown_without_private_fields(self):
        now = int(codegen.datetime.now(codegen.timezone.utc).timestamp())
        with TemporaryDirectory() as tmp, patch.object(codegen, "GLOBAL_ETF_STATE_ROOT", Path(tmp)):
            root = Path(tmp)
            self._write_legacy_deferred(root, retry_at=now + 60)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(codegen.main(["--project-result"]), 0)
            deferred = json.loads(output.getvalue())
            self.assertEqual(deferred["status"], "deferred")
            self.assertEqual(deferred["execution_id"], codegen.GLOBAL_ETF_EXECUTION_ID)
            self.assertTrue(deferred["replay"])
            self.assertEqual(deferred["run_id"], _OFFLINE_ACTIONS["run_id"])
            self.assertEqual(deferred["job_id"], _OFFLINE_ACTIONS["job_id"])
            self.assertNotIn("source", deferred)
            self.assertNotIn("raw", deferred)

            identity = json.loads((root / "claim.json").read_text())["identity"]
            codegen._write_resume_terminal(root, {"status": "review_completed", "identity": identity})
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(codegen.main(["--project-result"]), 0)
            completed = json.loads(output.getvalue())
            self.assertEqual(completed["status"], "review_completed")
            self.assertEqual(completed["execution_id"], codegen.GLOBAL_ETF_EXECUTION_ID)
            self.assertTrue(completed["replay"])
            self.assertEqual(completed["run_id"], _OFFLINE_ACTIONS["run_id"])
            self.assertEqual(completed["job_id"], _OFFLINE_ACTIONS["job_id"])
            self.assertNotIn("source", completed)
            self.assertNotIn("raw", completed)

        with TemporaryDirectory() as tmp, patch.object(codegen, "GLOBAL_ETF_STATE_ROOT", Path(tmp)):
            (Path(tmp) / "resume.lock").write_text("{}", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(codegen.main(["--project-result"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {
                "execution_id": codegen.GLOBAL_ETF_EXECUTION_ID,
                "job_id": "",
                "live_authority_granted": False, "no_order": True,
                "promotion_eligible": False, "replay": False, "run_id": "",
                "status": "unknown",
            })

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
            "GITHUB_RUN_ID": "35565950756", "GITHUB_JOB": "codegen",
        }
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(codegen.platform, "system", return_value="Linux"), patch.object(
            codegen, "run_global_etf_research_codegen_case", return_value={
                "status": "failed", "reason": "safe",
                "execution_id": codegen.GLOBAL_ETF_EXECUTION_ID,
                "replay": False, "run_id": "35565950756", "job_id": "codegen",
            }
        ), redirect_stdout(output):
            status = codegen.main(["--execute", "--ues-repo-root", "/tmp/ues"])
        self.assertEqual(status, 1)
        self.assertIn('"no_order":true', output.getvalue())
        self.assertIn('"execution_id":"global-etf-review-20260922-quota-recovery-once"', output.getvalue())
        self.assertIn('"run_id":"35565950756"', output.getvalue())

    def test_fixed_execution_id_is_stable_and_ignores_legacy_terminal(self):
        self.assertEqual(codegen.GLOBAL_ETF_EXECUTION_ID, "global-etf-review-20260922-quota-recovery-once")
        self.assertEqual(
            codegen.GLOBAL_ETF_STATE_ROOT,
            codegen.GLOBAL_ETF_STATE_PARENT / codegen.GLOBAL_ETF_EXECUTION_ID,
        )
        self.assertNotEqual(codegen.GLOBAL_ETF_STATE_ROOT, codegen.GLOBAL_ETF_LEGACY_STATE_ROOT)
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            parent = Path(tmp)
            legacy = parent / "legacy"
            fresh = parent / "fresh"
            legacy.mkdir()
            source = _source()
            identity = codegen._identity(source=source, source_commit=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT)
            (legacy / "claim.json").write_text(json.dumps({
                "status": "claimed", "identity": identity, "source": source,
            }), encoding="utf-8")
            (legacy / "result.json").write_text(json.dumps({
                "status": "failed", "reason": "global_codegen_gateway_failed",
                "failure_category": "quota_or_capacity_failure", "identity": identity,
                "no_order": True, "promotion_eligible": False, "research_only": True,
                "live_authority_granted": False,
            }), encoding="utf-8")
            calls = []
            result = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=fresh, run_root=fresh, source_ref="a" * 40, fetch_source=_source,
                candidate_test_runner=lambda *args, **kwargs: {"status": "passed"},
                execute=lambda _: calls.append("model") or _response(),
                actions={"run_id": "99", "job_id": "codegen"},
            )
            self.assertEqual(result["status"], "review_completed")
            self.assertEqual(calls, ["model"])
            _assert_fresh_projection(result, run_id="99", job_id="codegen")
            claim = json.loads((fresh / "claim.json").read_text(encoding="utf-8"))
            self.assertEqual(claim["execution_id"], codegen.GLOBAL_ETF_EXECUTION_ID)
            self.assertEqual(claim["actions"], {"run_id": "99", "job_id": "codegen"})
            self.assertEqual(
                claim["input_summary"],
                codegen._input_summary(source=source, identity=identity),
            )
            self.assertFalse((legacy / "response.json").exists())
            replay = codegen.run_global_etf_research_codegen_case(
                ues_repo_root=fresh, run_root=fresh, source_ref="a" * 40,
                fetch_source=lambda: self.fail("legacy must not leak"),
                execute=lambda _: self.fail("fresh terminal must replay once"),
            )
            self.assertEqual(_core_result(replay), _core_result(result))
            _assert_replay_projection(replay, run_id="99", job_id="codegen")
            self.assertEqual(calls, ["model"])

    def test_concurrent_fresh_claims_allow_at_most_one_model_call(self):
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight"), patch.object(
            codegen, "_read_global_base", return_value=(codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT, {})), patch.dict(
                sys.modules, {"scripts.run_new_research": SimpleNamespace(_archive_codegen_base=lambda *a, **k: None)}):
            root = Path(tmp)
            barrier = threading.Barrier(2)
            original_claim = codegen._claim_or_recover
            calls, outcomes = [], []

            def synchronized_claim(*args, **kwargs):
                barrier.wait(timeout=5)
                return original_claim(*args, **kwargs)

            def run_once():
                try:
                    outcomes.append(codegen.run_global_etf_research_codegen_case(
                        ues_repo_root=root, run_root=root, source_ref="a" * 40, fetch_source=_source,
                        candidate_test_runner=lambda *args, **kwargs: {"status": "passed"},
                        execute=lambda _: calls.append("model") or _response(),
                        actions={"run_id": "42", "job_id": "codegen"},
                    ))
                except codegen.GlobalResearchCodegenError as exc:
                    outcomes.append(str(exc))

            with patch.object(codegen, "_claim_or_recover", side_effect=synchronized_claim):
                threads = [threading.Thread(target=run_once), threading.Thread(target=run_once)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(calls, ["model"])
            self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
            self.assertIn("global_codegen_claim_unknown", outcomes)
            winner = next(item for item in outcomes if isinstance(item, dict))
            _assert_fresh_projection(winner, run_id="42", job_id="codegen")

    def test_unknown_claim_without_result_parks_and_does_not_retry(self):
        source = _source()
        identity = codegen._identity(source=source, source_commit=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT)
        with TemporaryDirectory() as tmp, patch.object(codegen, "_docker_preflight") as docker:
            root = Path(tmp)
            codegen._claim_or_recover(root, identity, source, actions={"run_id": "7", "job_id": "codegen"})
            with self.assertRaisesRegex(codegen.GlobalResearchCodegenError, "claim_unknown"):
                codegen.run_global_etf_research_codegen_case(
                    ues_repo_root=root, run_root=root, source_ref="a" * 40,
                    fetch_source=lambda: self.fail("unknown must not fetch"),
                    execute=lambda _: self.fail("unknown must not call"),
                )
            with self.assertRaisesRegex(codegen.GlobalResearchCodegenError, "claim_unknown"):
                codegen.run_global_etf_research_codegen_case(
                    ues_repo_root=root, run_root=root, source_ref="a" * 40,
                    fetch_source=lambda: self.fail("unknown must not retry"),
                    execute=lambda _: self.fail("unknown must not retry"),
                )
            docker.assert_not_called()

    def test_project_result_rejects_foreign_execution_identity(self):
        with TemporaryDirectory() as tmp, patch.object(codegen, "GLOBAL_ETF_STATE_ROOT", Path(tmp)):
            root = Path(tmp)
            source = _source()
            identity = codegen._identity(source=source, source_commit=codegen.GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT)
            codegen._claim_or_recover(root, identity, source, actions={"run_id": "8", "job_id": "codegen"})
            claim = json.loads((root / "claim.json").read_text(encoding="utf-8"))
            claim["execution_id"] = "foreign-execution-id"
            (root / "claim.json").write_text(json.dumps(claim), encoding="utf-8")
            codegen._write_terminal(root, {
                "status": "failed", "reason": "global_codegen_gateway_failed", "identity": identity,
            })
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(codegen.main(["--project-result"]), 0)
            projected = json.loads(output.getvalue())
            self.assertEqual(projected["status"], "unknown")
            self.assertEqual(projected["execution_id"], codegen.GLOBAL_ETF_EXECUTION_ID)
            self.assertFalse(projected["replay"])
            self.assertEqual(projected["run_id"], "")
            self.assertEqual(projected["job_id"], "")

    def test_workflow_artifact_name_binds_fixed_execution_id(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/global_etf_research_codegen.yml").read_text()
        self.assertIn("global-etf-research-codegen-20260922-quota-recovery-once-", workflow)
        self.assertIn("global-etf-review-20260922-quota-recovery-once", workflow)
