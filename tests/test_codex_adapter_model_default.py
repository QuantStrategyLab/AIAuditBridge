"""Regression: VPS CodexAdapter must not pass deprecated/static auto defaults to CLI."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service.model_catalog import (
    TierAssignment,
    allow_catalog_parent,
    load_catalog,
    save_catalog_atomic,
)
from service.model_catalog_sync import bootstrap_records, build_catalog
from service.model_resolver import reset_catalog_cache

_RETIRED_LEGACY = frozenset({"gpt-5.4", "gpt-5.4-mini"})


def _model_from_command(command: list[str]) -> str | None:
    if "--model" not in command:
        return None
    return command[command.index("--model") + 1]


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
                selected = _model_from_command(command)
                self.assertIsNotNone(selected)
                self.assertNotIn(selected.lower(), {"auto", "tier:auto"})
                self.assertNotIn(selected, _RETIRED_LEGACY)
                self.assertIn(selected, catalog.models)

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
                self.assertNotIn(selected, _RETIRED_LEGACY)
                self.assertNotIn(selected.lower(), {"auto", "tier:auto"})

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
            self.assertNotIn(first, _RETIRED_LEGACY)

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
            self.assertNotIn(second, _RETIRED_LEGACY)


if __name__ == "__main__":
    unittest.main()
