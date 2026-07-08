from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RunDualReviewCliTests(unittest.TestCase):
    def test_cli_disagreement_exit_code(self) -> None:
        payload = {
            "trigger": "drift",
            "strategy_profile": "cli_demo",
            "drift_score": 0.95,
            "primary_review": {"verdict": "approve", "confidence": 0.4},
        }
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_dual_review.py"),
                "--payload",
                json.dumps(payload),
                "--secondary-review",
                json.dumps(
                    {
                        "gpt": {"verdict": "reject", "confidence": 0.91},
                        "claude": {"verdict": "reject", "confidence": 0.9},
                    }
                ),
                "--secondary-mode",
                "stub",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(proc.returncode, 2)
        body = json.loads(proc.stdout)
        self.assertTrue(body["ok"])
        self.assertEqual(body["disagreements"], 1)


if __name__ == "__main__":
    unittest.main()
