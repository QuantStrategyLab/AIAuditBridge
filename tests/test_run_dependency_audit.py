from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts import run_dependency_audit as audit


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _manifest(otpauth="^9.2.2", playwright="^1.62.1", extra=None):
    return {
        "name": "schwab-token-refresher",
        "version": "1.0.0",
        "engines": {"node": "20.x"},
        "dependencies": {
            "axios": "^1.20.0",
            "otpauth": otpauth,
            "playwright": playwright,
            "playwright-extra": "^4.3.6",
        },
        **(extra or {}),
    }


def _entry(version, resolved="https://registry.npmjs.org/example/-/example.tgz"):
    return {"version": version, "resolved": resolved, "integrity": "sha512-test"}


def _lock(manifest, *, noble="2.2.0", otpauth="9.2.2", playwright="1.62.1", core="1.62.1", fsevents=False):
    packages = {
        "": {"name": manifest["name"], "version": manifest["version"], "dependencies": manifest["dependencies"]},
        "node_modules/@noble/hashes": _entry(noble),
        "node_modules/otpauth": {**_entry(otpauth), "dependencies": {"@noble/hashes": noble}},
        "node_modules/playwright": {**_entry(playwright), "dependencies": {"playwright-core": core}},
        "node_modules/playwright-core": _entry(core),
    }
    if fsevents:
        packages["node_modules/fsevents"] = {**_entry("2.3.3"), "optional": True, "os": ["darwin"]}
    return {
        "name": manifest["name"],
        "lockfileVersion": 3,
        "requires": True,
        "packages": packages,
    }


def _pr(**overrides):
    pr = {
        "number": 62,
        "state": "open",
        "draft": False,
        "body": "Dependabot update",
        "user": {"login": "dependabot[bot]"},
        "base": {"ref": "main", "sha": BASE_SHA, "repo": {"full_name": audit.SOURCE_REPOSITORY}},
        "head": {"sha": HEAD_SHA, "repo": {"full_name": audit.SOURCE_REPOSITORY}},
    }
    pr.update(overrides)
    return pr


def _files(*names):
    return [{"filename": name, "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"} for name in names]


def _gate(pr=None):
    base = _manifest()
    head = _manifest(otpauth="^9.5.2", playwright="^1.62.1")
    return audit.GateResult(
        pr or _pr(), base, head, _lock(base), _lock(head, noble="2.4.0", otpauth="9.5.2"), "diff",
        {"main.js": "main", "tests/test_totp_dependency.js": "totp", "tests/test_playwright_dependency.js": "pw"},
    )


def test_accepts_only_two_manifest_files_and_related_lock_updates():
    result = audit.validate_pr_gate(
        _pr(), _manifest(), _manifest(otpauth="^9.5.2"), _lock(_manifest()),
        _lock(_manifest(otpauth="^9.5.2"), noble="2.4.0", otpauth="9.5.2"), _files(*audit.ALLOWED_FILES),
        diff_text="small", base_context={}, dry_run=False,
    )
    assert result.head_manifest["dependencies"]["otpauth"] == "^9.5.2"


@pytest.mark.parametrize("mutator, message", [
    (lambda p: p["user"].update(login="someone"), "Dependabot"),
    (lambda p: p["base"].update(ref="release"), "main"),
    (lambda p: p["head"]["repo"].update(full_name="Other/Repo"), "fixed source"),
    (lambda p: p.update(draft=True), "Draft"),
    (lambda p: p.update(body="version-update: semver-major"), "major"),
])
def test_rejects_pr_identity_and_lifecycle_gates(mutator, message):
    pr = _pr()
    mutator(pr)
    with pytest.raises(audit.DependencyAuditError, match=message):
        audit.validate_pr_gate(
            pr, _manifest(), _manifest(otpauth="^9.5.2"), _lock(_manifest()),
            _lock(_manifest(otpauth="^9.5.2"), noble="2.4.0", otpauth="9.5.2"), _files(*audit.ALLOWED_FILES),
            diff_text="small", base_context={}, dry_run=False,
        )


