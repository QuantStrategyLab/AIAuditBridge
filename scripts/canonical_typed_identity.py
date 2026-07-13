"""Pure structured_tokens.v2 validation, canonicalization, and digesting."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

SCHEMA = "contract_identity.v2"
VERSION = "structured_tokens.v2"
MAX_RECORD_BYTES = 65536
PAYLOAD_FIELDS = ("schema", "canonicalizer_version", "scope", "anchors", "predicates", "required_behavior", "forbidden_behavior", "ordering_constraints")
RECORD_FIELDS = set(PAYLOAD_FIELDS) | {"contract_key", "behavior_digest", "fingerprint_v2"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*(?:\(\))?$")
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
PREDICATE_OPERATORS = {">=", "<=", ">", "<", "==", "!=", "===", "!=="}
ORDERING_OPERATORS = {"->", "=>"}
POLICY_STATES = {"required", "forbidden", "present", "absent", "enabled", "disabled", "valid", "invalid", "missing", "optional"}
SECRET_TYPES = {"credential", "authorization", "api_key", "session_token", "private_key"}
SECRET_ROLES = {"auth", "header", "query", "environment", "body"}
CATEGORIES = {"bug", "contract", "logic", "performance", "reliability", "security"}


class IdentityError(ValueError):
    """The typed identity is not valid structured_tokens.v2 data."""


def _fail(message: str) -> None:
    raise IdentityError(message)


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _fail(f"{label} fields are invalid")
    return value


def _text(value: Any, chars: int, size: int, label: str) -> str:
    if type(value) is not str:
        _fail(f"{label} must be a string")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f"{label} is not valid UTF-8")
    if len(value) > chars or len(raw) > size:
        _fail(f"{label} exceeds its limit")
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in normalized):
        _fail(f"{label} contains a control character")
    if len(normalized) > chars or len(normalized.encode("utf-8")) > size:
        _fail(f"{label} exceeds its normalized limit")
    return normalized


def _scope(value: Any) -> dict[str, str]:
    value = _object(value, {"repo", "file", "category"}, "scope")
    repo = _text(value["repo"], 140, 140, "scope.repo")
    if repo.count("/") != 1:
        _fail("scope.repo must be owner/name")
    owner, name = repo.split("/")
    if not OWNER_RE.fullmatch(owner) or "--" in owner or not NAME_RE.fullmatch(name) or name in {".", ".."}:
        _fail("scope.repo is invalid")
    path = _text(value["file"], 1024, 1024, "scope.file")
    if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        _fail("scope.file is invalid")
    category = _text(value["category"], 32, 32, "scope.category")
    if category not in CATEGORIES:
        _fail("scope.category is invalid")
    return {"repo": f"{owner.lower()}/{name.lower()}", "file": path, "category": category}


def _token(value: Any, expected: str, operators: set[str] | None = None) -> dict[str, Any]:
    value = _object(value, {"kind", "value"}, "token")
    if value["kind"] != expected:
        _fail(f"expected {expected} token")
    item = value["value"]
    if expected == "identifier":
        item = _text(item, 512, 512, "identifier")
        if not IDENTIFIER_RE.fullmatch(item):
            _fail("identifier grammar is invalid")
    elif expected == "operator":
        if type(item) is not str or item not in (operators or set()):
            _fail("operator is invalid here")
    elif expected == "policy_state":
        if type(item) is not str or item not in POLICY_STATES:
            _fail("policy_state is invalid")
    elif expected == "secret_ref":
        item = _object(item, {"type", "role", "position"}, "secret_ref")
        if type(item["type"]) is not str or item["type"] not in SECRET_TYPES or type(item["role"]) is not str or item["role"] not in SECRET_ROLES:
            _fail("secret_ref metadata is invalid")
        if type(item["position"]) is not int or not 0 <= item["position"] <= 1024:
            _fail("secret_ref position is invalid")
        item = dict(item)
    return {"kind": expected, "value": item}


def _items(value: Any, minimum: int, label: str) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= 32:
        _fail(f"{label} item count is invalid")
    return value


def _anchors(value: Any) -> list[dict[str, Any]]:
    result = []
    for index, item in enumerate(_items(value, 1, "anchors")):
        result.append(_token(item, "identifier") if index % 2 == 0 else _token(item, "operator", {"::"}))
    if len(result) % 2 == 0:
        _fail("anchors must end with an identifier")
    return result


def _clauses(value: Any, label: str, kind: str, minimum: int) -> list[list[dict[str, Any]]]:
    result = []
    for clause in _items(value, minimum, label):
        if type(clause) is not list:
            _fail(f"{label} clause must be an array")
        if kind == "predicate" and len(clause) == 3:
            parsed = [_token(clause[0], "identifier"), _token(clause[1], "operator", PREDICATE_OPERATORS), _token(clause[2], "identifier")]
        elif kind == "ordering" and len(clause) == 3:
            parsed = [_token(clause[0], "identifier"), _token(clause[1], "operator", ORDERING_OPERATORS), _token(clause[2], "identifier")]
        elif kind == "behavior" and len(clause) == 1:
            parsed = [_token(clause[0], "policy_state")]
        elif kind == "behavior" and len(clause) == 3:
            parsed = [_token(clause[0], "identifier"), _token(clause[1], "operator", {"==", "!="})]
            if type(clause[2]) is not dict or type(clause[2].get("kind")) is not str or clause[2].get("kind") not in {"policy_state", "secret_ref"}:
                _fail(f"{label} final operand is invalid")
            parsed.append(_token(clause[2], clause[2]["kind"]))
        else:
            _fail(f"{label} clause grammar is invalid")
        result.append(parsed)
    return result


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def build_identity_record(payload: Any) -> dict[str, Any]:
    payload = _object(payload, set(PAYLOAD_FIELDS), "payload")
    if payload["schema"] != SCHEMA or payload["canonicalizer_version"] != VERSION:
        _fail("schema or canonicalizer version is unsupported")
    canonical = {
        "schema": SCHEMA, "canonicalizer_version": VERSION, "scope": _scope(payload["scope"]),
        "anchors": _anchors(payload["anchors"]),
        "predicates": _clauses(payload["predicates"], "predicates", "predicate", 1),
        "required_behavior": _clauses(payload["required_behavior"], "required_behavior", "behavior", 1),
        "forbidden_behavior": _clauses(payload["forbidden_behavior"], "forbidden_behavior", "behavior", 0),
        "ordering_constraints": _clauses(payload["ordering_constraints"], "ordering_constraints", "ordering", 0),
    }
    contract = {key: canonical[key] for key in PAYLOAD_FIELDS[:5]}
    record = dict(canonical)
    record["contract_key"] = _digest(contract)
    behavior = {"contract_key": record["contract_key"], **{key: canonical[key] for key in PAYLOAD_FIELDS[5:]}}
    record["behavior_digest"] = _digest(behavior)
    record["fingerprint_v2"] = _digest({"contract_key": record["contract_key"], "behavior_digest": record["behavior_digest"]})
    if len(_json(record)) > MAX_RECORD_BYTES:
        _fail("canonical record exceeds 65536 bytes")
    return record


def verify_identity_record(record: Any) -> dict[str, Any]:
    record = _object(record, RECORD_FIELDS, "record")
    expected = build_identity_record({key: record[key] for key in PAYLOAD_FIELDS})
    if record != expected:
        _fail("record is not the exact canonical record")
    return expected
