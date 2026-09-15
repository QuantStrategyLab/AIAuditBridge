#!/usr/bin/env python3
"""Mint the least-privileged GitHub App token used by org-health."""

from __future__ import annotations

import base64
import datetime as dt
import grp
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

APP_ID_FILE = Path(os.environ.get("CODEX_ORG_HEALTH_APP_ID_FILE", "/etc/codex-org-health/app-id"))
INSTALLATION_ID_FILE = Path(
    os.environ.get("CODEX_ORG_HEALTH_INSTALLATION_ID_FILE", "/etc/codex-org-health/installation-id")
)
PRIVATE_KEY_FILE = Path(
    os.environ.get("CODEX_ORG_HEALTH_PRIVATE_KEY_FILE", "/etc/codex-org-health/app-private-key.pem")
)
TOKEN_FILE = Path(os.environ.get("CODEX_ORG_HEALTH_TOKEN_FILE", "/run/codex-org-health/installation-token.json"))
GITHUB_API = "https://api.github.com"
ORG = "QuantStrategyLab"
REPOSITORIES = (
    "AIAuditBridge",
    "UsEquitySnapshotPipelines",
    "HkEquitySnapshotPipelines",
    "CryptoLivePoolPipelines",
    "ResearchSignalContextPipelines",
    "BinancePlatform",
    "InteractiveBrokersPlatform",
    "LongBridgePlatform",
    "CharlesSchwabPlatform",
    "FirstradePlatform",
    "IBKRGatewayManager",
    "QuantAdvisorResearch",
)
JWT_LIFETIME_SECONDS = 540
EXPIRY_SKEW_SECONDS = 60


class RefreshFailure(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _read_identifier(path: Path) -> int:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RefreshFailure("config") from exc
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise RefreshFailure("config")
    return int(value)


def _jwt(app_id: int, now: int) -> str:
    header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + JWT_LIFETIME_SECONDS, "iss": app_id}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(PRIVATE_KEY_FILE)],
            input=signing_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RefreshFailure("jwt") from exc
    return f"{header}.{payload}.{_b64url(result.stdout)}"


def _request_token(jwt: str, installation_id: int) -> dict[str, object]:
    payload = {
        "repositories": list(REPOSITORIES),
        "permissions": {"actions": "read", "metadata": "read"},
    }
    request = Request(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AIAuditBridge-org-health",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read()
    except HTTPError as exc:
        raise RefreshFailure("http") from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise RefreshFailure("network") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RefreshFailure("response_invalid") from exc


def _parse_expiry(value: object) -> float:
    if not isinstance(value, str):
        raise RefreshFailure("response_expiry")
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise RefreshFailure("response_expiry") from exc


def _validate_response(payload: object, now: float) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise RefreshFailure("response_invalid")
    token = payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise RefreshFailure("response_invalid")
    permissions = payload.get("permissions")
    if permissions != {"actions": "read", "metadata": "read"}:
        raise RefreshFailure("response_permissions")
    repositories = payload.get("repositories")
    if not isinstance(repositories, list):
        raise RefreshFailure("response_repositories")
    actual = [item.get("full_name") for item in repositories if isinstance(item, dict)]
    expected = {f"{ORG}/{name}" for name in REPOSITORIES}
    if len(actual) != len(repositories) or set(actual) != expected or len(actual) != len(expected):
        raise RefreshFailure("response_repositories")
    expires_at = payload.get("expires_at")
    if _parse_expiry(expires_at) <= now + EXPIRY_SKEW_SECONDS:
        raise RefreshFailure("response_expiry")
    return token.strip(), str(expires_at)


def _ensure_token_directory() -> None:
    directory = TOKEN_FILE.parent
    try:
        if directory.is_symlink():
            raise RefreshFailure("output")
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chown(directory, 0, grp.getgrnam("ubuntu").gr_gid)
        os.chmod(directory, 0o750)
        directory_stat = directory.stat()
    except (OSError, KeyError) as exc:
        raise RefreshFailure("output") from exc
    if directory_stat.st_uid != 0 or directory_stat.st_mode & 0o022:
        raise RefreshFailure("output")


def _write_token(token: str, expires_at: str) -> None:
    _ensure_token_directory()
    if TOKEN_FILE.is_symlink():
        raise RefreshFailure("output")
    payload = json.dumps({"token": token, "expires_at": expires_at}, separators=(",", ":")) + "\n"
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".installation-token.", dir=TOKEN_FILE.parent)
        os.fchmod(fd, 0o640)
        os.fchown(fd, 0, grp.getgrnam("ubuntu").gr_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, TOKEN_FILE)
        temporary = None
    except (OSError, KeyError) as exc:
        raise RefreshFailure("output") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def refresh() -> None:
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    app_id = _read_identifier(APP_ID_FILE)
    installation_id = _read_identifier(INSTALLATION_ID_FILE)
    jwt = _jwt(app_id, now)
    payload = _request_token(jwt, installation_id)
    token, expires_at = _validate_response(payload, float(now))
    _write_token(token, expires_at)


def main() -> int:
    try:
        refresh()
    except RefreshFailure as exc:
        print(f"refresh_failed={exc.category}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
