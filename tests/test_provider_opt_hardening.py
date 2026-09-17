"""Offline hardening coverage for remaining provider-opt audit items."""

from __future__ import annotations

import unittest
from http import HTTPStatus
from pathlib import Path

import service.ai_gateway_service as gateway


class TestStrictReviewJsonParse(unittest.TestCase):
    def test_extract_confidence_requires_single_valid_object(self) -> None:
        self.assertEqual(
            gateway._extract_confidence_from_output('{"verdict":"approve","confidence":0.9}'),
            0.9,
        )
        self.assertIsNone(gateway._extract_confidence_from_output(""))
        self.assertIsNone(gateway._extract_confidence_from_output("not json"))
        self.assertIsNone(
            gateway._extract_confidence_from_output('{"verdict":"approve","confidence":0.9}{"confidence":0.1}')
        )
        self.assertIsNone(gateway._extract_confidence_from_output('{"verdict":"approve"}'))
        self.assertIsNone(gateway._extract_confidence_from_output('{"confidence":"NaN"}'))
        self.assertIsNone(gateway._extract_confidence_from_output('{"confidence":1.5}'))

    def test_compute_consensus_fail_closed_on_junk(self) -> None:
        self.assertEqual(
            gateway._compute_consensus([{"success": True, "output": '{"verdict":"approve","confidence":0.9}'}]),
            "approve",
        )
        self.assertEqual(
            gateway._compute_consensus([{"success": True, "output": "approve maybe"}]),
            "escalate",
        )
        self.assertEqual(gateway._compute_consensus([]), "escalate")


class TestPermissionErrorStatus(unittest.TestCase):
    def test_maps_auth_capacity_and_policy(self) -> None:
        self.assertEqual(
            gateway._http_status_for_permission_error(PermissionError("missing bearer token")),
            HTTPStatus.UNAUTHORIZED,
        )
        self.assertEqual(
            gateway._http_status_for_permission_error(PermissionError("OIDC token is expired")),
            HTTPStatus.UNAUTHORIZED,
        )
        self.assertEqual(
            gateway._http_status_for_permission_error(PermissionError("rate limit exceeded: 10 requests per 60s")),
            HTTPStatus.TOO_MANY_REQUESTS,
        )
        self.assertEqual(
            gateway._http_status_for_permission_error(PermissionError("too many active jobs: max 10")),
            HTTPStatus.TOO_MANY_REQUESTS,
        )
        self.assertEqual(
            gateway._http_status_for_permission_error(PermissionError("repository is not allowlisted")),
            HTTPStatus.FORBIDDEN,
        )
        self.assertEqual(
            gateway._http_status_for_permission_error(
                PermissionError("platform_bugfix manual approval requires GitHub OIDC")
            ),
            HTTPStatus.FORBIDDEN,
        )


class TestLegacyDeployFreeze(unittest.TestCase):
    def test_deploy_script_runs_gateway_and_skips_legacy_install(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "deploy_codex_audit_service.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("python3 -m service.ai_gateway_service", text)
        self.assertNotIn('install_file "scripts/codex_audit_service.py"', text)
        legacy = Path(__file__).resolve().parents[1] / "scripts" / "codex_audit_service.py"
        self.assertIn("FROZEN", legacy.read_text(encoding="utf-8").splitlines()[1])


if __name__ == "__main__":
    unittest.main()
