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


def test_engineering_review_uses_approved_default_off_source_only_producer() -> None:
    from scripts import run_monthly_codex_audit as monthly
    from service import ai_gateway_service as gateway

    assert gateway.ENGINEERING_REVIEW_PURPOSE == "engineering_evidence_resume"
    assert gateway.ENGINEERING_REVIEW_TASK == "pr_review"
    assert gateway.ENGINEERING_REVIEW_ENABLED_ENV == "CODEX_AUDIT_SERVICE_ENGINEERING_REVIEW_ENABLED"
    assert callable(monthly.resume_engineering_evidence_review)
    assert callable(monthly.parse_args)
    assert monthly.engineering_review_client_enabled() is False
    assert monthly.engineering_pr_review_client_enabled() is False
    assert monthly.ENGINEERING_EVIDENCE_RESUME_OPERATION == "engineering_evidence_resume"
    assert monthly.ENGINEERING_PR_REVIEW_OPERATION == "engineering_pr_review"
    assert "QuantStrategyLab/AIAuditBridge" not in monthly.SOURCE_REPO_TASKS

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "approved AI reviewer for ordinary engineering PRs is the existing cloud Codex service" in readme
    assert "GitHub Codex App is no longer the designated reviewer" in readme
    assert "engineering_evidence_resume" in readme
    assert "CODEX_AUDIT_ENGINEERING_REVIEW_ENABLED" in readme
    assert "default off" in readme.lower() or "default-off" in readme.lower() or "disabled by default" in readme.lower()
    # Must not claim the switch itself already works in production.
    assert "already enabled" not in readme.lower()
    assert "switch itself already works" not in readme.lower()
    assert "reviewer responsibility change is approved" in readme
    assert "has not been deployed or validated with a real producer run" in readme
    assert "Future production use still requires an explicit single-reviewer authorization" not in readme
    # Must not restore the retired always-on PR reviewer workflow.
    assert "codex_pr_review.yml" not in readme
    assert "run_codex_pr_review.py" not in readme

    workflow = (ROOT / ".github/workflows/engineering_pr_review.yml").read_text(encoding="utf-8")
    assert "github.repository == 'QuantStrategyLab/AIAuditBridge'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "type: number" in workflow
    assert "--pull-request-number" in workflow and "--ci-run-id" in workflow
    assert "validation_evidence:" not in workflow and "recovery_evidence:" not in workflow
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "contents: read" in workflow and "actions: read" in workflow
    assert "pull-requests: read" in workflow and "id-token: write" in workflow
    assert 'CODEX_AUDIT_MODE: review_only' in workflow
    assert 'CODEX_AUDIT_AUTO_MERGE: "false"' in workflow
    assert "pull-requests: write" not in workflow
    assert "codex_audit.yml" not in workflow

    deploy = (ROOT / "scripts/deploy_codex_audit_service.sh").read_text(encoding="utf-8")
    ops = (ROOT / ".github/workflows/vps_codex_service_ops.yml").read_text(encoding="utf-8")
    assert 'ENGINEERING_REVIEW_ENABLED="${CODEX_AUDIT_SERVICE_ENGINEERING_REVIEW_ENABLED:-false}"' in deploy
    assert 'ENGINEERING_REVIEW_WORKFLOW_SHA="${CODEX_AUDIT_SERVICE_ENGINEERING_REVIEW_WORKFLOW_SHA:-}"' in deploy
    assert "engineering review requires an exact CODEX_AUDIT_SERVICE_ENGINEERING_REVIEW_WORKFLOW_SHA" in deploy
    assert "CODEX_AUDIT_SERVICE_ENGINEERING_REVIEW_ENABLED: ${{ vars.CODEX_AUDIT_SERVICE_ENGINEERING_REVIEW_ENABLED || 'false' }}" in ops
    assert "CODEX_AUDIT_SERVICE_ENGINEERING_REVIEW_WORKFLOW_SHA: ${{ vars.CODEX_AUDIT_SERVICE_ENGINEERING_REVIEW_WORKFLOW_SHA || '' }}" in ops
    assert "QuantStrategyLab/AIAuditBridge/.github/workflows/engineering_pr_review.yml@refs/heads/main" in deploy
    assert "QuantStrategyLab/AIAuditBridge/.github/workflows/engineering_pr_review.yml@refs/heads/main" in ops

    chinese_readme = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "职责调整虽已批准" in chinese_readme
    assert "source-only 结果不能解除" in chinese_readme
    assert "尚未部署此配置" in chinese_readme
