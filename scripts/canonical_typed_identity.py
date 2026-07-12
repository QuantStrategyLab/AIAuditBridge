"""Pure, strict canonical identity model for structured review contracts."""
from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

SCHEMA = "contract_identity.v2"
CANONICALIZER_VERSION = "structured_tokens.v1"
_OPERATORS = frozenset({">=", "<=", ">", "<", "==", "!=", "===", "!==", "->", "=>", "::"})
_POLICY_STATES = frozenset({"required", "forbidden", "present", "absent", "enabled", "disabled", "valid", "invalid", "missing", "optional"})
_TOKEN_KINDS = frozenset({"identifier", "operator", "policy_state", "secret_ref"})
_CATEGORIES = frozenset({"bug", "contract", "logic", "performance", "reliability", "security"})
_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*(?:::[A-Za-z_][A-Za-z0-9_.-]*)*(?:\(\))?$")
_SAFE_REF = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TEXT_BYTES = 512
_PATH_BYTES = 1024
_MAX_ITEMS = 32
_MAX_TOKENS = 64
_MAX_CANONICAL_BYTES = 64 * 1024

class IdentityError(ValueError):
    """Invalid canonical identity."""

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

def _fields(value: Any, required: set[str], optional: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > len(required) + len(optional):
        raise IdentityError(f"{name} must be a bounded object")
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise IdentityError(f"{name} has invalid fields")
    return value
def _text(value: Any, name: str, limit: int = _TEXT_BYTES) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise IdentityError(f"{name} is not bounded text")
    raw = value.encode("utf-8")
    if len(raw) > limit:
        raise IdentityError(f"{name} is too large")
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise IdentityError(f"{name} contains a control character")
    if len(normalized.encode("utf-8")) > limit:
        raise IdentityError(f"{name} is too large after normalization")
    return normalized

def _scope(value: Any) -> dict[str, str]:
    scope = _fields(value, {"repo", "file", "category"}, set(), "scope")
    repo = _text(scope["repo"], "scope.repo", 140)
    parts = repo.split("/")
    if len(parts) != 2 or not _OWNER.fullmatch(parts[0]) or "--" in parts[0] or not _REPO.fullmatch(parts[1]) or parts[1] in {".", ".."}:
        raise IdentityError("scope.repo is not owner/name")
    path = _text(scope["file"], "scope.file", _PATH_BYTES)
    if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise IdentityError("scope.file is not a relative POSIX path")
    category = _text(scope["category"], "scope.category", 32)
    if category not in _CATEGORIES:
        raise IdentityError("scope.category is not controlled")
    return {"repo": repo, "file": path, "category": category}
def _reject_secret_text(value: str) -> None:
    lowered = value.lower()
    if lowered.startswith(("github_pat_", "ghp_", "aws_secret_access_key", "akia", "sk-", "eyj")):
        raise IdentityError("raw secret-like text is forbidden")

def _token(value: Any, name: str) -> dict[str, Any]:
    token = _fields(value, {"kind", "value"}, set(), name)
    kind = _text(token["kind"], f"{name}.kind", 32)
    if kind not in _TOKEN_KINDS:
        raise IdentityError(f"{name}.kind is unknown")
    if kind == "secret_ref":
        ref = _fields(token["value"], {"type", "role", "position"}, set(), f"{name}.value")
        ref_type = _text(ref["type"], f"{name}.value.type", 32)
        role = _text(ref["role"], f"{name}.value.role", 32)
        position = ref["position"]
        if not _SAFE_REF.fullmatch(ref_type) or not _SAFE_REF.fullmatch(role) or isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= 1024:
            raise IdentityError(f"{name}.value is not a safe secret reference")
        return {"kind": kind, "value": {"type": ref_type, "role": role, "position": position}}
    item = _text(token["value"], f"{name}.value")
    _reject_secret_text(item)
    if kind == "operator":
        if item not in _OPERATORS:
            raise IdentityError(f"{name} has unknown operator")
    elif kind == "policy_state":
        if item not in _POLICY_STATES:
            raise IdentityError(f"{name} has unknown policy state")
    elif not _IDENTIFIER.fullmatch(item):
        raise IdentityError(f"{name} is not a typed identifier")
    return {"kind": kind, "value": item}
def _tokens(value: Any, name: str, *, anchors: bool = False, required: bool = True) -> list[Any]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise IdentityError(f"{name} is not a bounded list")
    if required and not value:
        raise IdentityError(f"{name} must not be empty")
    result = []
    for index, item in enumerate(value):
        token = _token(item, f"{name}[{index}]")
        if anchors and token["kind"] != "identifier":
            raise IdentityError("anchors contain a non-anchor token")
        result.append(token)
    return result

def _clauses(value: Any, name: str, *, required: bool) -> list[list[dict[str, Any]]]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise IdentityError(f"{name} is not a bounded clause list")
    if required and not value:
        raise IdentityError(f"{name} must not be empty")
    result = []
    for index, clause in enumerate(value):
        if not isinstance(clause, list) or len(clause) > _MAX_TOKENS:
            raise IdentityError(f"{name}[{index}] is not a bounded token list")
        if not clause:
            raise IdentityError(f"{name}[{index}] must not be empty")
        result.append([_token(item, f"{name}[{index}][{token_index}]") for token_index, item in enumerate(clause)])
    return result
def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()

def validate_identity(payload: Any) -> CanonicalIdentity:
    fields = _fields(
        payload,
        {"schema", "canonicalizer_version", "scope", "anchors", "predicates", "required_behavior", "forbidden_behavior", "ordering_constraints"},
        {"severity"},
        "identity",
    )
    if fields["schema"] != SCHEMA or fields["canonicalizer_version"] != CANONICALIZER_VERSION:
        raise IdentityError("unsupported identity schema")
    scope = _scope(fields["scope"])
    anchors = _tokens(fields["anchors"], "anchors", anchors=True)
    predicates = _clauses(fields["predicates"], "predicates", required=True)
    required_behavior = _clauses(fields["required_behavior"], "required_behavior", required=True)
    forbidden_behavior = _clauses(fields["forbidden_behavior"], "forbidden_behavior", required=False)
    ordering = _clauses(fields["ordering_constraints"], "ordering_constraints", required=False)
    severity = fields.get("severity")
    if severity is not None and _text(severity, "severity", 16) not in _SEVERITIES:
        raise IdentityError("severity is not controlled")
    canonical = {
        "schema": SCHEMA,
        "canonicalizer_version": CANONICALIZER_VERSION,
        "scope": scope,
        "anchors": anchors,
        "predicates": predicates,
        "required_behavior": required_behavior,
        "forbidden_behavior": forbidden_behavior,
        "ordering_constraints": ordering,
    }
    if severity is not None:
        canonical["severity"] = severity
    contract_key = _hash({k: canonical[k] for k in ("schema", "canonicalizer_version", "scope", "anchors", "predicates")})
    behavior_digest = _hash({"contract_key": contract_key, "required_behavior": required_behavior, "forbidden_behavior": forbidden_behavior, "ordering_constraints": ordering})
    fingerprint = _hash({"contract_key": contract_key, "behavior_digest": behavior_digest})
    identity = CanonicalIdentity(_json(canonical), contract_key, behavior_digest, fingerprint)
    if len(_json(identity.as_record()).encode("utf-8")) > _MAX_CANONICAL_BYTES:
        raise IdentityError("identity is too large")
    return identity

def verify_identity_record(record: Any) -> CanonicalIdentity:
    if not isinstance(record, dict) or len(record) > 12:
        raise IdentityError("record must be an object")
    expected = {"contract_key", "behavior_digest", "fingerprint_v2"}
    if not expected.issubset(record):
        raise IdentityError("record is missing identity digests")
    if any(not isinstance(record[key], str) or not _DIGEST.fullmatch(record[key]) for key in expected):
        raise IdentityError("record contains invalid digest")
    payload = {key: value for key, value in record.items() if key not in expected}
    identity = validate_identity(payload)
    if (identity.contract_key, identity.behavior_digest, identity.fingerprint_v2) != tuple(record[key] for key in ("contract_key", "behavior_digest", "fingerprint_v2")):
        raise IdentityError("identity digest mismatch")
    return identity

def canonical_json(identity: CanonicalIdentity) -> str:
    if not isinstance(identity, CanonicalIdentity):
        raise IdentityError("expected CanonicalIdentity")
    return _json(identity.as_record())
