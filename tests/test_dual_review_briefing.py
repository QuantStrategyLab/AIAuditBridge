from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from service.dual_review_briefing import collect_dual_review_payloads


class DualReviewBriefingTests(unittest.TestCase):
    def test_collect_payload_from_drift_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            payload = {
                "domain": "cn_equity",
                "strategies": [
                    {
                        "strategy_profile": "cn_demo",
                        "drift_score": 0.92,
                        "primary_review": {"verdict": "approve", "confidence": 0.55},
                    }
                ],
            }
            (report_dir / "cn_equity.json").write_text(json.dumps(payload), encoding="utf-8")
            items = collect_dual_review_payloads(report_dir)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["trigger"], "drift")
            self.assertEqual(items[0]["strategy_profile"], "cn_demo")


if __name__ == "__main__":
    unittest.main()
