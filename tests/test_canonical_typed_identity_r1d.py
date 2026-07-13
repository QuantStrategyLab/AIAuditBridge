import copy
import json
import unittest
from scripts.canonical_typed_identity import IdentityError, canonical_json, validate_identity, verify_identity_record
class R1dGrammarTests(unittest.TestCase):
    def tok(self, kind, value):
        return {"kind": kind, "value": value}
    def secret(self):
        return self.tok("secret_ref", {"type": "credential", "role": "auth", "position": 0})
    def payload(self):
        return {
            "schema": "contract_identity.v2",
            "canonicalizer_version": "structured_tokens.v2",
            "scope": {"repo": "AcMe/Audit-Bridge", "file": "service/review.py", "category": "contract"},
            "anchors": [self.tok("identifier", "Namespace"), self.tok("operator", "::"), self.tok("identifier", "validate()")],
            "predicates": [[self.tok("identifier", "score"), self.tok("operator", ">="), self.tok("identifier", "threshold")]],
            "required_behavior": [[self.tok("policy_state", "required")]],
            "forbidden_behavior": [[self.tok("identifier", "mode"), self.tok("operator", "=="), self.tok("policy_state", "forbidden")]],
            "ordering_constraints": [[self.tok("identifier", "validate"), self.tok("operator", "->"), self.tok("identifier", "persist")]],
        }
    def invalid(self, value):
        with self.assertRaises(IdentityError):
            validate_identity(value)
    def test_valid_field_grammars(self):
        for operator in (">=", "<=", ">", "<", "==", "!=", "===", "!=="):
            value = self.payload()
            value["predicates"][0][1]["value"] = operator
            validate_identity(value)
        for behavior in ([[self.tok("policy_state", "required")]], [[self.tok("identifier", "mode"), self.tok("operator", "=="), self.tok("policy_state", "required")]], [[self.tok("identifier", "token"), self.tok("operator", "=="), self.secret()]]):
            value = self.payload()
            value["required_behavior"] = behavior
            validate_identity(value)
        for operator in ("->", "=>"):
            value = self.payload()
            value["ordering_constraints"][0][1]["value"] = operator
            validate_identity(value)
    def test_predicate_and_behavior_operator_boundaries(self):
        bad_clauses = ([[self.tok("operator", ">=")]], [[self.tok("operator", ">="), self.tok("identifier", "x")]], [[self.tok("identifier", "x"), self.tok("operator", ">=")]], [[self.tok("identifier", "x"), self.tok("operator", "::"), self.tok("identifier", "y")]], [[self.tok("identifier", "x"), self.tok("operator", "=="), self.tok("operator", "!=")]])
        for clause in bad_clauses:
            value = self.payload()
            value["predicates"] = [clause]
            self.invalid(value)
        for clause in ([[self.tok("operator", "==")]], [[self.secret()]], [[self.tok("secret_ref", {"type": "credential", "role": "auth", "position": 0}), self.tok("operator", "=="), self.tok("identifier", "x")]], [[self.tok("identifier", "x"), self.tok("operator", "->"), self.secret()]], [[self.tok("identifier", "x"), self.tok("operator", "=="), self.secret(), self.tok("operator", "=="), self.tok("identifier", "y")]]):
            value = self.payload()
            value["required_behavior"] = [clause]
            self.invalid(value)
    def test_ordering_is_exactly_one_explicit_relation(self):
        for clause in ([[self.tok("operator", "->")]], [[self.tok("identifier", "a"), self.tok("operator", "=="), self.tok("identifier", "b")]], [[self.tok("identifier", "a"), self.tok("operator", "->")]], [[self.tok("identifier", "a"), self.tok("operator", "->"), self.tok("identifier", "b"), self.tok("operator", "->"), self.tok("identifier", "c")]]):
            value = self.payload()
            value["ordering_constraints"] = [clause]
            self.invalid(value)
    def test_near_miss_identifiers_and_strict_input(self):
        for name in ("ghs_database", "github_pat_validator", "Eurasia", "keyJson", "secret_manager", "secret_ref_validator"):
            value = self.payload()
            value["anchors"] = [self.tok("identifier", name)]
            validate_identity(value)
        for field, item in (("canonicalizer_version", "structured_tokens.v1"), ("evidence", {}), ("description", "raw prose"), ("severity", "high")):
            value = self.payload()
            if field == "canonicalizer_version":
                value[field] = item
            else:
                value[field] = item
            self.invalid(value)
        for text in ("raw prose", "password=secret", "bad\x00name", "bad\ud800name"):
            value = self.payload()
            value["anchors"] = [self.tok("identifier", text)]
            self.invalid(value)
        value = self.payload()
        value["anchors"] = [self.tok("mystery", "x")]
        self.invalid(value)
    def test_secret_ref_and_wire_tamper_fail_closed(self):
        for ref in ({"type": "credential", "role": "auth", "position": 0, "raw": "x"}, {"type": "unknown", "role": "auth", "position": 0}, {"type": "credential", "role": "other", "position": 0}, {"type": "credential", "role": "auth", "position": 1025}):
            value = self.payload()
            value["required_behavior"] = [[self.tok("identifier", "token"), self.tok("operator", "=="), self.tok("secret_ref", ref)]]
            self.invalid(value)
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
if __name__ == "__main__":
    unittest.main()
