from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service.model_catalog import (
    ModelRecord,
    allow_catalog_parent,
    apply_sticky_assignments,
    assign_tiers,
    capability_score_for,
    is_chat_candidate,
    load_catalog,
    save_catalog_atomic,
)
from service.model_catalog_sync import (
    _sanitize_api_key,
    bootstrap_records,
    build_catalog,
    merge_records,
    sync_catalog,
    update_absence_counts,
)
from service.model_resolver import reset_catalog_cache, resolve_model, tier_for_budget


class ModelCatalogScoringTests(unittest.TestCase):
    def test_api_key_sanitizer_rejects_injection(self) -> None:
        self.assertEqual(_sanitize_api_key("sk-test_key-1234567890"), "sk-test_key-1234567890")
        self.assertEqual(_sanitize_api_key("Bearer sk-test_key-1234567890"), "sk-test_key-1234567890")
        self.assertEqual(_sanitize_api_key("sk-evil\r\ninjected"), "")
        self.assertEqual(_sanitize_api_key("not a key"), "")

    def test_catalog_path_rejects_outside_allowed_roots(self) -> None:
        from service.model_catalog import validate_catalog_path

        with self.assertRaises(ValueError):
            validate_catalog_path(Path("/etc/passwd"))
        with self.assertRaises(ValueError):
            validate_catalog_path(Path("/tmp/not-model-catalog.json"))
        with self.assertRaises(ValueError):
            validate_catalog_path(Path("/tmp/evil/model_catalog.json"))
        with self.assertRaises(ValueError):
            validate_catalog_path(Path("/tmp/evil/nested/model_catalog.json"))

    def test_flagship_scores_higher_than_mini(self) -> None:
        self.assertGreater(capability_score_for("gpt-5.5"), capability_score_for("gpt-5.4-mini"))

    def test_chat_candidate_filters_non_text_models(self) -> None:
        self.assertTrue(is_chat_candidate("gpt-5.4-mini"))
        self.assertTrue(is_chat_candidate("claude-sonnet-4-6"))
        self.assertFalse(is_chat_candidate("gpt-image-1"))
        self.assertFalse(is_chat_candidate("gpt-4o-audio-preview"))
        self.assertFalse(is_chat_candidate("gpt-4o-search-preview"))

    def test_assign_tiers_picks_distinct_roles(self) -> None:
        records = bootstrap_records()
        tiers = assign_tiers(records)
        self.assertEqual(set(tiers), {"nano", "fast", "standard", "capable", "flagship"})
        self.assertEqual(tiers["flagship"].model, "gpt-5.6-sol")

    def test_gpt56_family_scores_above_legacy_flagship(self) -> None:
        self.assertGreater(capability_score_for("gpt-5.6-sol"), capability_score_for("gpt-5.6-terra"))
        self.assertGreater(capability_score_for("gpt-5.6-terra"), capability_score_for("gpt-5.6-luna"))
        self.assertGreater(capability_score_for("gpt-5.6-sol"), capability_score_for("gpt-5.5"))
        self.assertGreater(capability_score_for("gpt-5.6-sol"), capability_score_for("gpt-5.6-luna"))
        self.assertGreater(capability_score_for("claude-fable-5"), capability_score_for("claude-sonnet-4-6"))


class ModelCatalogPersistenceTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        catalog = build_catalog(bootstrap_records())
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            save_catalog_atomic(catalog, path)
            loaded = load_catalog(path)
            self.assertEqual(loaded.tiers["flagship"].model, catalog.tiers["flagship"].model)


