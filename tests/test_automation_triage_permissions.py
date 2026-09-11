"""Triage recommends a bounded next step; it cannot authorize a deployment."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from service.ai_gateway_service import AiGatewayRequestHandler, _automation_triage_snapshot


class TriagePermissionsTest(unittest.TestCase):
    def setUp(self) -> None:
        health = type("Health", (), {"status": "healthy"})()
        quota = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "ok"}})()
        ledger = type("Ledger", (), {"snapshot": lambda self, limit=None: {"runs": []}})()
        for target, value in (
            ("read_org_health", {"status": "ok"}),
            ("get_health_monitor", health),
            ("get_quota_manager", quota),
            ("get_automation_run_ledger", ledger),
            ("load_execution_policy", {}),
            ("load_autonomy_policy", {}),
        ):
            self.enterContext(patch(f"service.ai_gateway_service.{target}", return_value=value))

    def test_missing_write_set_requires_review(self) -> None:
        for paths in (None, []):
            with self.subTest(paths=paths):
                triage = _automation_triage_snapshot("local/repo", changed_paths=paths)
                self.assertFalse(triage["auto_fix_allowed"])
                self.assertFalse(triage["deploy_allowed"])
                self.assertTrue(triage["human_review_required"])
                self.assertEqual(triage["file_risk"], "unknown")
                self.assertEqual(triage["recommended_action"], "open_issue")

    def test_fix_pr_permission_never_implies_deployment(self) -> None:
        for paths in (["docs/runbook.md"], ["service/worker.py"]):
            for autonomy in ("auto_pr", "auto_merge"):
                with self.subTest(paths=paths, autonomy=autonomy), patch(
                    "service.ai_gateway_service.load_execution_policy",
                    return_value={"default": {"max_autonomy": autonomy}},
                ):
                    triage = _automation_triage_snapshot("local/repo", changed_paths=paths)
                    self.assertTrue(triage["auto_fix_allowed"])
                    self.assertEqual(triage["recommended_action"], "open_fix_pr")
                    self.assertFalse(triage["deploy_allowed"])

    def test_invalid_write_set_is_rejected_without_discarding_entries(self) -> None:
        for paths in ("docs/runbook.md", {}, [42], ["docs/runbook.md", 42], [""], ["docs/runbook.md", " "], ["../orders.py"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                _automation_triage_snapshot("local/repo", changed_paths=paths)

    def test_existing_review_only_and_protected_path_guards_remain(self) -> None:
        for kwargs in (
            {"requested_mode": "review_only", "changed_paths": ["docs/runbook.md"]},
            {"changed_paths": ["src/us_equity_strategies/strategy.py"]},
            {"changed_paths": [".github/codex_auto_merge_policy.json"]},
        ):
            with self.subTest(kwargs=kwargs):
                triage = _automation_triage_snapshot("local/repo", **kwargs)
                self.assertFalse(triage["auto_fix_allowed"])
                self.assertFalse(triage["deploy_allowed"])
                self.assertNotEqual(triage["recommended_action"], "open_fix_pr")

    def test_unavailable_quota_or_service_does_not_admit_a_fix(self) -> None:
        unavailable = type("Quota", (), {"runtime_status": lambda self, repo: {"status": "unavailable"}})()
        unhealthy = type("Health", (), {"status": "unhealthy"})()
        for target, value in (("get_quota_manager", unavailable), ("get_health_monitor", unhealthy)):
            with self.subTest(target=target), patch(f"service.ai_gateway_service.{target}", return_value=value):
                triage = _automation_triage_snapshot("local/repo", changed_paths=["docs/runbook.md"])
                self.assertFalse(triage["auto_fix_allowed"])
                self.assertFalse(triage["deploy_allowed"])

    def test_failure_category_preserves_retry_advice_without_write_permission(self) -> None:
        for category, retry in (("transient_service_failure", True), ("quota_or_capacity_failure", True), ("auth_or_config_failure", False)):
            with self.subTest(category=category):
                triage = _automation_triage_snapshot("local/repo", failure_category=category, changed_paths=["docs/runbook.md"])
                self.assertEqual(triage["retry_allowed"], retry)
                self.assertFalse(triage["auto_fix_allowed"])
                self.assertFalse(triage["deploy_allowed"])

    def test_http_boundary_keeps_unknown_and_invalid_write_sets_distinct(self) -> None:
        with patch("service.ai_gateway_service.authenticate", return_value={"repository": "local", "auth_method": "none"}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                cases = [({}, 200), ({"changed_paths": []}, 200), ({"changed_paths": None}, 200)]
                cases += [({"changed_paths": paths}, 400) for paths in ("docs/runbook.md", {}, [42], ["docs/runbook.md", 42], [""], ["../orders.py"])]
                cases.append(({"changed_paths": ["docs/runbook.md"]}, 200))
                for payload, expected in cases:
                    with self.subTest(payload=payload):
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{server.server_port}/v1/ai/automation/triage",
                            data=json.dumps(payload).encode(),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        try:
                            response = urllib.request.urlopen(request, timeout=5)
                        except urllib.error.HTTPError as exc:
                            response = exc
                        with response:
                            body = json.loads(response.read())
                            self.assertEqual(response.status, expected)
                        if expected == 200:
                            self.assertFalse(body["triage"]["deploy_allowed"])
                            self.assertEqual(body["triage"]["auto_fix_allowed"], payload.get("changed_paths") == ["docs/runbook.md"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
