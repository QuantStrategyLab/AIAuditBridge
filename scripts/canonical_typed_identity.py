from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
SCHEMA = "contract_identity.v2"
VERSION = "structured_tokens.v2"
OPS = frozenset({">=", "<=", ">", "<", "==", "!=", "===", "!==", "->", "=>", "::"})
PREDICATE_OPS = frozenset({">=", "<=", ">", "<", "==", "!=", "===", "!=="})
BEHAVIOR_OPS = frozenset({"==", "!=", "->", "=>"})
ORDERING_OPS = frozenset({"->", "=>"})
POLICY = frozenset({"required", "forbidden", "present", "absent", "enabled", "disabled", "valid", "invalid", "missing", "optional"})
KINDS = frozenset({"identifier", "operator", "policy_state", "secret_ref"})
OPERANDS = frozenset({"identifier", "policy_state", "secret_ref"})
CATEGORIES = frozenset({"bug", "contract", "logic", "performance", "reliability", "security"})
SECRET_TYPES = frozenset({"credential", "authorization", "api_key", "session_token", "private_key"})
SECRET_ROLES = frozenset({"auth", "header", "query", "environment", "body"})
OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*(?:\(\))?$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_ITEMS = 32
MAX_BYTES = 64 * 1024
class IdentityError(ValueError):
    pass
@dataclass(frozen=True)
class CanonicalIdentity:
    _payload_json: str
    contract_key: str
    behavior_digest: str
    fingerprint_v2: str
    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)
    def as_record(self) -> dict[str, Any]:
        record = self.payload
        record.update(contract_key=self.contract_key, behavior_digest=self.behavior_digest, fingerprint_v2=self.fingerprint_v2)
        return record
def _obj(value: Any, required: set[str], optional: set[str] = set()) -> dict[str, Any]:
    keys = set(value) if isinstance(value, dict) else set()
    if not isinstance(value, dict) or len(value) > len(required) + len(optional) or not required <= keys or not keys <= required | optional:
        raise IdentityError("invalid object fields")
    return value
def _text(value: Any, limit: int = 512) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise IdentityError("invalid bounded text")
    try:
        raw_size = len(value.encode())
    except UnicodeEncodeError as exc:
        raise IdentityError("invalid unicode text") from exc
    if raw_size > limit:
        raise IdentityError("invalid bounded text")
    value = unicodedata.normalize("NFC", value)
    try:
        normalized_size = len(value.encode())
    except UnicodeEncodeError as exc:
        raise IdentityError("invalid unicode text") from exc
    if any(unicodedata.category(char).startswith("C") for char in value) or normalized_size > limit:
        raise IdentityError("invalid text")
    return value
def _scope(value: Any) -> dict[str, str]:
    scope = _obj(value, {"repo", "file", "category"})
    repo = _text(scope["repo"], 140)
    parts = repo.split("/")
    if len(parts) != 2 or not OWNER.fullmatch(parts[0]) or "--" in parts[0] or not REPO.fullmatch(parts[1]) or parts[1] in {".", ".."}:
        raise IdentityError("invalid repo")
    path = _text(scope["file"], 1024)
    if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise IdentityError("invalid path")
    category = _text(scope["category"], 32)
    if category not in CATEGORIES:
        raise IdentityError("invalid category")
    return {"repo": f"{parts[0].lower()}/{parts[1].lower()}", "file": path, "category": category}
def _token(value: Any) -> dict[str, Any]:
    token = _obj(value, {"kind", "value"})
    kind = _text(token["kind"], 32)
    if kind not in KINDS:
        raise IdentityError("unknown token kind")
    if kind == "secret_ref":
        ref = _obj(token["value"], {"type", "role", "position"})
        typ, role, position = ref["type"], ref["role"], ref["position"]
        if not isinstance(typ, str) or not isinstance(role, str):
            raise IdentityError("invalid secret reference")
        typ, role = _text(typ, 32), _text(role, 32)
        if typ not in SECRET_TYPES or role not in SECRET_ROLES or isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= 1024:
            raise IdentityError("invalid secret reference")
        return {"kind": kind, "value": {"type": typ, "role": role, "position": position}}
    item = _text(token["value"])
    if kind == "operator" and item not in OPS or kind == "policy_state" and item not in POLICY or kind == "identifier" and not IDENTIFIER.fullmatch(item):
        raise IdentityError("invalid typed token")
    return {"kind": kind, "value": item}
