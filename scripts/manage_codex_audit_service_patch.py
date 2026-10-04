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
import pwd
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
PROC_ROOT = Path("/proc")
MAX_PROC_STAT_BYTES = 8 * 1024
MAX_PROC_ARG_BYTES = 8 * 1024
MAX_PROC_ENV_BYTES = 64 * 1024
MAX_ENV_RECORDS = 256
UNKNOWN_REASONS = (
    "per_file_limit", "total_byte_limit", "entry_limit", "unreadable_or_not_regular",
    "file_or_directory_changed", "invalid_name", "invalid_json_or_duplicate_key",
    "invalid_status", "directory_unavailable",
)


def _regular_bytes(path: Path | str, limit: int, *, directory_fd: int | None = None) -> tuple[bytes | None, str | None, int]:
    """Reject links/special files and races, and read at most limit+1 bytes."""
    fd = None
    data = bytearray()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None, "unreadable_or_not_regular", 0
        if before.st_size > limit:
            return None, "byte_limit", 0
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if len(data) > limit:
            return None, "byte_limit", len(data)
        current = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            return None, "file_or_directory_changed", len(data)
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            return None, "file_or_directory_changed", len(data)
        return bytes(data), None, len(data)
    except OSError:
        return None, "unreadable_or_not_regular", len(data)
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
        "unknown_reasons": dict.fromkeys(UNKNOWN_REASONS, 0),
    }
    directory_fd = None
    consumed = 0

    def unknown(reason: str, *, truncated: bool = False) -> None:
        result["unknown"] += 1
        result["unknown_reasons"][reason] += 1
        result["truncated"] |= truncated

    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = os.fstat(directory_fd)
        with os.scandir(directory_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_JOB_ENTRIES:
                    unknown("entry_limit", truncated=True)  # remaining count is not enumerated
                    break
                if entry.name == "quota.json" or not entry.name.endswith(".json"):
                    continue
                if not JOB_NAME.fullmatch(entry.name):
                    unknown("invalid_name")
                    continue
                remaining = MAX_TOTAL_JOB_BYTES - consumed
                if remaining <= 0:
                    unknown("total_byte_limit", truncated=True)
                    break
                data, reason, bytes_read = _regular_bytes(entry.name, min(MAX_JOB_BYTES, remaining), directory_fd=directory_fd)
                consumed += bytes_read
                if data is None:
                    if reason == "byte_limit":
                        unknown("per_file_limit" if remaining >= MAX_JOB_BYTES else "total_byte_limit", truncated=True)
                    else:
                        unknown(reason or "unreadable_or_not_regular")
                    continue
                try:
                    payload = json.loads(data, object_pairs_hook=_unique_object)
                    status = payload.get("status") if isinstance(payload, dict) else None
                    if status in ("queued", "running"):
                        result[status] += 1
                    elif status not in ("succeeded", "failed"):
                        unknown("invalid_status")
                except (ValueError, UnicodeError, RecursionError):
                    unknown("invalid_json_or_duplicate_key")
        after = os.fstat(directory_fd)
        current = os.stat(directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            unknown("file_or_directory_changed")
        if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
            unknown("file_or_directory_changed")
    except OSError:
        unknown("directory_unavailable")
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    result["complete"] = result["unknown"] == 0 and not result["truncated"]
    return result


def _process_identity(pid: int) -> tuple[int, int] | None:
    """Read only starttime and fresh owner UID for the fixed unit's PID."""
    directory = PROC_ROOT / str(pid)
    data, _, _ = _regular_bytes(directory / "stat", MAX_PROC_STAT_BYTES)
    if data is None:
        return None
    try:
        # comm may contain spaces/parentheses. Starttime is field 22, after
        # the closing comm delimiter and fields 3..21; never print comm/stat.
        closing = data.rfind(b")")
        fields = data[closing + 2:].split()
        if closing < 0 or int(data.split(b" ", 1)[0]) != pid or len(fields) < 20:
            return None
        starttime = int(fields[19])
        info = directory.stat(follow_symlinks=False)
        if starttime <= 0 or not stat.S_ISDIR(info.st_mode):
            return None
        return starttime, info.st_uid
    except (OSError, ValueError, IndexError):
        return None


def _initial_environment(data: bytes | None) -> dict[str, str] | None:
    """Select two keys only; no other value is decoded, retained or exported.

    /proc/environ is the initial process environment, not Python's current
    os.environ. Even a complete match cannot prove absence of later mutations.
    """
    if data is None or not data.endswith(b"\0"):
        return None
    records = data.split(b"\0")[:-1]
    if len(records) > MAX_ENV_RECORDS:
        return None
    selected = {}
    allowed = {b"PATH": 4096, b"CODEX_AUDIT_SERVICE_CODEX_BIN": 512}
    for record in records:
        key, separator, value = record.partition(b"=")
        if not separator:
            return None
        if key not in allowed:
            continue
        if key in selected or len(value) > allowed[key]:
            return None
        try:
            selected[key] = value.decode("utf-8")
        except UnicodeError:
            return None
    return {key.decode("ascii"): value for key, value in selected.items()}


def service_process_metadata(unit: dict) -> dict:
    """One-time, bounded fixed-unit proof; never execute a configured binary.

    Source review found no CODEX_BIN/PATH mutations in this repository's service
    sources, but procfs does not attest the current Python environment,
    imports, managed policy, or running source bytes. Keep these limits explicit.
    """
    result = {
        "snapshot_coherent": False, "entrypoint_matches_gateway": False,
        "initial_environment_complete": False, "initial_environment_known_cli_choice": False,
        "known_cli_launch_sha256": None, "runtime_python_environment_verified": False,
        "running_source_identity_verified": False,
    }
    pid, user = unit.get("main_pid"), unit.get("user")
    if (
        not unit.get("identity_verified") or not unit.get("known_working_directory")
        or type(pid) is not int or not 0 < pid < 2**31 or not isinstance(user, str)
    ):
        return result
    try:
        uid = pwd.getpwnam(user).pw_uid
        before = _process_identity(pid)
        if before is None or before[1] != uid:
            return result
        argv, _, _ = _regular_bytes(PROC_ROOT / str(pid) / "cmdline", MAX_PROC_ARG_BYTES)
        tokens = argv.split(b"\0")[:-1] if argv and argv.endswith(b"\0") else []
        entrypoint = len(tokens) == 3 and tokens[0] in (
            b"python3", b"/usr/bin/python3", b"/usr/local/bin/python3",
        ) and tokens[1:] == [b"-m", b"service.ai_gateway_service"]
        environment = None
        if entrypoint:
            data, _, _ = _regular_bytes(PROC_ROOT / str(pid) / "environ", MAX_PROC_ENV_BYTES)
            environment = _initial_environment(data)
        after_unit = unit_metadata()
        after = _process_identity(pid)
        if (
            after != before or after_unit.get("main_pid") != pid or after_unit.get("user") != user
            or not after_unit.get("identity_verified") or not after_unit.get("known_working_directory")
            or pwd.getpwnam(user).pw_uid != uid
        ):
            return result
        result.update(snapshot_coherent=True, entrypoint_matches_gateway=entrypoint,
                      initial_environment_complete=environment is not None)
        if environment is None:
            return result
        path = environment.get("PATH")
        # Do not model relative/empty PATH entries or execute a new configured
        # path. This conservative positive case exactly matches the known PATH.
        if path != SERVICE_PATH or any(not part or not Path(part).is_absolute() for part in path.split(":")):
            return result
        known = shutil.which("codex", path=SERVICE_PATH)
        configured = environment.get("CODEX_AUDIT_SERVICE_CODEX_BIN", "codex")
        if known not in {str(Path(parent) / "codex") for parent in SERVICE_PATH.split(":")}:
            return result
        if configured not in ("codex", known):
            return result
        result["initial_environment_known_cli_choice"] = True
        # Only the previously inspected global launch path is hashed. Following
        # its installation symlink is independent of the configured CODEX_BIN.
        result["known_cli_launch_sha256"] = source_hash(Path(known).resolve())
    except (OSError, KeyError, ValueError, RuntimeError):
        return result
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
    unit = unit_metadata()
    return {
        "operation": "inspect",
        "source_sha256": {name: source_hash(path) for name, path in SOURCE_FILES.items()},
        "unit": unit, "service_process": service_process_metadata(unit),
        "job_counts": job_counts(JOB_DIRECTORY), "cli": cli_capabilities(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect",))
    parser.parse_args(argv)
    print(json.dumps(inspect_service(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
