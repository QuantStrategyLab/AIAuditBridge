from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from scripts import refresh_org_health_github_token as issuer


class OrgHealthGithubTokenTest(unittest.TestCase):
    def _response(self, *, permissions=None, repositories=None, expires_at=None):
        return {
            "token": "ghs_test",
            "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "permissions": permissions or {"actions": "read", "metadata": "read"},
            "repositories": repositories if repositories is not None else [{"full_name": f"{issuer.ORG}/{repo}"} for repo in issuer.REPOSITORIES],
        }

    def test_request_is_fixed_host_and_exact_read_scope(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self._payload).encode()

        response = Response()
        response._payload = self._response()

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return response

        with patch.object(issuer, "urlopen", fake_urlopen):
            payload = issuer._request_token("jwt", 120834787)

        request = captured["request"]
        self.assertEqual(request.full_url, "https://api.github.com/app/installations/120834787/access_tokens")
        self.assertEqual(json.loads(request.data), {
            "repositories": list(issuer.REPOSITORIES),
            "permissions": {"actions": "read", "metadata": "read"},
        })
        self.assertEqual(request.get_header("Authorization"), "Bearer jwt")
        self.assertEqual(payload["token"], "ghs_test")

    def test_jwt_uses_short_rs256_window_and_app_id(self) -> None:
        captured = {}

        def fake_run(args, input, stdout, stderr, check):
            captured.update(args=args, input=input)
            return SimpleNamespace(stdout=b"signature")

        with patch.object(issuer, "PRIVATE_KEY_FILE", Path("/etc/codex-org-health/app-private-key.pem")), patch(
            "scripts.refresh_org_health_github_token.subprocess.run", fake_run
        ):
            token = issuer._jwt(3250578, 1_000)

        header, body, signature = token.split(".")
        decoded = json.loads(base64.urlsafe_b64decode(body + "=="))
        self.assertEqual(json.loads(base64.urlsafe_b64decode(header + "==")), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(decoded, {"iat": 940, "exp": 1540, "iss": 3250578})
        self.assertEqual(signature, "c2lnbmF0dXJl")
        self.assertEqual(captured["args"][:4], ["openssl", "dgst", "-sha256", "-sign"])
        self.assertEqual(captured["input"].decode().count("."), 1)

    def test_response_rejects_write_permission_missing_repo_and_expired_token(self) -> None:
        with self.assertRaisesRegex(issuer.RefreshFailure, "response_permissions"):
            issuer._validate_response(self._response(permissions={"actions": "write", "metadata": "read"}), 0)
        with self.assertRaisesRegex(issuer.RefreshFailure, "response_repositories"):
            issuer._validate_response(self._response(repositories=[]), 0)
        expired = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        with self.assertRaisesRegex(issuer.RefreshFailure, "response_expiry"):
            issuer._validate_response(self._response(expires_at=expired), time.time())

    def test_network_failure_is_fixed_category(self) -> None:
        with patch.object(issuer, "urlopen", side_effect=URLError("private body must not escape")):
            with self.assertRaisesRegex(issuer.RefreshFailure, "network"):
                issuer._request_token("jwt", 120834787)

    def test_token_write_is_atomic_and_restricts_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "installation-token.json"
            with patch.object(issuer, "TOKEN_FILE", output), patch.object(
                issuer.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())
            ), patch.object(issuer.os, "chown"), patch.object(issuer.os, "fchown"), patch.object(
                Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o40750)
            ):
                issuer._write_token("ghs_test", "2030-01-01T00:00:00Z")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)
            self.assertEqual(json.loads(output.read_text()), {"token": "ghs_test", "expires_at": "2030-01-01T00:00:00Z"})




if __name__ == "__main__":
    unittest.main()
