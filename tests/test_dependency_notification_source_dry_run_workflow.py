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

    def test_invokes_existing_source_cli_only(self) -> None:
        self.assertIn(
            "python3 -m scripts.run_dependency_notification_source_dry_run",
            self.workflow,
        )
        # Allowlist must enter the step via env, not shell interpolation of inputs.
        self.assertIn("REPO_ALLOWLIST: ${{ inputs.repos }}", self.workflow)
        self.assertIn('--repos "$REPO_ALLOWLIST"', self.workflow)
        self.assertNotIn('--repos "${{ inputs.repos }}"', self.workflow)
        run_script = re.search(
            r"(?ms)^\s{8}run:\s*\|\n((?:^\s{10}.*\n?)*)",
            self.workflow,
        )
        self.assertIsNotNone(run_script)
        assert run_script is not None
        self.assertNotIn("${{ inputs.repos }}", run_script.group(1))
        self.assertNotIn("${{", run_script.group(1))
        self.assertIn("set -euo pipefail", self.workflow)
        # Failures must surface; do not swallow non-zero exits.
        self.assertNotIn("continue-on-error:", self.workflow)
        self.assertNotIn("|| true", self.workflow)

    def test_reuses_existing_tokens_without_new_secrets_or_echo(self) -> None:
        self.assertIn("secrets.CODEX_AUDIT_GH_TOKEN", self.workflow)
        self.assertIn("secrets.GH_TOKEN", self.workflow)
        self.assertIn("secrets.GITHUB_TOKEN", self.workflow)
        self.assertNotRegex(self.workflow, r"echo:.*TOKEN|print.*TOKEN", re.I)
        # No new secret names or App token minting in this verification entry.
        self.assertNotIn("create-github-app-token", self.workflow)
        self.assertNotIn("CROSS_REPO_GITHUB_APP", self.workflow)

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
