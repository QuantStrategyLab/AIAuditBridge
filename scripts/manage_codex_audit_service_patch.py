"""Inspect one existing service; no apply, restart, import, config or auth operation.

The live job counts are a bounded async snapshot, never a drain or a sync-work
oracle. CLI help proves option syntax only: feature names and removal of every
built-in tool require separate, version-specific review. No deployed module is
imported, because importing service helpers can create or recover job state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
from pathlib import Path

UNIT = "codex-audit-service"
DEPLOY_DIRECTORY = Path("/opt/codex-audit-bridge")
JOB_DIRECTORY = Path("/var/lib/codex-audit-bridge/jobs")
SOURCE_FILES = {
    "gateway": DEPLOY_DIRECTORY / "service/ai_gateway_service.py",
    "codex_adapter": DEPLOY_DIRECTORY / "service/adapters/codex_adapter.py",
}
SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Metadata commands exit before config/auth loading. Also do not inherit the
# runner's token/provider environment or its existing Codex credential home.
METADATA_ENV = {"PATH": SERVICE_PATH, "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent", "CODEX_HOME": "/nonexistent"}
UNIT_COMMAND = [
    "/usr/bin/systemctl", "show", UNIT, "--no-pager",
    "--property=Id,LoadState,ActiveState,SubState,MainPID,WorkingDirectory,User,NoNewPrivileges",
]
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_JOB_ENTRIES = 512
MAX_JOB_BYTES = 256 * 1024
MAX_TOTAL_JOB_BYTES = 8 * 1024 * 1024
MAX_COMMAND_BYTES = 64 * 1024
COMMAND_TIMEOUT_SECONDS = 10
JOB_NAME = re.compile(r"[A-Za-z0-9_-]{24,96}\.json\Z")


def _regular_bytes(path: Path | str, limit: int, *, directory_fd: int | None = None) -> tuple[bytes | None, bool, int]:
    """Reject links/special files and races, and read at most limit+1 bytes."""
    fd = None
    data = bytearray()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None, False, 0
        if before.st_size > limit:
            return None, True, 0
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if len(data) > limit:
            return None, True, len(data)
        current = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            return None, False, len(data)
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            return None, False, len(data)
        return bytes(data), False, len(data)
    except OSError:
        return None, False, len(data)
    finally:
        if fd is not None:
            os.close(fd)


def source_hash(path: Path) -> str | None:
    data, _, _ = _regular_bytes(path, MAX_SOURCE_BYTES)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON object")
        result[key] = value
    return result


def job_counts(directory: Path) -> dict:
    result = {
        "queued": 0, "running": 0, "unknown": 0, "complete": True,
        "truncated": False, "admission_closed": False, "includes_sync_requests": False,
        # Only the fixed documented directory is read; environment overrides
        # are deliberately not queried, so configured-directory identity is not proved.
        "configured_directory_verified": False,
    }
    directory_fd = None
    consumed = 0
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = os.fstat(directory_fd)
        with os.scandir(directory_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_JOB_ENTRIES:
                    result["unknown"] += 1  # lower bound: remaining count is not enumerated
                    result["truncated"] = True
                    break
                if entry.name == "quota.json" or not entry.name.endswith(".json"):
                    continue
                if not JOB_NAME.fullmatch(entry.name):
                    result["unknown"] += 1
                    continue
                remaining = MAX_TOTAL_JOB_BYTES - consumed
                if remaining <= 0:
                    result["unknown"] += 1
                    result["truncated"] = True
                    break
                data, truncated, bytes_read = _regular_bytes(entry.name, min(MAX_JOB_BYTES, remaining), directory_fd=directory_fd)
                consumed += bytes_read
                if data is None:
                    result["unknown"] += 1
                    result["truncated"] |= truncated
                    continue
                try:
                    payload = json.loads(data, object_pairs_hook=_unique_object)
                    status = payload.get("status") if isinstance(payload, dict) else None
                    if status in ("queued", "running"):
                        result[status] += 1
                    elif status not in ("succeeded", "failed"):
                        result["unknown"] += 1
                except (ValueError, UnicodeError, RecursionError):
                    result["unknown"] += 1
        after = os.fstat(directory_fd)
        current = os.stat(directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            result["unknown"] += 1
        if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
            result["unknown"] += 1
    except OSError:
        result["unknown"] += 1
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    result["complete"] = result["unknown"] == 0 and not result["truncated"]
    return result


def metadata_output(command: list[str]) -> str | None:
    """Only allow fixed systemd properties or Codex parser-only metadata commands."""
    codex_paths = {str(Path(parent) / "codex") for parent in SERVICE_PATH.split(":")}
    cli_allowed = command and command[0] in codex_paths and command[1:] in (
        ["--version"], ["--help"], ["exec", "--help"],
    )
    if command != UNIT_COMMAND and not cli_allowed:
        return None
    process = None
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=METADATA_ENV, cwd="/", start_new_session=True,
        )
        deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None
                chunk = os.read(process.stdout.fileno(), min(8192, MAX_COMMAND_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_COMMAND_BYTES:
                    return None
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            return None
        return data.decode("utf-8")
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()  # only this newly started metadata process, never the service
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if process.stdout is not None:
                process.stdout.close()


def unit_metadata() -> dict:
    output = metadata_output(UNIT_COMMAND)
    values = dict(line.split("=", 1) for line in (output or "").splitlines() if "=" in line)
    pid = values.get("MainPID", "")
    user = values.get("User", "")
    return {
        "unit": UNIT,
        "identity_verified": values.get("Id") == f"{UNIT}.service",
        "load_state": values.get("LoadState") if values.get("LoadState") in {"loaded", "not-found", "masked", "error"} else "unknown",
        "active_state": values.get("ActiveState") if values.get("ActiveState") in {"active", "inactive", "failed", "activating", "deactivating", "reloading"} else "unknown",
        "sub_state": values.get("SubState") if values.get("SubState") in {"running", "dead", "failed", "start", "stop", "auto-restart", "exited"} else "unknown",
        "main_pid": int(pid) if re.fullmatch(r"[0-9]{1,10}", pid) else None,
        "known_working_directory": values.get("WorkingDirectory") == str(DEPLOY_DIRECTORY),
        "user": user if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}\$?", user) else None,
        "no_new_privileges": {"yes": True, "no": False}.get(values.get("NoNewPrivileges")),
        "running_source_identity_verified": False,
    }


def cli_capabilities() -> dict:
    binary = shutil.which("codex", path=SERVICE_PATH)
    outputs = [metadata_output([binary, *args]) if binary else None for args in (
        ["--version"], ["--help"], ["exec", "--help"],
    )]
    version_match = re.fullmatch(r"codex(?:-cli)? ([0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.]+)?)", (outputs[0] or "").strip())
    help_text = "\n".join(output or "" for output in outputs[1:])
    flags = ("--disable", "--config", "--ignore-user-config", "--ephemeral", "--cd", "--sandbox", "--output-last-message", "--skip-git-repo-check")
    return {
        "version": version_match.group(1) if version_match else None,
        "metadata_complete": all(output is not None for output in outputs) and version_match is not None,
        "flags": {flag: bool(re.search(r"(?<!\S)" + re.escape(flag) + r"(?:\s|[=,]|$)", help_text)) for flag in flags},
        "feature_names_verified": False,
        "all_builtin_tools_disabled_proven": False,
        "service_binary_identity_verified": False,
    }


def inspect_service() -> dict:
    return {
        "operation": "inspect",
        "source_sha256": {name: source_hash(path) for name, path in SOURCE_FILES.items()},
        "unit": unit_metadata(), "job_counts": job_counts(JOB_DIRECTORY), "cli": cli_capabilities(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect",))
    parser.parse_args(argv)
    print(json.dumps(inspect_service(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
