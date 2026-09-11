from __future__ import annotations

from pathlib import Path
import unittest


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/strategy_optimization_watcher.yml"


class StrategyOptimizationWatcherWorkflowTest(unittest.TestCase):
    def test_workflow_is_issue_only_and_dry_run_by_default(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("name: Strategy Optimization Watcher", text)
        self.assertIn('cron: "17 6 * * *"', text)
        self.assertIn('cron: "23 6 * * *"', text)
        self.assertIn("github.event.schedule == '23 6 * * *'", text)
        self.assertIn("soxl-p1-p3-daily-research.yml", text)
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
        self.assertIn("STRATEGY_WATCH_TERMINAL_STATUS_PATH", text)
        self.assertIn("p1-status.json", text)
        self.assertIn("soxl_soxx_trend_income", text)
        self.assertIn("python -m scripts.run_research_task_diagnosis", text)
        self.assertIn("Two completed observations are required", text)
        self.assertIn("--limit 5", text)
        self.assertIn('current_run_id="${RUN_IDS[0]}"', text)
        self.assertIn("--baseline-candidate", text)
        self.assertNotIn('baseline_run_id="${RUN_IDS[1]}"', text)
        self.assertIn('terminal_file=$(find "source/data/output/_artifacts/${current_run_id}"', text)
        self.assertIn("resolve_input_path(source_root=sys.argv[1], metrics_path=sys.argv[2])", text)
        self.assertNotIn('rm -f "source/${METRICS_PATH}"', text)
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

    def test_exact_soxl_task_hands_off_only_sanitized_artifacts_to_vps_consumer(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("soxl-watcher-learning-ready", text)
        self.assertIn("needs.strategy-optimization-watcher.outputs.soxl_learning_ready == 'true'", text)
        self.assertIn("vars.SOXL_WATCHER_GCP_WIF_PROVIDER != ''", text)
        self.assertIn("vars.SOXL_WATCHER_GCP_PROJECT_ID != ''", text)
        self.assertLess(text.index("vars.SOXL_WATCHER_GCP_WIF_PROVIDER != ''"), text.index("runs-on: [self-hosted, codex-vps]"))
        self.assertIn("runs-on: [self-hosted, codex-vps]", text)
        self.assertIn("python -m scripts.run_soxl_manual_learning --watcher-result", text)
        self.assertIn("strategy-optimization-watcher-${{ github.run_id }}", text)
        self.assertIn("GH_TOKEN: ${{ steps.source_app_token.outputs.token || github.token }}", text)
        self.assertIn("soxl-p1-p3/${P1_MANIFEST_SHA256}", text)
        self.assertIn("--body-file", Path(__file__).resolve().parents[1].joinpath("scripts/run_soxl_manual_learning.py").read_text())
        self.assertNotIn("bars.json=${{ needs.", text)

    def test_watcher_validation_is_version_gated_and_keeps_frozen_learning_runtime(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("SOXL_WATCHER_VALIDATION_CONSUMER_REVISION", text)
        self.assertIn("^[0-9a-f]{40}$", text)
        self.assertIn("--watcher-preflight", text)
        self.assertIn('validation_args+=(--watcher-validation)', text)
        self.assertIn("ref: b03ecbe4e0a7a0de22f298499f867a7039e4b60a", text)
        self.assertIn("ref: 7363011d56926d39f4fffeb036e511391114e39f", text)
        self.assertIn("path: validation-consumer-source", text)
        self.assertIn('"$LEARNING_ROOT/validation-control/bin/python" -m scripts.run_soxl_manual_learning', text)
        self.assertIn('--consumer-source "$GITHUB_WORKSPACE/validation-consumer-source"', text)
        self.assertIn('--output "$LEARNING_ROOT/validation.json"', text)
        self.assertIn("name: soxl-watcher-validation-${{ github.run_id }}-${{ github.run_attempt }}", text)
        self.assertIn("vars.SOXL_WATCHER_VALIDATION_CONSUMER_REVISION != ''", text)


if __name__ == "__main__":
    unittest.main()
