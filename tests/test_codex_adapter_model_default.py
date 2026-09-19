"""Regression: VPS CodexAdapter must not pass deprecated/static auto defaults to CLI."""

from __future__ import annotations

import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service.adapters.codex_adapter import _CODEX_AUTO_ROSTER
from service.model_catalog import (
    ModelRecord,
    TierAssignment,
    allow_catalog_parent,
    load_catalog,
    save_catalog_atomic,
)
from service.model_catalog_sync import bootstrap_records, build_catalog
from service.model_resolver import reset_catalog_cache

_RETIRED_LEGACY = frozenset({"gpt-5.4", "gpt-5.4-mini"})
_CLAUDE_PREFIXES = ("claude-",)


def _model_from_command(command: list[str]) -> str | None:
    if "--model" not in command:
        return None
    return command[command.index("--model") + 1]


def _assert_codex_auto_model(selected: str | None, catalog) -> None:
    assert selected is not None
    lowered = selected.lower()
    assert lowered not in {"auto", "tier:auto"}
    assert selected not in _RETIRED_LEGACY
    assert not any(lowered.startswith(prefix) for prefix in _CLAUDE_PREFIXES)
    assert selected in _CODEX_AUTO_ROSTER
    record = catalog.models.get(selected)
    assert record is not None
    assert str(record.provider).lower() != "anthropic"


