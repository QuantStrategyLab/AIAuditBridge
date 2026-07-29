"""Shared static gate helpers for Codex app review checks.

This module keeps secret-safe static scanning logic reusable across repos.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_POLICY_PATH = Path(".github/codex_auto_merge_policy.json")


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "version": 1,
        "blocked_path_patterns": [
            r"(^|/)(\.env|.*secret.*|.*credential.*|.*token.*|.*private.*|.*\.pem|.*\.key)$",
        ],
        "max_changed_files": 50,
        "max_changed_lines": 5000,
    }


def compile_patterns(policy: dict[str, Any]) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for p in policy.get("blocked_path_patterns", []):
        if isinstance(p, str) and p.strip():
            try:
                patterns.append(re.compile(p, re.IGNORECASE))
            except re.error:
                pass
    return patterns


_SENSITIVE = re.compile(
    r'(?P<field>api[_\s]?key|secret|password|token|credential|private[_\s]?key)\s*[:=]\s*["\']'
    r'(?!\$\{\{|{{|example|placeholder|test|your[-_\s]|xxx|TODO|CHANGEME)[^"\']{12,}["\']',
    re.IGNORECASE,
)


def scan_diff(diff_text: str, path_patterns: list[re.Pattern[str]]) -> list[str]:
    violations: list[str] = []
    current = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            current = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            for pat in path_patterns:
                if current and pat.search(current):
                    violations.append(f"**Blocked file**: `{current}` matches `{pat.pattern}`")
                    break
            continue
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = _SENSITIVE.search(line[1:])
        if m:
            violations.append(f"**Hardcoded secret** in `{current}`: `{m.group('field')}=<redacted>`")
    return list(dict.fromkeys(violations))


def _safe_exact_paths(values: Any) -> set[str] | None:
    if not isinstance(values, list) or not values:
        return None
    paths: set[str] = set()
    for value in values:
        if not isinstance(value, str) or value != value.strip():
            return None
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or path.as_posix() != value
            or ".." in path.parts
            or "\\" in value
            or any(character in value for character in "*?[]")
            or value in paths
        ):
            return None
        paths.add(value)
    return paths


def check_metadata(files: list[dict[str, Any]], policy: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    approved_deleted_paths: set[str] = set()
    max_files = policy.get("max_changed_files", 50)
    max_lines = policy.get("max_changed_lines", 5000)
    changed_paths = {
        filename
        for f in files
        if isinstance((filename := f.get("filename")), str)
    }
    removed_paths = {
        f["filename"]
        for f in files
        if isinstance(f.get("filename"), str)
        and (f.get("status") or "").lower().strip() == "removed"
    }
    configured_bundles = policy.get("approved_change_bundles", [])
    if isinstance(configured_bundles, list):
        for bundle in configured_bundles:
            if not isinstance(bundle, dict):
                continue
            exact_changed_paths = _safe_exact_paths(bundle.get("exact_changed_paths"))
            exact_deleted_paths = _safe_exact_paths(bundle.get("exact_deleted_paths"))
            bundle_max_lines = bundle.get("max_changed_lines")
            if (
                exact_changed_paths is None
                or exact_deleted_paths is None
                or not exact_deleted_paths.issubset(exact_changed_paths)
                or type(bundle_max_lines) is not int
                or bundle_max_lines < max_lines
                or changed_paths != exact_changed_paths
                or removed_paths != exact_deleted_paths
            ):
                continue
            approved_deleted_paths = exact_deleted_paths
            max_lines = bundle_max_lines
            break

    total_added = sum(f.get("additions", 0) or 0 for f in files)
    total_deleted = sum(f.get("deletions", 0) or 0 for f in files)
    for f in files:
        filename = f.get("filename", "?")
        status = (f.get("status") or "").lower().strip()
        if status == "removed" and filename not in approved_deleted_paths:
            issues.append(f"**File deleted**: `{filename}` — verify intentional")
        elif status == "renamed":
            issues.append(f"**File renamed**: `{f.get('previous_filename', '?')}` → `{filename}`")
    if len(files) > max_files:
        issues.append(f"**Too many files**: {len(files)} changed (limit {max_files})")
    if total_added + total_deleted > max_lines:
        issues.append(f"**Too many lines**: {total_added + total_deleted} changed (limit {max_lines})")
    return issues


def collect_static_gate_issues(files: list[dict[str, Any]], diff_text: str,
                              policy: dict[str, Any]) -> list[str]:
    return check_metadata(files, policy) + scan_diff(diff_text, compile_patterns(policy))