def _anchors(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ITEMS:
        raise IdentityError("invalid anchors")
    result = [_token(item) for item in value]
    for index, token in enumerate(result):
        if token["kind"] != ("identifier" if index % 2 == 0 else "operator") or token["kind"] == "operator" and token["value"] != "::":
            raise IdentityError("invalid anchor grammar")
    if result[-1]["kind"] != "identifier":
        raise IdentityError("anchor must end with identifier")
    return result
def _clause(value: Any, operators: frozenset[str], name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ITEMS:
        raise IdentityError(f"invalid {name} clause")
    result = [_token(item) for item in value]
    for index, token in enumerate(result):
        if index % 2 == 0 and token["kind"] not in OPERANDS or index % 2 == 1 and (token["kind"] != "operator" or token["value"] not in operators):
            raise IdentityError(f"invalid {name} grammar")
    if result[-1]["kind"] == "operator":
        raise IdentityError(f"invalid {name} ending")
    secret_indexes = [index for index, token in enumerate(result) if token["kind"] == "secret_ref"]
    if secret_indexes and (secret_indexes != [len(result) - 1] or len(result) < 3 or result[-2]["kind"] != "operator" or result[-2]["value"] not in {"==", "!="}):
        raise IdentityError(f"invalid {name} secret_ref placement")
    return result
def _clauses(value: Any, operators: frozenset[str], name: str, required: bool) -> list[list[dict[str, Any]]]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS or required and not value:
        raise IdentityError(f"invalid {name} clauses")
    return [_clause(clause, operators, name) for clause in value]
def _ordering(value: Any) -> list[list[dict[str, Any]]]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise IdentityError("invalid ordering constraints")
    result = []
    for clause in value:
        if not isinstance(clause, list) or len(clause) != 3:
            raise IdentityError("ordering must be one relation")
        tokens = [_token(item) for item in clause]
        if tokens[0]["kind"] != "identifier" or tokens[1]["kind"] != "operator" or tokens[1]["value"] not in ORDERING_OPS or tokens[2]["kind"] != "identifier":
            raise IdentityError("invalid ordering relation")
        result.append(tokens)
    return result
def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()
def validate_identity(payload: Any) -> CanonicalIdentity:
    fields = {"schema", "canonicalizer_version", "scope", "anchors", "predicates", "required_behavior", "forbidden_behavior", "ordering_constraints"}
    value = _obj(payload, fields)
    if value["schema"] != SCHEMA or value["canonicalizer_version"] != VERSION:
        raise IdentityError("unsupported schema")
    canonical = {"schema": SCHEMA, "canonicalizer_version": VERSION, "scope": _scope(value["scope"]), "anchors": _anchors(value["anchors"]), "predicates": _clauses(value["predicates"], PREDICATE_OPS, "predicate", True), "required_behavior": _clauses(value["required_behavior"], BEHAVIOR_OPS, "required_behavior", True), "forbidden_behavior": _clauses(value["forbidden_behavior"], BEHAVIOR_OPS, "forbidden_behavior", False), "ordering_constraints": _ordering(value["ordering_constraints"])}
    key = _hash({name: canonical[name] for name in ("schema", "canonicalizer_version", "scope", "anchors", "predicates")})
    behavior = _hash({"contract_key": key, "required_behavior": canonical["required_behavior"], "forbidden_behavior": canonical["forbidden_behavior"], "ordering_constraints": canonical["ordering_constraints"]})
    identity = CanonicalIdentity(_json(canonical), key, behavior, _hash({"contract_key": key, "behavior_digest": behavior}))
    if len(_json(identity.as_record()).encode()) > MAX_BYTES:
        raise IdentityError("identity too large")
    return identity
def verify_identity_record(record: Any) -> CanonicalIdentity:
    payload_fields = {"schema", "canonicalizer_version", "scope", "anchors", "predicates", "required_behavior", "forbidden_behavior", "ordering_constraints"}
    digest_fields = {"contract_key", "behavior_digest", "fingerprint_v2"}
    if not isinstance(record, dict) or set(record) != payload_fields | digest_fields or any(not isinstance(record[name], str) or not DIGEST.fullmatch(record[name]) for name in digest_fields):
        raise IdentityError("invalid verified record")
    identity = validate_identity({name: record[name] for name in payload_fields})
    if (identity.contract_key, identity.behavior_digest, identity.fingerprint_v2) != tuple(record[name] for name in ("contract_key", "behavior_digest", "fingerprint_v2")) or identity.as_record() != record:
        raise IdentityError("record mismatch")
    return identity
def canonical_json(identity: CanonicalIdentity) -> str:
    if not isinstance(identity, CanonicalIdentity):
        raise IdentityError("expected identity")
    return _json(identity.as_record())
