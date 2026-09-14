from __future__ import annotations

import json
from pathlib import Path

from service.org_health import DEFAULT_WORKFLOW_ALLOWLIST


ROOT = Path(__file__).resolve().parents[1]
RETIRED_PATHS = (
    "prompts/pr_review.md",
    "scripts/run_codex_pr_review.py",
    "tests/test_run_codex_pr_review.py",
)
RETIRED_WORKFLOWS = (
    ROOT / ".github/workflows/codex_pr_review.yml",
    ROOT / ".github/workflows/codex_review_gate.yml",
)


def test_legacy_ai_pr_review_workflows_are_absent() -> None:
    for relative_path in RETIRED_PATHS:
        assert not (ROOT / relative_path).exists(), relative_path
    for workflow in RETIRED_WORKFLOWS:
        assert not workflow.exists(), workflow

    actionlint_config = (ROOT / ".github/actionlint.yaml").read_text(encoding="utf-8")
    assert "codex_pr_review" not in actionlint_config

    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / ".github/workflows").glob("*.yml")
    )
    assert "name: Codex PR Review" not in workflow_text
    assert "name: Codex Review Gate" not in workflow_text
    assert (ROOT / ".github/workflows/codex_audit.yml").is_file()
    assert (ROOT / ".github/workflows/monthly-orchestrator.yml").is_file()


def test_retired_pr_reviewer_is_not_advertised_as_active() -> None:
    policy = json.loads(
        (ROOT / ".github/codex_auto_merge_policy.json").read_text(encoding="utf-8")
    )
    assert "pr_review" not in policy
    assert "approved_change_bundles" not in policy
    assert policy["max_changed_lines"] == 2_000
    assert "Codex PR Review" not in DEFAULT_WORKFLOW_ALLOWLIST

    for relative_path in (
        "README.md",
        "README.zh-CN.md",
        "docs/ai_autonomy_architecture.md",
    ):
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "codex_pr_review.yml" not in content
        assert "run_codex_pr_review.py" not in content
        assert "CODEX_PR_REVIEW_API_FALLBACK_ENABLED" not in content
        assert "CODEX_PR_REVIEW_DIRECT_API_PRIMARY_ENABLED" not in content


def test_retired_pr_reviewer_is_not_authorized_by_deployment_defaults() -> None:
    for relative_path in (
        ".github/workflows/vps_codex_service_ops.yml",
        "scripts/deploy_codex_audit_service.sh",
    ):
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "codex_pr_review.yml@" not in content
        assert "86458c44b06593b6d7a1602b3c38e7a1c143ef17" not in content


def test_schwab_dependency_lane_is_the_narrow_service_exception() -> None:
    workflow = (ROOT / ".github/workflows/dependency_audit.yml").read_text(encoding="utf-8")
    assert "0 */6 * * *" in workflow
    assert "DEPENDENCY_AUDIT_ENABLED" in workflow
    assert "QuantStrategyLab/SchwabTokenAutoRefresher" in workflow or "SchwabTokenAutoRefresher" in workflow
    assert "permission-actions: read" in workflow
    assert "pull-requests: write" in workflow
    assert "dry_run" in workflow

    for relative_path in ("README.md", "README.zh-CN.md"):
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "SchwabTokenAutoRefresher" in content
        assert "dependency" in content.lower() or "依赖" in content
        assert "GitHub Codex App" in content or "GitHub Codex App" in content
