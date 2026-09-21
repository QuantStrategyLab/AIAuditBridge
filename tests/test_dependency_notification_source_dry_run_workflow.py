"""YAML contract for the manual source dry-run verification workflow."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / ".github/workflows/dependency_notification_source_dry_run.yml"
)

_DANGEROUS_PERMISSIONS = (
    "contents: write",
    "pull-requests: write",
    "issues: write",
    "id-token: write",
    "id-token: read",
    "actions: write",
    "packages: write",
    "deployments: write",
    "administration:",
    "permission-contents: write",
    "permission-pull-requests: write",
    "permission-issues: write",
    "permission-actions: write",
    "permission-administration:",
    "permission-id-token:",
)

_FORBIDDEN_NEW_SECRET_NAMES = (
    "DEPENDENCY_NOTIFICATION",
    "SOURCE_DRY_RUN",
    "CROSS_REPO_READONLY",
    "APP_TOKEN_SECRET",
)


def _top_level_block(workflow: str, key: str) -> str:
    lines = workflow.splitlines()
    start = lines.index(f"{key}:")
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "\t")):
            break
        block.append(line)
    return "\n".join(block)


def _step_named(workflow: str, name: str) -> str:
    # Multiline only (not DOTALL): each body line is exactly 8+ spaces.
    pattern = re.compile(
        rf"(?m)^\s{{6}}- name: {re.escape(name)}\n((?:^\s{{8}}.*\n)*)",
    )
    match = pattern.search(workflow)
    if match is None:
        raise AssertionError(f"missing step: {name}")
    return match.group(0)


class DependencyNotificationSourceDryRunWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_only_workflow_dispatch_trigger(self) -> None:
        on_block = _top_level_block(self.workflow, "on")
        self.assertIn("workflow_dispatch:", on_block)
        self.assertNotIn("schedule:", on_block)
        self.assertNotIn("push:", on_block)
        self.assertNotIn("pull_request:", on_block)
        # Whole file: no alternate event hooks.
        self.assertNotIn("\nschedule:", self.workflow)
        self.assertNotIn("\npush:", self.workflow)
        self.assertNotIn("\npull_request:", self.workflow)
        self.assertNotRegex(self.workflow, r"(?m)^  cron:")

    def test_minimal_read_permissions_only(self) -> None:
        perms = _top_level_block(self.workflow, "permissions").rstrip()
        self.assertEqual(
            perms,
            "permissions:\n  contents: read\n  pull-requests: read\n  actions: read",
        )
        for dangerous in _DANGEROUS_PERMISSIONS:
            self.assertNotIn(dangerous, self.workflow)

    def test_required_repos_allowlist_input_has_no_default_scan(self) -> None:
        self.assertIn("repos:", self.workflow)
        self.assertIn("required: true", self.workflow)
        # No default that would imply empty allowlist scanning.
        self.assertNotRegex(
            self.workflow,
            r"(?ms)repos:.*?default:\s*[\"']?[^\"'\n]+",
        )
        self.assertIn("empty", self.workflow.lower())
        # Input must stay required without a default key under repos.
        repos_block = re.search(
            r"(?ms)^\s{6}repos:.*?(?=^\s{4}\S|\Z)",
            self.workflow,
        )
        self.assertIsNotNone(repos_block)
        assert repos_block is not None
        self.assertNotIn("default:", repos_block.group(0))
        self.assertIn("required: true", repos_block.group(0))

    def test_quantstrategylab_owner_validation_rejects_illegal_inputs(self) -> None:
        validate = _step_named(self.workflow, "Validate QuantStrategyLab allowlist")
        self.assertIn("REPO_ALLOWLIST: ${{ inputs.repos }}", validate)
        self.assertIn("QuantStrategyLab/", validate)
        self.assertIn("^QuantStrategyLab/[A-Za-z0-9_.-]+$", validate)
        self.assertIn("multi-repo allowlist forbidden", validate)
        self.assertIn("empty forbidden", validate)
        # Unvalidated inputs must not be interpolated into shell/${{ }} beyond env.
        run_script = re.search(
            r"(?m)^\s{8}run:\s*\|\n((?:^\s{10}.*\n)*)",
            validate,
        )
        self.assertIsNotNone(run_script)
        assert run_script is not None
        self.assertNotIn("${{ inputs.repos }}", run_script.group(1))
        # Shell may use ${VAR}; GitHub expressions must not appear inside run:.
        self.assertNotIn("${{", run_script.group(1))
        # Hardcoded owner for token minting; repository name comes from validated output.
        token_step = _step_named(self.workflow, "Create read-only cross-repo App token")
        self.assertIn("owner: QuantStrategyLab", token_step)
        self.assertIn(
            "repositories: ${{ steps.allowlist.outputs.repository }}",
            token_step,
        )
        self.assertNotIn("repositories: ${{ inputs.repos }}", self.workflow)
        self.assertNotIn("owner: ${{ inputs", self.workflow)

    def test_invokes_existing_source_cli_only(self) -> None:
        self.assertIn(
            "python3 -m scripts.run_dependency_notification_source_dry_run",
            self.workflow,
        )
        self.assertIn('--repos "$REPO_ALLOWLIST"', self.workflow)
        self.assertNotIn('--repos "${{ inputs.repos }}"', self.workflow)
        self.assertIn("REPO_ALLOWLIST: ${{ steps.allowlist.outputs.full }}", self.workflow)
        self.assertIn("set -euo pipefail", self.workflow)
        # Failures must surface; do not swallow non-zero exits.
        self.assertNotIn("continue-on-error:", self.workflow)
        self.assertNotIn("|| true", self.workflow)

    def test_reuses_existing_app_credentials_without_new_secrets_or_echo(self) -> None:
        self.assertIn("vars.CROSS_REPO_GITHUB_APP_ID", self.workflow)
        self.assertIn("secrets.CROSS_REPO_GITHUB_APP_PRIVATE_KEY", self.workflow)
        self.assertIn("actions/create-github-app-token@v3.2.0", self.workflow)
        # Self-repo path may still reference existing org/workflow tokens.
        self.assertIn("secrets.CODEX_AUDIT_GH_TOKEN", self.workflow)
        self.assertIn("secrets.GH_TOKEN", self.workflow)
        self.assertIn("secrets.GITHUB_TOKEN", self.workflow)
        self.assertNotRegex(self.workflow, r"echo:.*TOKEN|print.*TOKEN", re.I)
        for forbidden in _FORBIDDEN_NEW_SECRET_NAMES:
            self.assertNotIn(forbidden, self.workflow)
        # No alternate new App secret names.
        secret_refs = re.findall(r"secrets\.([A-Z0-9_]+)", self.workflow)
        self.assertEqual(
            set(secret_refs),
            {
                "CROSS_REPO_GITHUB_APP_PRIVATE_KEY",
                "CODEX_AUDIT_GH_TOKEN",
                "GH_TOKEN",
                "GITHUB_TOKEN",
            },
        )

    def test_app_token_permissions_are_read_only(self) -> None:
        token_step = _step_named(self.workflow, "Create read-only cross-repo App token")
        self.assertIn("permission-contents: read", token_step)
        self.assertIn("permission-pull-requests: read", token_step)
        self.assertIn("permission-actions: read", token_step)
        for write_perm in (
            "permission-contents: write",
            "permission-pull-requests: write",
            "permission-issues: write",
            "permission-actions: write",
            "permission-administration",
            "permission-id-token",
            "permission-statuses: write",
            "permission-checks: write",
        ):
            self.assertNotIn(write_perm, token_step)
        # Token minting must not continue-on-error / fallback to GITHUB_TOKEN.
        self.assertNotIn("continue-on-error:", token_step)
        verify = _step_named(self.workflow, "Verify cross-repo App token")
        self.assertIn("refusing GITHUB_TOKEN fallback", verify)
        self.assertIn("exit 1", verify)
        creds = _step_named(self.workflow, "Detect cross-repo GitHub App credentials")
        self.assertIn("exit 1", creds)
        self.assertIn("CROSS_REPO_GITHUB_APP_ID", creds)

    def test_cross_repo_injects_only_app_token_self_repo_keeps_github_token(self) -> None:
        self_step = _step_named(
            self.workflow,
            "Run dependency notification source dry-run (self-repo)",
        )
        self.assertIn("if: steps.allowlist.outputs.cross_repo == 'false'", self_step)
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", self_step)
        cross_step = _step_named(
            self.workflow,
            "Run dependency notification source dry-run (cross-repo)",
        )
        self.assertIn("if: steps.allowlist.outputs.cross_repo == 'true'", cross_step)
        self.assertIn(
            "CODEX_AUDIT_GH_TOKEN: ${{ steps.source_app_token.outputs.token }}",
            cross_step,
        )
        # Ignore comments that mention GITHUB_TOKEN/GH_TOKEN as non-goals.
        cross_env = "\n".join(
            line
            for line in cross_step.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotRegex(cross_env, r"(?m)^\s+GITHUB_TOKEN:")
        self.assertNotRegex(cross_env, r"(?m)^\s+GH_TOKEN:")
        self.assertNotIn("secrets.CODEX_AUDIT_GH_TOKEN", cross_env)

    def test_no_live_send_model_ledger_or_deploy(self) -> None:
        # Ignore `#` comments; they may document non-goals without wiring them.
        executable = "\n".join(
            line
            for line in self.workflow.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ).lower()
        for needle in (
            "telegram",
            "dispatch_briefing",
            "automation_run_ledger",
            "openai",
            "anthropic",
            "codex_audit_service",
            "gcloud",
            "deploy",
            "placeorder",
            "broker",
        ):
            self.assertNotIn(needle, executable)


if __name__ == "__main__":
    unittest.main()
