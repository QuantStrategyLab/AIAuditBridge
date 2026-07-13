import copy
import json
import unittest
from scripts.canonical_typed_identity import IdentityError, canonical_json, validate_identity, verify_identity_record
class R1cIdentityTests(unittest.TestCase):
    def tok(self, kind, value):
        return {"kind": kind, "value": value}
    def payload(self):
        return {
            "schema": "contract_identity.v2",
            "canonicalizer_version": "structured_tokens.v2",
            "scope": {"repo": "AcMe/Audit-Bridge", "file": "service/review.py", "category": "contract"},
            "anchors": [self.tok("identifier", "Namespace"), self.tok("operator", "::"), self.tok("identifier", "validate()")],
            "predicates": [[self.tok("identifier", "score"), self.tok("operator", ">="), self.tok("identifier", "threshold")]],
            "required_behavior": [[self.tok("policy_state", "required")]],
            "forbidden_behavior": [],
            "ordering_constraints": [],
        }
    def invalid(self, value):
        with self.assertRaises(IdentityError):
            validate_identity(value)
    def test_near_miss_identifiers_are_structural_not_secret_classified(self):
        for name in ("ghs_database", "github_pat_validator", "Eurasia", "keyJson", "secret_manager", "secret_ref_validator", "api_config"):
            value = self.payload()
            value["anchors"] = [self.tok("identifier", name)]
            validate_identity(value)
    def test_secret_ref_is_finite_and_has_no_raw_value(self):
        value = self.payload()
        value["predicates"] = [[self.tok("secret_ref", {"type": "credential", "role": "auth", "position": 0})]]
        record = validate_identity(value).as_record()
        self.assertEqual(record["predicates"][0][0]["value"], {"type": "credential", "role": "auth", "position": 0})
        for ref in ({"type": "ghs_database", "role": "auth", "position": 0}, {"type": "credential", "role": "auth", "position": 0, "raw": "x"}, {"type": "credential", "role": "other", "position": 0}, {"type": "credential", "role": "auth", "position": 1025}):
            bad = self.payload()
            bad["predicates"] = [[self.tok("secret_ref", ref)]]
            self.invalid(bad)
    def test_wire_is_canonical_and_digests_are_exact(self):
        identity = validate_identity(self.payload())
        record = identity.as_record()
        self.assertEqual(verify_identity_record(record), identity)
        self.assertEqual(canonical_json(identity), canonical_json(verify_identity_record(json.loads(canonical_json(identity)))))
        for field in ("contract_key", "behavior_digest", "fingerprint_v2"):
            bad = copy.deepcopy(record)
            bad[field] = "0" * 64
            with self.assertRaises(IdentityError):
                verify_identity_record(bad)
        bad = copy.deepcopy(record)
        bad["scope"]["repo"] = "AcMe/Audit-Bridge"
        with self.assertRaises(IdentityError):
            verify_identity_record(bad)
    def test_unknown_evidence_severity_prose_assignment_and_controls_reject(self):
        for field, value in (("evidence", {}), ("description", "raw prose"), ("severity", "high")):
            bad = self.payload()
            bad[field] = value
            self.invalid(bad)
        for text in ("raw prose", "password=secret", "bad\x00name", "bad\ud800name"):
            bad = self.payload()
            bad["anchors"] = [self.tok("identifier", text)]
            self.invalid(bad)
        bad = self.payload()
        bad["anchors"] = [self.tok("mystery", "x")]
        self.invalid(bad)
    def test_v1_is_not_migrated_and_key_order_is_stable(self):
        old = self.payload()
        old["canonicalizer_version"] = "structured_tokens.v1"
        self.invalid(old)
        value = self.payload()
        reordered = {key: value[key] for key in reversed(tuple(value))}
        self.assertEqual(validate_identity(value).contract_key, validate_identity(reordered).contract_key)
    def test_operators_order_anchor_shape_path_category_and_bounds(self):
        for operator in (">=", "<=", ">", "<", "==", "!=", "===", "!==", "->", "=>", "::"):
            value = self.payload()
            value["predicates"][0][1]["value"] = operator
            validate_identity(value)
        for anchors in ([self.tok("operator", "::")], [self.tok("identifier", "A"), self.tok("operator", "::")], [self.tok("identifier", "A::B")]):
            value = self.payload()
            value["anchors"] = anchors
            self.invalid(value)
        for path in ("/abs.py", "../escape.py", "a//b.py", "a\\b.py"):
            value = self.payload()
            value["scope"]["file"] = path
            self.invalid(value)
        value = self.payload()
        value["scope"]["file"] = "x" * 1025
        self.invalid(value)
if __name__ == "__main__":
    unittest.main()