def test_rejects_manifest_permission_or_unrelated_field_change():
    head = _manifest(otpauth="^9.5.2", extra={"private": True})
    with pytest.raises(audit.DependencyAuditError, match="outside dependencies"):
        audit.validate_pr_gate(
            _pr(), _manifest(), head, _lock(_manifest()), _lock(head, noble="2.4.0", otpauth="9.5.2"),
            _files(*audit.ALLOWED_FILES), diff_text="small", base_context={}, dry_run=False,
        )


def test_rejects_unrelated_lock_package_and_missing_integrity():
    base = _lock(_manifest())
    head = _lock(_manifest(otpauth="^9.5.2"), noble="2.4.0", otpauth="9.5.2")
    head["packages"]["node_modules/unrelated"] = _entry("1.0.0")
    with pytest.raises(audit.DependencyAuditError, match="unrelated packages"):
        audit.validate_pr_gate(
            _pr(), _manifest(), _manifest(otpauth="^9.5.2"), base, head, _files(*audit.ALLOWED_FILES),
            diff_text="small", base_context={}, dry_run=False,
        )


def test_rejects_two_dependency_changes_and_branch_unrelated_lock_change():
    base = _lock(_manifest())
    head_manifest = _manifest(otpauth="^9.5.2", playwright="^1.63.0")
    head = _lock(head_manifest, noble="2.4.0", otpauth="9.5.2", playwright="1.63.0", core="1.63.0")
    with pytest.raises(audit.DependencyAuditError, match="Exactly one"):
        audit.validate_pr_gate(_pr(), _manifest(), head_manifest, base, head, _files(*audit.ALLOWED_FILES), diff_text="small", base_context={}, dry_run=False)

    head_manifest = _manifest(otpauth="^9.5.2")
    head = _lock(head_manifest, noble="2.4.0", otpauth="9.5.2")
    head["packages"]["node_modules/playwright"]["version"] = "1.63.0"
    with pytest.raises(audit.DependencyAuditError, match="unrelated packages"):
        audit.validate_pr_gate(_pr(), _manifest(), head_manifest, base, head, _files(*audit.ALLOWED_FILES), diff_text="small", base_context={}, dry_run=False)


def test_allows_only_optional_darwin_fsevents_removal_for_playwright():
    base_manifest = _manifest()
    head_manifest = _manifest(playwright="^1.63.0")
    base = _lock(base_manifest, fsevents=True)
    head = _lock(head_manifest, playwright="1.63.0", core="1.63.0", fsevents=False)
    audit.validate_pr_gate(_pr(), base_manifest, head_manifest, base, head, _files(*audit.ALLOWED_FILES), diff_text="small", base_context={}, dry_run=False)
    bad_base = _lock(base_manifest, fsevents=True)
    bad_base["packages"]["node_modules/fsevents"]["os"] = ["linux"]
    with pytest.raises(audit.DependencyAuditError, match="entry is missing"):
        audit.validate_pr_gate(_pr(), base_manifest, head_manifest, bad_base, head, _files(*audit.ALLOWED_FILES), diff_text="small", base_context={}, dry_run=False)


def test_rejects_major_lock_version_even_when_manifest_range_looks_safe():
    base = _lock(_manifest())
    head = _lock(_manifest(otpauth="^9.5.2"), noble="2.4.0", otpauth="10.0.0")
    with pytest.raises(audit.DependencyAuditError, match="Major lockfile"):
        audit.validate_pr_gate(
            _pr(), _manifest(), _manifest(otpauth="^9.5.2"), base, head, _files(*audit.ALLOWED_FILES),
            diff_text="small", base_context={}, dry_run=False,
        )

    head = _lock(_manifest(otpauth="^9.5.2"), noble="2.4.0", otpauth="9.5.2")
    del head["packages"]["node_modules/otpauth"]["integrity"]
    with pytest.raises(audit.DependencyAuditError, match="no integrity"):
        audit.validate_pr_gate(
            _pr(), _manifest(), _manifest(otpauth="^9.5.2"), base, head, _files(*audit.ALLOWED_FILES),
            diff_text="small", base_context={}, dry_run=False,
        )


