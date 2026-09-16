#!/usr/bin/env python3
"""Run the fixed, review-only Global ETF research codegen case.

The default command is plan-only.  The execute path is limited to the manual
main-branch self-hosted workflow and keeps its claim/response/result under the
fixed VPS state directory.  It never grants deployment, trading, or research
execution authority.
"""

from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

GLOBAL_ETF_RESEARCH_CODEGEN_TASK = "global_etf_research_codegen"
GLOBAL_ETF_RESEARCH_CODEGEN_MODEL = "gpt-5.6-luna"
GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT = "5f11fcfe8c5473de20e1b590e9aa3e87665b6108"
GLOBAL_ETF_RESEARCH_SOURCE_URL = "https://www.nber.org/papers/w22208"
GLOBAL_ETF_RESEARCH_SOURCE_MAX_BYTES = 512 * 1024
GLOBAL_ETF_RESEARCH_OBJECTIVE = (
    "Rename only the local variables frame and subset for readability without changing behavior."
)
GLOBAL_ETF_ALLOWED_PATHS = frozenset({
    "src/us_equity_strategies/strategies/global_etf_rotation.py",
    "tests/test_global_etf_rotation.py",
})
GLOBAL_ETF_TARGET_PATH = "src/us_equity_strategies/strategies/global_etf_rotation.py"
GLOBAL_ETF_TARGET_FUNCTION = "_closes_for_symbol"
GLOBAL_ETF_STATE_ROOT = Path.home() / ".local/state/aiauditbridge/global-etf-codegen-20260916"
GLOBAL_ETF_WORKFLOW_NAME = "Global ETF Research Codegen"
GLOBAL_ETF_SOURCE_REPOSITORY = "QuantStrategyLab/AIAuditBridge"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATUSES = frozenset({"no_changes", "patch_validated", "failed"})


