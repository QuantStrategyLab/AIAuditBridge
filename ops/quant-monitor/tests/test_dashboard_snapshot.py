import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "quant_monitor_dashboard_snapshot",
    ROOT / "scripts" / "build_dashboard_snapshot.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
build_payload = MODULE.build_payload


class DashboardSnapshotTests(unittest.TestCase):
    def test_missing_health_file_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_payload(health_file=Path(tmp) / "missing.json")

        self.assertEqual(payload["data_status"], "unavailable")
        self.assertEqual(payload["summary"]["strategy_count"], 0)
        self.assertTrue(payload["errors"])
        self.assertTrue(all("/" not in error and "\\" not in error for error in payload["errors"]))
        self.assertEqual(payload["schema_version"], "strategy_health_dashboard.v1")

    def test_normalizes_health_scores_and_review_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health = root / "health.json"
            health.write_text(json.dumps({
                "computed_at": "2026-07-11T00:00:00+00:00",
                "strategies": [{
                    "strategy_profile": "demo_trend",
                    "domain": "crypto",
                    "as_of": "2026-07-10",
                    "overall_score": 76.5,
                    "performance_score": 80,
                    "risk_score": 75,
                    "decay_score": 70,
                    "stability_score": 78,
                    "operational_score": 82,
                    "status": "healthy",
                    "source_revision": "https://example.invalid/revisions/rev-1",
                }],
            }), encoding="utf-8")
            review_dir = root / "reviews"
            review_dir.mkdir()
            (review_dir / "demo.json").write_text(json.dumps({
                "profile": "demo_trend",
                "requested_stage": "shadow_candidate",
                "evidence_package_id": "https://example.invalid/evidence-1",
                "validation": {"oos_passed": True},
                "kelly_readiness": {"level": "K1", "full_kelly_allowed": False},
            }), encoding="utf-8")

            payload = build_payload(health_file=health, review_dir=review_dir)

        strategy = payload["strategies"][0]
        self.assertEqual(payload["data_status"], "ready")
        self.assertEqual(payload["summary"]["healthy"], 1)
        self.assertEqual(strategy["review"]["requested_stage"], "shadow_candidate")
        self.assertEqual(strategy["decision"]["code"], "auto_advance")
        self.assertEqual(strategy["freshness"]["status"], "unknown")
        self.assertEqual(strategy["source_revision"], "https://example.invalid/revisions/rev-1")
        self.assertEqual(strategy["review"]["evidence_package_id"], "https://example.invalid/evidence-1")

    def test_upstream_unavailable_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp) / "health.json"
            health.write_text(json.dumps({
                "data_status": "unavailable",
                "strategies": [{
                    "strategy_profile": "must_not_publish",
                    "domain": "crypto",
                    "status": "healthy",
                }],
            }), encoding="utf-8")

            payload = build_payload(health_file=health)

        self.assertEqual(payload["data_status"], "unavailable")
        self.assertEqual(payload["summary"]["strategy_count"], 0)
        self.assertEqual(payload["strategies"], [])

    def test_malformed_strategy_shape_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp) / "health.json"
            health.write_text(json.dumps({"strategies": {"not": "a list"}}), encoding="utf-8")

            payload = build_payload(health_file=health)

        self.assertEqual(payload["data_status"], "unavailable")
        self.assertIn("strategies_not_array", payload["errors"])
        self.assertEqual(payload["strategies"], [])

    def test_invalid_computed_at_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp) / "health.json"
            health.write_text(json.dumps({
                "computed_at": "not-a-timestamp",
                "strategies": [{
                    "strategy_profile": "must_not_publish",
                    "domain": "crypto",
                    "status": "healthy",
                }],
            }), encoding="utf-8")

            payload = build_payload(health_file=health)

        self.assertEqual(payload["data_status"], "unavailable")
        self.assertIn("computed_at_invalid", payload["errors"])
        self.assertEqual(payload["strategies"], [])

    def test_non_finite_freshness_age_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp) / "health.json"
            health.write_text(json.dumps({
                "strategies": [{
                    "strategy_profile": "safe",
                    "domain": "crypto",
                    "status": "healthy",
                    "freshness": {"status": "fresh", "age_seconds": float("inf")},
                }],
            }), encoding="utf-8")

            payload = build_payload(health_file=health)

        self.assertEqual(payload["data_status"], "ready")
        self.assertIsNone(payload["strategies"][0]["freshness"]["age_seconds"])

    def test_mixed_strategy_rows_are_fail_closed_without_partial_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp) / "health.json"
            health.write_text(json.dumps({"strategies": [
                {"strategy_profile": "valid", "domain": "crypto", "status": "healthy"},
                {"strategy_profile": "invalid", "domain": "unknown", "status": "healthy"},
            ]}), encoding="utf-8")

            payload = build_payload(health_file=health)

        self.assertEqual(payload["data_status"], "unavailable")
        self.assertEqual(payload["strategies"], [])
        self.assertEqual(payload["summary"]["strategy_count"], 0)

    def test_non_finite_review_metrics_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health = root / "health.json"
            health.write_text(json.dumps({
                "strategies": [{"strategy_profile": "safe", "domain": "crypto", "status": "healthy"}],
            }), encoding="utf-8")
            reviews = root / "reviews"
            reviews.mkdir()
            (reviews / "safe.json").write_text(
                '{"profile":"safe","validation":{"bad":NaN,"good":1}}',
                encoding="utf-8",
            )

            payload = build_payload(health_file=health, review_dir=reviews)

        self.assertNotIn("bad", payload["strategies"][0]["review"]["validation"])
        self.assertEqual(payload["strategies"][0]["review"]["validation"]["good"], 1)

    def test_duplicate_review_artifacts_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health = root / "health.json"
            health.write_text(json.dumps({"strategies": [
                {"strategy_profile": "safe", "domain": "crypto", "status": "healthy"},
            ]}), encoding="utf-8")
            reviews = root / "reviews"
            reviews.mkdir()
            for name, stage in (("old.json", "shadow_candidate"), ("new.json", "live_candidate")):
                (reviews / name).write_text(
                    json.dumps({"profile": "safe", "requested_stage": stage}),
                    encoding="utf-8",
                )

            payload = build_payload(health_file=health, review_dir=reviews)

        self.assertEqual(payload["data_status"], "unavailable")
        self.assertIn("review_artifact_ambiguous", payload["errors"])
        self.assertEqual(payload["strategies"], [])

    def test_case_only_duplicate_review_artifacts_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health = root / "health.json"
            health.write_text(json.dumps({"strategies": [
                {"strategy_profile": "Alpha", "domain": "crypto", "status": "healthy"},
            ]}), encoding="utf-8")
            reviews = root / "reviews"
            reviews.mkdir()
            for name, profile in (("upper.json", "Alpha"), ("lower.json", "alpha")):
                (reviews / name).write_text(
                    json.dumps({"profile": profile, "requested_stage": "live_candidate"}),
                    encoding="utf-8",
                )

            payload = build_payload(health_file=health, review_dir=reviews)

        self.assertEqual(payload["data_status"], "unavailable")
        self.assertIn("review_artifact_ambiguous", payload["errors"])
        self.assertEqual(payload["strategies"], [])

    def test_force_unavailable_ignores_existing_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = Path(tmp) / "health.json"
            health.write_text(json.dumps({"strategies": [{
                "strategy_profile": "injected", "domain": "crypto", "status": "healthy",
            }]}), encoding="utf-8")

            payload = build_payload(health_file=health, force_unavailable=True)

        self.assertEqual(payload["data_status"], "unavailable")
        self.assertEqual(payload["strategies"], [])

    def test_redacts_untrusted_review_fields_and_keeps_missing_scores_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health = root / "health.json"
            health.write_text(json.dumps({"strategies": [{
                "strategy_profile": "safe_profile",
                "domain": "crypto",
                "status": "watch",
                "overall_score": "not-a-score",
                "risk_score": 0,
            }]}), encoding="utf-8")
            reviews = root / "reviews"
            reviews.mkdir()
            (reviews / "safe.json").write_text(json.dumps({
                "profile": "safe_profile",
                "requested_stage": "shadow_candidate",
                "validation": {"oos_passed": True, "token": "do-not-publish"},
                "secret": "do-not-publish",
            }), encoding="utf-8")
            payload = build_payload(health_file=health, review_dir=reviews)

        strategy = payload["strategies"][0]
        self.assertIsNone(strategy["score"])
        self.assertEqual(strategy["components"]["performance"], None)
        self.assertNotIn("secret", strategy["review"])
        self.assertNotIn("token", strategy["review"]["validation"])

    def test_policy_keeps_normal_live_as_human_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_payload(health_file=Path(tmp) / "missing.json")

        self.assertIn("bounded_canary_run", payload["policy"]["automatic_modes"])
        self.assertEqual(payload["policy"]["human_gate_stages"], ["live_candidate", "runtime_enabled"])

    def test_healthy_live_candidate_still_waits_for_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health = root / "health.json"
            health.write_text(json.dumps({"strategies": [{
                "strategy_profile": "live_ready",
                "domain": "crypto",
                "status": "healthy",
                "overall_score": 90,
            }]}), encoding="utf-8")
            reviews = root / "reviews"
            reviews.mkdir()
            (reviews / "live.json").write_text(json.dumps({
                "profile": "live_ready",
                "requested_stage": "live_candidate",
            }), encoding="utf-8")
            payload = build_payload(health_file=health, review_dir=reviews)

        self.assertEqual(payload["strategies"][0]["decision"]["code"], "human_live_gate")


if __name__ == "__main__":
    unittest.main()