def _strip_model_assignments_from_dropin(text: str) -> str:
    """Mirror deploy_codex_audit_service.sh legacy MODEL cleaner."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        ending = ""
        body = line
        if body.endswith("\r\n"):
            ending = "\r\n"
            body = body[:-2]
        elif body.endswith("\n"):
            ending = "\n"
            body = body[:-1]
        stripped = body.lstrip(" \t")
        indent = body[: len(body) - len(stripped)]
        if not stripped.startswith("Environment="):
            out.append(line)
            continue
        raw = stripped[len("Environment=") :]
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            tokens = [raw] if raw else []
        kept = [token for token in tokens if not token.startswith("CODEX_AUDIT_SERVICE_MODEL=")]
        if kept == tokens:
            out.append(line)
            continue
        if not kept:
            continue
        formatted = " ".join(
            '"' + token.replace("\\", "\\\\").replace('"', '\\"') + '"' for token in kept
        )
        out.append(f"{indent}Environment={formatted}{ending}")
    return "".join(out)


class CodexAdapterModelDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo_catalog = Path(__file__).resolve().parents[1] / "generated" / "model_catalog.json"
        if not repo_catalog.is_file():
            raise unittest.SkipTest("generated/model_catalog.json missing")
        cls._catalog_path = str(repo_catalog)

    def setUp(self) -> None:
        reset_catalog_cache()
        self._env = patch.dict(
            os.environ,
            {"MODEL_CATALOG_PATH": self._catalog_path},
            clear=False,
        )
        self._env.start()
        os.environ.pop("CODEX_AUDIT_SERVICE_MODEL", None)
        os.environ.pop("CODEX_AUDIT_SERVICE_REASONING_EFFORT", None)

    def tearDown(self) -> None:
        reset_catalog_cache()
        self._env.stop()

    def _command(self, **kwargs):
        from service.adapters.codex_adapter import _codex_command

        with patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"):
            return _codex_command(Path("message.md"), **kwargs)

    def test_empty_and_auto_resolve_to_non_retired_catalog_model(self) -> None:
        catalog = load_catalog(Path(self._catalog_path))
        cases = [
            ({}, "medium"),
            ({"model": ""}, "low"),
            ({"model": "auto"}, "minimal"),
            ({"model": "tier:auto"}, "high"),
            ({"model": "auto", "reasoning_effort": "xhigh"}, "xhigh"),
        ]
        for kwargs, effort in cases:
            env = {"CODEX_AUDIT_SERVICE_MODEL": "auto"}
            if "reasoning_effort" not in kwargs:
                env["CODEX_AUDIT_SERVICE_REASONING_EFFORT"] = effort
            with self.subTest(kwargs=kwargs, effort=effort), patch.dict(os.environ, env, clear=False):
                command = self._command(**kwargs)
                _assert_codex_auto_model(_model_from_command(command), catalog)

    def test_auto_never_selects_claude_from_mixed_api_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            catalog = build_catalog(bootstrap_records())
            for tier_name, effort in (
                ("nano", "low"),
                ("fast", "low"),
                ("standard", "medium"),
                ("capable", "high"),
                ("flagship", "xhigh"),
            ):
                catalog.tiers[tier_name] = TierAssignment(
                    tier=tier_name,
                    model="claude-fable-5",
                    provider="anthropic",
                    effort=effort,
                )
            save_catalog_atomic(catalog, path)
            os.environ["MODEL_CATALOG_PATH"] = str(path)
            reset_catalog_cache()

            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_MODEL": "auto"}, clear=False):
                selected = _model_from_command(
                    self._command(model="auto", reasoning_effort="medium")
                )

            _assert_codex_auto_model(selected, catalog)
            self.assertEqual(selected, "gpt-5.6-sol")

    def test_auto_never_selects_high_score_openai_api_only_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            catalog = build_catalog(bootstrap_records())
            catalog.models["gpt-4.1-nano"] = ModelRecord(
                model_id="gpt-4.1-nano",
                provider="openai",
                capability_score=99.0,
                input_cost_per_1m=0.1,
                output_cost_per_1m=0.4,
            )
            for tier_name, effort in (
                ("nano", "low"),
                ("fast", "low"),
                ("standard", "medium"),
                ("capable", "high"),
                ("flagship", "xhigh"),
            ):
                catalog.tiers[tier_name] = TierAssignment(
                    tier=tier_name,
                    model="gpt-4.1-nano",
                    provider="openai",
                    effort=effort,
                )
            save_catalog_atomic(catalog, path)
            os.environ["MODEL_CATALOG_PATH"] = str(path)
            reset_catalog_cache()

            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_MODEL": "auto"}, clear=False):
                selected = _model_from_command(
                    self._command(model="auto", reasoning_effort="medium")
                )

            _assert_codex_auto_model(selected, catalog)
            self.assertNotEqual(selected, "gpt-4.1-nano")
            self.assertIn(selected, _CODEX_AUTO_ROSTER)

    def test_auto_fails_closed_without_usable_codex_roster_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            catalog = build_catalog(bootstrap_records())
            catalog.models = {
                "gpt-4.1-nano": ModelRecord(
                    model_id="gpt-4.1-nano",
                    provider="openai",
                    capability_score=99.0,
                ),
                "claude-fable-5": catalog.models["claude-fable-5"],
            }
            for tier_name, effort in (
                ("nano", "low"),
                ("fast", "low"),
                ("standard", "medium"),
                ("capable", "high"),
                ("flagship", "xhigh"),
            ):
                catalog.tiers[tier_name] = TierAssignment(
                    tier=tier_name,
                    model="gpt-4.1-nano",
                    provider="openai",
                    effort=effort,
                )
            save_catalog_atomic(catalog, path)
            os.environ["MODEL_CATALOG_PATH"] = str(path)
            reset_catalog_cache()

            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_MODEL": "auto"}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "no usable Codex roster model"):
                    self._command(model="auto", reasoning_effort="medium")

    def test_auto_skips_retired_tier_assignment_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            catalog = build_catalog(bootstrap_records())
            for tier_name, effort in (
                ("nano", "low"),
                ("fast", "low"),
                ("standard", "medium"),
                ("capable", "high"),
                ("flagship", "xhigh"),
            ):
                catalog.tiers[tier_name] = TierAssignment(
                    tier=tier_name,
                    model="gpt-5.4-mini" if tier_name != "standard" else "gpt-5.4",
                    provider="openai",
                    effort=effort,
                )
            catalog.tiers["capable"] = TierAssignment(
                tier="capable", model="gpt-5.6-luna", provider="openai", effort="high"
            )
            save_catalog_atomic(catalog, path)
            os.environ["MODEL_CATALOG_PATH"] = str(path)
            reset_catalog_cache()

            with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_MODEL": "auto"}, clear=False):
                low = _model_from_command(self._command(model="auto", reasoning_effort="low"))
                medium = _model_from_command(self._command(model="tier:auto", reasoning_effort="medium"))
                high = _model_from_command(self._command(model="auto", reasoning_effort="high"))

            self.assertEqual(high, "gpt-5.6-luna")
            self.assertEqual(medium, "gpt-5.6-luna")
            self.assertEqual(low, "gpt-5.6-luna")
            for selected in (low, medium, high):
                _assert_codex_auto_model(selected, catalog)

    def test_explicit_request_and_env_models_are_preserved(self) -> None:
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_MODEL": "env-pinned-model"}, clear=False):
            self.assertEqual(_model_from_command(self._command(model="request-pinned")), "request-pinned")
            self.assertEqual(_model_from_command(self._command(model="auto")), "env-pinned-model")
            self.assertEqual(_model_from_command(self._command(model="")), "env-pinned-model")
            self.assertEqual(_model_from_command(self._command()), "env-pinned-model")
        # Explicit non-auto may still name retired legacy models.
        self.assertEqual(_model_from_command(self._command(model="gpt-5.4")), "gpt-5.4")
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_MODEL": "gpt-5.4-mini"}, clear=False):
            self.assertEqual(_model_from_command(self._command(model="auto")), "gpt-5.4-mini")

    def test_catalog_mtime_change_reresolves_without_static_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            initial = build_catalog(bootstrap_records())
            initial.tiers["standard"] = TierAssignment(
                tier="standard",
                model="gpt-5.6-luna",
                provider="openai",
                effort="medium",
            )
            save_catalog_atomic(initial, path)
            os.environ["MODEL_CATALOG_PATH"] = str(path)
            reset_catalog_cache()

            first = _model_from_command(self._command(model="auto", reasoning_effort="medium"))
            self.assertEqual(first, "gpt-5.6-luna")
            _assert_codex_auto_model(first, initial)

            mutated = build_catalog(bootstrap_records())
            mutated.tiers["standard"] = TierAssignment(
                tier="standard",
                model="gpt-5.6-terra",
                provider="openai",
                effort="medium",
            )
            save_catalog_atomic(mutated, path)
            # Do not reset adapter/resolver caches manually beyond mtime reload.
            second = _model_from_command(self._command(model="tier:auto", reasoning_effort="medium"))
            self.assertEqual(second, "gpt-5.6-terra")
            self.assertNotEqual(second, first)
            _assert_codex_auto_model(second, mutated)


class DeployCodexAuditServiceModelOverrideTests(unittest.TestCase):
    def test_managed_dropin_rewrites_service_model_and_clears_legacy_overrides(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "deploy_codex_audit_service.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("write_managed_audit_service_dropin", script)
        self.assertIn("clear_legacy_audit_service_model_overrides", script)
        self.assertIn('Environment="CODEX_AUDIT_SERVICE_MODEL=%s"', script)
        self.assertIn("CODEX_AUDIT_SERVICE_MODEL=", script)
        self.assertIn("shlex.split", script)
        self.assertIn('token.startswith("CODEX_AUDIT_SERVICE_MODEL=")', script)
        # When deploy sets MODEL (including auto), managed drop-in must win over
        # leftover drop-ins; legacy conf files must be stripped of MODEL pins.
        self.assertIn('if [ -n "$AUDIT_MODEL" ]; then', script)
        self.assertRegex(
            script,
            r"clear_legacy_audit_service_model_overrides[\s\S]*write_managed_audit_service_dropin",
        )
        self.assertIn("zzzz-managed-allowlists.conf", script)
        self.assertIn("CODEX_AUDIT_SERVICE_MODEL", script.split("zzzz-managed-allowlists.conf", 1)[1])

    def test_legacy_dropin_model_lines_are_stripped_by_embedded_cleaner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "10-legacy-model.conf"
            legacy.write_text(
                "[Service]\n"
                'Environment="CODEX_AUDIT_SERVICE_MODEL=gpt-5.4"\n'
                'Environment="CODEX_AUDIT_SERVICE_MODEL=gpt-5.4" '
                '"CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=keep" '
                '"CODEX_AUDIT_SERVICE_SANDBOX=read-only"\n'
                'Environment="CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=keep-alone"\n',
                encoding="utf-8",
            )
            updated = _strip_model_assignments_from_dropin(legacy.read_text(encoding="utf-8"))
            legacy.write_text(updated, encoding="utf-8")
            text = legacy.read_text(encoding="utf-8")
            self.assertNotIn("CODEX_AUDIT_SERVICE_MODEL", text)
            self.assertIn("CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=keep", text)
            self.assertIn("CODEX_AUDIT_SERVICE_SANDBOX=read-only", text)
            self.assertIn("CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=keep-alone", text)


if __name__ == "__main__":
    unittest.main()
