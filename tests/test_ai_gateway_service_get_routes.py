import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from service.ai_gateway_service import AiGatewayRequestHandler


class AiGatewayGetRoutesTest(unittest.TestCase):
    def test_read_routes_accept_query_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    effectiveness_url = f"{base_url}/v1/ai/changes/effectiveness?days=90"
                    with urllib.request.urlopen(effectiveness_url, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        effectiveness = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(effectiveness["status"], "ok")
                    self.assertIn("report", effectiveness)

                    quota_url = f"{base_url}/v1/ai/quota?repo=QuantStrategyLab/AIAuditBridge"
                    with urllib.request.urlopen(quota_url, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        quota = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(quota["status"], "ok")
                    self.assertEqual(quota["quota"]["repo"], "QuantStrategyLab/AIAuditBridge")
                finally:
                    server.shutdown()
                    server.server_close()

    def test_feedback_register_uses_source_repository_for_change_feed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_AUDIT_SERVICE_AUTH": "none",
                "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
                "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            }
            with patch.dict(os.environ, env, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    payload = {
                        "source_repository": "local/repo",
                        "task": "monthly_snapshot_audit",
                        "action": "auto_pr",
                        "risk": "low",
                        "changed_paths": ["docs/runbook.md"],
                        "external_url": "https://example.test/pr/12",
                        "issue_number": 7,
                        "pr_number": 12,
                    }
                    request = urllib.request.Request(
                        f"{base_url}/v1/ai/feedback/register",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)

                    with urllib.request.urlopen(f"{base_url}/v1/ai/changes?days=7", timeout=5) as response:
                        changes = json.loads(response.read().decode("utf-8"))["changes"]
                    self.assertEqual(changes[0]["repo"], "local/repo")
                    self.assertEqual(changes[0]["external_url"], "https://example.test/pr/12")
                    self.assertEqual(changes[0]["pr_number"], 12)
                    self.assertEqual(changes[0]["state"], "waiting_for_ci")
                    self.assertFalse(changes[0]["human_review_required"])
                finally:
                    server.shutdown()
                    server.server_close()
