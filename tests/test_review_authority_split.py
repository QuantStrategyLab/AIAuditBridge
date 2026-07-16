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
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("persist-credentials: false", workflow)

    def test_connector_review_uses_a_distinct_advisory_check(self) -> None:
        workflow = (ROOT / ".github/workflows/codex_review_advisory.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("name: Codex Review Advisory", workflow)
        self.assertIn("pull_request_review:", workflow)
        self.assertIn("advisory:", workflow)
        self.assertNotIn("gate:", workflow)
        self.assertIn("report_codex_app_review.py", workflow)

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
                patch.object(module, "run_static_guard", side_effect=RuntimeError("API unavailable")),
            ):
                self.assertEqual(module.main(), 1)

    def test_advisory_ignores_stale_review_commit(self) -> None:
        module = _load_module("report_codex_app_review", "scripts/report_codex_app_review.py")
        event = {
            "pull_request": {"number": 7, "head": {"sha": "current-head"}},
            "review": {
                "user": {"login": module.BOT_LOGIN},
                "commit_id": "old-head",
                "state": "CHANGES_REQUESTED",
            },
        }

        self.assertEqual(module.evaluate_event(event)[0], "ignored_stale")


if __name__ == "__main__":
    unittest.main()
