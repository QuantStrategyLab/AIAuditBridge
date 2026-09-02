from __future__ import annotations

import dataclasses
import unittest

from service.ai_provenance import build_provenance_receipt, verify_receipt


class AiProvenanceReceiptTests(unittest.TestCase):
    def test_verified_review_receipt_is_frozen_and_tamper_evident(self) -> None:
        output = '{"verdict":"approve","confidence":0.9,"summary":"ok"}'

        receipt = build_provenance_receipt(
            operation="review",
            requested_provider="openai",
            requested_model="gpt-5.4-mini",
            actual_provider="openai",
            actual_model="gpt-5.4-mini",
            system="review system",
            user="review input",
            output=output,
        )

        self.assertEqual(receipt.identity_verdict, "verified")
        self.assertEqual(receipt.evaluation_status, "verified")
        self.assertEqual(receipt.evaluation_verdict, "approve")
        self.assertEqual(receipt.policy_verdict, "eligible")
        self.assertTrue(verify_receipt(
            receipt.to_dict(),
            system="review system",
            user="review input",
            output=output,
        ))
        self.assertFalse(verify_receipt(
            receipt.to_dict(),
            system="review system",
            user="review input",
            output="different output",
        ))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.actual_model = "other"  # type: ignore[misc]

        tampered = receipt.to_dict()
        tampered["actual_model"] = "other"
        self.assertFalse(verify_receipt(tampered))

    def test_model_mismatch_is_advisory(self) -> None:
        receipt = build_provenance_receipt(
            operation="review",
            requested_provider="openai",
            requested_model="gpt-5.4-mini",
            actual_provider="openai",
            actual_model="gpt-5.4-mini-2026-08-01",
            system="review system",
            user="review input",
            output='{"verdict":"approve","confidence":0.9}',
        )

        self.assertEqual(receipt.identity_verdict, "mismatch")
        self.assertEqual(receipt.policy_verdict, "advisory")

    def test_verified_reject_verdict_is_still_advisory(self) -> None:
        receipt = build_provenance_receipt(
            operation="review",
            requested_provider="openai",
            requested_model="gpt-5.4-mini",
            actual_provider="openai",
            actual_model="gpt-5.4-mini",
            system="review system",
            user="review input",
            output='{"verdict":"reject","confidence":0.99}',
        )

        self.assertEqual(receipt.evaluation_status, "verified")
        self.assertEqual(receipt.evaluation_verdict, "reject")
        self.assertEqual(receipt.policy_verdict, "advisory")

    def test_analyze_without_evaluator_is_advisory(self) -> None:
        receipt = build_provenance_receipt(
            operation="analyze",
            requested_provider="anthropic",
            requested_model="claude-sonnet-4-6",
            actual_provider="anthropic",
            actual_model="claude-sonnet-4-6",
            system="analysis system",
            user="analysis input",
            output="analysis output",
        )

        self.assertEqual(receipt.identity_verdict, "verified")
        self.assertEqual(receipt.evaluation_status, "unavailable")
        self.assertEqual(receipt.evaluation_verdict, "unavailable")
        self.assertEqual(receipt.policy_verdict, "advisory")


if __name__ == "__main__":
    unittest.main()