class ModelCatalogSyncTests(unittest.TestCase):
    def test_rediscovered_model_removed_from_deprecated(self) -> None:
        previous = build_catalog(bootstrap_records())
        previous.deprecated = ["gpt-5.5"]
        previous.absence_counts = {"gpt-5.5": 2}
        previous.presence_counts = {}
        absence_counts, deprecated, presence_counts = update_absence_counts(
            previous,
            discovered_ids={"gpt-5.5", "gpt-5.4-mini"},
            deprecation_misses=2,
        )
        # First rediscovery keeps deprecated (hysteresis).
        self.assertIn("gpt-5.5", deprecated)
        self.assertEqual(presence_counts.get("gpt-5.5"), 1)
        previous.absence_counts = absence_counts
        previous.deprecated = deprecated
        previous.presence_counts = presence_counts
        absence_counts, deprecated, presence_counts = update_absence_counts(
            previous,
            discovered_ids={"gpt-5.5", "gpt-5.4-mini"},
            deprecation_misses=2,
        )
        self.assertNotIn("gpt-5.5", deprecated)
        self.assertNotIn("gpt-5.5", absence_counts)
        self.assertNotIn("gpt-5.5", presence_counts)

    def test_sync_keeps_previous_catalog_when_discovery_empty(self) -> None:
        previous = build_catalog(bootstrap_records())
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            save_catalog_atomic(previous, path)
            with (
                patch("service.model_catalog_sync.discover_all_records", return_value=[]),
                patch("service.model_catalog_sync._provider_keys_configured", return_value=True),
            ):
                result = sync_catalog(output_path=str(path), force=True)
            self.assertEqual(result.synced_at, previous.synced_at)
            self.assertTrue(result.last_sync_attempt_at)

    def test_force_sync_requires_provider_keys_when_catalog_exists(self) -> None:
        from service.model_catalog_sync import CatalogSyncError

        previous = build_catalog(bootstrap_records())
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            save_catalog_atomic(previous, path)
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}, clear=False),
                self.assertRaises(CatalogSyncError),
            ):
                sync_catalog(output_path=str(path), force=True)

    def test_deprecated_model_stays_deprecated_when_still_absent(self) -> None:
        previous = build_catalog(bootstrap_records())
        previous.deprecated = ["gpt-5.5"]
        previous.absence_counts = {"gpt-5.5": 2}
        _, deprecated, _ = update_absence_counts(
            previous,
            discovered_ids={"gpt-5.4-mini"},
            deprecation_misses=2,
        )
        self.assertIn("gpt-5.5", deprecated)

    def test_deprecation_after_two_misses(self) -> None:
        previous = build_catalog(bootstrap_records())
        absence_counts, deprecated, _ = update_absence_counts(
            previous,
            discovered_ids={"gpt-5.4-mini"},
            deprecation_misses=2,
        )
        previous.absence_counts = absence_counts
        previous.deprecated = deprecated
        _, deprecated, _ = update_absence_counts(
            previous,
            discovered_ids={"gpt-5.4-mini"},
            deprecation_misses=2,
        )
        self.assertIn("gpt-5.5", deprecated)

    def test_sticky_keeps_previous_assignment_only_when_tier_changes(self) -> None:
        previous = build_catalog(bootstrap_records(), catalog_source="live")
        # Near-equal peer: sticky should hold the previous flagship.
        peer = ModelRecord(
            model_id="gpt-5.6-peer",
            provider="openai",
            capability_score=float(previous.models[previous.tiers["flagship"].model].capability_score) + 0.01,
            input_cost_per_1m=6.0,
            output_cost_per_1m=30.0,
        )
        records = merge_records(bootstrap_records(), [peer])
        new_tiers = assign_tiers(records)
        merged = apply_sticky_assignments(
            new_tiers,
            previous,
            discovered_ids={record.model_id for record in records},
            sticky_days=30,
            model_scores={record.model_id: float(record.capability_score) for record in records},
        )
        self.assertEqual(merged["flagship"].model, previous.tiers["flagship"].model)
        self.assertNotEqual(new_tiers["flagship"].model, previous.tiers["flagship"].model)

    def test_sticky_allows_clear_capability_upgrade(self) -> None:
        previous = build_catalog(bootstrap_records(), catalog_source="live")
        upgrade = ModelRecord(
            model_id="gpt-6.0-sol",
            provider="openai",
            capability_score=1.4,
            input_cost_per_1m=8.0,
            output_cost_per_1m=32.0,
        )
        records = merge_records(bootstrap_records(), [upgrade])
        new_tiers = assign_tiers(records)
        merged = apply_sticky_assignments(
            new_tiers,
            previous,
            discovered_ids={record.model_id for record in records},
            sticky_days=30,
            model_scores={record.model_id: float(record.capability_score) for record in records},
        )
        self.assertEqual(merged["flagship"].model, "gpt-6.0-sol")
        self.assertEqual(new_tiers["flagship"].model, "gpt-6.0-sol")

    def test_sticky_keeps_assignment_when_model_temporarily_missing(self) -> None:
        previous = build_catalog(bootstrap_records(), catalog_source="live")
        old_flagship = previous.tiers["flagship"].model
        remaining = [record for record in bootstrap_records() if record.model_id != old_flagship]
        new_tiers = assign_tiers(remaining)
        merged = apply_sticky_assignments(
            new_tiers,
            previous,
            discovered_ids={record.model_id for record in remaining},
            sticky_days=30,
            model_scores={record.model_id: float(record.capability_score) for record in remaining},
        )
        self.assertEqual(merged["flagship"].model, old_flagship)


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

    def test_resolver_reloads_when_catalog_mtime_changes(self) -> None:
        from service.model_catalog import TierAssignment

        catalog = load_catalog(Path(self._catalog_path))
        first = resolve_model(task_type="dual_review")
        self.assertEqual(first["model"], catalog.tiers["flagship"].model)
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            mutated = build_catalog(bootstrap_records())
            mutated.tiers["flagship"] = TierAssignment(
                tier="flagship",
                model="gpt-5.4",
                provider="openai",
                effort="xhigh",
            )
            save_catalog_atomic(mutated, path)
            os.environ["MODEL_CATALOG_PATH"] = str(path)
            # Do not reset cache: mtime mismatch should trigger reload.
            second = resolve_model(task_type="dual_review")
            self.assertEqual(second["model"], "gpt-5.4")


if __name__ == "__main__":
    unittest.main()
