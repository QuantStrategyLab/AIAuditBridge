import copy
import hashlib
import json
import unittest

from scripts.canonical_typed_identity import IdentityError, build_identity_record, verify_identity_record


def token(kind, value=None):
    values = {"identifier": "subject", "policy_state": "required", "secret_ref": {"type": "credential", "role": "auth", "position": 0}}
    return {"kind": kind, "value": values.get(kind) if value is None else value}


def clause(parts):
    return [token(part) if part in {"identifier", "policy_state", "secret_ref"} else token("operator", part) for part in parts]


def payload():
    return {
        "schema": "contract_identity.v2",
        "canonicalizer_version": "structured_tokens.v2",
        "scope": {"repo": "QuantStrategyLab/AIAuditBridge", "file": "service/Auth.py", "category": "contract"},
        "anchors": [token("identifier", "Auth")],
        "predicates": [clause(["identifier", ">=", "identifier"])],
        "required_behavior": [clause(["policy_state"])],
        "forbidden_behavior": [],
        "ordering_constraints": [],
    }


def modified(path, value):
    result = payload()
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


CORPUS = [
    ("near_miss_ghs_database", "anchor", {"kind": "identifier", "value": "ghs_database"}, True),
    ("near_miss_github_pat_validator", "anchor", {"kind": "identifier", "value": "github_pat_validator"}, True),
    ("near_miss_ordinary_identifiers", "anchors", ["Eurasia", "keyJson", "secret_manager", "secret_ref_validator"], True),
    ("valid_predicate", "predicates", ["identifier", ">=", "identifier"], True),
    ("valid_policy_behavior", "required_behavior", ["policy_state"], True),
    ("valid_secret_final_behavior", "required_behavior", ["identifier", "==", "secret_ref"], True),
    ("valid_ordering", "ordering_constraints", ["identifier", "->", "identifier"], True),
    ("reject_predicate_policy_state", "predicates", ["policy_state", "==", "identifier"], False),
    ("reject_predicate_namespace", "predicates", ["identifier", "::", "identifier"], False),
    ("reject_trailing_operator", "predicates", ["identifier", ">="], False),
    ("reject_secret_lhs", "required_behavior", ["secret_ref", "==", "identifier"], False),
    ("reject_secret_middle", "required_behavior", ["identifier", "==", "secret_ref", "==", "identifier"], False),
    ("reject_ordering_comparison", "ordering_constraints", ["identifier", "==", "identifier"], False),
    ("reject_unknown_field", "top", {"evidence": {}}, False),
    ("reject_prose", "anchor", {"kind": "identifier", "value": "raw prose"}, False),
    ("reject_assignment", "anchor", {"kind": "identifier", "value": "password=secret"}, False),
    ("reject_control", "anchor", {"kind": "identifier", "value": "bad\\u0000name"}, False),
    ("reject_v1", "top", {"canonicalizer_version": "structured_tokens.v1"}, False),
]


class CanonicalTypedIdentityR1dTests(unittest.TestCase):
    def test_corpus_and_oracle(self):
        for case_id, target, value, accepted in CORPUS:
            with self.subTest(case_id=case_id):
                candidate = payload()
                if target == "anchor":
                    candidate["anchors"] = [value]
                elif target == "anchors":
                    for identifier in value:
                        candidate["anchors"] = [token("identifier", identifier)]
                        build_identity_record(candidate)
                    continue
                elif target == "top":
                    candidate.update(value)
                else:
                    candidate[target] = [clause(value)]
                if accepted:
                    build_identity_record(candidate)
                else:
                    with self.assertRaises(IdentityError):
                        build_identity_record(candidate)

    def test_canonicalization_digests_and_exact_verification(self):
        source = payload()
        source["scope"]["file"] = "Cafe\u0301.py"
        record = build_identity_record(source)
        self.assertEqual(record["scope"]["repo"], "quantstrategylab/aiauditbridge")
        self.assertEqual(record["scope"]["file"], "Caf\u00e9.py")
        contract = {key: record[key] for key in ("schema", "canonicalizer_version", "scope", "anchors", "predicates")}
        self.assertEqual(record["contract_key"], digest(contract))
        behavior = {"contract_key": record["contract_key"], **{key: record[key] for key in ("required_behavior", "forbidden_behavior", "ordering_constraints")}}
        self.assertEqual(record["behavior_digest"], digest(behavior))
        self.assertEqual(record["fingerprint_v2"], digest({"contract_key": record["contract_key"], "behavior_digest": record["behavior_digest"]}))
        self.assertEqual(verify_identity_record(record), record)
        for key in ("contract_key", "behavior_digest", "fingerprint_v2"):
            tampered = copy.deepcopy(record)
            tampered[key] = "0" * 64
            with self.subTest(key=key), self.assertRaises(IdentityError):
                verify_identity_record(tampered)

    def test_strict_schema_unicode_bounds_and_secret_metadata(self):
        invalid = [
            *[modified(("scope", "repo"), value) for value in ("owner", "-owner/name", "owner--x/name", "owner/..")],
            *[modified(("scope", "file"), value) for value in ("/absolute.py", "a\\b.py", "a/../b.py", "a//b.py")],
            modified(("anchors",), [token("identifier", "bad\x00name")]),
            modified(("anchors",), [token("identifier", "a" * 513)]),
            modified(("required_behavior",), [[token("identifier"), token("operator", "=="), {"kind": "secret_ref", "value": {"type": "credential", "role": "auth", "position": 0, "raw": "forbidden"}}]]),
            modified(("required_behavior",), [[token("identifier"), token("operator", "=="), token("secret_ref", {"type": "credential", "role": "auth", "position": True})]]),
            modified(("anchors",), [token("identifier", "a"), token("identifier", "b")]),
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(IdentityError):
                build_identity_record(candidate)

        canonical = modified(("anchors",), [token("identifier", "Cafe\u0301")])
        canonical["scope"]["repo"] = "Owner/Name"
        with self.assertRaises(IdentityError):
            build_identity_record(canonical)


if __name__ == "__main__":
    unittest.main()
