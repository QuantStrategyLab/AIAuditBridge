from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
SCHEMA = "contract_identity.v2"
VERSION = "structured_tokens.v1"
OPS = frozenset({">=", "<=", ">", "<", "==", "!=", "===", "!==", "->", "=>", "::"})
POLICY = frozenset({"required", "forbidden", "present", "absent", "enabled", "disabled", "valid", "invalid", "missing", "optional"})
KINDS = frozenset({"identifier", "operator", "policy_state", "secret_ref"})
CATEGORIES = frozenset({"bug", "contract", "logic", "performance", "reliability", "security"})
SEVERITIES = frozenset({"critical", "high", "medium", "low"})
OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*(?:\(\))?$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])(?:github_pat_|gh[pours]_)[A-Za-z0-9_]{8,}", re.I),
    re.compile(r"(?<![A-Za-z0-9_])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_])"),
)
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
        result = self.payload
        result.update(contract_key=self.contract_key, behavior_digest=self.behavior_digest, fingerprint_v2=self.fingerprint_v2)
        return result
def _obj(value: Any, required: set[str], optional: set[str] = set()) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > len(required) + len(optional) or not required <= set(value) or not set(value) <= required | optional:
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
        typ, role, position = _text(ref["type"], 32), _text(ref["role"], 32), ref["position"]
        if any(pattern.search(candidate) for pattern in SECRET_PATTERNS for candidate in (typ, role)):
            raise IdentityError("reserved secret marker")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}", typ) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}", role) or isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= 1024:
            raise IdentityError("invalid secret reference")
        return {"kind": kind, "value": {"type": typ, "role": role, "position": position}}
    item = _text(token["value"])
    if any(pattern.search(item) for pattern in SECRET_PATTERNS):
        raise IdentityError("reserved secret marker")
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
def _clauses(value: Any, required: bool) -> list[list[dict[str, Any]]]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS or required and not value:
        raise IdentityError("invalid clauses")
    result = []
    for clause in value:
        if not isinstance(clause, list) or not clause or len(clause) > MAX_ITEMS:
            raise IdentityError("invalid clause")
        result.append([_token(item) for item in clause])
    return result
def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()
def validate_identity(payload: Any) -> CanonicalIdentity:
    fields = _obj(payload, {"schema", "canonicalizer_version", "scope", "anchors", "predicates", "required_behavior", "forbidden_behavior", "ordering_constraints"}, {"severity"})
    if fields["schema"] != SCHEMA or fields["canonicalizer_version"] != VERSION:
        raise IdentityError("unsupported schema")
    if "severity" in fields and _text(fields["severity"], 16) not in SEVERITIES:
        raise IdentityError("invalid severity")
    canonical = {"schema": SCHEMA, "canonicalizer_version": VERSION, "scope": _scope(fields["scope"]), "anchors": _anchors(fields["anchors"]), "predicates": _clauses(fields["predicates"], True), "required_behavior": _clauses(fields["required_behavior"], True), "forbidden_behavior": _clauses(fields["forbidden_behavior"], False), "ordering_constraints": _clauses(fields["ordering_constraints"], False)}
    key = _hash({name: canonical[name] for name in ("schema", "canonicalizer_version", "scope", "anchors", "predicates")})
    behavior = _hash({"contract_key": key, "required_behavior": canonical["required_behavior"], "forbidden_behavior": canonical["forbidden_behavior"], "ordering_constraints": canonical["ordering_constraints"]})
    identity = CanonicalIdentity(_json(canonical), key, behavior, _hash({"contract_key": key, "behavior_digest": behavior}))
    if len(_json(identity.as_record()).encode()) > MAX_BYTES:
        raise IdentityError("identity too large")
    return identity
def verify_identity_record(record: Any) -> CanonicalIdentity:
    fields = {"schema", "canonicalizer_version", "scope", "anchors", "predicates", "required_behavior", "forbidden_behavior", "ordering_constraints", "contract_key", "behavior_digest", "fingerprint_v2"}
    if not isinstance(record, dict) or set(record) != fields or any(not isinstance(record[name], str) or not DIGEST.fullmatch(record[name]) for name in fields - {"schema", "canonicalizer_version", "scope", "anchors", "predicates", "required_behavior", "forbidden_behavior", "ordering_constraints"}):
        raise IdentityError("invalid verified record")
    identity = validate_identity({name: record[name] for name in fields - {"contract_key", "behavior_digest", "fingerprint_v2"}})
    if (identity.contract_key, identity.behavior_digest, identity.fingerprint_v2) != tuple(record[name] for name in ("contract_key", "behavior_digest", "fingerprint_v2")):
        raise IdentityError("digest mismatch")
    if identity.as_record() != record:
        raise IdentityError("noncanonical record")
    return identity
def canonical_json(identity: CanonicalIdentity) -> str:
    if not isinstance(identity, CanonicalIdentity):
        raise IdentityError("expected identity")
    return _json(identity.as_record())
