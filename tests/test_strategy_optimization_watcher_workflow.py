from __future__ import annotations

from pathlib import Path
import unittest


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/strategy_optimization_watcher.yml"


class StrategyOptimizationWatcherWorkflowTest(unittest.TestCase):
    def test_workflow_is_issue_only_and_dry_run_by_default(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("name: Strategy Optimization Watcher", text)
        self.assertIn("default: true", text)
        self.assertIn("STRATEGY_WATCH_DRY_RUN", text)
        self.assertIn("python -m scripts.run_strategy_optimization_watcher", text)
        self.assertIn("permission-issues: write", text)
        self.assertNotIn("pull-request", text.lower())
        self.assertNotIn("auto_merge", text.lower())
        self.assertNotIn("deploy", text.lower())

    def test_workflow_uses_source_metrics_checkout(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("SOURCE_REPO", text)
        self.assertIn("QuantStrategyLab/CryptoLivePoolPipelines", text)
        self.assertIn("QuantStrategyLab/UsEquitySnapshotPipelines", text)
        self.assertIn("STRATEGY_WATCH_ALLOWED_SOURCE_REPOS", text)
        self.assertIn("STRATEGY_WATCH_ALLOWED_SOURCE_REFS", text)
        self.assertIn("SOURCE_REF is not allowed", text)
        self.assertNotIn("vars.STRATEGY_WATCH_SOURCE_REPO || github.repository", text)
        self.assertIn("METRICS_PATH", text)
        self.assertIn("SOURCE_WORKFLOW_FILE", text)
        self.assertIn("METRICS_FILENAME", text)
        self.assertIn("build_strategy_watcher_artifact_payload.py", text)
        self.assertIn("python -m scripts.run_research_task_diagnosis", text)
        self.assertIn("Two completed observations are required", text)
        self.assertIn("path: source", text)
        self.assertIn("STRATEGY_WATCH_SOURCE_ROOT: ${{ github.workspace }}/source", text)
        self.assertIn("STRATEGY_WATCH_METRICS_PATH: ${{ env.METRICS_PATH }}", text)
        self.assertIn("actions/create-github-app-token", text)
        self.assertIn("permission-actions: read", text)
        self.assertIn("format('{0}', inputs.dry_run)", text)
        self.assertNotIn("github.event.inputs.dry_run ||", text)

    def test_workflow_fails_closed_for_cross_repo_without_app_token(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", text)
        self.assertIn("Verify Source Repository Token", text)
        self.assertIn("${SOURCE_REPO}" + '" != "' + "${GITHUB_REPOSITORY}", text)
        self.assertIn("Cross-repository strategy watcher requires", text)
        self.assertIn("SOURCE_REPO is not allowed", text)
        self.assertIn("owner=${owner}", text)
        self.assertIn("owner: ${{ steps.source_repo.outputs.owner }}", text)

    def test_research_task_index_is_scheduled_and_uses_a_dedicated_sync_identity(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("qsl_research_task_source_snapshot.v1", text)
        self.assertIn("QSL_RESEARCH_TASK_SYNC_URL", text)
        self.assertIn("QSL_RESEARCH_TASK_SYNC_TOKEN", text)
        self.assertIn("github.event_name == 'schedule'", text)
        self.assertIn("/api/internal/sync-research-task-source", text)
        self.assertIn("RESEARCH_TASK_SYNC_STATUS=NOT_CONFIGURED", text)
        self.assertNotIn("/api/switch", text)


if __name__ == "__main__":
    unittest.main()
