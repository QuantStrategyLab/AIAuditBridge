"""Regression tests for reusable-workflow OIDC trust boundaries."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
import re
from unittest.mock import patch

from service import auth


class ReusableWorkflowOidcAuthTests(unittest.TestCase):
    def _verify(self, payload: dict[str, object], env: dict[str, str]) -> dict[str, object]:
        with (
            patch.dict("os.environ", env, clear=True),
            patch.object(auth, "_jwt_parts", return_value=({"alg": "RS256", "kid": "1"}, payload, b"x", b"y")),
            patch.object(auth, "_load_jwks", return_value={"keys": [{"kid": "1", "kty": "RSA"}]}),
            patch.object(auth, "_verify_rs256", return_value=None),
        ):
            return auth.verify_github_oidc("header.payload.signature")

    def test_non_direct_caller_requires_trusted_reusable_workflow(self) -> None:
        qpk_job_ref = (
            "QuantStrategyLab/QuantPlatformKit/.github/workflows/"
            "reusable-drift-check.yml@644cd9002ae92f2aaca6f7efb4afa4986fae05ea"
        )
        payload: dict[str, object] = {
            "aud": "quant-codex-audit",
            "iss": auth.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/QuantRuntimeSettings",
            "workflow_ref": "QuantStrategyLab/QuantRuntimeSettings/.github/workflows/ci.yml@refs/heads/main",
            "ref": "refs/heads/main",
            "repository_visibility": "public",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge,QuantStrategyLab/QuantRuntimeSettings",
            "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": "QuantStrategyLab/QuantRuntimeSettings/.github/workflows/ci.yml@refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS": qpk_job_ref,
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES": "public",
        }

        with self.assertRaisesRegex(PermissionError, "job workflow ref is required"):
            self._verify(payload, env)

        payload["job_workflow_ref"] = qpk_job_ref
        self.assertEqual(self._verify(payload, env)["repository"], "QuantStrategyLab/QuantRuntimeSettings")

    def test_retired_audit_bridge_sha_is_not_allowlisted(self) -> None:
        retired_job_ref = (
            "QuantStrategyLab/AIAuditBridge/.github/workflows/"
            "codex_pr_review.yml@86458c44b06593b6d7a1602b3c38e7a1c143ef17"
        )
        qpk_job_ref = (
            "QuantStrategyLab/QuantPlatformKit/.github/workflows/"
            "reusable-drift-check.yml@644cd9002ae92f2aaca6f7efb4afa4986fae05ea"
        )
        payload: dict[str, object] = {
            "aud": "quant-codex-audit",
            "iss": auth.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/QuantRuntimeSettings",
            "workflow_ref": "QuantStrategyLab/QuantRuntimeSettings/.github/workflows/ci.yml@refs/heads/main",
            "job_workflow_ref": retired_job_ref,
            "ref": "refs/heads/main",
            "repository_visibility": "public",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/QuantRuntimeSettings",
            "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": "QuantStrategyLab/QuantRuntimeSettings/.github/workflows/ci.yml@refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS": qpk_job_ref,
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES": "public",
        }

        with self.assertRaisesRegex(PermissionError, "job workflow ref is not allowed"):
            self._verify(payload, env)

    def test_direct_audit_bridge_caller_does_not_require_reusable_workflow(self) -> None:
        payload: dict[str, object] = {
            "aud": "quant-codex-audit",
            "iss": auth.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/AIAuditBridge",
            "workflow_ref": "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main",
            "ref": "refs/heads/main",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS": "",
        }
        self.assertEqual(self._verify(payload, env)["repository"], "QuantStrategyLab/AIAuditBridge")

        payload["job_workflow_ref"] = ""
        with self.assertRaisesRegex(PermissionError, "cannot be empty"):
            self._verify(payload, env)

        payload["job_workflow_ref"] = 0
        with self.assertRaisesRegex(PermissionError, "must be a string"):
            self._verify(payload, env)

        payload.pop("job_workflow_ref")
        payload.pop("workflow_ref")
        with self.assertRaisesRegex(PermissionError, "workflow_ref is missing"):
            self._verify(payload, env)

        payload["workflow_ref"] = False
        with self.assertRaisesRegex(PermissionError, "workflow_ref must be a string"):
            self._verify(payload, env)

    def test_global_etf_codegen_workflow_is_direct_audit_bridge_caller(self) -> None:
        workflow_ref = (
            "QuantStrategyLab/AIAuditBridge/.github/workflows/"
            "global_etf_research_codegen.yml@refs/heads/main"
        )
        payload: dict[str, object] = {
            "aud": "quant-codex-audit",
            "iss": auth.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/AIAuditBridge",
            "workflow_ref": workflow_ref,
            "ref": "refs/heads/main",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": workflow_ref,
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS": "",
        }

        self.assertEqual(self._verify(payload, env)["workflow_ref"], workflow_ref)
        for rejected_workflow_ref, rejected_ref in (
            (workflow_ref.replace("refs/heads/main", "refs/heads/feature"), "refs/heads/feature"),
            (workflow_ref.replace("global_etf_research_codegen.yml", "other.yml"), "refs/heads/main"),
        ):
            with self.subTest(workflow_ref=rejected_workflow_ref):
                payload["workflow_ref"] = rejected_workflow_ref
                payload["ref"] = rejected_ref
                with self.assertRaises(PermissionError):
                    self._verify(payload, env)

    def test_global_etf_codegen_workflow_is_in_both_deployment_allowlists(self) -> None:
        workflow_ref = (
            "QuantStrategyLab/AIAuditBridge/.github/workflows/"
            "global_etf_research_codegen.yml@refs/heads/main"
        )
        root = Path(__file__).parents[1]
        deploy_script = (root / "scripts/deploy_codex_audit_service.sh").read_text()
        ops_workflow = (root / ".github/workflows/vps_codex_service_ops.yml").read_text()

        self.assertIn(workflow_ref, deploy_script)
        self.assertIn(workflow_ref, ops_workflow)

    def test_global_etf_codegen_job_workflow_ref_is_exactly_allowlisted(self) -> None:
        workflow_ref = (
            "QuantStrategyLab/AIAuditBridge/.github/workflows/"
            "global_etf_research_codegen.yml@refs/heads/main"
        )
        payload: dict[str, object] = {
            "aud": "quant-codex-audit",
            "iss": auth.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/AIAuditBridge",
            "workflow_ref": workflow_ref,
            "job_workflow_ref": workflow_ref,
            "ref": "refs/heads/main",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": workflow_ref,
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS": workflow_ref,
        }

        self.assertEqual(self._verify(payload, env)["job_workflow_ref"], workflow_ref)
        for rejected_job_workflow_ref in (
            workflow_ref.replace("refs/heads/main", "refs/heads/feature"),
            workflow_ref.replace("global_etf_research_codegen.yml", "other.yml"),
        ):
            with self.subTest(job_workflow_ref=rejected_job_workflow_ref):
                payload["job_workflow_ref"] = rejected_job_workflow_ref
                with self.assertRaisesRegex(PermissionError, "job workflow ref is not allowed"):
                    self._verify(payload, env)

    def test_global_etf_codegen_job_workflow_ref_is_in_both_deployment_job_allowlists(self) -> None:
        workflow_ref = (
            "QuantStrategyLab/AIAuditBridge/.github/workflows/"
            "global_etf_research_codegen.yml@refs/heads/main"
        )
        root = Path(__file__).parents[1]
        deploy_script = (root / "scripts/deploy_codex_audit_service.sh").read_text()
        ops_workflow = (root / ".github/workflows/vps_codex_service_ops.yml").read_text()
        deploy_job_refs = re.search(r'^ALLOWED_JOB_WORKFLOW_REFS="\$\{[^:]+:-([^}]*)\}"$', deploy_script, re.MULTILINE)
        ops_job_refs = re.search(r'^\s+CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS: (.+)$', ops_workflow, re.MULTILINE)

        self.assertIsNotNone(deploy_job_refs)
        self.assertIsNotNone(ops_job_refs)
        self.assertIn(workflow_ref, deploy_job_refs.group(1))
        self.assertIn(workflow_ref, ops_job_refs.group(1))

    def test_strategy_drift_requires_trusted_qpk_reusable_workflow(self) -> None:
        qpk_job_ref = (
            "QuantStrategyLab/QuantPlatformKit/.github/workflows/"
            "reusable-drift-check.yml@644cd9002ae92f2aaca6f7efb4afa4986fae05ea"
        )
        audit_job_ref = "QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main"
        payload: dict[str, object] = {
            "aud": "quant-codex-audit",
            "iss": auth.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/CnEquityStrategies",
            "workflow_ref": "QuantStrategyLab/CnEquityStrategies/.github/workflows/drift-check.yml@refs/heads/main",
            "job_workflow_ref": qpk_job_ref,
            "ref": "refs/heads/main",
            "repository_visibility": "public",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/CnEquityStrategies",
            "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": (
                "QuantStrategyLab/CnEquityStrategies/.github/workflows/drift-check.yml@refs/heads/main"
            ),
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS": f"{qpk_job_ref},{audit_job_ref}",
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES": "public",
        }

        self.assertEqual(self._verify(payload, env)["repository"], "QuantStrategyLab/CnEquityStrategies")

        payload["job_workflow_ref"] = audit_job_ref
        with self.assertRaisesRegex(PermissionError, "strategy drift caller must use"):
            self._verify(payload, env)

        suffixed_job_ref = f"{qpk_job_ref}_evil"
        payload["job_workflow_ref"] = suffixed_job_ref
        env["CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS"] = f"{qpk_job_ref},{suffixed_job_ref}"
        with self.assertRaisesRegex(PermissionError, "exact QPK reusable workflow SHA"):
            self._verify(payload, env)

        different_job_ref = (
            "QuantStrategyLab/QuantPlatformKit/.github/workflows/reusable-drift-check.yml@" + "a" * 40
        )
        payload["job_workflow_ref"] = qpk_job_ref
        env["CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS"] = different_job_ref
        with self.assertRaisesRegex(PermissionError, "job workflow ref is not allowed"):
            self._verify(payload, env)

        payload.pop("job_workflow_ref")
        env["CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES"] = "QuantStrategyLab/CnEquityStrategies"
        with self.assertRaisesRegex(PermissionError, "exact QPK reusable workflow SHA"):
            self._verify(payload, env)

    def test_dependency_audit_job_workflow_ref_is_parsed_and_allowlisted(self) -> None:
        dependency_job_ref = (
            "QuantStrategyLab/AIAuditBridge/.github/workflows/"
            "dependency_audit.yml@refs/heads/main"
        )
        payload: dict[str, object] = {
            "aud": "quant-codex-audit",
            "iss": auth.GITHUB_OIDC_ISSUER,
            "exp": int(time.time()) + 300,
            "repository": "QuantStrategyLab/AIAuditBridge",
            "workflow_ref": dependency_job_ref,
            "job_workflow_ref": dependency_job_ref,
            "ref": "refs/heads/main",
            "repository_visibility": "public",
        }
        env = {
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS": dependency_job_ref,
            "CODEX_AUDIT_SERVICE_ALLOWED_REFS": "refs/heads/main",
            "CODEX_AUDIT_SERVICE_ALLOWED_DIRECT_REPOSITORIES": "QuantStrategyLab/AIAuditBridge",
            "CODEX_AUDIT_SERVICE_ALLOWED_JOB_WORKFLOW_REFS": dependency_job_ref,
            "CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES": "public",
        }

        self.assertEqual(self._verify(payload, env)["workflow_ref"], dependency_job_ref)
