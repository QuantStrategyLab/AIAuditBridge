from __future__ import annotations
import json
import math
import re
import unicodedata
from typing import Any
from scripts import canonical_typed_identity as r1
SCHEMA = "secret_safe_reviewer_adapter.v1"
MAX_INPUT = 262144
MAX_FINDINGS = 32
MAX_RECORDS = 1048576
R1_FIELDS = {"schema", "canonicalizer_version", "scope", "anchors", "predicates", "required_behavior", "forbidden_behavior", "ordering_constraints"}
ROOT_FIELDS = {"schema", "summary", "findings"}
FINDING_FIELDS = {"severity", "category", "file", "line", "description", "suggestion", "structured_tokens"}
SHAPES = (
    ("credential", re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}")),
    ("credential", re.compile(r"github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}")),
    ("api_key", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("api_key", re.compile(r"ASIA[A-Z0-9]{16}")),
    ("authorization", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("api_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
)
PROSE_SHAPES = tuple(pattern for _, pattern in SHAPES)
MARKER = re.compile(r"(?<![A-Za-z0-9_-])(?:ghs_|github_pat_|AKIA|ASIA|eyJ|sk-)[A-Za-z0-9_.-]*(?![A-Za-z0-9_-])")
_MISSING = object()
class AdapterError(ValueError):
    def __init__(self, code: str, path: str = "/") -> None:
        self.code, self.path = code, path
        super().__init__(code, path)
    def __str__(self) -> str:
        return f"{self.code} at {self.path}"
class AdapterResult(dict[str, Any]):
    pass
def _fail(code: str, path: str = "/") -> None:
    raise AdapterError(code, path)
def _pointer(path: str, key: Any) -> str:
    value = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path.rstrip('/')}/{value}" if path != "/" else f"/{value}"
def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise RuntimeError("duplicate")
        result[key] = value
    return result
def _scan(value: Any, depth: int, path: str) -> None:
    if depth > 10:
        _fail("depth_limit_exceeded", path)
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                _fail("wrong_type", path)
            _scan(child, depth + 1, _pointer(path, key))
    elif type(value) is list:
        for index, child in enumerate(value):
            _scan(child, depth + 1, _pointer(path, index))
    elif type(value) is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            _fail("invalid_utf8_or_control_character", path)
        if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value) or len(encoded) > MAX_INPUT:
            _fail("invalid_utf8_or_control_character", path)
    elif type(value) is float and not math.isfinite(value):
        _fail("invalid_json", path)
    elif value is not None and type(value) not in {bool, int, float}:
        _fail("wrong_type", path)
def _compact(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        _fail("invalid_json")
def _load(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        if len(value) > MAX_INPUT:
            _fail("size_limit_exceeded")
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            _fail("invalid_utf8_or_control_character")
        try:
            value = json.loads(text, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except RuntimeError:
            _fail("duplicate_json_key")
        except (TypeError, ValueError, json.JSONDecodeError):
            _fail("invalid_json")
    elif isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError:
            _fail("invalid_utf8_or_control_character")
        if len(raw) > MAX_INPUT:
            _fail("size_limit_exceeded")
        try:
            value = json.loads(value, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except RuntimeError:
            _fail("duplicate_json_key")
        except (TypeError, ValueError, json.JSONDecodeError):
            _fail("invalid_json")
    _scan(value, 0, "/")
    if type(value) is not dict:
        _fail("wrong_type", "/")
    if len(_compact(value)) > MAX_INPUT:
        _fail("size_limit_exceeded")
    return value
def _object(value: Any, fields: set[str], path: str, code: str = "wrong_type") -> dict[str, Any]:
    if type(value) is not dict:
        _fail(code, path)
    extra = set(value) - fields
    if extra:
        _fail("unknown_field", _pointer(path, "<unknown>"))
    if set(value) != fields:
        _fail(code, path)
    return value
def _bounded(value: Any, path: str, chars: int, bytes_limit: int, code: str) -> str:
    if type(value) is not str:
        _fail(code, path)
    encoded = value.encode("utf-8")
    if len(value) > chars or len(encoded) > bytes_limit:
        _fail("size_limit_exceeded", path)
    return value
def _display_text(value: Any, path: str, chars: int, byte_limit: int) -> str:
    text = _bounded(value, path, chars, byte_limit, "invalid_display_field")
    for pattern in PROSE_SHAPES:
        text = pattern.sub("[REDACTED_CREDENTIAL]", text)
    if MARKER.search(text):
        _fail("ambiguous_credential_material", path)
    return text
def _credential_in(value: str) -> bool:
    return any(pattern.search(value) for pattern in PROSE_SHAPES)
def _map_node(value: Any, path: str) -> Any:
    if type(value) is dict:
        if set(value) == {"kind", "value"} and value.get("kind") == "secret_ref":
            return _map_secret(value, path)
        if set(value) == {"kind", "value"} and value.get("kind") == "identifier" and type(value.get("value")) is str and _credential_in(value["value"]):
            _fail("credential_material_in_identifier", _pointer(path, "value"))
        return {key: _map_node(child, _pointer(path, key)) for key, child in value.items()}
    if type(value) is list:
        return [_map_node(child, _pointer(path, index)) for index, child in enumerate(value)]
    if type(value) is str and _credential_in(value):
        _fail("credential_material_in_identity", path)
    return value
def _map_secret(token: dict[str, Any], path: str) -> dict[str, Any]:
    value = token["value"]
    if type(value) is not dict:
        _fail("invalid_structured_payload", _pointer(path, "value"))
    if "material" not in value:
        _fail("missing_credential_material", _pointer(_pointer(path, "value"), "material"))
    if set(value) != {"type", "role", "position", "material"}:
        _fail("invalid_token_mapping", _pointer(path, "value"))
    material = value["material"]
    if type(material) is not str or material != material.strip():
        _fail("ambiguous_credential_material", _pointer(_pointer(path, "value"), "material"))
    derived = next((kind for kind, pattern in SHAPES if pattern.fullmatch(material)), None)
    if derived is None:
        _fail("ambiguous_credential_material", _pointer(_pointer(path, "value"), "material"))
    if type(value["type"]) is not str or value["type"] != derived:
        _fail("credential_type_mismatch", _pointer(_pointer(path, "value"), "type"))
    if type(value["role"]) is not str or value["role"] not in r1.SECRET_ROLES or type(value["position"]) is not int or type(value["position"]) is bool or not 0 <= value["position"] <= 1024:
        _fail("invalid_token_mapping", _pointer(path, "value"))
    return {"kind": "secret_ref", "value": {"type": value["type"], "role": value["role"], "position": value["position"]}}
def _structured(value: Any, path: str) -> dict[str, Any]:
    payload = _object(value, R1_FIELDS, path, "invalid_structured_payload")
    if payload["schema"] != r1.SCHEMA or payload["canonicalizer_version"] != r1.VERSION:
        _fail("invalid_structured_payload", path)
    try:
        mapped = _map_node(payload, path)
        return r1.build_identity_record(mapped)
    except r1.IdentityError:
        _fail("r1_identity_rejected", path)
def _display(root: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    extra = set(root) - ROOT_FIELDS
    if extra:
        _fail("unknown_field", _pointer("/", "<unknown>"))
    for required in ("summary", "findings"):
        if required not in root:
            _fail("wrong_type", _pointer("/", required))
    schema = root.get("schema") if "schema" in root else None
    if "schema" in root and type(schema) is not str:
        _fail("wrong_type", "/schema")
    if schema not in {None, "reviewer_output.v1", "reviewer_output.v2"}:
        _fail("unsupported_schema", "/schema")
    display = {"summary": _display_text(root["summary"], "/summary", 4096, 16384), "findings": []}
    findings = root["findings"]
    if type(findings) is not list:
        _fail("wrong_type", "/findings")
    if len(findings) > MAX_FINDINGS:
        _fail("size_limit_exceeded", "/findings")
    present: list[Any] = []
    for index, finding in enumerate(findings):
        path = f"/findings/{index}"
        if type(finding) is not dict:
            _fail("wrong_type", path)
        extra = set(finding) - FINDING_FIELDS
        if extra:
            _fail("unknown_field", _pointer(path, "<unknown>"))
        required = {"severity", "category", "file", "line", "description", "suggestion"}
        if not required <= set(finding):
            _fail("wrong_type", path)
        if type(finding["severity"]) is not str or finding["severity"] not in {"critical", "high", "medium", "low"}:
            _fail("invalid_display_field", _pointer(path, "severity"))
        if type(finding["category"]) is not str or finding["category"] not in r1.CATEGORIES:
            _fail("invalid_display_field", _pointer(path, "category"))
        line = finding["line"]
        if type(line) is not int or type(line) is bool or not 1 <= line <= 1000000:
            _fail("invalid_display_field", _pointer(path, "line"))
        shown = {"severity": finding["severity"], "category": finding["category"], "file": _display_text(finding["file"], f"{path}/file", 300, 300), "line": line,
                 "description": _display_text(finding["description"], f"{path}/description", 8192, 32768),
                 "suggestion": _display_text(finding["suggestion"], f"{path}/suggestion", 8192, 32768)}
        display["findings"].append(shown)
        present.append(finding["structured_tokens"] if "structured_tokens" in finding else _MISSING)
    return display, present, schema
def adapt_reviewer_output(value: Any) -> AdapterResult:
    root = _load(value)
    display, payloads, schema = _display(root)
    trusted = schema == "reviewer_output.v2" and all(payload is not _MISSING for payload in payloads)
    records = []
    if trusted:
        for index, payload in enumerate(payloads):
            if type(payload) is not dict:
                _fail("invalid_structured_payload", f"/findings/{index}/structured_tokens")
            records.append({"finding_index": index, "record": _structured(payload, f"/findings/{index}/structured_tokens")})
    if not trusted:
        records = []
    if len(_compact(records)) > MAX_RECORDS:
        _fail("output_size_limit_exceeded", "/structured_records")
    return AdapterResult({"schema": SCHEMA, "status": "trusted" if trusted else "legacy_unverified", "display": display, "structured_records": records})
