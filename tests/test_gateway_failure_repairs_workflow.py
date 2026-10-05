"""Verify the real release workflow guards without GitHub or host operations."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shlex
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/vps_codex_service_ops.yml"
MODE = "release-gateway-failure-repairs"


def _job(workflow: str, name: str) -> str:
    match = re.search(r"^  " + re.escape(name) + r":\n.*?(?=^  [a-z][a-z0-9-]*:\n|\Z)", workflow, re.M | re.S)
    if match is None:
        raise AssertionError(f"Missing workflow job: {name}")
    return match.group()


def _script(job: str, step: str) -> str:
    body = job.split(f"      - name: {step}\n", 1)[1]
    return textwrap.dedent(body.split("        run: |\n", 1)[1].split("      - name:", 1)[0])


class GatewayFailureRepairsWorkflowTests(unittest.TestCase):
    def test_release_is_separate_acknowledged_exact_main_protected_mode(self):
        workflow = WORKFLOW.read_text()
        self.assertIn(f"          - {MODE}\n", workflow)
        generic = _job(workflow, "vps-codex-service-ops")
        self.assertIn(f"inputs.mode != '{MODE}'", generic)
        job = _job(workflow, MODE)
        expected = (
            "github.event_name == 'workflow_dispatch' && github.repository == 'QuantStrategyLab/AIAuditBridge' "
            "&& github.ref == 'refs/heads/main' && inputs.mode == 'release-gateway-failure-repairs' "
            "&& inputs.acknowledge_interruption"
        )
        self.assertIn(f"    if: {expected}\n", job)
        for value in (
            "environment: codex-vps-ops", "- self-hosted", "- codex-vps", "timeout-minutes: 8",
            "persist-credentials: false", "ref: ${{ github.sha }}",
            "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10",
        ):
            self.assertIn(value, job)
        self.assertEqual(job.count("      - name:"), 3)
        self.assertLess(job.index("current_main="), job.index("release_gateway_failure_repairs.py\" release"))
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertIn("concurrency:\n  group: vps-codex-service-ops\n  cancel-in-progress: false\n", workflow)
        for forbidden in (
            "secrets.", "deploy_codex_audit_service.sh", "retry-once", "repair-ssh", "chmod", "chown",
            "systemctl", "nginx", "environment-file", "ssh_unban_ip", "id-token:", "curl", "wget",
        ):
            self.assertNotIn(forbidden, job)

    def test_actual_main_gate_accepts_only_clean_current_bound_acknowledged_source(self):
        job = _job(WORKFLOW.read_text(), MODE)
        script = _script(job, "Verify exact main and reviewed release bytes before acknowledged maintenance")
        # All external metadata/digest commands are shell fixtures. Success is
        # reached only after the real workflow's complete gate succeeds.
        fixtures = r'''
        git() {
          case "$*" in
            'rev-parse HEAD') [ "$TEST_GIT_HEAD_FAIL" = 0 ] || return 1; printf '%s\n' "$TEST_HEAD" ;;
            'status --porcelain --untracked-files=all') [ "$TEST_GIT_STATUS_FAIL" = 0 ] || return 1; printf '%s' "$TEST_DIRTY" ;;
            *) return 99 ;;
          esac
        }
        gh() {
          [ "$*" = 'api repos/QuantStrategyLab/AIAuditBridge/git/ref/heads/main --jq .object.sha' ] || return 99
          [ "$TEST_GH_FAIL" = 0 ] || return 1
          printf '%s\n' "$TEST_MAIN"
        }
        timeout() { [ "$1" = 30s ] || return 99; shift; "$@"; }
        sha256sum() { [ "$*" = '--check --status' ] || return 99; cat >/dev/null; [ "$TEST_DIGEST_FAIL" = 0 ]; }
        '''
        sha = "a" * 40
        env = {
            "PATH": "/usr/bin:/bin", "GITHUB_WORKSPACE": str(ROOT),
            "RUN_REPOSITORY": "QuantStrategyLab/AIAuditBridge", "RUN_REF": "refs/heads/main",
            "RUN_SHA": sha, "RUN_WORKFLOW_SHA": sha, "RUN_EVENT_NAME": "workflow_dispatch", "RUN_MODE": MODE,
            "RUN_WORKFLOW_REF": "QuantStrategyLab/AIAuditBridge/.github/workflows/vps_codex_service_ops.yml@refs/heads/main",
            "ACKNOWLEDGE_INTERRUPTION": "true", "TEST_HEAD": sha, "TEST_MAIN": sha, "TEST_DIRTY": "",
            "TEST_GH_FAIL": "0", "TEST_GIT_HEAD_FAIL": "0", "TEST_GIT_STATUS_FAIL": "0", "TEST_DIGEST_FAIL": "0",
        }
        command = ["/bin/bash", "-c", textwrap.dedent(fixtures) + script + "\necho RELEASE_BOUND\n"]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "RELEASE_BOUND")
        failures = (
            ("ACKNOWLEDGE_INTERRUPTION", "false"), ("ACKNOWLEDGE_INTERRUPTION", ""),
            ("RUN_REPOSITORY", "other/repository"), ("RUN_REF", "refs/heads/spoof"),
            ("RUN_EVENT_NAME", "push"), ("RUN_MODE", "retry-audit-patch-once"),
            ("RUN_SHA", "$(echo caller-command)"), ("RUN_SHA", "A" * 40), ("RUN_SHA", "a" * 39),
            ("RUN_SHA", sha + "\n"), ("RUN_WORKFLOW_SHA", "b" * 40),
            ("RUN_WORKFLOW_REF", "QuantStrategyLab/AIAuditBridge/.github/workflows/other.yml@refs/heads/main"),
            ("RUN_WORKFLOW_REF", "QuantStrategyLab/AIAuditBridge/.github/workflows/vps_codex_service_ops.yml@refs/heads/spoof"),
            ("TEST_HEAD", "b" * 40), ("TEST_MAIN", "b" * 40), ("TEST_MAIN", ""),
            ("TEST_DIRTY", " M scripts/source.py"), ("TEST_DIRTY", "?? injected.py"),
            ("TEST_GH_FAIL", "1"), ("TEST_GIT_HEAD_FAIL", "1"), ("TEST_GIT_STATUS_FAIL", "1"),
            ("TEST_DIGEST_FAIL", "1"),
        )
        for key, value in failures:
            with self.subTest(key=key, value=value):
                result = subprocess.run(command, env={**env, key: value}, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("RELEASE_BOUND", result.stdout)

    def test_host_entrypoint_has_fixed_isolated_arguments_and_reviewed_bytes(self):
        job = _job(WORKFLOW.read_text(), MODE)
        script = _script(job, "Release fixed gateway with create-only receipt and code-only rollback")
        self.assertEqual(shlex.split(script.replace("\\\n", "")), [
            "set", "-euo", "pipefail", "env", "-i", "PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C",
            "/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B",
            "$GITHUB_WORKSPACE/scripts/release_gateway_failure_repairs.py", "release", "--acknowledge-interruption",
        ])
        hashes = {
            "scripts/release_gateway_failure_repairs.py": "551b219111a4df19e58e0f7f8dc93ab5507d95f05130b6a97b7442e5d1b0be0a",
            "scripts/manage_codex_audit_service_patch.py": "e0e4367eefe8c6fb9ea6c973716890145957b3466e22c60e2714035e292424c8",
            "service/ai_gateway_service.py": "60fc288f0135720e1a26daf897ad5c898cada7a884753a6e6495aaf6de4f7ec0",
            "service/adapters/codex_adapter.py": "e5175a5fb2c132b24dfc59a0af365c5af8ba2f3b8f6079fe398f97111c347cd0",
        }
        for name, digest in hashes.items():
            with self.subTest(path=name):
                self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)
                self.assertIn(f"{digest}  {name}\n", job)
        # Run only the extracted checksum command against fixture checkout
        # files; no host paths, tokens, service CLI or network are touched.
        gate = _script(job, "Verify exact main and reviewed release bytes before acknowledged maintenance")
        digests = gate[gate.index('cd "$GITHUB_WORKSPACE"'):]
        result = subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + digests],
                                env={"PATH": "/usr/bin:/bin", "GITHUB_WORKSPACE": str(ROOT)},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_consumed_legacy_operation_jobs_remain_byte_for_byte(self):
        workflow = WORKFLOW.read_text()
        for name, digest in (
            ("apply-audit-patch", "a9b45bab162ee700d666290629f5924e3f9a52991efa5f70b3c49055fc3f132c"),
            ("retry-audit-patch-once", "1f97e782e7cc7f9df6b651f1b1fe8179977311ec4ba72556d0a6644577a1718e"),
            ("inspect-audit-patch", "08df738fb4bfaa8634f17f48d713aa85411633216ce908bf64fb0b7432e8d764"),
        ):
            with self.subTest(job=name):
                self.assertEqual(hashlib.sha256(_job(workflow, name).encode()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