def test_rejects_failed_latest_ci_run_and_old_head():
    failed_latest = {
        "id": 9, "path": audit.CI_WORKFLOW_PATH, "event": "pull_request", "head_sha": HEAD_SHA,
        "status": "completed", "conclusion": "failure", "created_at": "2026-09-14T02:00:00Z", "updated_at": "2026-09-14T04:00:00Z", "run_attempt": 1,
    }
    old_success = {**failed_latest, "id": 8, "head_sha": "c" * 40, "conclusion": "success", "created_at": "2026-09-14T03:00:00Z"}
    with patch.object(audit, "github_request", return_value=[failed_latest, old_success]):
        with pytest.raises(audit.DependencyAuditError, match="exact head"):
            audit.require_successful_ci("token", HEAD_SHA)
    wrong_workflow = {**failed_latest, "path": ".github/workflows/other.yml", "conclusion": "success"}
    with patch.object(audit, "github_request", return_value=[wrong_workflow]):
        with pytest.raises(audit.DependencyAuditError, match="exact head"):
            audit.require_successful_ci("token", HEAD_SHA)


def test_updated_failure_wins_over_older_success_for_same_head():
    latest_failure = {"id": 11, "path": audit.CI_WORKFLOW_PATH, "event": "pull_request", "head_sha": HEAD_SHA, "status": "completed", "conclusion": "failure", "created_at": "2026-09-14T01:00:00Z", "updated_at": "2026-09-14T05:00:00Z", "run_attempt": 2}
    older_success = {**latest_failure, "id": 10, "conclusion": "success", "created_at": "2026-09-14T04:00:00Z", "updated_at": "2026-09-14T04:30:00Z", "run_attempt": 1}
    with patch.object(audit, "github_request", return_value=[older_success, latest_failure]):
        with pytest.raises(audit.DependencyAuditError, match="exact head"):
            audit.require_successful_ci("token", HEAD_SHA)


def test_actions_api_wrappers_are_paginated_from_workflow_runs_and_jobs():
    responses = iter([
        {"workflow_runs": [{"id": 7}]},
        {"jobs": [{"name": "test", "status": "completed", "conclusion": "success"}]},
    ])
    with patch.object(audit, "github_request", side_effect=lambda *args, **kwargs: next(responses)):
        assert audit.github_list_all("token", f"/repos/{audit.SOURCE_REPOSITORY}/actions/runs?head_sha={HEAD_SHA}") == [{"id": 7}]
        assert audit.github_list_all("token", f"/repos/{audit.SOURCE_REPOSITORY}/actions/runs/7/jobs") == [{"name": "test", "status": "completed", "conclusion": "success"}]

def test_ai_contract_rejects_invalid_json_or_decision():
    with pytest.raises(audit.DependencyAuditError, match="strict JSON"):
        audit.parse_ai_decision("```json {} ```")
    with pytest.raises(audit.DependencyAuditError, match="invalid decision"):
        audit.parse_ai_decision(json.dumps({"decision": "merge", "summary": "x", "reason": "y"}))


def test_dry_run_calls_ai_but_performs_no_github_write():
    calls = []
    def approved_service(*, prompt, source_ref, timeout_minutes):
        calls.append((source_ref, timeout_minutes))
        return json.dumps({"decision": "approve", "summary": "ok", "reason": "safe"})
    with patch.object(audit, "fetch_gate", return_value=_gate()), patch.object(
        audit, "github_request", side_effect=lambda *args, **kwargs: calls.append((args, kwargs))
    ):
        result = audit.audit_one(
            "token", 62, dry_run=True,
            service_request=approved_service,
        )
    assert result["decision"] == "approve"
    assert calls == [(BASE_SHA, 10)]


def test_duplicate_bot_marker_skips_ai_and_writes():
    bot = audit.EXPECTED_BOT_LOGIN
    comments = [{"id": 3, "user": {"login": bot}, "body": audit.marker(pr_number=62, head_sha=HEAD_SHA, base_sha=BASE_SHA)}]
    calls = []
    def fake_request(token, method, path, payload=None):
        calls.append((method, path, payload))
        return comments
    def fail_service(*, prompt, source_ref, timeout_minutes):
        raise AssertionError("AI called")
    with patch.object(audit, "fetch_gate", return_value=_gate()), patch.object(audit, "github_request", side_effect=fake_request):
        result = audit.audit_one("token", 62, dry_run=False, bot_login=bot, service_request=fail_service)
    assert result["decision"] == "defer"
    assert "duplicate" in result["reason"]
    assert calls and all(method == "GET" for method, _, _ in calls)


