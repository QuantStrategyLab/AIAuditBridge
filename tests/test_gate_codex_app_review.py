from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "gate_codex_app_review.py"
SPEC = importlib.util.spec_from_file_location("gate_codex_app_review", MODULE_PATH)
gate_codex_app_review = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = gate_codex_app_review
SPEC.loader.exec_module(gate_codex_app_review)


class GateCodexAppReviewTest(unittest.TestCase):
    def test_scan_diff_does_not_echo_secret_values(self) -> None:
        diff = "\n".join(
            [
                "diff --git a/example.py b/example.py",
                "+++ b/example.py",
                '+api_key = "sk-' 'live-12345678901234567890"',
            ]
        )

        violations = gate_codex_app_review.scan_diff(diff, [])

        self.assertEqual(len(violations), 1)
        self.assertIn("api_key=<redacted>", violations[0])
        self.assertNotIn("sk-live-12345678901234567890", violations[0])


if __name__ == "__main__":
    unittest.main()
