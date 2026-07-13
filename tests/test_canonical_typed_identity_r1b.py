import copy
import unittest
from scripts.canonical_typed_identity import IdentityError, validate_identity, verify_identity_record
class R1bIdentityTests(unittest.TestCase):
    def tok(self, kind, value):
        return {"kind": kind, "value": value}
    def payload(self, severity="high"):
        return {
            "schema": "contract_identity.v2",
            "canonicalizer_version": "structured_tokens.v1",
            "scope": {"repo": "AcMe/Audit-Bridge", "file": "service/review.py", "category": "contract"},
            "anchors": [self.tok("identifier", "Namespace"), self.tok("operator", "::"), self.tok("identifier", "validate()")],
            "predicates": [[self.tok("identifier", "score"), self.tok("operator", ">="), self.tok("identifier", "threshold")]],
            "required_behavior": [[self.tok("policy_state", "required")]],
            "forbidden_behavior": [],
            "ordering_constraints": [],
            "severity": severity,
        }
    def invalid(self, value):
        with self.assertRaises(IdentityError):
            validate_identity(value)
    def test_reserved_markers_anywhere_and_safe_identifiers(self):
        for safe in ("secret_manager", "secret_ref_validator", "Eurasia", "keyJson", "api_config", "key_json_v2"):
            value = self.payload()
            value["anchors"] = [self.tok("identifier", safe)]
            validate_identity(value)
        for marker in (
            "ghs_1234567890abcdef1234567890abcdef1234",
            "env.ghs_1234567890abcdef1234567890abcdef1234",
            "ASIAABCDEFGHIJKLMNOP",
            "key.ASIAABCDEFGHIJKLMNOP",
            "api.sk-proj-1234567890abcdef",
            "env.eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "api_ghs_1234567890abcdef1234567890abcdef1234",
            "ghs_1234567890abcdef1234567890abcdef1234_suffix",
            "api_ASIAABCDEFGHIJKLMNOP_suffix",
            "api_sk-proj-1234567890abcdef_suffix",
            "api_eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature_suffix",
        ):
            value = self.payload()
            value["anchors"] = [self.tok("identifier", marker)]
            self.invalid(value)
    def test_anchor_grammar_requires_explicit_namespace_tokens(self):
        for anchors in (
            [self.tok("operator", "::")],
            [self.tok("identifier", "A"), self.tok("operator", "::")],
            [self.tok("identifier", "A"), self.tok("operator", "::"), self.tok("operator", "::"), self.tok("identifier", "B")],
            [self.tok("identifier", "A"), self.tok("operator", "=>"), self.tok("identifier", "B")],
            [self.tok("identifier", "A::B")],
        ):
            value = self.payload()
            value["anchors"] = anchors
            self.invalid(value)
    def test_severity_is_not_verified_metadata(self):
        identity = validate_identity(self.payload())
        critical = validate_identity(self.payload("critical"))
        self.assertEqual((identity.contract_key, identity.behavior_digest, identity.fingerprint_v2), (critical.contract_key, critical.behavior_digest, critical.fingerprint_v2))
        record = identity.as_record()
        self.assertNotIn("severity", record)
        self.assertEqual(verify_identity_record(record), identity)
        for severity in (None, "critical"):
            tampered = copy.deepcopy(record)
            tampered["severity"] = severity
            with self.assertRaises(IdentityError):
                verify_identity_record(tampered)
    def test_repo_is_lowercase_but_file_and_identifier_case_remain(self):
        identity = validate_identity(self.payload())
        self.assertEqual(identity.payload["scope"]["repo"], "acme/audit-bridge")
        self.assertEqual(identity.payload["scope"]["file"], "service/review.py")
        self.assertEqual(identity.payload["anchors"][0]["value"], "Namespace")
        lower = copy.deepcopy(self.payload())
        lower["scope"]["repo"] = "acme/audit-bridge"
        self.assertEqual(identity.contract_key, validate_identity(lower).contract_key)
    def test_unknown_evidence_secret_ref_and_digest_tamper_fail_closed(self):
        value = self.payload()
        value["predicates"] = [[self.tok("secret_ref", {"type": "credential", "role": "auth", "position": 0})]]
        record = validate_identity(value).as_record()
        self.assertNotIn("secret", record["predicates"][0][0]["value"])
        for field in ("evidence", "unknown"):
            invalid = self.payload()
            invalid[field] = {}
            self.invalid(invalid)
        record["contract_key"] = "0" * 64
        with self.assertRaises(IdentityError):
            verify_identity_record(record)
    def test_surrogates_fail_closed_as_identity_error(self):
        value = self.payload()
        value["scope"]["file"] = "bad\ud800.py"
        self.invalid(value)
    def test_verified_record_must_be_canonical_wire_form(self):
        record = validate_identity(self.payload()).as_record()
        record["scope"]["repo"] = "AcMe/Audit-Bridge"
        with self.assertRaises(IdentityError):
            verify_identity_record(record)
    def test_secret_ref_metadata_rejects_reserved_markers(self):
        for marker in ("ghs_1234567890abcdef", "ASIAABCDEFGHIJKLMNOP", "api.sk-proj-1234567890abcdef"):
            value = self.payload()
            value["predicates"] = [[self.tok("secret_ref", {"type": marker, "role": "auth", "position": 0})]]
            self.invalid(value)
    def test_scope_rejects_secret_markers_but_allows_ordinary_names(self):
        for repo, path in (("owner/ghs_1234567890abcdef", "service/review.py"), ("owner/ASIAABCDEFGHIJKLMNOP", "service/review.py"), ("owner/audit-bridge", "service/ghs_1234567890abcdef.py")):
            value = self.payload()
            value["scope"]["repo"], value["scope"]["file"] = repo, path
            self.invalid(value)
        value = self.payload()
        value["scope"]["repo"], value["scope"]["file"] = "owner/Eurasia", "service/keyJson.py"
        validate_identity(value)
