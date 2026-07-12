import copy
import json
import unittest
from scripts.canonical_typed_identity import IdentityError, canonical_json, validate_identity, verify_identity_record

class CanonicalTypedIdentityTests(unittest.TestCase):
    def assert_invalid(self, value):
        with self.assertRaises(IdentityError):
            validate_identity(value)
    def payload(self):
        def token(kind, value):
            return {"kind": kind, "value": value}
        return {
            "schema": "contract_identity.v2",
            "canonicalizer_version": "structured_tokens.v1",
            "scope": {"repo": "acme/audit-bridge", "file": "service/review.py", "category": "logic"},
            "anchors": [token("identifier", "Review.validate")],
            "predicates": [[token("identifier", "score"), token("operator", ">="), token("identifier", "threshold")]],
            "required_behavior": [[token("policy_state", "required")]],
            "forbidden_behavior": [[token("policy_state", "forbidden")]],
            "ordering_constraints": [[token("identifier", "validate"), token("operator", "->"), token("identifier", "persist")]],
            "severity": "high",
        }
    def test_operator_matrix_and_order_are_identity_input(self):
        for operator in (">=", "<=", ">", "<", "==", "!=", "===", "!==", "->", "=>", "::"):
            value = self.payload()
            value["predicates"][0][1]["value"] = operator
            self.assertEqual(validate_identity(value).payload["predicates"][0][1]["value"], operator)
        reversed_value = self.payload()
        reversed_value["predicates"][0] = list(reversed(reversed_value["predicates"][0]))
        self.assertNotEqual(validate_identity(self.payload()).contract_key, validate_identity(reversed_value).contract_key)
    def test_policy_and_severity_semantics(self):
        required = validate_identity(self.payload())
        forbidden = copy.deepcopy(self.payload())
        forbidden["required_behavior"], forbidden["forbidden_behavior"] = forbidden["forbidden_behavior"], forbidden["required_behavior"]
        self.assertNotEqual(required.behavior_digest, validate_identity(forbidden).behavior_digest)
        critical = copy.deepcopy(self.payload())
        critical["severity"] = "critical"
        self.assertEqual((required.contract_key, required.behavior_digest, required.fingerprint_v2), (validate_identity(critical).contract_key, validate_identity(critical).behavior_digest, validate_identity(critical).fingerprint_v2))
    def test_same_file_unrelated_contracts_do_not_collide(self):
        other = self.payload()
        other["anchors"] = [{"kind": "identifier", "value": "Auth.validate"}]
        self.assertNotEqual(validate_identity(self.payload()).contract_key, validate_identity(other).contract_key)
        unknown = self.payload()
        unknown["anchors"][0]["kind"] = "prose"
        self.assert_invalid(unknown)
    def test_strict_fields_paths_controls_and_bounds(self):
        for key in ("evidence", "evidence_digest", "description", "suggestion"):
            value = self.payload()
            value[key] = "not part of R1"
            self.assert_invalid(value)
        for path in ("/abs.py", "../escape.py", "a//b.py", "a\\b.py"):
            value = self.payload()
            value["scope"]["file"] = path
            self.assert_invalid(value)
        for bad_file in ("x" * 1025, "bad\x00.py"):
            value = self.payload()
            value["scope"]["file"] = bad_file
            self.assert_invalid(value)
        exact = self.payload()
        exact["scope"]["file"] = "x" * 1024
        self.assertEqual(validate_identity(exact).payload["scope"]["file"], "x" * 1024)
        missing = self.payload()
        del missing["anchors"]
        self.assert_invalid(missing)
        invalid = self.payload()
        invalid["scope"]["repo"] = "owner--name/repo"
        self.assert_invalid(invalid)
        invalid["scope"]["repo"] = "owner/repo"
        invalid["scope"]["category"] = "other"
        self.assert_invalid(invalid)
        invalid = self.payload()
        invalid["anchors"] = [{"kind": "identifier", "value": "x"}] * 33
        self.assert_invalid(invalid)
        invalid = self.payload()
        invalid["predicates"] = [[{"kind": "identifier", "value": "x"}] * 65]
        self.assert_invalid(invalid)
    def test_secret_free_typed_tokens_only(self):
        for literal in ("github_pat_123456789", "password=supersecret", "raw prose with spaces"):
            value = self.payload()
            value["anchors"] = [{"kind": "identifier", "value": literal}]
            self.assert_invalid(value)
        value = self.payload()
        value["anchors"] = [{"kind": "secret_ref", "value": {"type": "credential", "role": "auth", "position": 0}}]
        record = validate_identity(value).as_record()
        self.assertNotIn("supersecret", json.dumps(record))
    def test_canonical_json_roundtrip_and_digest_tamper(self):
        identity = validate_identity(self.payload())
        record = identity.as_record()
        self.assertEqual(verify_identity_record(record), identity)
        self.assertEqual(canonical_json(identity), canonical_json(verify_identity_record(json.loads(canonical_json(identity)))))
        record["fingerprint_v2"] = "0" * 64
        with self.assertRaises(IdentityError):
            verify_identity_record(record)
        record = identity.as_record()
        record["evidence_digest"] = identity.contract_key
        with self.assertRaises(IdentityError):
            verify_identity_record(record)
