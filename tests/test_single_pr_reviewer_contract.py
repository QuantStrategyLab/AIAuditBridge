from __future__ import annotations

import json
from pathlib import Path

from service.org_health import DEFAULT_WORKFLOW_ALLOWLIST


ROOT = Path(__file__).resolve().parents[1]
RETIRED_PATHS = (
    ".github/workflows/codex_pr_review.yml",
    "prompts/pr_review.md",
    "scripts/run_codex_pr_review.py",
    "tests/test_run_codex_pr_review.py",
)


def test_github_codex_app_is_the_only_ai_pr_reviewer() -> None:
    for relative_path in RETIRED_PATHS:
        assert not (ROOT / relative_path).exists(), relative_path

    actionlint_config = (ROOT / ".github/actionlint.yaml").read_text(encoding="utf-8")
    assert "codex_pr_review" not in actionlint_config

    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / ".github/workflows").glob("*.yml")
    )
    assert "name: Codex PR Review" not in workflow_text
    assert "name: Codex Review Gate" in workflow_text
    assert (ROOT / ".github/workflows/codex_audit.yml").is_file()
    assert (ROOT / ".github/workflows/monthly-orchestrator.yml").is_file()


def test_retired_pr_reviewer_is_not_advertised_as_active() -> None:
    policy = json.loads(
        (ROOT / ".github/codex_auto_merge_policy.json").read_text(encoding="utf-8")
    )
    assert "pr_review" not in policy
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
