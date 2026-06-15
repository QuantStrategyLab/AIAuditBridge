from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts.codex_service_router import (
    DEFAULT_AUDIT_UPSTREAM,
    DEFAULT_GATEWAY_UPSTREAM,
    resolve_upstream,
)


class CodexServiceRouterTests(unittest.TestCase):
    def test_resolve_upstream_routes_codex_audit_to_audit_service(self) -> None:
        self.assertEqual(resolve_upstream("/v1/codex-audit"), DEFAULT_AUDIT_UPSTREAM)

    def test_resolve_upstream_keeps_gateway_routes_on_gateway_service(self) -> None:
        self.assertEqual(resolve_upstream("/v1/codex"), DEFAULT_GATEWAY_UPSTREAM)
        self.assertEqual(resolve_upstream("/unknown"), DEFAULT_GATEWAY_UPSTREAM)

    def test_resolve_upstream_accepts_environment_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_SERVICE_ROUTER_AUDIT_UPSTREAM": "http://127.0.0.1:9001/",
                "CODEX_SERVICE_ROUTER_GATEWAY_UPSTREAM": "http://127.0.0.1:9002/",
            },
        ):
            self.assertEqual(resolve_upstream("/v1/codex-audit"), "http://127.0.0.1:9001")
            self.assertEqual(resolve_upstream("/v1/codex"), "http://127.0.0.1:9002")


if __name__ == "__main__":
    unittest.main()
