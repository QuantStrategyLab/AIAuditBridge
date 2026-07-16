from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ReviewAuthoritySplitTest(unittest.TestCase):
    def test_required_gate_has_no_review_event_or_polling(self) -> None:
        workflow = (ROOT / ".github/workflows/codex_review_gate.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("pull_request_review:", workflow)
        self.assertNotIn("CODEX_GATE_POLL", workflow)
        self.assertIn("checks: write", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("persist-credentials: false", workflow)

    def test_connector_review_remains_native_advisory_evidence(self) -> None:
        self.assertFalse((ROOT / ".github/workflows/codex_review_advisory.yml").exists())
        self.assertFalse((ROOT / "scripts/report_codex_app_review.py").exists())

    def test_static_gate_api_failure_is_blocking(self) -> None:
        module = _load_module("gate_codex_app_review_split", "scripts/gate_codex_app_review.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = Path(tmpdir) / "event.json"
            event_path.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "number": 7,
                            "head": {"sha": "abc123"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_EVENT_NAME": "pull_request_target",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(module, "github_request", return_value={"id": 42}),
                patch.object(
                    module,
                    "run_static_guard",
                    side_effect=RuntimeError("API unavailable"),
                ),
            ):
                self.assertEqual(module.main(), 1)

    def test_static_gate_publishes_success_on_exact_head(self) -> None:
        module = _load_module("gate_codex_app_review_head", "scripts/gate_codex_app_review.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = Path(tmpdir) / "event.json"
            event_path.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "number": 7,
                            "head": {"sha": "a" * 40},
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "GH_TOKEN": "token",
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_RUN_ID": "1234",
                "GITHUB_SERVER_URL": "https://github.example",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    module,
                    "github_request",
                    side_effect=[{"id": 42}, {}],
                ) as request,
                patch.object(module, "run_static_guard", return_value=0),
            ):
                self.assertEqual(module.main(), 0)

            create = request.call_args_list[0]
            self.assertEqual(create.args[:3], ("token", "POST", "/repos/org/repo/check-runs"))
            self.assertEqual(create.args[3]["name"], module.HEAD_CHECK_NAME)
            self.assertEqual(create.args[3]["head_sha"], "a" * 40)
            complete = request.call_args_list[1]
            self.assertEqual(
                complete.args[:3],
                ("token", "PATCH", "/repos/org/repo/check-runs/42"),
            )
            self.assertEqual(complete.args[3]["conclusion"], "success")


if __name__ == "__main__":
    unittest.main()
