from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from service.model_catalog import (
    ModelRecord,
    apply_sticky_assignments,
    assign_tiers,
    capability_score_for,
    load_catalog,
    save_catalog_atomic,
)
from service.model_catalog_sync import bootstrap_records, build_catalog, merge_records, update_absence_counts
from service.model_resolver import reset_catalog_cache, resolve_model, tier_for_budget


class ModelCatalogScoringTests(unittest.TestCase):
    def test_flagship_scores_higher_than_mini(self) -> None:
        self.assertGreater(capability_score_for("gpt-5.5"), capability_score_for("gpt-5.4-mini"))

    def test_assign_tiers_picks_distinct_roles(self) -> None:
        records = bootstrap_records()
        tiers = assign_tiers(records)
        self.assertEqual(set(tiers), {"nano", "fast", "standard", "capable", "flagship"})
        self.assertEqual(tiers["flagship"].model, "gpt-5.5")


class ModelCatalogPersistenceTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        catalog = build_catalog(bootstrap_records())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_catalog.json"
            save_catalog_atomic(catalog, path)
            loaded = load_catalog(path)
            self.assertEqual(loaded.tiers["flagship"].model, catalog.tiers["flagship"].model)


class ModelCatalogSyncTests(unittest.TestCase):
    def test_deprecation_after_two_misses(self) -> None:
        previous = build_catalog(bootstrap_records())
        absence_counts, deprecated = update_absence_counts(
            previous,
            discovered_ids={"gpt-5.4-mini"},
            deprecation_misses=2,
        )
        previous.absence_counts = absence_counts
        previous.deprecated = deprecated
        _, deprecated = update_absence_counts(
            previous,
            discovered_ids={"gpt-5.4-mini"},
            deprecation_misses=2,
        )
        self.assertIn("gpt-5.5", deprecated)

    def test_sticky_keeps_previous_assignment(self) -> None:
        previous = build_catalog(bootstrap_records())
        records = merge_records(
            bootstrap_records(),
            [
                ModelRecord(
                    model_id="gpt-6.0",
                    provider="openai",
                    capability_score=1.0,
                    input_cost_per_1m=5.0,
                    output_cost_per_1m=20.0,
                )
            ],
        )
        new_tiers = assign_tiers(records)
        merged = apply_sticky_assignments(
            new_tiers,
            previous,
            discovered_ids={record.model_id for record in records},
            sticky_days=30,
        )
        self.assertEqual(merged["flagship"].model, previous.tiers["flagship"].model)


class ModelResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo_catalog = Path(__file__).resolve().parents[1] / "generated" / "model_catalog.json"
        if not repo_catalog.is_file():
            raise unittest.SkipTest("generated/model_catalog.json missing")
        cls._catalog_path = str(repo_catalog)

    def setUp(self) -> None:
        reset_catalog_cache()
        os.environ["MODEL_CATALOG_PATH"] = self._catalog_path

    def tearDown(self) -> None:
        reset_catalog_cache()
        os.environ.pop("MODEL_CATALOG_PATH", None)

    def test_dual_review_resolves_flagship_tier(self) -> None:
        route = resolve_model(task_type="dual_review")
        catalog = load_catalog(Path(self._catalog_path))
        self.assertEqual(route["tier"], "flagship")
        self.assertEqual(route["model"], catalog.tiers["flagship"].model)
        self.assertEqual(route["effort"], "xhigh")

    def test_daily_briefing_resolves_nano_tier(self) -> None:
        route = resolve_model(task_type="daily_briefing")
        catalog = load_catalog(Path(self._catalog_path))
        self.assertEqual(route["tier"], "nano")
        self.assertEqual(route["model"], catalog.tiers["nano"].model)

    def test_low_budget_downgrades_tier(self) -> None:
        route = resolve_model(task_type="dual_review", budget_remaining=0.0, quota_status="low")
        self.assertEqual(route["tier"], tier_for_budget(0.0))
        self.assertEqual(route["effort"], "low")


if __name__ == "__main__":
    unittest.main()
