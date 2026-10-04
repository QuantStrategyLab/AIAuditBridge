"""Inspect or maintain two fixed source files of one existing service.

The live job counts are a bounded async snapshot, never a drain or a sync-work
oracle. CLI help proves option syntax only: feature names and removal of every
built-in tool require separate, version-specific review. No deployed module is
imported, because importing service helpers can create or recover job state.
Maintenance is an explicitly acknowledged interruption, not a graceful drain.
Backups/rollback cover code only: startup may fail orphaned jobs/service_restart;
sync work, provider consumption and job/quota/receipt state cannot be rewound.
Only this fixed-unit stop/start maintenance exists; no broad legacy deploy,
config/credential/unit/nginx/provider changes or installation operation exists.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
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
REVIEWED_SOURCE_COMMIT = "8dd1b0ca830b3bdb90de2c4ca555a2f3db090d15"
TARGET_FILES = {name: Path(__file__).resolve().parents[1] / path.relative_to(DEPLOY_DIRECTORY)
                for name, path in SOURCE_FILES.items()}
PREIMAGE_SHA256 = {
    "gateway": "1f21f4f626f8374bd957667dc471a5bc5b75bafda4f81cc95dc95b31911f5014",
    "codex_adapter": "954476a6e82eade56a51b71c1b8087766bfa57c9aac54730a5f7376cb5f67013",
}
TARGET_SHA256 = {
    "gateway": "60012ad523cd0fbef0996a6d879e3625ace3a3c52a0c84957973d6ae01b95cdf",
    "codex_adapter": "e5175a5fb2c132b24dfc59a0af365c5af8ba2f3b8f6079fe398f97111c347cd0",
}
# Creation is exclusive and never reused, even after a failed operation. This
# serializes this one-shot entrypoint only, not other administrators.
BACKUP_DIRECTORY = DEPLOY_DIRECTORY / ".audit-patch-backup-8dd1b0ca"
BACKUP_OWNER_UID = 0  # the existing sudo-owned private backup, never caller-selected
MAX_BACKUP_METADATA_BYTES = 8 * 1024
# This prerequisite was independently reviewed from the exact GitHub run/step
# receipt. It is source-owned, never supplied by a caller or inferred from a
# directory. Runtime eligibility additionally requires the immutable backup
# and fresh preimages/metadata/identity. No new GitHub/credential lookup exists.
REVIEWED_ROLLBACK_BINDING = {
    "run_id": 37238721342, "head_sha": "a0ae533072693fbf407a1e9702abd062790a85c7",
    "canonical_receipt_sha256": "6a2d54f0067f17fbc262b5fa85050e9bfbe4f89f07459f871d7446617d52d690",
    "status": "rolled_back", "reason": "start_failed",
}
UNIT_ACTION_TIMEOUT_SECONDS = 30
HEALTH_TIMEOUT_SECONDS = 20
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


def service_process_metadata(unit: dict, *, deadline: float | None = None) -> dict:
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
    if deadline is not None and time.monotonic() >= deadline:
        return result
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
        after_unit = unit_metadata(**({"deadline": deadline} if deadline is not None else {}))
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


def metadata_output(command: list[str], *, deadline: float | None = None) -> str | None:
    """Only allow fixed systemd properties or Codex parser-only metadata commands."""
    codex_paths = {str(Path(parent) / "codex") for parent in SERVICE_PATH.split(":")}
    cli_allowed = command and command[0] in codex_paths and command[1:] in (
        ["--version"], ["--help"], ["exec", "--help"],
    )
    if command != UNIT_COMMAND and not cli_allowed:
        return None
    command_deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    if deadline is not None:
        command_deadline = min(command_deadline, deadline)
    if time.monotonic() >= command_deadline:
        return None
    process = None
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=METADATA_ENV, cwd="/", start_new_session=True,
        )
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = command_deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None
                chunk = os.read(process.stdout.fileno(), min(8192, MAX_COMMAND_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_COMMAND_BYTES:
                    return None
        remaining = command_deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0 or time.monotonic() >= command_deadline:
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


def unit_metadata(*, deadline: float | None = None) -> dict:
    output = metadata_output(UNIT_COMMAND, **({"deadline": deadline} if deadline is not None else {}))
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


def _file_snapshot(path: Path) -> tuple[bytes, os.stat_result]:
    if path.resolve() != path:
        raise ValueError("noncanonical source")
    before = path.stat(follow_symlinks=False)
    data, _, _ = _regular_bytes(path, MAX_SOURCE_BYTES)
    after = path.stat(follow_symlinks=False)
    if (data is None or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
            or stat.S_IMODE(after.st_mode) & 0o7000 or _file_identity(before) != _file_identity(after)):
        raise ValueError("source changed or unsupported")
    return data, after


def _file_identity(info: os.stat_result) -> tuple:
    # Reads may change atime; compare mutation/ownership identity only.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_uid, info.st_gid, info.st_mode, info.st_nlink)


def _unit_action(action: str) -> bool:
    """Only fixed stop/start; timeout is uncertainty, never permission to write."""
    if action not in ("stop", "start"):
        return False
    try:
        return subprocess.run(
            ["/usr/bin/systemctl", action, UNIT], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=METADATA_ENV,
            cwd="/", timeout=UNIT_ACTION_TIMEOUT_SECONDS,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _stopped() -> bool:
    unit = unit_metadata()
    return (unit.get("identity_verified") is True and unit.get("load_state") == "loaded"
            and unit.get("active_state") == "inactive" and unit.get("main_pid") == 0)


def _local_health(*, deadline: float | None = None, diagnostics: dict | None = None) -> bool:
    """Fixed loopback GETs only, no credentials, redirects, proxy or model call.

    The documented port is intentionally fixed: an override is a failed
    acceptance check, not a reason to search config or try another endpoint.
    """
    if deadline is None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(healthz_ok=False, unauthenticated_health_rejected=False)
    while time.monotonic() < deadline:
        healthy = False
        for route, expected in (("/healthz", 200), ("/v1/ai/health", 401)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            connection = http.client.HTTPConnection("127.0.0.1", 8797, timeout=min(2, remaining))
            try:
                connection.request("GET", route)
                response = connection.getresponse()
                data = response.read(4097)
                if response.status != expected or len(data) > 4096:
                    break
                if route == "/healthz":
                    payload = json.loads(data, object_pairs_hook=_unique_object)
                    if not isinstance(payload, dict) or payload.get("status") not in ("ok", "healthy"):
                        break
                    healthy = True
                    diagnostics["healthz_ok"] = True
                elif healthy:
                    diagnostics["unauthenticated_health_rejected"] = True
                    return time.monotonic() < deadline
            except (OSError, ValueError, http.client.HTTPException):
                break
            finally:
                connection.close()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.2, remaining))
    return False


def _startup_status() -> dict:
    return {"unit_ready": False, "process_identity_stable": False, "process_coherent": False,
            "entrypoint_matches_gateway": False, "initial_cli_choice_known": False,
            "healthz_ok": False, "unauthenticated_health_rejected": False,
            "deadline_expired": False, "failure_phase": None}


def _started(old_pid: int, *, diagnostics: dict | None = None) -> bool:
    """Wait for pre-exec transients, never a new PID/reused PID or another start.

    Type=simple can return before Python exec. One deadline covers readiness,
    bounded metadata commands and health; no gate gains a fresh time budget.
    Fixed booleans mean proved/not proved, not unobserved failure causes.
    """
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(_startup_status())
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    pinned = None
    while time.monotonic() < deadline:
        unit = unit_metadata(deadline=deadline)
        if time.monotonic() >= deadline:
            diagnostics.update(deadline_expired=True, failure_phase="startup_deadline")
            return False
        pid = unit.get("main_pid")
        trusted_unit = unit.get("identity_verified") is True and unit.get("known_working_directory") is True
        if pinned is not None and (unit.get("active_state") in ("inactive", "failed") or pid == 0):
            diagnostics["failure_phase"] = "startup_exit"
            return False
        if trusted_unit and type(pid) is int and 0 < pid < 2**31 and pid != old_pid:
            if pinned is not None and pid != pinned[0]:
                diagnostics["failure_phase"] = "startup_identity_changed"
                return False
            identity = _process_identity(pid)
            if identity is not None:
                observed = (pid, identity[0])  # UID may settle before exec; the coherent proof checks it fresh.
                if pinned is not None and observed != pinned:
                    diagnostics["failure_phase"] = "startup_identity_changed"
                    return False
                pinned = observed
        else:
            identity = None
        diagnostics["unit_ready"] = (
            trusted_unit and unit.get("active_state") == "active" and unit.get("sub_state") == "running"
            and identity is not None)
        diagnostics["failure_phase"] = "startup_unit"
        if diagnostics["unit_ready"]:
            process = service_process_metadata(unit, deadline=deadline)
            after = _process_identity(pid)
            if after is not None and (pid, after[0]) != pinned:
                diagnostics["failure_phase"] = "startup_identity_changed"
                return False
            diagnostics.update(
                process_identity_stable=after is not None and (pid, after[0]) == pinned,
                process_coherent=process.get("snapshot_coherent") is True,
                entrypoint_matches_gateway=process.get("entrypoint_matches_gateway") is True,
                initial_cli_choice_known=process.get("initial_environment_known_cli_choice") is True,
            )
            for key, phase in (("process_identity_stable", "startup_process"), ("process_coherent", "startup_process"),
                               ("entrypoint_matches_gateway", "startup_entrypoint"), ("initial_cli_choice_known", "startup_cli")):
                diagnostics["failure_phase"] = phase
                if not diagnostics[key]:
                    break
            else:
                if time.monotonic() >= deadline:
                    diagnostics.update(deadline_expired=True, failure_phase="startup_deadline")
                    return False
                accepted = _local_health(deadline=deadline, diagnostics=diagnostics)
                diagnostics["deadline_expired"] = time.monotonic() >= deadline
                if accepted and not diagnostics["deadline_expired"]:
                    final_unit = unit_metadata(deadline=deadline)
                    if time.monotonic() >= deadline:
                        diagnostics.update(deadline_expired=True, failure_phase="startup_deadline")
                        return False
                    final_identity = _process_identity(pid)
                    if final_unit.get("active_state") in ("inactive", "failed") or final_unit.get("main_pid") == 0:
                        diagnostics["failure_phase"] = "startup_exit"
                        return False
                    if final_unit.get("main_pid") != pid or final_identity != after:
                        diagnostics.update(process_identity_stable=False, failure_phase="startup_identity_changed")
                        return False
                    if (final_unit.get("identity_verified") is not True or final_unit.get("known_working_directory") is not True
                            or final_unit.get("active_state") != "active" or final_unit.get("sub_state") != "running"
                            or final_unit.get("user") != unit.get("user")):
                        diagnostics.update(unit_ready=False, failure_phase="startup_unit")
                        return False
                    if time.monotonic() >= deadline:
                        diagnostics.update(deadline_expired=True, failure_phase="startup_deadline")
                        return False
                    diagnostics.update(healthz_ok=True, unauthenticated_health_rejected=True, failure_phase=None)
                    return True
                diagnostics["failure_phase"] = (
                    "startup_deadline" if diagnostics["deadline_expired"] else
                    "startup_auth" if diagnostics["healthz_ok"] else "startup_health")
                return False
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    diagnostics["deadline_expired"] = True
    return False


def _write_exclusive(path: Path | str, data: bytes, metadata: os.stat_result | None = None,
                     *, directory_fd: int | None = None, cleanup_on_failure: bool = False) -> os.stat_result:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    complete = False
    try:
        if metadata is not None:
            os.fchown(fd, metadata.st_uid, metadata.st_gid)
            os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short source write")
            view = view[written:]
        os.fsync(fd)
        complete = True
        return os.fstat(fd)
    finally:
        try:
            if not complete and cleanup_on_failure:
                current = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
                owned = os.fstat(fd)
                if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                    os.unlink(path, dir_fd=directory_fd)
        finally:
            os.close(fd)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_replace(path: Path, data: bytes, metadata: os.stat_result) -> None:
    """Create-only staging in the same fixed directory; preserve original UID/GID/mode."""
    if path.parent.resolve() != path.parent:
        raise ValueError("noncanonical directory")
    stage = path.with_name(f".{path.name}.audit-patch-stage")
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created = None
    try:
        created = _write_exclusive(stage.name, data, metadata, directory_fd=parent_fd, cleanup_on_failure=True)
        parent = os.fstat(parent_fd)
        current = path.parent.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (parent.st_dev, parent.st_ino):
            raise ValueError("directory changed")
        name = next(name for name, fixed in SOURCE_FILES.items() if fixed == path)
        current_data, current = _file_snapshot(path)
        wanted_hash = hashlib.sha256(data).hexdigest()
        current_hash = hashlib.sha256(current_data).hexdigest()
        same_owner_mode = ((current.st_uid, current.st_gid, current.st_mode)
                           == (metadata.st_uid, metadata.st_gid, metadata.st_mode))
        if (not same_owner_mode or wanted_hash not in (PREIMAGE_SHA256[name], TARGET_SHA256[name])
                or current_hash not in (PREIMAGE_SHA256[name], TARGET_SHA256[name])
                or (wanted_hash == TARGET_SHA256[name] and _file_identity(current) != _file_identity(metadata))):
            raise ValueError("source compare-and-swap mismatch")
        os.replace(stage.name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            if created is not None:
                try:
                    current = os.stat(stage.name, dir_fd=parent_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                        os.unlink(stage.name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)


def _maintenance_inputs(result: dict) -> tuple | str:
    targets, originals, metadata = {}, {}, {}
    try:
        for name, path in TARGET_FILES.items():
            targets[name], _ = _file_snapshot(path)
            if hashlib.sha256(targets[name]).hexdigest() != TARGET_SHA256[name]:
                return "target_source_mismatch"
        for name, path in SOURCE_FILES.items():
            originals[name], metadata[name] = _file_snapshot(path)
            if hashlib.sha256(originals[name]).hexdigest() != PREIMAGE_SHA256[name]:
                return "preimage_mismatch"
        unit = unit_metadata()
        process = service_process_metadata(unit)
        if (unit.get("active_state") != "active" or unit.get("sub_state") != "running"
                or not all(process.get(key) is True for key in (
                    "snapshot_coherent", "entrypoint_matches_gateway", "initial_environment_known_cli_choice"))):
            return "service_identity_unproven"
        result["job_counts"] = job_counts(JOB_DIRECTORY)
        return targets, originals, metadata, unit
    except (OSError, ValueError, KeyError):
        return "backup_unavailable"


def _maintenance_result() -> dict:
    return {
        "operation": "maintain", "status": "refused", "reason": None,
        "failure_phase": None,
        "reviewed_source_commit": REVIEWED_SOURCE_COMMIT,
        "requests_may_be_interrupted": True, "drain_proven": False,
        "all_builtin_tools_disabled_proven": False, "rollback_scope": "code_only",
        "running_source_identity_verified": False, "runtime_python_environment_verified": False,
        "startup_status": {"start_command_ok": False, "disk_hashes_match": False, **_startup_status()},
    }


def maintain_service(*, acknowledge_interruption: bool = False) -> dict:
    """One reviewed two-file CAS maintenance, with code-only recovery.

    A complete snapshot would still omit sync requests; explicit acknowledgement
    accepts possible interruption, never proves idle. Power loss/SIGKILL or a
    failed restore needs operator recovery from the durable create-only backups.
    A stale/unknown file is never overwritten and no request/model is replayed.
    """
    result = _maintenance_result()
    if acknowledge_interruption is not True:
        return {**result, "reason": "interruption_not_acknowledged"}
    prepared = _maintenance_inputs(result)
    if isinstance(prepared, str):
        return {**result, "reason": prepared}
    targets, originals, metadata, unit = prepared
    try:
        if BACKUP_DIRECTORY.parent.resolve() != BACKUP_DIRECTORY.parent:
            raise ValueError("noncanonical backup parent")
        BACKUP_DIRECTORY.mkdir(mode=0o700)  # exclusive one-shot lock and backup namespace
        for name in SOURCE_FILES:
            _write_exclusive(BACKUP_DIRECTORY / f"{name}.py", originals[name])
        receipt = {"reviewed_source_commit": REVIEWED_SOURCE_COMMIT, "preimage_sha256": PREIMAGE_SHA256,
                   "target_sha256": TARGET_SHA256, "metadata": {
                       name: {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
                       for name, info in metadata.items()}}
        _write_exclusive(BACKUP_DIRECTORY / "metadata.json", json.dumps(receipt, sort_keys=True).encode())
        _sync_directory(BACKUP_DIRECTORY)
        _sync_directory(BACKUP_DIRECTORY.parent)
        for name in SOURCE_FILES:
            if source_hash(BACKUP_DIRECTORY / f"{name}.py") != PREIMAGE_SHA256[name]:
                raise ValueError("backup mismatch")
        if unit_metadata() != unit:
            return {**result, "reason": "service_changed_before_stop"}
    except (OSError, ValueError, KeyError):
        return {**result, "reason": "backup_unavailable"}
    return _maintenance_phases(targets, originals, metadata, unit, result)


def _maintenance_phases(targets: dict, originals: dict, metadata: dict, unit: dict, result: dict) -> dict:
    result["status"] = "maintenance_failed"
    if not _unit_action("stop"):
        return {**result, "reason": "stop_failed"}
    if not _stopped():
        return {**result, "reason": "stop_not_confirmed"}
    # Do not clobber a concurrent administrator's changed file, even to restore
    # our preimage. Leave the unit stopped and require explicit recovery.
    for name, path in SOURCE_FILES.items():
        try:
            data, current = _file_snapshot(path)
            if data != originals[name] or _file_identity(current) != _file_identity(metadata[name]):
                return {**result, "status": "recovery_required", "reason": "preimage_changed_after_stop"}
        except (OSError, ValueError):
            return {**result, "status": "recovery_required", "reason": "preimage_changed_after_stop"}
    reason = "replacement_failed"
    result["failure_phase"] = "replacement"
    try:
        for name, path in SOURCE_FILES.items():
            _atomic_replace(path, targets[name], metadata[name])
        if any(source_hash(path) != TARGET_SHA256[name] for name, path in SOURCE_FILES.items()):
            raise ValueError("target mismatch")
        reason = "start_failed"
        result["failure_phase"] = "start_command"
        startup = result["startup_status"]
        startup["start_command_ok"] = _unit_action("start")
        if startup["start_command_ok"]:
            result["failure_phase"] = "startup_unit"
            accepted = _started(unit["main_pid"], diagnostics=startup)
            result["failure_phase"] = startup["failure_phase"]
            if accepted:
                result["failure_phase"] = "target_hash_readback"
                startup["disk_hashes_match"] = all(source_hash(path) == TARGET_SHA256[name] for name, path in SOURCE_FILES.items())
                if startup["disk_hashes_match"]:
                    return {**result, "status": "applied", "reason": None, "failure_phase": None, "source_sha256": TARGET_SHA256}
                startup["failure_phase"] = "target_hash_readback"
    except (Exception, KeyboardInterrupt):
        pass  # emit only a fixed reason, never exception/source/auth values
    # A timed-out start can have succeeded on systemd. Stop and confirm again
    # before recovery; never replace code beneath a possibly running process.
    if not _stopped() and (not _unit_action("stop") or not _stopped()):
        return {**result, "status": "recovery_required", "reason": "rollback_stop_unconfirmed", "failure_phase": "rollback_stop"}
    restored = True
    for name, path in SOURCE_FILES.items():
        try:
            backup, _ = _file_snapshot(BACKUP_DIRECTORY / f"{name}.py")
            current, _ = _file_snapshot(path)
            if (hashlib.sha256(backup).hexdigest() != PREIMAGE_SHA256[name]
                    or hashlib.sha256(current).hexdigest() not in (PREIMAGE_SHA256[name], TARGET_SHA256[name])):
                raise ValueError("rollback compare-and-swap mismatch")
            _atomic_replace(path, backup, metadata[name])
        except (Exception, KeyboardInterrupt):
            restored = False  # still attempt the other fixed file
    if not restored or any(source_hash(path) != PREIMAGE_SHA256[name] for name, path in SOURCE_FILES.items()):
        return {**result, "status": "recovery_required", "reason": "rollback_failed"}
    rollback = {"code_restored": True, "start_command_ok": _unit_action("start"), **_startup_status()}
    result["rollback_status"] = rollback
    if not rollback["start_command_ok"] or not _started(unit["main_pid"], diagnostics=rollback):
        # Failed original-code start is not retried. Close uncertain admission.
        stopped = _unit_action("stop") and _stopped()
        return {**result, "status": "recovery_required", "reason": "rollback_start_failed",
                "stopped_after_rollback": stopped}
    return {**result, "status": "rolled_back", "reason": reason, "source_sha256": PREIMAGE_SHA256}


def _original_backup() -> tuple | str:
    """Only the three original private files; no rewrite, rename or new backup.

    The sole retry marker is checked without following it. Existing markers,
    including incomplete/crashed attempts, consume the retry forever.
    """
    directory_fd = None
    try:
        if BACKUP_DIRECTORY.resolve() != BACKUP_DIRECTORY:
            return "original_backup_invalid"
        directory_fd = os.open(BACKUP_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = os.fstat(directory_fd)
        if before.st_uid != BACKUP_OWNER_UID or stat.S_IMODE(before.st_mode) != 0o700:
            return "original_backup_invalid"
        try:
            os.stat("retry-1", dir_fd=directory_fd, follow_symlinks=False)
            return "retry_already_consumed"
        except FileNotFoundError:
            pass
        names = set()
        with os.scandir(directory_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= 3:
                    return "original_backup_invalid"
                names.add(entry.name)
        if names != {"gateway.py", "codex_adapter.py", "metadata.json"}:
            return "original_backup_invalid"
        files = {}
        for name in ("gateway.py", "codex_adapter.py", "metadata.json"):
            initial = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            limit = MAX_BACKUP_METADATA_BYTES if name == "metadata.json" else MAX_SOURCE_BYTES
            data, _, _ = _regular_bytes(name, limit, directory_fd=directory_fd)
            after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (data is None or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
                    or after.st_uid != BACKUP_OWNER_UID or stat.S_IMODE(after.st_mode) != 0o600
                    or _file_identity(initial) != _file_identity(after)):
                return "original_backup_invalid"
            files[name] = data
        header = json.loads(files["metadata.json"], object_pairs_hook=_unique_object)
        if (not isinstance(header, dict) or set(header) != {
                "reviewed_source_commit", "preimage_sha256", "target_sha256", "metadata"}
                or header["reviewed_source_commit"] != REVIEWED_SOURCE_COMMIT
                or header["preimage_sha256"] != PREIMAGE_SHA256 or header["target_sha256"] != TARGET_SHA256
                or not isinstance(header["metadata"], dict) or set(header["metadata"]) != set(SOURCE_FILES)):
            return "original_backup_invalid"
        for name in SOURCE_FILES:
            info = header["metadata"][name]
            if (not isinstance(info, dict) or set(info) != {"uid", "gid", "mode"}
                    or any(type(info[key]) is not int or not 0 <= info[key] < 2**31 for key in ("uid", "gid"))
                    or type(info["mode"]) is not int or not 0 <= info["mode"] <= 0o777
                    or hashlib.sha256(files[f"{name}.py"]).hexdigest() != PREIMAGE_SHA256[name]):
                return "original_backup_invalid"
        after = os.fstat(directory_fd)
        current = BACKUP_DIRECTORY.stat(follow_symlinks=False)
        if _file_identity(before) != _file_identity(after) or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            return "original_backup_invalid"
        return header, hashlib.sha256(files["metadata.json"]).hexdigest()
    except (OSError, ValueError, KeyError, UnicodeError, RecursionError):
        return "original_backup_invalid"
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def retry_service_once(*, acknowledge_interruption: bool = False) -> dict:
    """One explicitly reviewed retry of one known rollback, not a general retry.

    Original backups stay immutable. A durable separate retry-1 admission is
    required before STOP and is never removed, even after crash/failure. A
    terminal receipt failure reports the true runtime outcome and audit failure;
    it cannot trigger another stop/start, rollback, model or request replay.
    """
    result = {**_maintenance_result(), "operation": "retry_once", "reviewed_prior_run": REVIEWED_ROLLBACK_BINDING,
              "retry_slot_consumed": False, "admission_receipt_written": False, "terminal_receipt_written": False}
    if acknowledge_interruption is not True:
        return {**result, "reason": "interruption_not_acknowledged"}
    original = _original_backup()
    if isinstance(original, str):
        return {**result, "reason": original, "retry_slot_consumed": original == "retry_already_consumed"}
    header, header_hash = original
    prepared = _maintenance_inputs(result)
    if isinstance(prepared, str):
        return {**result, "reason": prepared}
    targets, originals, metadata, unit = prepared
    for name, info in metadata.items():
        if header["metadata"][name] != {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}:
            return {**result, "reason": "current_metadata_mismatch"}
    retry_directory = BACKUP_DIRECTORY / "retry-1"
    try:
        # No alternate namespace/fallback: mkdir is the exclusive sole-retry
        # admission. Refused/crashed attempts cannot be silently reissued.
        retry_directory.mkdir(mode=0o700)
        result["retry_slot_consumed"] = True
        operator_hash = source_hash(Path(__file__).resolve())
        if operator_hash is None:
            raise ValueError("operator hash unavailable")
        admission = {"retry_index": 1, "reviewed_prior_run": REVIEWED_ROLLBACK_BINDING,
                     "reviewed_source_commit": REVIEWED_SOURCE_COMMIT, "operator_disk_sha256": operator_hash,
                     "original_metadata_sha256": header_hash, "preimage_sha256": PREIMAGE_SHA256,
                     "target_sha256": TARGET_SHA256, "metadata": header["metadata"]}
        _write_exclusive(retry_directory / "admission.json", json.dumps(admission, sort_keys=True).encode())
        _sync_directory(retry_directory)
        _sync_directory(BACKUP_DIRECTORY)
        result["admission_receipt_written"] = True
    except FileExistsError:
        return {**result, "reason": "retry_already_consumed", "retry_slot_consumed": True}
    except (OSError, ValueError):
        # An admission write failure never authorizes STOP or namespace reuse.
        outcome = {**result, "reason": "retry_admission_unavailable"}
        if not result["retry_slot_consumed"]:
            return outcome
    else:
        if unit_metadata() != unit:
            outcome = {**result, "reason": "service_changed_before_stop"}
        else:
            try:
                outcome = _maintenance_phases(targets, originals, metadata, unit, result)
            except Exception:
                # No unrequested restart after an unexpected phase error; the
                # state is unconfirmed, not a pre-stop/admission refusal.
                outcome = {**result, "status": "recovery_required", "reason": "maintenance_state_unconfirmed",
                           "failure_phase": "maintenance_exception"}
    try:
        _write_exclusive(retry_directory / "terminal.json", json.dumps(outcome, sort_keys=True).encode())
        _sync_directory(retry_directory)
        return {**outcome, "terminal_receipt_written": True}
    except (OSError, ValueError):
        return {**outcome, "terminal_receipt_written": False, "receipt_error": "terminal_receipt_unavailable"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "maintain", "retry-once"))
    parser.add_argument("--acknowledge-interruption", action="store_true")
    args = parser.parse_args(argv)
    if args.operation == "inspect":
        result = inspect_service()
    elif args.operation == "maintain":
        result = maintain_service(acknowledge_interruption=args.acknowledge_interruption)
    else:
        result = retry_service_once(acknowledge_interruption=args.acknowledge_interruption)
    print(json.dumps(result, sort_keys=True))
    if args.operation == "inspect":
        return 0
    successful = result["status"] == "applied" and (args.operation != "retry-once" or result["terminal_receipt_written"] is True)
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
