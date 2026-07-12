"""Pure canonical contract identity schema and digest helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

SCHEMA = "contract_identity.v2"
CANONICALIZER_VERSION = "operator_tokens.v1"
ALLOWED_CATEGORIES = frozenset(
    {"bug", "contract", "logic", "performance", "reliability", "security"}
)
ALLOWED_SEVERITIES = frozenset({"critical", "high", "medium", "low"})
ALLOWED_ANCHOR_KINDS = frozenset(
    {"config", "endpoint", "field", "identifier", "schema", "symbol", "type"}
)

MAX_REPO_BYTES = 200
MAX_FILE_BYTES = 512
MAX_ANCHOR_BYTES = 256
MAX_TOKEN_BYTES = 256
MAX_EVIDENCE_BYTES = 256
MAX_ITEMS = 32
MAX_TOKENS_PER_CLAUSE = 128
MAX_CANONICAL_BYTES = 16_384

_TOP_REQUIRED = frozenset(
    {
        "schema",
        "canonicalizer_version",
        "scope",
        "anchors",
        "predicates",
        "required_behavior",
        "forbidden_behavior",
        "ordering_constraints",
        "evidence",
    }
)
_DIGEST_FIELDS = frozenset({"contract_key", "behavior_digest", "fingerprint_v2"})
_OPERATORS = ("===", "!==", ">=", "<=", "==", "!=", "->", "=>", "::", ">", "<")
_TOKEN_RE = re.compile(
    r"<SECRET:[A-Z_]+:\d+>|===|!==|>=|<=|==|!=|->|=>|::|>|<|[\w.$]+|[^\s]",
    re.UNICODE,
)
_PLACEHOLDER_RE = re.compile(r"<SECRET:[A-Z_]+:\d+>")
_SECRET_PATTERNS = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.I | re.S)),
    ("AWS", re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b")),
    ("SLACK", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("GITLAB", re.compile(r"\bglpat-[A-Za-z0-9_-]{10,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("DSN", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@[^\s]+", re.I)),
    ("BEARER", re.compile(r"(?i)\bbearer\s+\S+")),
    ("CREDENTIAL", re.compile(
        r"(?i)(?<!<)\b(?:token|secret|password|api[ _-]?key|authorization|cookie)\b\s*[:=]\s*\S+"
    )),
    ("API_KEY", re.compile(r"\b(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{20,}\b")),
)


class IdentityValidationError(ValueError):
    """Raised when canonical identity input cannot be safely validated."""


@dataclass(frozen=True)
class Scope:
    repo: str
    file: str
    category: str

    def as_dict(self) -> dict[str, str]:
        return {"repo": self.repo, "file": self.file, "category": self.category}


@dataclass(frozen=True)
class Anchor:
    kind: str
    value: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class Evidence:
    head_sha: str
    diff_digest: str
    file: str
    location_or_hunk_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "head_sha": self.head_sha,
            "diff_digest": self.diff_digest,
            "file": self.file,
            "location_or_hunk_digest": self.location_or_hunk_digest,
        }


@dataclass(frozen=True)
class ContractIdentity:
    scope: Scope
    anchors: tuple[Anchor, ...]
    predicates: tuple[tuple[str, ...], ...]
    required_behavior: tuple[tuple[str, ...], ...]
    forbidden_behavior: tuple[tuple[str, ...], ...]
    ordering_constraints: tuple[tuple[str, ...], ...]
    evidence: Evidence
    severity: str | None
    contract_key: str
    behavior_digest: str
    fingerprint_v2: str

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": SCHEMA,
            "canonicalizer_version": CANONICALIZER_VERSION,
            "scope": self.scope.as_dict(),
            "anchors": [anchor.as_dict() for anchor in self.anchors],
            "predicates": [list(clause) for clause in self.predicates],
            "required_behavior": [list(clause) for clause in self.required_behavior],
            "forbidden_behavior": [list(clause) for clause in self.forbidden_behavior],
            "ordering_constraints": [list(clause) for clause in self.ordering_constraints],
            "evidence": self.evidence.as_dict(),
            "contract_key": self.contract_key,
            "behavior_digest": self.behavior_digest,
            "fingerprint_v2": self.fingerprint_v2,
        }
        if self.severity is not None:
            record["severity"] = self.severity
        return record


class _SecretRedactor:
    def __init__(self, *, allow_placeholders: bool) -> None:
        self.allow_placeholders = allow_placeholders
        self.counts: dict[str, int] = defaultdict(int)

    def redact(self, value: str, field: str) -> str:
        if "<SECRET:" in value and not self.allow_placeholders:
            raise IdentityValidationError(f"{field} contains a reserved secret placeholder")
        if self.allow_placeholders and any(pattern.search(value) for _, pattern in _SECRET_PATTERNS):
            raise IdentityValidationError(f"{field} persists an unredacted secret")
        text = value
        for secret_type, pattern in _SECRET_PATTERNS:
            def replacement(_match: re.Match[str], kind: str = secret_type) -> str:
                self.counts[kind] += 1
                return f"<SECRET:{kind}:{self.counts[kind]}>"

            text = pattern.sub(replacement, text)
        malformed = re.search(r"<SECRET:[^>]*>", text)
        if malformed and not _PLACEHOLDER_RE.fullmatch(malformed.group(0)):
            raise IdentityValidationError(f"{field} contains an invalid secret placeholder")
        return text


def _exact_fields(value: Any, required: frozenset[str], optional: frozenset[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required | (set(value) & optional):
        raise IdentityValidationError(f"{field} has missing or extra fields")
    return value


def _text(value: Any, field: str, max_bytes: int, redactor: _SecretRedactor) -> str:
    if not isinstance(value, str):
        raise IdentityValidationError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or any(unicodedata.category(char).startswith("C") for char in normalized):
        raise IdentityValidationError(f"{field} is empty or contains control characters")
    normalized = redactor.redact(normalized, field)
    if len(normalized.encode("utf-8")) > max_bytes:
        raise IdentityValidationError(f"{field} exceeds {max_bytes} bytes")
    return normalized


def _repo(value: Any, field: str, redactor: _SecretRedactor) -> str:
    repo = _text(value, field, MAX_REPO_BYTES, redactor)
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
        raise IdentityValidationError(f"{field} must be owner/name")
    return repo


def _relative_path(value: Any, field: str, redactor: _SecretRedactor) -> str:
    path = _text(value, field, MAX_FILE_BYTES, redactor)
    parts = path.split("/")
    if (
        "<SECRET:" in path
        or path.startswith("/")
        or "\\" in path
        or any(not part or part in {".", ".."} for part in parts)
    ):
        raise IdentityValidationError(f"{field} must be a repository-relative POSIX path")
    return path


def _reference(value: Any, field: str, redactor: _SecretRedactor) -> str:
    reference = _text(value, field, MAX_EVIDENCE_BYTES, redactor)
    if "<SECRET:" in reference:
        raise IdentityValidationError(f"{field} must not contain a secret placeholder")
    return reference


def _clauses(
    value: Any, field: str, redactor: _SecretRedactor, *, allow_empty: bool
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS or (not value and not allow_empty):
        raise IdentityValidationError(f"{field} must be a bounded list")
    clauses: list[tuple[str, ...]] = []
    for index, clause in enumerate(value):
        if not isinstance(clause, list) or not clause:
            raise IdentityValidationError(f"{field}[{index}] must be a non-empty token list")
        tokens: list[str] = []
        for raw_token in clause:
            token = _text(raw_token, f"{field}[{index}]", MAX_TOKEN_BYTES, redactor)
            tokens.extend(_TOKEN_RE.findall(token))
        if not tokens or len(tokens) > MAX_TOKENS_PER_CLAUSE:
            raise IdentityValidationError(f"{field}[{index}] has invalid token count")
        clauses.append(tuple(tokens))
    return tuple(clauses)


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_stable_json(value)).hexdigest()


def build_contract_identity(payload: dict[str, Any], *, _allow_placeholders: bool = False) -> ContractIdentity:
    top = _exact_fields(payload, _TOP_REQUIRED, frozenset({"severity"}), "identity")
    if top["schema"] != SCHEMA or top["canonicalizer_version"] != CANONICALIZER_VERSION:
        raise IdentityValidationError("unsupported identity schema or canonicalizer")
    redactor = _SecretRedactor(allow_placeholders=_allow_placeholders)

    raw_scope = _exact_fields(top["scope"], frozenset({"repo", "file", "category"}), frozenset(), "scope")
    category = str(raw_scope["category"]).lower() if isinstance(raw_scope["category"], str) else ""
    if category not in ALLOWED_CATEGORIES:
        raise IdentityValidationError("scope.category is not controlled")
    scope = Scope(
        repo=_repo(raw_scope["repo"], "scope.repo", redactor),
        file=_relative_path(raw_scope["file"], "scope.file", redactor),
        category=category,
    )

    raw_anchors = top["anchors"]
    if not isinstance(raw_anchors, list) or not raw_anchors or len(raw_anchors) > MAX_ITEMS:
        raise IdentityValidationError("anchors must be a non-empty bounded list")
    anchors: list[Anchor] = []
    for index, raw_anchor in enumerate(raw_anchors):
        anchor = _exact_fields(raw_anchor, frozenset({"kind", "value"}), frozenset(), f"anchors[{index}]")
        kind = str(anchor["kind"]).lower() if isinstance(anchor["kind"], str) else ""
        if kind not in ALLOWED_ANCHOR_KINDS:
            raise IdentityValidationError(f"anchors[{index}].kind is not controlled")
        anchors.append(Anchor(kind, _text(anchor["value"], f"anchors[{index}].value", MAX_ANCHOR_BYTES, redactor)))

    predicates = _clauses(top["predicates"], "predicates", redactor, allow_empty=False)
    required = _clauses(
        top["required_behavior"], "required_behavior", redactor, allow_empty=False
    )
    forbidden = _clauses(
        top["forbidden_behavior"], "forbidden_behavior", redactor, allow_empty=True
    )
    ordering = _clauses(
        top["ordering_constraints"], "ordering_constraints", redactor, allow_empty=True
    )

    raw_evidence = _exact_fields(
        top["evidence"],
        frozenset({"head_sha", "diff_digest", "file", "location_or_hunk_digest"}),
        frozenset(),
        "evidence",
    )
    head_sha = _text(raw_evidence["head_sha"], "evidence.head_sha", 64, redactor).lower()
    diff_digest = _text(raw_evidence["diff_digest"], "evidence.diff_digest", 64, redactor).lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", head_sha) or not re.fullmatch(r"[0-9a-f]{64}", diff_digest):
        raise IdentityValidationError("evidence digests are malformed")
    evidence = Evidence(
        head_sha=head_sha,
        diff_digest=diff_digest,
        file=_relative_path(raw_evidence["file"], "evidence.file", redactor),
        location_or_hunk_digest=_reference(
            raw_evidence["location_or_hunk_digest"],
            "evidence.location_or_hunk_digest",
            redactor,
        ),
    )
    if evidence.file != scope.file:
        raise IdentityValidationError("evidence.file must match scope.file")
    severity = top.get("severity")
    if severity is not None:
        severity = str(severity).lower() if isinstance(severity, str) else ""
        if severity not in ALLOWED_SEVERITIES:
            raise IdentityValidationError("severity is not controlled")

    contract_payload = {
        "schema": SCHEMA,
        "canonicalizer_version": CANONICALIZER_VERSION,
        "scope": scope.as_dict(),
        "anchors": [anchor.as_dict() for anchor in anchors],
        "predicates": [list(clause) for clause in predicates],
    }
    contract_key = _digest(contract_payload)
    behavior_payload = {
        "contract_key": contract_key,
        "required_behavior": [list(clause) for clause in required],
        "forbidden_behavior": [list(clause) for clause in forbidden],
        "ordering_constraints": [list(clause) for clause in ordering],
    }
    behavior_digest = _digest(behavior_payload)
    fingerprint_v2 = _digest(
        {"contract_key": contract_key, "behavior_digest": behavior_digest}
    )
    identity = ContractIdentity(
        scope, tuple(anchors), predicates, required, forbidden, ordering,
        evidence, severity, contract_key, behavior_digest, fingerprint_v2,
    )
    if len(_stable_json(identity.as_record())) > MAX_CANONICAL_BYTES:
        raise IdentityValidationError("canonical identity exceeds total byte limit")
    return identity


def verify_persisted_identity(record: dict[str, Any]) -> ContractIdentity:
    top = _exact_fields(record, _TOP_REQUIRED | _DIGEST_FIELDS, frozenset({"severity"}), "record")
    expected = {field: top[field] for field in _DIGEST_FIELDS}
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in expected.values()):
        raise IdentityValidationError("persisted identity digest is malformed")
    payload = {key: value for key, value in top.items() if key not in _DIGEST_FIELDS}
    identity = build_contract_identity(payload, _allow_placeholders=True)
    if not all(hmac.compare_digest(expected[field], getattr(identity, field)) for field in _DIGEST_FIELDS):
        raise IdentityValidationError("persisted identity digest mismatch")
    return identity


def canonical_json(identity: ContractIdentity) -> str:
    return _stable_json(identity.as_record()).decode("utf-8")


def operators() -> tuple[str, ...]:
    return _OPERATORS