def test_head_change_after_approve_never_merges():
    def fake_request(token, method, path, payload=None):
        if method == "GET" and "/pulls/62" in path:
            return {"head": {"sha": "c" * 40}, "base": {"sha": BASE_SHA}}
        if method == "GET" and "/issues/62/comments" in path:
            return []
        if method == "POST" and "/issues/62/comments" in path:
            return {"id": 4}
        if method == "PATCH" and "/issues/comments/4" in path:
            return {"id": 4}
        raise AssertionError((method, path))
    def approved_service(*, prompt, source_ref, timeout_minutes):
        return json.dumps({"decision": "approve", "summary": "ok", "reason": "safe"})
    with patch.object(audit, "fetch_gate", return_value=_gate()), patch.object(audit, "github_request", side_effect=fake_request):
        result = audit.audit_one("token", 62, dry_run=False, bot_login=audit.EXPECTED_BOT_LOGIN, service_request=approved_service)
    assert result["decision"] == "defer"
    assert "changed" in result["reason"]


def test_prompt_contains_untrusted_base_consumer_context_and_source_sha():
    prompt = audit.build_prompt(_gate())
    assert "untrusted evidence" in prompt
    assert "main" in prompt
    assert "totp" in prompt and "playwright" in prompt
    assert HEAD_SHA in prompt and BASE_SHA in prompt


def test_merge_metadata_rejects_human_review_label_and_accepts_exact_open_pr():
    exact = _pr(labels=[])
    audit.validate_merge_metadata(exact, base_sha=BASE_SHA, head_sha=HEAD_SHA)
    labeled = _pr(labels=[{"name": "human-review-required"}])
    with pytest.raises(audit.DependencyAuditError, match="human review"):
        audit.validate_merge_metadata(labeled, base_sha=BASE_SHA, head_sha=HEAD_SHA)


def test_scan_skips_non_dependabot_and_ineligible_prs_but_reaches_later_candidate(capsys):
    prs = [_pr(number=1, user={"login": "someone"}), _pr(number=2), _pr(number=3)]
    eligible_gate = _gate(_pr(number=3))
    def gate_for(token, number, *, dry_run):
        if number == 2:
            raise audit.DependencyAuditError("unsupported dependency")
        return eligible_gate
    seen = []
    def fake_audit(token, number, *, dry_run, bot_login, gate):
        seen.append((number, gate.pr["number"]))
        return {"pr": number, "decision": "defer"}
    with patch.object(audit, "resolve_source_repo_token", return_value="token"), patch.object(audit, "github_list_all", return_value=prs), patch.object(audit, "fetch_gate", side_effect=gate_for), patch.object(audit, "audit_one", side_effect=fake_audit):
        assert audit.main(["--dry-run"]) == 0
    assert seen == [(3, 3)]
    assert '"pr": 3' in capsys.readouterr().out


def test_service_request_is_codex_only_read_only_high_effort():
    responses = iter([
        {"status": "queued", "job_id": "job-123456789012345678901"},
        {"status": "succeeded", "output": "{}"},
    ])
    calls = []
    with patch.dict("os.environ", {"CODEX_AUDIT_SERVICE_URL": "https://service.example"}), patch.object(audit, "request_codex_service_json", side_effect=lambda **kwargs: calls.append(kwargs) or next(responses)), patch.object(audit.time, "sleep"):
        assert audit.request_dependency_service(prompt="p", source_ref=BASE_SHA) == "{}"
    payload = calls[0]["payload"]
    assert payload["model"] == "gpt-6-astra"
    assert payload["reasoning_effort"] == "high"
    assert payload["allowed_providers"] == ["codex"]
    assert payload["sandbox"] == "read-only"
    assert payload["source_ref"] == BASE_SHA
