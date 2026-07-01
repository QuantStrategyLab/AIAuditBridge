from __future__ import annotations

from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/monthly-orchestrator.yml"


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_monthly_orchestrator_has_repository_variable_defaults() -> None:
    text = workflow_text()

    assert "vars.AUDIT_TARGET_REPOS ||" in text
    assert "QuantStrategyLab/HkEquitySnapshotPipelines" in text
    assert "QuantStrategyLab/UsEquitySnapshotPipelines" in text
    assert "vars.AUDIT_MONTHLY_LABEL || 'monthly-review'" in text
    assert "vars.AUDIT_AUTO_MERGE_LABEL || 'auto-merge-ok'" in text
    assert "vars.AUDIT_REVIEW_TITLE_PREFIX || 'Monthly Audit Review'" in text


def test_monthly_orchestrator_uses_resolved_month_output() -> None:
    text = workflow_text()

    assert "id: resolve-month" in text
    assert "MONTH_OVERRIDE: ${{ steps.resolve-month.outputs.month }}" in text
    assert "core.setOutput('month', stamp)" in text


def test_monthly_orchestrator_does_not_dispatch_without_source_issue() -> None:
    text = workflow_text()

    assert "peter-evans/repository-dispatch" not in text
    assert "event-type: monthly-audit" not in text
    assert "CodexAuditBridge execution requires a source repository issue number." in text
    assert "source_repo and issue_number" in text
