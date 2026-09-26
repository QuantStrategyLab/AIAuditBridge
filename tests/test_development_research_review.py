from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from service.development_research_review import (
    A_SUMMARY_PATH,
    A_SUMMARY_SHA256,
    DuplicateReviewRegistry,
    DevelopmentResearchReviewError,
    build_message_from_file,
    canonical_json,
    create_message,
    validate_development_research_review,
    validate_summary,
)


class DevelopmentResearchReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        if not A_SUMMARY_PATH.is_file():
            self.skipTest("requires the fixed private A summary")
        self.summary_bytes = A_SUMMARY_PATH.read_bytes()
        self.summary = json.loads(self.summary_bytes)
        self.message = build_message_from_file(A_SUMMARY_PATH)

    def _reseal_projection(self, message: dict) -> dict:
        message["result_digest"] = hashlib.sha256(canonical_json(message["result"]).encode("utf-8")).hexdigest()
        message["provenance"]["normalized_result_sha256"] = message["result_digest"]
        message["provenance"]["upstream_input_index_sha256"] = hashlib.sha256(
            canonical_json(message["upstream_input_index"]).encode("utf-8")
        ).hexdigest()
        identities = message["identities"]
        key_material = {
            "schema": message["schema"],
            "identities": identities,
            "upstream_input_index_sha256": message["provenance"]["upstream_input_index_sha256"],
            "policy_id": identities["policy_id"],
            "policy_sha256": identities["policy_sha256"],
            "settlement_policy_id": identities["settlement_policy_id"],
            "settlement_policy_sha256": identities["settlement_policy_sha256"],
            "cost_id": identities["cost_id"],
            "message_version": message["message_version"],
        }
        message["duplicate_key"] = hashlib.sha256(canonical_json(key_material).encode("utf-8")).hexdigest()
        message.pop("message_sha256", None)
        message["message_sha256"] = hashlib.sha256(canonical_json(message).encode("utf-8")).hexdigest()
        return message

    def test_fixed_a_summary_builds_complete_advisory_review_message(self) -> None:
        validated = validate_development_research_review(self.message)

        self.assertEqual(validated["schema"], "qsl.development_research_review.v1")
        self.assertEqual(validated["evidence_kind"], "development_research_review")
        self.assertEqual(validated["provenance"]["source_summary_bytes_sha256"], A_SUMMARY_SHA256)
        self.assertEqual(validated["result_digest"], hashlib.sha256(
            canonical_json(validated["result"]).encode("utf-8")
        ).hexdigest())
        self.assertEqual(validated["research_stage"], "development")
        producer_source = Path(__file__).resolve().parents[1] / "service" / "development_research_review.py"
        self.assertEqual(
            validated["identities"]["producer_revision_sha256"],
            hashlib.sha256(producer_source.read_bytes()).hexdigest(),
        )
        self.assertNotIn("producer_revision", validated["identities"])
        self.assertFalse(validated["strict_point_in_time_certified"])
        self.assertEqual(validated["authority"], {
            "review_disposition": "advisory",
            "mode": "read_only",
            "no_order": True,
            "adoption": False,
            "codegen": False,
            "experiment": False,
            "notification": False,
            "trade": False,
        })
        self.assertEqual(len(validated["upstream_input_index"]), 25)
        self.assertNotIn("private_ledger_sha256", json.dumps(validated))

    def test_runner_reads_the_exact_fixed_summary_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            altered = Path(directory) / "summary.json"
            altered.write_bytes(self.summary_bytes + b"\n")
            with self.assertRaisesRegex(DevelopmentResearchReviewError, "source_summary_digest_mismatch"):
                build_message_from_file(altered)
            with self.assertRaisesRegex(DevelopmentResearchReviewError, "source_summary_digest_mismatch"):
                create_message(self.summary_bytes + b"\n")

    def test_missing_upstream_input_is_rejected(self) -> None:
        summary = copy.deepcopy(self.summary)
        summary["input_validation"].pop("future_manifest_sha256")

        with self.assertRaisesRegex(DevelopmentResearchReviewError, "missing_upstream_input"):
            validate_summary(summary)

    def test_source_assurance_and_pit_cannot_be_upgraded(self) -> None:
        for field, value in (
            ("source_assurance", "cross_provider_verified"),
            ("strict_point_in_time_certified", True),
        ):
            with self.subTest(field=field):
                summary = copy.deepcopy(self.summary)
                summary[field] = value
                with self.assertRaises(DevelopmentResearchReviewError):
                    validate_summary(summary)

    def test_permission_elevation_and_private_fields_are_rejected(self) -> None:
        summary = copy.deepcopy(self.summary)
        summary["live_authorized"] = True
        with self.assertRaisesRegex(DevelopmentResearchReviewError, "permission_upgrade"):
            validate_summary(summary)

        message = copy.deepcopy(self.message)
        message["source_path"] = "/private/research/summary.json"
        with self.assertRaises(DevelopmentResearchReviewError):
            validate_development_research_review(message)

    def test_producer_revision_must_match_current_source_bytes(self) -> None:
        changed = copy.deepcopy(self.message)
        changed["identities"]["producer_revision_sha256"] = "0" * 64
        with self.assertRaisesRegex(DevelopmentResearchReviewError, "producer_revision_mismatch"):
            validate_development_research_review(changed)

    def test_resealed_projection_tampering_is_rejected_against_fixed_a_anchors(self) -> None:
        def change_policy_id(message: dict) -> None:
            message["identities"]["policy_id"] = "forged_policy"

        def change_policy_digest(message: dict) -> None:
            message["identities"]["policy_sha256"] = "a" * 64
            next(item for item in message["upstream_input_index"] if item["name"] == "capital_policy")["sha256"] = "a" * 64

        def change_settlement(message: dict) -> None:
            message["identities"]["settlement_policy_id"] = "forged_settlement"
            message["identities"]["settlement_policy_sha256"] = "b" * 64
            next(item for item in message["upstream_input_index"] if item["name"] == "settlement_policy")["sha256"] = "b" * 64

        def change_runner(message: dict) -> None:
            message["identities"]["runner_id"] = "c" * 64
            next(item for item in message["upstream_input_index"] if item["name"] == "research_runner")["sha256"] = "c" * 64

        def change_strategy(message: dict) -> None:
            message["identities"]["strategy_revision_sha256"] = "d" * 64
            next(item for item in message["upstream_input_index"] if item["name"] == "r8_engine")["sha256"] = "d" * 64

        def change_input_index(message: dict) -> None:
            next(item for item in message["upstream_input_index"] if item["name"] == "source_manifest")["sha256"] = "e" * 64

        def change_result(message: dict) -> None:
            message["result"]["paths"]["1000"]["C0"]["cumulative_return"] += 0.01

        for label, mutation in (
            ("policy_id", change_policy_id),
            ("policy_digest", change_policy_digest),
            ("settlement", change_settlement),
            ("runner", change_runner),
            ("strategy", change_strategy),
            ("input_index", change_input_index),
            ("result", change_result),
        ):
            with self.subTest(label=label):
                forged = copy.deepcopy(self.message)
                mutation(forged)
                self._reseal_projection(forged)
                with self.assertRaises(DevelopmentResearchReviewError):
                    validate_development_research_review(forged)

    def test_digest_conflicts_and_old_p3_schema_relabeling_are_rejected(self) -> None:
        message = copy.deepcopy(self.message)
        message["result_digest"] = "0" * 64
        with self.assertRaisesRegex(DevelopmentResearchReviewError, "result_digest_mismatch"):
            validate_development_research_review(message)

        for schema in ("qsl.research_task.v1", "qsl.research.p3_observation.v1"):
            with self.subTest(schema=schema):
                message = copy.deepcopy(self.message)
                message["schema"] = schema
                with self.assertRaises(DevelopmentResearchReviewError):
                    validate_development_research_review(message)

    def test_duplicate_economic_identity_is_idempotent_and_conflicts_are_rejected(self) -> None:
        registry = DuplicateReviewRegistry()
        first = registry.record(self.message)

        later = copy.deepcopy(self.message)
        later["created_at"] = "2026-09-27T12:00:00Z"
        later.pop("message_sha256")
        later["message_sha256"] = hashlib.sha256(
            canonical_json(later).encode("utf-8")
        ).hexdigest()
        duplicate = registry.record(later)
        self.assertEqual(duplicate["duplicate_key"], first["duplicate_key"])
        self.assertEqual(duplicate["result_digest"], first["result_digest"])
        self.assertEqual(duplicate["status"], "duplicate")

        conflict = copy.deepcopy(self.message)
        conflict["result"]["paths"]["1000"]["C0"]["cumulative_return"] += 0.01
        conflict["result_digest"] = hashlib.sha256(
            canonical_json(conflict["result"]).encode("utf-8")
        ).hexdigest()
        conflict["provenance"]["normalized_result_sha256"] = conflict["result_digest"]
        conflict.pop("message_sha256")
        conflict["message_sha256"] = hashlib.sha256(
            canonical_json(conflict).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(DevelopmentResearchReviewError, "result_anchor_mismatch"):
            registry.record(conflict)

    def test_valid_negative_differences_are_preserved(self) -> None:
        message = self.message
        differences = message["result"]["comparisons"]
        values = [
            value
            for scale in differences.values()
            for comparison in scale.values()
            for value in comparison.values()
        ]
        self.assertTrue(any(value < 0 for value in values))
        self.assertEqual(validate_development_research_review(message)["result"], message["result"])


if __name__ == "__main__":
    unittest.main()
