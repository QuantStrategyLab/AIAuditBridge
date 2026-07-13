import copy
import json
import unittest
from unittest.mock import patch
from scripts import canonical_typed_identity as r1
from scripts.secret_safe_reviewer_adapter import AdapterError, adapt_reviewer_output
def ident(value):
    return {"kind": "identifier", "value": value}
def op(value):
    return {"kind": "operator", "value": value}
def state(value="required"):
    return {"kind": "policy_state", "value": value}
def secret(material, kind="credential"):
    return {"kind": "secret_ref", "value": {"type": kind, "role": "auth", "position": 0, "material": material}}
def payload(anchor="subject", secret_token=None):
    return {
        "schema": "contract_identity.v2", "canonicalizer_version": "structured_tokens.v2",
        "scope": {"repo": "QuantStrategyLab/AIAuditBridge", "file": "service/Auth.py", "category": "contract"},
        "anchors": [ident(anchor)],
        "predicates": [[ident(anchor), op(">="), ident(anchor)]],
        "required_behavior": [[state()]],
        "forbidden_behavior": [], "ordering_constraints": [],
    } | ({"required_behavior": [[ident(anchor), op("=="), secret_token]]} if secret_token else {})
def finding(tokens=None, description="Review is safe."):
    item = {"severity": "high", "category": "security", "file": "service/Auth.py", "line": 8,
            "description": description, "suggestion": "Apply the bounded fix."}
    if tokens is not None:
        item["structured_tokens"] = tokens
    return item
def review(*findings, schema="reviewer_output.v2", summary="Review complete.", **extra):
    result = {"summary": summary, "findings": list(findings), **extra}
    if schema is not None:
        result["schema"] = schema
    return result
def credential(prefix):
    if prefix == "github_pat_":
        return prefix + "A" * 22 + "_" + "B" * 59
    sizes = {"ghs_": 36, "AKIA": 16, "ASIA": 16, "sk-": 24}
    return prefix + "A" * sizes[prefix] if prefix in sizes else "eyJ" + "A" * 8 + "." + "B" * 8 + "." + "C" * 8
class SecretSafeReviewerAdapterR2Tests(unittest.TestCase):
    def test_trusted_mapping_redacts_display_and_strips_material(self):
        material = credential("ghs_")
        result = adapt_reviewer_output(review(finding(payload(secret_token=secret(material)))))
        self.assertEqual(result["schema"], "secret_safe_reviewer_adapter.v1")
        self.assertEqual(result["status"], "trusted")
        self.assertEqual(len(result["structured_records"]), 1)
        record = result["structured_records"][0]["record"]
        self.assertEqual(record["required_behavior"][0][2]["value"], {"type": "credential", "role": "auth", "position": 0})
        self.assertNotIn("structured_tokens", result["display"]["findings"][0])
        self.assertTrue(material not in json.dumps(result, ensure_ascii=False), "credential material leaked")
    def test_all_shape_classes_map_to_declared_types(self):
        cases = [("ghs_", "credential"), ("github_pat_", "credential"), ("AKIA", "api_key"),
                 ("ASIA", "api_key"), ("jwt", "authorization"), ("sk-", "api_key")]
        for prefix, kind in cases:
            with self.subTest(prefix=prefix):
                material = credential(prefix)
                result = adapt_reviewer_output(review(finding(payload(secret_token=secret(material, kind)))))
                self.assertEqual(result["status"], "trusted")
                self.assertTrue(material not in json.dumps(result), "credential material leaked")
    def test_near_miss_identifiers_remain_identifiers(self):
        for value in ("ghs_database", "github_pat_validator", "Eurasia", "keyJson", "secret_manager", "secret_ref_validator"):
            with self.subTest(value=value):
                result = adapt_reviewer_output(review(finding(payload(anchor=value))))
                self.assertEqual(result["status"], "trusted")
    def test_legacy_is_unverified_and_prose_is_redacted(self):
        material = credential("sk-")
        result = adapt_reviewer_output(review(finding(tokens={}, description=f"Do not print {material}."), schema=None, summary=f"Found {material}."))
        self.assertEqual(result["status"], "legacy_unverified")
        self.assertEqual(result["structured_records"], [])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertTrue(material not in rendered, "credential material leaked")
        self.assertIn("[REDACTED_CREDENTIAL]", rendered)
    def test_fail_closed_codes_and_atomicity(self):
        malformed = [("missing", {"kind": "secret_ref", "value": {"type": "credential", "role": "auth", "position": 0}}, "missing_credential_material"), ("null", None, "invalid_structured_payload"), ("ambiguous", secret("ghs_"), "ambiguous_credential_material"), ("mismatch", secret(credential("AKIA"), "credential"), "credential_type_mismatch")]
        for label, token, code in malformed:
            with self.subTest(label=label):
                candidate = review(finding(payload(secret_token=token)))
                if label == "null":
                    candidate["findings"][0]["structured_tokens"] = None
                with self.assertRaises(AdapterError) as ctx:
                    adapt_reviewer_output(candidate)
                self.assertEqual(ctx.exception.code, code)
        source = review(finding(payload(secret_token=secret(credential("ASIA"), "api_key"))))
        before = copy.deepcopy(source)
        adapt_reviewer_output(source)
        self.assertEqual(source, before)
    def test_identity_screening_and_ambiguous_prose(self):
        material = credential("AKIA")
        bad = review(finding(payload(anchor="subject")))
        bad["findings"][0]["structured_tokens"]["scope"]["file"] = f"src/{material}.py"
        with self.assertRaises(AdapterError) as ctx:
            adapt_reviewer_output(bad)
        self.assertEqual(ctx.exception.code, "credential_material_in_identity")
        with self.assertRaises(AdapterError) as ctx:
            adapt_reviewer_output(review(finding(description="Observed ghs_invalid_marker in output.")))
        self.assertEqual(ctx.exception.code, "ambiguous_credential_material")
    def test_strict_parser_and_exact_r1_delegation(self):
        raw = '{"summary":"ok","summary":"again","findings":[],"schema":"reviewer_output.v2"}'
        with self.assertRaises(AdapterError) as ctx:
            adapt_reviewer_output(raw)
        self.assertEqual(ctx.exception.code, "duplicate_json_key")
        material = credential("ghs_")
        with self.assertRaises(AdapterError) as ctx:
            adapt_reviewer_output(review(finding(), **{f"unexpected_{material}": True}))
        self.assertTrue(ctx.exception.code == "unknown_field" and material not in str(ctx.exception), "unsafe unknown-field error")
        data = review(finding(payload()))
        with patch.object(r1, "build_identity_record", return_value={"canonical": "record"}) as build:
            result = adapt_reviewer_output(data)
        sent = build.call_args.args[0]
        self.assertEqual(result["structured_records"][0]["record"], {"canonical": "record"})
        self.assertFalse(any("material" in json.dumps(value) for value in sent.values()), "raw material crossed R1")
    def test_bounds_and_safe_errors(self):
        with self.assertRaises(AdapterError) as ctx:
            adapt_reviewer_output(review(finding(), summary="x" * 4097))
        self.assertEqual(ctx.exception.code, "size_limit_exceeded")
        with self.assertRaises(AdapterError) as ctx:
            adapt_reviewer_output(b"{\xff")
        self.assertEqual(ctx.exception.code, "invalid_utf8_or_control_character")
        with self.assertRaises(AdapterError) as ctx:
            adapt_reviewer_output(review(finding(description="\ud800")))
        self.assertEqual(ctx.exception.code, "invalid_utf8_or_control_character")