class GlobalResearchCodegenError(ValueError):
    """Safe failure for the fixed Global ETF codegen case."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are disabled", headers, fp)


class _SourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.in_intro = False
        self.intro_depth = 0
        self.in_intro_p = False
        self.intro_parts: list[str] = []
        self.blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() in {"script", "style"}:
            self.blocked_depth += 1
            return
        if tag.lower() == "title":
            self.in_title = True
        if tag.lower() == "div" and "page-header__intro-inner" in values.get("class", "").split():
            self.in_intro = True
            self.intro_depth = 1
        elif self.in_intro:
            self.intro_depth += 1
        if tag.lower() == "p" and self.in_intro:
            self.in_intro_p = True
        if tag.lower() == "meta":
            key = values.get("name", "").lower() or values.get("property", "").lower()
            if key in {"citation_title", "description", "og:description"} and values.get("content"):
                self.meta[key] = values["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"}:
            self.blocked_depth = max(0, self.blocked_depth - 1)
            return
        if tag.lower() == "title":
            self.in_title = False
        if self.in_intro:
            if tag.lower() == "p":
                self.in_intro_p = False
            self.intro_depth -= 1
            if self.intro_depth <= 0:
                self.in_intro = False

    def handle_data(self, data: str) -> None:
        if self.blocked_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        if self.in_intro_p:
            self.intro_parts.append(data)


def _source_fields(body: bytes) -> tuple[str, str]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
    parser = _SourceParser()
    try:
        parser.feed(text)
    except Exception:
        raise GlobalResearchCodegenError("global_source_parse_failed") from None
    title = " ".join(parser.meta.get("citation_title", "").split()) or " ".join("".join(parser.title_parts).split())
    abstract = " ".join("".join(parser.intro_parts).split())
    if not title or not abstract:
        raise GlobalResearchCodegenError("global_source_metadata_missing")
    return title, abstract


def fetch_global_research_source(*, opener: Any = None, retrieved_at: datetime | None = None) -> dict[str, Any]:
    """Read exactly the fixed NBER page and retain its bounded body in memory."""
    opener = opener or urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(GLOBAL_ETF_RESEARCH_SOURCE_URL, headers={"User-Agent": "AIAuditBridge-research/1"})
    try:
        with opener.open(request, timeout=15) as response:
            if response.geturl() != GLOBAL_ETF_RESEARCH_SOURCE_URL:
                raise GlobalResearchCodegenError("global_source_redirected")
            body = response.read(GLOBAL_ETF_RESEARCH_SOURCE_MAX_BYTES + 1)
    except GlobalResearchCodegenError:
        raise
    except Exception:
        raise GlobalResearchCodegenError("global_source_unavailable") from None
    if len(body) > GLOBAL_ETF_RESEARCH_SOURCE_MAX_BYTES:
        raise GlobalResearchCodegenError("global_source_too_large")
    title, abstract = _source_fields(body)
    timestamp = retrieved_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise GlobalResearchCodegenError("global_source_time_invalid")
    return {
        "url": GLOBAL_ETF_RESEARCH_SOURCE_URL,
        "retrieved_at": timestamp.astimezone(timezone.utc).isoformat(),
        "title": title,
        "abstract": abstract,
        "body": body.decode("utf-8", errors="replace"),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def _docker_preflight() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise GlobalResearchCodegenError("global_codegen_docker_unavailable")
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GlobalResearchCodegenError("global_codegen_docker_unavailable") from None
    if result.returncode != 0 or not result.stdout.strip():
        raise GlobalResearchCodegenError("global_codegen_docker_unavailable")
    return result.stdout.strip()


def _git(root: Path, *args: str, strip: bool = False) -> str:
    try:
        output = subprocess.run(
            ["git", "--no-optional-locks", *args], cwd=root, env={"PATH": os.environ.get("PATH", "")},
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout
        return output.strip() if strip else output
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise GlobalResearchCodegenError("global_codegen_base_unavailable") from None


def _read_global_base(root: Path) -> tuple[str, dict[str, str]]:
    root = root.resolve()
    commit = _git(root, "rev-parse", "HEAD", strip=True)
    if commit != GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT:
        raise GlobalResearchCodegenError("global_codegen_base_identity_mismatch")
    files: dict[str, str] = {}
    for relative in GLOBAL_ETF_ALLOWED_PATHS:
        try:
            value = _git(root, "show", f"{commit}:{relative}")
        except GlobalResearchCodegenError:
            raise
        if len(value.encode("utf-8")) > GLOBAL_ETF_RESEARCH_SOURCE_MAX_BYTES:
            raise GlobalResearchCodegenError("global_codegen_base_too_large")
        files[relative] = value
    validate_global_etf_codegen_change(GLOBAL_ETF_TARGET_PATH, files[GLOBAL_ETF_TARGET_PATH], files[GLOBAL_ETF_TARGET_PATH])
    return commit, files


def _function_node(tree: ast.AST) -> ast.FunctionDef:
    nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == GLOBAL_ETF_TARGET_FUNCTION]
    if len(nodes) != 1 or not isinstance(nodes[0], ast.FunctionDef):
        raise GlobalResearchCodegenError("global_codegen_target_function_invalid")
    return nodes[0]


def _span(source: str, node: ast.FunctionDef) -> tuple[int, int]:
    lines = source.encode("utf-8").splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset
    return start, end


def _drop_docstring(node: ast.FunctionDef) -> ast.FunctionDef:
    value = deepcopy(node)
    if value.body and isinstance(value.body[0], ast.Expr) and isinstance(getattr(value.body[0], "value", None), ast.Constant) and isinstance(value.body[0].value.value, str):
        value.body = value.body[1:]
    return value


def _normalise_local_names(node: ast.FunctionDef, *, candidate: bool = False) -> ast.FunctionDef:
    value = deepcopy(node)
    names = {"history_frame": "frame", "symbol_frame": "subset"} if candidate else {}

    class Rename(ast.NodeTransformer):
        def visit_Name(self, current: ast.Name):
            if current.id in names:
                current.id = names[current.id]
            return current

    return Rename().visit(value)


def _validate_local_name_mapping(old: ast.FunctionDef, new: ast.FunctionDef) -> dict[str, str]:
    """Accept only one-way frame/subset local renames, with no mixed names."""
    old_names = {item.id for item in ast.walk(old) if isinstance(item, ast.Name)}
    new_names = {item.id for item in ast.walk(new) if isinstance(item, ast.Name)}
    mapping: dict[str, str] = {}
    for before, after in (("frame", "history_frame"), ("subset", "symbol_frame")):
        if before in old_names:
            if after in new_names:
                if before in new_names or after in old_names:
                    raise GlobalResearchCodegenError("global_codegen_local_name_mapping_invalid")
                mapping[before] = after
            elif before not in new_names:
                raise GlobalResearchCodegenError("global_codegen_local_name_mapping_invalid")
        elif before in new_names or after in old_names:
            raise GlobalResearchCodegenError("global_codegen_local_name_mapping_invalid")
    return mapping


def validate_global_etf_codegen_change(path: str, original: str, updated: str) -> None:
    """Allow only docstrings or the two fixed local-name normalizations."""
    if path not in GLOBAL_ETF_ALLOWED_PATHS:
        raise GlobalResearchCodegenError("global_codegen_path_not_allowed")
    if path != GLOBAL_ETF_TARGET_PATH:
        return
    try:
        original_tree = ast.parse(original)
        updated_tree = ast.parse(updated)
        old = _function_node(original_tree)
        new = _function_node(updated_tree)
    except (SyntaxError, GlobalResearchCodegenError):
        raise GlobalResearchCodegenError("global_codegen_ast_invalid") from None
    if ast.dump(old.args, include_attributes=False) != ast.dump(new.args, include_attributes=False):
        raise GlobalResearchCodegenError("global_codegen_signature_changed")
    _validate_local_name_mapping(old, new)
    old_start, old_end = _span(original, old)
    new_start, new_end = _span(updated, new)
    old_bytes, new_bytes = original.encode("utf-8"), updated.encode("utf-8")
    if old_bytes[:old_start] != new_bytes[:new_start] or old_bytes[old_end:] != new_bytes[new_end:]:
        raise GlobalResearchCodegenError("global_codegen_bytes_outside_target_changed")
    if ast.dump(_normalise_local_names(_drop_docstring(old)), include_attributes=False) != ast.dump(_normalise_local_names(_drop_docstring(new), candidate=True), include_attributes=False):
        raise GlobalResearchCodegenError("global_codegen_ast_changed")


def _identity(*, source: Mapping[str, Any], source_commit: str) -> dict[str, Any]:
    return {
        "task": GLOBAL_ETF_RESEARCH_CODEGEN_TASK,
        "source_repository": GLOBAL_ETF_SOURCE_REPOSITORY,
        "source_commit": source_commit,
        "objective": GLOBAL_ETF_RESEARCH_OBJECTIVE,
        "source_url": source["url"],
        "source_body_sha256": source["body_sha256"],
        "allowed_paths": sorted(GLOBAL_ETF_ALLOWED_PATHS),
    }


def _validate_stored_identity(identity: Any) -> None:
    if not isinstance(identity, dict):
        raise GlobalResearchCodegenError("global_codegen_claim_invalid")
    if (
        identity.get("task") != GLOBAL_ETF_RESEARCH_CODEGEN_TASK
        or identity.get("source_repository") != GLOBAL_ETF_SOURCE_REPOSITORY
        or identity.get("source_commit") != GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT
        or identity.get("objective") != GLOBAL_ETF_RESEARCH_OBJECTIVE
        or identity.get("source_url") != GLOBAL_ETF_RESEARCH_SOURCE_URL
        or identity.get("allowed_paths") != sorted(GLOBAL_ETF_ALLOWED_PATHS)
        or not _SHA256.fullmatch(str(identity.get("source_body_sha256") or ""))
    ):
        raise GlobalResearchCodegenError("global_codegen_claim_identity_mismatch")


def _validate_stored_source(source: Any, identity: Mapping[str, Any]) -> None:
    if not isinstance(source, Mapping) or not isinstance(source.get("body"), str) or not source.get("body"):
        raise GlobalResearchCodegenError("global_codegen_claim_invalid")
    body_hash = hashlib.sha256(source["body"].encode("utf-8")).hexdigest()
    if source.get("url") != GLOBAL_ETF_RESEARCH_SOURCE_URL or source.get("body_sha256") != body_hash:
        raise GlobalResearchCodegenError("global_codegen_claim_source_mismatch")
    if source.get("body_sha256") != identity.get("source_body_sha256"):
        raise GlobalResearchCodegenError("global_codegen_claim_source_mismatch")


def _read_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise GlobalResearchCodegenError(reason) from None
    if not isinstance(value, dict):
        raise GlobalResearchCodegenError(reason)
    return value


def _recover_existing(root: Path) -> dict[str, Any] | None:
    result_path = root / "result.json"
    claim_path = root / "claim.json"
    if result_path.exists():
        if not claim_path.exists():
            raise GlobalResearchCodegenError("global_codegen_terminal_claim_missing")
        result = _read_json(result_path, "global_codegen_terminal_invalid")
        claim = _read_json(claim_path, "global_codegen_claim_invalid")
        _validate_stored_identity(result.get("identity"))
        _validate_stored_identity(claim.get("identity"))
        if claim.get("identity") != result.get("identity"):
            raise GlobalResearchCodegenError("global_codegen_terminal_identity_mismatch")
        _validate_stored_source(claim.get("source"), result["identity"])
        if result.get("source") is not None:
            _validate_stored_source(result.get("source"), result["identity"])
        if result.get("status") not in _TERMINAL_STATUSES:
            raise GlobalResearchCodegenError("global_codegen_terminal_invalid")
        return result
    if claim_path.exists():
        claim = _read_json(claim_path, "global_codegen_claim_invalid")
        identity = claim.get("identity")
        _validate_stored_identity(identity)
        _validate_stored_source(claim.get("source"), identity)
        raise GlobalResearchCodegenError("global_codegen_claim_unknown")
    return None


def _claim_or_recover(root: Path, identity: dict[str, Any], source: Mapping[str, Any]) -> dict[str, Any] | None:
    root.mkdir(parents=True, exist_ok=True)
    claim_path = root / "claim.json"
    result_path = root / "result.json"
    if result_path.exists():
        result = _read_json(result_path, "global_codegen_terminal_invalid")
        if result.get("identity") != identity or result.get("status") not in _TERMINAL_STATUSES:
            raise GlobalResearchCodegenError("global_codegen_terminal_identity_mismatch")
        return result
    if claim_path.exists():
        claim = _read_json(claim_path, "global_codegen_claim_invalid")
        if claim.get("identity") != identity:
            raise GlobalResearchCodegenError("global_codegen_claim_identity_mismatch")
        raise GlobalResearchCodegenError("global_codegen_claim_unknown")
    claim = {
        "status": "claimed", "claimed_at": datetime.now(timezone.utc).isoformat(),
        "identity": identity, "source": dict(source),
    }
    try:
        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(claim, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except FileExistsError:
        raise GlobalResearchCodegenError("global_codegen_claim_unknown") from None
    except OSError:
        raise GlobalResearchCodegenError("global_codegen_claim_failed") from None
    return None


def _write_terminal(root: Path, result: dict[str, Any]) -> None:
    result.update(
        no_order=True,
        promotion_eligible=False,
        research_only=True,
        live_authority_granted=False,
    )
    try:
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        raise GlobalResearchCodegenError("global_codegen_terminal_write_unknown") from None


def _prompt(files: Mapping[str, str], source: Mapping[str, Any]) -> str:
    file_sections = []
    for path in sorted(GLOBAL_ETF_ALLOWED_PATHS):
        content = files[path]
        file_sections.append(
            f"File: {path}\nFile SHA256: {hashlib.sha256(content.encode('utf-8')).hexdigest()}\n"
            f"File content (untrusted working material):\n{content}\n"
        )
    return (
        "You are a review-only Codex researcher. Work only on the fixed Global ETF codegen task.\n"
        f"Objective: {GLOBAL_ETF_RESEARCH_OBJECTIVE}\n"
        f"Allowed paths: {', '.join(sorted(GLOBAL_ETF_ALLOWED_PATHS))}\n"
        f"Source URL: {source['url']}\nSource retrieved_at: {source['retrieved_at']}\n"
        f"Source title: {source['title']}\nSource abstract: {source['abstract']}\n"
        f"Source body SHA256: {source['body_sha256']}\n"
        + "\n".join(file_sections)
        + "Return one JSON patch response. Keep behavior equivalent: only comments/docstrings or the fixed local names "
        "frame->history_frame and subset->symbol_frame in _closes_for_symbol. The response schema is exactly "
        "{final_message:string, changes:[{path:string, base_sha256:string, edits:[{old:string,new:string}]}]}; "
        "use changes=[] for no_changes, and never return complete file contents."
    )


def _codex_execute(*, source_ref: str):
    from ai_gateway_client import AiGatewayClient, GatewayConfig

    config = GatewayConfig.from_env()
    if config.research_providers != ("codex",):
        raise GlobalResearchCodegenError("global_codegen_codex_only_required")
    client = AiGatewayClient(config)

    def execute(prompt: str):
        return client.execute(
            prompt,
            task=GLOBAL_ETF_RESEARCH_CODEGEN_TASK,
            mode="review_only",
            model=GLOBAL_ETF_RESEARCH_CODEGEN_MODEL,
            complexity="medium",
            research_stage="optimization",
            research_objective=GLOBAL_ETF_RESEARCH_OBJECTIVE,
            reasoning_effort="medium",
            sandbox="read-only",
            allowed_providers=["codex"],
            source_repository=GLOBAL_ETF_SOURCE_REPOSITORY,
            source_ref=source_ref,
            timeout=1800,
        )

    return execute


def _run_global_candidate_tests(candidate_root: Path, *, baseline_root: Path) -> dict[str, str]:
    """Run the fixed Global profile through the shared Docker test helper."""
    try:
        from scripts.run_new_research import _run_codegen_candidate_tests

        return _run_codegen_candidate_tests(
            candidate_root,
            baseline_root=baseline_root,
            profile="global_etf",
        )
    except Exception as exc:
        if isinstance(exc, GlobalResearchCodegenError):
            raise
        raise GlobalResearchCodegenError("global_codegen_candidate_tests_failed") from None


def run_global_etf_research_codegen_case(
    *, ues_repo_root: str | Path, run_root: str | Path = GLOBAL_ETF_STATE_ROOT,
    source_ref: str, execute: Callable[[str], Any] | None = None,
    fetch_source: Callable[[], dict[str, Any]] | None = None,
    candidate_test_runner: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one fixed Global codegen attempt with exclusive persistent claim."""
    root = Path(run_root).resolve()
    recovered = _recover_existing(root)
    if recovered is not None:
        return recovered
    _docker_preflight()
    source_commit, files = _read_global_base(Path(ues_repo_root))
    source = (fetch_source or fetch_global_research_source)()
    identity = _identity(source=source, source_commit=source_commit)
    recovered = _claim_or_recover(root, identity, source)
    if recovered is not None:
        return recovered
    prompt = _prompt(files, source)
    try:
        response = (execute or _codex_execute(source_ref=source_ref))(prompt)
    except Exception:
        raise GlobalResearchCodegenError("global_codegen_response_unknown") from None
    response_path = root / "response.json"
    try:
        response_path.write_text(json.dumps({
            "success": getattr(response, "success", False), "provider": getattr(response, "provider", ""),
            "model": getattr(response, "model", ""), "raw": getattr(response, "raw", {}),
            "output": getattr(response, "output", ""),
        }, ensure_ascii=False, sort_keys=True, allow_nan=False), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        raise GlobalResearchCodegenError("global_codegen_response_unknown") from None
    raw = getattr(response, "raw", {}) if isinstance(getattr(response, "raw", {}), Mapping) else {}
    if getattr(response, "success", False) is not True:
        result = {"status": "failed", "reason": "global_codegen_gateway_failed", "identity": identity}
        _write_terminal(root, result)
        return result
    if (
        getattr(response, "provider", "") != "codex"
        or getattr(response, "model", "") != GLOBAL_ETF_RESEARCH_CODEGEN_MODEL
        or raw.get("provider") != "codex"
        or raw.get("status") != "succeeded"
        or raw.get("research_stage") != "optimization"
    ):
        result = {"status": "failed", "reason": "global_codegen_result_invalid", "identity": identity}
        _write_terminal(root, result)
        return result
    try:
        from scripts.run_monthly_codex_audit import GLOBAL_ETF_RESEARCH_CODEGEN_TASK, apply_service_changes, parse_service_patch_response

        final_message, changes = parse_service_patch_response(getattr(response, "output", ""), task=GLOBAL_ETF_RESEARCH_CODEGEN_TASK)
    except Exception:
        raise GlobalResearchCodegenError("global_codegen_patch_invalid") from None
    if not changes:
        result = {"status": "no_changes", "final_message": final_message, "changed_paths": [], "identity": identity}
        _write_terminal(root, result)
        return result
    try:
        from scripts.run_monthly_codex_audit import GLOBAL_ETF_RESEARCH_CODEGEN_TASK, apply_service_changes
        from scripts.run_new_research import _archive_codegen_base

        baseline = Path(tempfile.mkdtemp(prefix="aab-global-etf-baseline-"))
        candidate = root / "candidate"
        _archive_codegen_base(Path(ues_repo_root).resolve(), baseline, approved_commit=source_commit)
        _archive_codegen_base(Path(ues_repo_root).resolve(), candidate, approved_commit=source_commit)
        changed = apply_service_changes(candidate, changes, task=GLOBAL_ETF_RESEARCH_CODEGEN_TASK, validate_updated=validate_global_etf_codegen_change)
        test_result = dict((candidate_test_runner or _run_global_candidate_tests)(candidate, baseline_root=baseline))
        if test_result.get("status") != "passed":
            raise GlobalResearchCodegenError("global_codegen_candidate_tests_failed")
    except GlobalResearchCodegenError:
        raise
    except Exception:
        raise GlobalResearchCodegenError("global_codegen_candidate_failed") from None
    finally:
        if "baseline" in locals():
            shutil.rmtree(baseline, ignore_errors=True)
    result = {"status": "patch_validated", "final_message": final_message, "changed_paths": changed, "candidate_tests": test_result, "identity": identity, "research_only": True, "live_authority_granted": False}
    _write_terminal(root, result)
    return result


def plan() -> dict[str, Any]:
    return {
        "status": "PLAN_ONLY", "task": GLOBAL_ETF_RESEARCH_CODEGEN_TASK,
        "source_repository": GLOBAL_ETF_SOURCE_REPOSITORY, "ues_commit": GLOBAL_ETF_RESEARCH_CODEGEN_UES_COMMIT,
        "source_url": GLOBAL_ETF_RESEARCH_SOURCE_URL, "allowed_paths": sorted(GLOBAL_ETF_ALLOWED_PATHS),
        "objective": GLOBAL_ETF_RESEARCH_OBJECTIVE, "research_only": True,
        "no_order": True, "promotion_eligible": False, "live_authority_granted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="run only in the fixed main self-hosted workflow")
    parser.add_argument("--ues-repo-root", type=Path, default=Path("/opt/ues-source"))
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(plan(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    environ = os.environ
    if (
        platform.system() != "Linux"
        or environ.get("RUNNER_ENVIRONMENT") != "self-hosted"
        or environ.get("GITHUB_ACTIONS") != "true"
        or environ.get("GITHUB_REF") != "refs/heads/main"
        or environ.get("GITHUB_WORKFLOW") != GLOBAL_ETF_WORKFLOW_NAME
        or environ.get("GITHUB_RUN_ATTEMPT") != "1"
    ):
        print("global_codegen_unavailable")
        return 2
    try:
        result = run_global_etf_research_codegen_case(ues_repo_root=args.ues_repo_root, source_ref=environ.get("GITHUB_SHA", ""))
        public = {
            key: value for key, value in result.items()
            if key not in {"identity", "source"}
        }
        public.update(no_order=True, promotion_eligible=False, live_authority_granted=False)
        print(json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1 if result.get("status") == "failed" else 0
    except GlobalResearchCodegenError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
