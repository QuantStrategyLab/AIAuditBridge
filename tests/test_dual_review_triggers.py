from __future__ import annotations

import unittest

from service.dual_review_triggers import (
    drift_trigger,
    hit_rate_trigger,
    promotion_trigger,
    resolve_trigger,
)


class DualReviewTriggerTests(unittest.TestCase):
    def test_promotion_trigger(self) -> None:
        self.assertTrue(promotion_trigger(old_status="shadow_candidate", new_status="live_candidate"))
        self.assertFalse(promotion_trigger(old_status="research", new_status="shadow_candidate"))

    def test_hit_rate_trigger(self) -> None:
        self.assertTrue(hit_rate_trigger([0.55, 0.58, 0.59]))
        self.assertFalse(hit_rate_trigger([0.55, 0.62, 0.59]))

    def test_drift_trigger(self) -> None:
        self.assertTrue(drift_trigger(drift_sigma=3.1))
        self.assertTrue(drift_trigger(drift_score=0.8))
        self.assertFalse(drift_trigger(drift_sigma=2.5, drift_score=0.4))

    def test_resolve_trigger_explicit(self) -> None:
        trigger = resolve_trigger({"trigger": "drift", "drift_score": 0.5})
        self.assertEqual(trigger.value, "drift")

    def test_resolve_trigger_from_promotion_fields(self) -> None:
        trigger = resolve_trigger({"old_status": "shadow_candidate", "new_status": "live_ready"})
        self.assertEqual(trigger.value, "promotion")


if __name__ == "__main__":
    unittest.main()
