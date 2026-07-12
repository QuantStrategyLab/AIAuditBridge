from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from scripts.contract_identity import (
    MAX_ANCHOR_BYTES,
    MAX_CLAUSE_BYTES,
    MAX_ITEMS,
    MAX_TOKEN_BYTES,
    MAX_TOKENS_PER_CLAUSE,
    IdentityValidationError,
    _SecretRedactor,
    _text,
    build_contract_identity,
    canonical_json,
    operators,
    verify_persisted_identity,
)


class ContractIdentityTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return {
            "schema": "contract_identity.v2",
            "canonicalizer_version": "operator_tokens.v1",
            "scope": {
                "repo": "org/audit-bridge",
                "file": "service/review.py",
                "category": "contract",
            },
            "anchors": [{"kind": "symbol", "value": "Review.validate()"}],
            "predicates": [["score>=threshold"]],
            "required_behavior": [["return", "blocked"]],
            "forbidden_behavior": [],
            "ordering_constraints": [["validate", "before", "dispatch"]],
            "evidence": {
                "head_sha": "deadbeef",
                "diff_digest": "a" * 64,
                "file": "service/review.py",
                "location_or_hunk_digest": "hunk:review:1",
            },
            "severity": "high",
        }

    def test_operator_matrix_is_atomic_and_collision_resistant(self) -> None:
        fingerprints = set()
        for operator in operators():
            payload = self.payload()
            payload["predicates"] = [[f"left{operator}right"]]
            identity = build_contract_identity(payload)
            self.assertIn(operator, identity.predicates[0])
            fingerprints.add(identity.fingerprint_v2)
        self.assertEqual(len(fingerprints), len(operators()))

    def test_anchor_and_predicate_matrix_changes_contract_key(self) -> None:
        schema = self.payload()
        schema["anchors"] = [{"kind": "schema", "value": "schema_v2"}]
        fingerprint = copy.deepcopy(schema)
        fingerprint["anchors"] = [{"kind": "schema", "value": "fingerprint_v2"}]
        self.assertNotEqual(
            build_contract_identity(schema).contract_key,
            build_contract_identity(fingerprint).contract_key,
        )

        auth = self.payload()
        auth["predicates"] = [["validate()", "checks", "auth_header"]]
        database = copy.deepcopy(auth)
        database["predicates"] = [["validate()", "prevents", "database_leak"]]
        self.assertNotEqual(
            build_contract_identity(auth).contract_key,
            build_contract_identity(database).contract_key,
        )

        unrelated = self.payload()
        unrelated["anchors"] = [{"kind": "symbol", "value": "Audit.redact()"}]
        self.assertNotEqual(
            build_contract_identity(auth).contract_key,
            build_contract_identity(unrelated).contract_key,
        )

    def test_severity_is_excluded_and_opposite_behavior_is_distinct(self) -> None:
        high = build_contract_identity(self.payload())
        critical_payload = self.payload()
        critical_payload["severity"] = "critical"
        critical = build_contract_identity(critical_payload)
        self.assertEqual(high.contract_key, critical.contract_key)
        self.assertEqual(high.behavior_digest, critical.behavior_digest)
        self.assertEqual(high.fingerprint_v2, critical.fingerprint_v2)

        opposite_payload = self.payload()
        opposite_payload["required_behavior"] = [["return", "success"]]
        opposite = build_contract_identity(opposite_payload)
        self.assertEqual(high.contract_key, opposite.contract_key)
        self.assertNotEqual(high.behavior_digest, opposite.behavior_digest)

    def test_limits_reject_instead_of_truncating_and_keep_long_tails(self) -> None:
        exact = self.payload()
        exact["anchors"] = [{"kind": "identifier", "value": "a" * MAX_ANCHOR_BYTES}]
        self.assertEqual(
            build_contract_identity(exact).anchors[0].value,
            "a" * MAX_ANCHOR_BYTES,
        )
        oversize = copy.deepcopy(exact)
        oversize["anchors"][0]["value"] += "a"
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(oversize)

        first = self.payload()
        first["predicates"] = [["p" * 240, "tail_alpha"]]
        second = copy.deepcopy(first)
        second["predicates"] = [["p" * 240, "tail_beta"]]
        self.assertNotEqual(
            build_contract_identity(first).contract_key,
            build_contract_identity(second).contract_key,
        )
        first["predicates"] = [["p" * (MAX_TOKEN_BYTES + 1)]]
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(first)

    def test_secret_literals_become_typed_placeholders_before_hashing(self) -> None:
        payload = self.payload()
        payload["required_behavior"] = [["token=secret-value"]]
        identity = build_contract_identity(payload)
        record = identity.as_record()
        serialized = canonical_json(identity)
        self.assertIn("<SECRET:CREDENTIAL:1>", serialized)
        self.assertNotIn("secret-value", serialized)
        self.assertEqual(verify_persisted_identity(record), identity)

        payload["required_behavior"] = [["<SECRET:CREDENTIAL:1>"]]
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(payload)
        record["required_behavior"] = [["token=secret-value"]]
        with self.assertRaises(IdentityValidationError):
            verify_persisted_identity(record)
        for malformed in ("<SECRET:CREDENTIAL:1> <SECRET:BAD>", "<SECRET:CREDENTIAL:1> <SECRET:"):
            record = identity.as_record()
            record["required_behavior"] = [[malformed]]
            with self.assertRaises(IdentityValidationError):
                verify_persisted_identity(record)

        oversized_secret = self.payload()
        oversized_secret["required_behavior"] = [["x" * MAX_TOKEN_BYTES + "AKIAIOSFODNN7EXAMPLE"]]
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(oversized_secret)

    def test_credential_states_are_not_collapsed_and_common_literals_are_redacted(self) -> None:
        required = self.payload()
        required["required_behavior"] = [["authorization=required"]]
        forbidden = copy.deepcopy(required)
        forbidden["required_behavior"] = [["authorization=forbidden"]]
        self.assertEqual(
            verify_persisted_identity(build_contract_identity(required).as_record()),
            build_contract_identity(required),
        )
        self.assertEqual(
            verify_persisted_identity(build_contract_identity(forbidden).as_record()),
            build_contract_identity(forbidden),
        )
        self.assertNotEqual(
            build_contract_identity(required).behavior_digest,
            build_contract_identity(forbidden).behavior_digest,
        )
        secrets = self.payload()
        secrets["required_behavior"] = [["github_pat_" + "x" * 30], ["aws_secret_access_key=secret-value"]]
        identity = build_contract_identity(secrets)
        serialized = canonical_json(identity)
        self.assertNotIn("github_pat_", serialized)
        self.assertNotIn("secret-value", serialized)

        for state in ("required", "forbidden"):
            split = self.payload()
            split["required_behavior"] = [["authorization", "=", state]]
            split_identity = build_contract_identity(split)
            self.assertEqual(verify_persisted_identity(split_identity.as_record()), split_identity)
        for clauses in (
            [["token", "=", "supersecret"]],
            [["github_pat_" + "x" * 30], ["aws_secret_access_key", "=", "secret-value"]],
        ):
            split = self.payload()
            split["required_behavior"] = clauses
            serialized = canonical_json(build_contract_identity(split))
            self.assertNotIn("supersecret", serialized)
            self.assertNotIn("github_pat_", serialized)
            self.assertNotIn("secret-value", serialized)

    def test_schema_fields_category_unicode_and_controls_are_strict(self) -> None:
        for mutate in (
            lambda value: value.pop("schema"),
            lambda value: value.update({"description": "raw prose"}),
            lambda value: value["scope"].update({"category": "unknown"}),
        ):
            payload = self.payload()
            mutate(payload)
            with self.assertRaises(IdentityValidationError):
                build_contract_identity(payload)

        payload = self.payload()
        payload["anchors"] = [{"kind": "identifier", "value": "Cafe\u0301"}]
        self.assertEqual(build_contract_identity(payload).anchors[0].value, "Café")
        payload["anchors"] = [{"kind": "identifier", "value": "bad\nanchor"}]
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(payload)

    def test_repo_and_relative_path_boundaries(self) -> None:
        for repo in ("QuantStrategyLab/AIAuditBridge", "owner/repo.name"):
            payload = self.payload()
            payload["scope"]["repo"] = repo
            self.assertEqual(build_contract_identity(payload).scope.repo, repo)
        for repo in (
            "owner", "/owner/repo", "owner/", "owner//repo", "owner/repo/extra",
            "owner_name/repo", "-owner/repo", "owner-/repo", "owner--name/repo", "./repo", "owner/.", "owner/..",
        ):
            payload = self.payload()
            payload["scope"]["repo"] = repo
            with self.assertRaises(IdentityValidationError):
                build_contract_identity(payload)

        payload = self.payload()
        payload["scope"]["file"] = "src/nested/review.py"
        payload["evidence"]["file"] = "src/nested/review.py"
        self.assertEqual(build_contract_identity(payload).scope.file, "src/nested/review.py")
        for path in ("/abs/review.py", "../escape.py", "a//b.py", "a\\b.py", "./review.py"):
            invalid = self.payload()
            invalid["scope"]["file"] = path
            invalid["evidence"]["file"] = path
            with self.assertRaises(IdentityValidationError):
                build_contract_identity(invalid)

        mismatch = self.payload()
        mismatch["evidence"]["file"] = "service/other.py"
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(mismatch)
        secret_scope = self.payload()
        secret_scope["scope"]["repo"] = "owner/token=secret-value"
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(secret_scope)
        secret_ref = self.payload()
        secret_ref["evidence"]["location_or_hunk_digest"] = "token=secret-value"
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(secret_ref)

    def test_untrusted_shape_and_normalize_bounds_fail_closed(self) -> None:
        anchors = self.payload()
        anchors["anchors"] = anchors["anchors"] * (MAX_ITEMS + 1)
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(anchors)
        clauses = self.payload()
        clauses["predicates"] = [["x"] * (MAX_TOKENS_PER_CLAUSE + 1)]
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(clauses)
        aggregate = self.payload()
        aggregate["predicates"] = [["x" * MAX_TOKEN_BYTES] * MAX_TOKENS_PER_CLAUSE]
        self.assertGreater(sum(map(len, aggregate["predicates"][0])), MAX_CLAUSE_BYTES - 1)
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(aggregate)
        oversized_dict = self.payload()
        oversized_dict.update({f"extra_{index}": index for index in range(1000)})
        with self.assertRaises(IdentityValidationError):
            build_contract_identity(oversized_dict)
        with patch("scripts.contract_identity.unicodedata.normalize") as normalize:
            with self.assertRaises(IdentityValidationError):
                _text("x" * 2_000_000, "oversized", MAX_TOKEN_BYTES, _SecretRedactor(allow_placeholders=False))
            normalize.assert_not_called()

    def test_digest_tamper_order_and_evidence_binding_are_explicit(self) -> None:
        identity = build_contract_identity(self.payload())
        tampered = identity.as_record()
        tampered["behavior_digest"] = "0" * 64
        with self.assertRaises(IdentityValidationError):
            verify_persisted_identity(tampered)

        reversed_payload = self.payload()
        reversed_payload["ordering_constraints"] = [["dispatch", "before", "validate"]]
        reversed_identity = build_contract_identity(reversed_payload)
        self.assertEqual(identity.contract_key, reversed_identity.contract_key)
        self.assertNotEqual(identity.behavior_digest, reversed_identity.behavior_digest)

        evidence_change = self.payload()
        evidence_change["evidence"]["head_sha"] = "feedface"
        evidence_change["evidence"]["diff_digest"] = "b" * 64
        rebound = build_contract_identity(evidence_change)
        self.assertEqual(identity.fingerprint_v2, rebound.fingerprint_v2)
        self.assertNotEqual(
            json.dumps(identity.evidence.as_dict(), sort_keys=True),
            json.dumps(rebound.evidence.as_dict(), sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
