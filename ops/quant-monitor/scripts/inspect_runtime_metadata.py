#!/usr/bin/env python3
"""Fixed, read-only quant-monitor metadata. No secrets, service actions or imports.

No path/unit overrides are accepted. Permission failures are unknown. Environment
properties/files and unit/drop-in contents are deliberately never requested/read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone

RUNTIME_ROOT = "/home/ubuntu/quant-monitor-runtime/AIAuditBridge"
RELEASE_ROOT = "/opt/quant-monitor/releases"
MONITOR = "ops/quant-monitor"
SYNC_FILE = MONITOR + "/scripts/sync_lifecycle_artifacts.py"
SERVICES = {"codex-quant.service": "health_check.sh",
            "codex-daily-briefing.service": "daily_briefing_pipeline.sh"}
TIMERS = {name.replace(".service", ".timer"): name for name in SERVICES}
COMMON_PROPERTIES = ("LoadState", "ActiveState", "SubState", "UnitFileState", "FragmentPath", "DropInPaths")
SERVICE_PROPERTIES = COMMON_PROPERTIES + ("User", "Group", "WorkingDirectory", "ExecStart", "ExecStartPre")
TIMER_PROPERTIES = COMMON_PROPERTIES + ("Unit", "NextElapseUSecRealtime", "NextElapseUSecMonotonic")
PUBLIC_FILES = {SYNC_FILE: 2 * 1024 * 1024, ".git/HEAD": 256, ".git/refs/heads/main": 256}
LINK_PATHS = {"data": MONITOR + "/data", "venv": MONITOR + "/.venv",
              "qpk": MONITOR + "/data/lifecycle-projects/QuantPlatformKit"}
ENV_PATHS = ("QUANT_MONITOR_ROOT", "AIAUDIT_BRIDGE_ROOT", "QUANT_PLATFORM_KIT_ROOT",
             "QUANT_MONITOR_VENV", "PROJECTS_ROOT", "QUANT_PROJECTS_ROOT", "LIFECYCLE_LOCAL_ROOT")
SHA_RE = re.compile(r"[0-9a-f]{40}")
STATES = {"LoadState": {"loaded", "not-found", "error", "masked", "bad-setting", "stub"},
          "ActiveState": {"active", "inactive", "activating", "deactivating", "failed", "reloading", "maintenance", "refreshing"},
          "SubState": {"dead", "running", "exited", "failed", "waiting", "elapsed", "start", "start-pre", "start-post", "auto-restart", "stop", "stop-sigterm", "stop-sigkill"},
          "UnitFileState": {"enabled", "enabled-runtime", "disabled", "static", "indirect", "masked", "masked-runtime", "generated", "transient", "linked", "linked-runtime", "alias"}}
EXEC_RE = re.compile(
    r"\{ path=(/bin/bash|/usr/bin/bash) ; argv\[\]=(.*?) ; ignore_errors=(?:yes|no)"
    r" ; start_time=[^;{}]* ; stop_time=[^;{}]* ; pid=\d+ ; code=[^;{}]* ; status=[^;{}]* \}")


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def classify_root(root: str) -> dict | None:
    if root == RUNTIME_ROOT:
        return {"kind": "runtime_checkout", "release_sha": None, "path_sha256": digest(root)}
    prefix = RELEASE_ROOT + "/"
    if root.startswith(prefix) and SHA_RE.fullmatch(root[len(prefix):]):
        return {"kind": "immutable_release", "release_sha": root[len(prefix):], "path_sha256": digest(root)}
    return None


def root_path(descriptor: dict | None) -> str | None:
    if descriptor is None:
        return None
    return RUNTIME_ROOT if descriptor["kind"] == "runtime_checkout" else RELEASE_ROOT + "/" + descriptor["release_sha"]


def split_code_path(path: str, suffix: str) -> dict | None:
    return classify_root(path[:-len(suffix)]) if path.endswith(suffix) else None


def parse_exec(raw: str | None, expected_script: str, *, pre: bool) -> dict:
    result = {"status": "unknown", "code_root": None, "script": None,
              "path_sha256": None, "expected_entrypoint": None, "expected_shape": False}
    if raw == "":
        result["status"] = "absent"
        return result
    match = EXEC_RE.fullmatch(raw or "")
    if not match:
        return result
    parts = match[2].split(" ")
    if len(parts) < 2 or parts[0] != match[1]:
        return result
    for script in (*SERVICES.values(), "load_telegram_env.sh"):
        descriptor = split_code_path(parts[1], "/" + MONITOR + "/scripts/" + script)
        if descriptor:
            expected_args = [parts[0], parts[1]] + (["/run/quant-monitor/telegram.env"] if pre else [])
            result.update(status="known", code_root=descriptor, script=script,
                          path_sha256=digest(parts[1]), expected_entrypoint=script == expected_script,
                          expected_shape=parts == expected_args)
            break
    return result


def query_systemd(unit: str) -> dict[str, str] | None:
    if unit not in SERVICES and unit not in TIMERS:
        return None
    properties = SERVICE_PROPERTIES if unit in SERVICES else TIMER_PROPERTIES
    argv = ["/usr/bin/systemctl", "--no-pager", "show", unit, "--property=" + ",".join(properties)]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=5,
                                   env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "SYSTEMD_COLORS": "0"})
        if completed.returncode or len(completed.stdout) > 128 * 1024:
            return None
        found: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in properties:
                if key in found:
                    return None
                found[key] = value
        return found or None
    except (OSError, subprocess.SubprocessError):
        # Never echo exceptions: command output may contain credential arguments.
        return None


def source_metadata(props: dict, unit: str) -> dict:
    fragment = props.get("FragmentPath", "")
    kind = "unknown"
    for base, label in (("/etc/systemd/system", "system_unit"), ("/run/systemd/system", "runtime_unit"),
                        ("/lib/systemd/system", "vendor_unit"), ("/usr/lib/systemd/system", "vendor_unit")):
        if fragment == base + "/" + unit:
            kind = label
    drops = props.get("DropInPaths")
    paths = drops.split() if drops is not None and len(drops) <= 8192 else None
    pattern = r"/(?:etc|run|lib|usr/lib)/systemd/system/" + re.escape(unit) + r"\.d/[A-Za-z0-9_-]+\.conf"
    return {"fragment_kind": kind, "fragment_path_sha256": digest(fragment) if kind != "unknown" else None,
            "drop_in_count": len(paths) if paths is not None else None,
            "drop_ins_at_known_unit_roots": all(re.fullmatch(pattern, path) for path in paths) if paths is not None else None}


def states(props: dict) -> dict:
    return {key: props.get(key) if props.get(key) in choices else "unknown" for key, choices in STATES.items()}


def parse_timestamp(value: str | None) -> str | None:
    if not value or not re.fullmatch(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", value):
        return None
    try:
        parsed = datetime.strptime(value[4:], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        if parsed.strftime("%a") != value[:3]:
            return None
        return parsed.isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def parse_duration(value: str | None) -> int | None:
    if not value or len(value) > 128:
        return None
    if re.fullmatch(r"[1-9][0-9]{0,18}", value):
        return int(value)
    if not re.fullmatch(r"\d+(?:d|h|min|s|ms|us)(?: \d+(?:d|h|min|s|ms|us))*", value):
        return None
    scale = {"d": 86400000000, "h": 3600000000, "min": 60000000, "s": 1000000, "ms": 1000, "us": 1}
    total = sum(int(number) * scale[unit] for number, unit in re.findall(r"(\d+)(d|h|min|ms|us|s)", value))
    return total if 0 < total < 2**64 else None


class FixedReader:
    """No directory listing, arbitrary targets, symlink traversal, or data reads."""

    def _parent(self, root: str, relative: str) -> tuple[int, str]:
        if classify_root(root) is None or relative not in PUBLIC_FILES and relative not in LINK_PATHS.values():
            raise ValueError("outside fixed readset")
        components = (root + "/" + relative).split("/")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in components[1:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd, components[-1]
        except BaseException:
            os.close(fd)
            raise

    def read_file(self, root: str, relative: str) -> tuple[bytes, os.stat_result]:
        if relative not in PUBLIC_FILES:
            raise ValueError("outside fixed file readset")
        parent, name = self._parent(root, relative)
        try:
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode) or entry.st_size > PUBLIC_FILES[relative]:
                raise OSError("not bounded regular file")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > PUBLIC_FILES[relative] or (before.st_dev, before.st_ino) != (entry.st_dev, entry.st_ino):
                raise OSError("not bounded regular file")
            contents = stream.read(PUBLIC_FILES[relative] + 1)
            after = os.fstat(stream.fileno())
            if len(contents) > PUBLIC_FILES[relative] or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError("unstable file")
            return contents, after

    def link_metadata(self, root: str, relative: str, target: str) -> dict:
        result = {"kind": "unknown", "matches_known_runtime_target": None}
        try:
            parent, name = self._parent(root, relative)
            try:
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode):
                    raw_target = os.readlink(name, dir_fd=parent)
                    resolved = os.path.normpath(os.path.join(root, os.path.dirname(relative), raw_target))
                    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                        result.update(kind="symlink", matches_known_runtime_target=resolved == target)
                else:
                    result["kind"] = "directory" if stat.S_ISDIR(before.st_mode) else "other"
            finally:
                os.close(parent)
        except FileNotFoundError:
            result["kind"] = "missing"
        except OSError:
            pass
        return result

    def inspect(self, root: str) -> dict:
        if classify_root(root) is None:
            raise ValueError("outside fixed roots")
        code = {"status": "unknown", "sha256": None, "size_bytes": None, "mode_octal": None, "mtime_ns": None}
        try:
            contents, info = self.read_file(root, SYNC_FILE)
            code.update(status="readable", sha256=digest(contents), size_bytes=info.st_size,
                        mode_octal=format(stat.S_IMODE(info.st_mode), "04o"), mtime_ns=info.st_mtime_ns)
        except FileNotFoundError:
            code["status"] = "missing"
        except OSError:
            pass
        head = None
        try:
            raw = self.read_file(root, ".git/HEAD")[0].decode("ascii").strip()
            if raw == "ref: refs/heads/main":
                raw = self.read_file(root, ".git/refs/heads/main")[0].decode("ascii").strip()
            head = raw if SHA_RE.fullmatch(raw) else None
        except (OSError, UnicodeError):
            pass
        runtime_monitor = RUNTIME_ROOT + "/" + MONITOR
        data = self.link_metadata(root, LINK_PATHS["data"], runtime_monitor + "/data")
        venv = self.link_metadata(root, LINK_PATHS["venv"], runtime_monitor + "/.venv")
        qpk = {"kind": "unknown", "matches_known_runtime_target": None}
        qpk_root = root if data["kind"] == "directory" else RUNTIME_ROOT if data["matches_known_runtime_target"] is True else None
        if qpk_root:
            qpk = self.link_metadata(qpk_root, LINK_PATHS["qpk"], runtime_monitor + "/data/lifecycle-projects/QuantPlatformKit")
        return {"sync_lifecycle_artifacts": code, "git_head": head, "dirty_count": None,
                "links": {"data": data, "venv": venv, "qpk": qpk}}


def collect_metadata(*, query=query_systemd, reader=None) -> dict:
    reader = reader or FixedReader()
    output = {"schema_version": 1, "services": {}, "timers": {}, "code_roots": [],
              "services_share_code_root": None, "uninspected_environment_paths": {name: "unknown" for name in ENV_PATHS}}
    roots: set[str] = set()
    service_roots: list[str | None] = []
    for unit, script in SERVICES.items():
        props = query(unit)
        known = props is not None
        props = props or {}
        start = parse_exec(props.get("ExecStart"), script, pre=False)
        pre = parse_exec(props.get("ExecStartPre"), "load_telegram_env.sh", pre=True)
        working = split_code_path(props.get("WorkingDirectory", ""), "/" + MONITOR)
        descriptors = [start["code_root"], pre["code_root"], working]
        paths = [root_path(value) for value in descriptors]
        roots.update(path for path in paths if path)
        complete = all(paths) and start["expected_shape"] and pre["expected_shape"]
        consistent = len(set(paths)) == 1 if complete else None
        service_roots.append(paths[0] if start["expected_shape"] else None)
        output["services"][unit] = {"query_status": "ok" if known else "unknown", "source": source_metadata(props, unit),
                                    "state": states(props), "user_class": props.get("User") if props.get("User") in {"ubuntu", "root"} else "other" if props.get("User") else "unknown",
                                    "group_class": props.get("Group") if props.get("Group") in {"ubuntu", "root"} else "other" if props.get("Group") else "unknown",
                                    "exec_start": start, "exec_start_pre": pre, "working_directory": working,
                                    "code_root_consistent": consistent}
    if all(service_roots):
        output["services_share_code_root"] = len(set(service_roots)) == 1
    for unit, expected in TIMERS.items():
        props = query(unit)
        known = props is not None
        props = props or {}
        output["timers"][unit] = {"query_status": "ok" if known else "unknown", "source": source_metadata(props, unit), "state": states(props),
                                  "targets_expected_service": props["Unit"] == expected if "Unit" in props else None,
                                  "next_trigger_utc": parse_timestamp(props.get("NextElapseUSecRealtime")),
                                  "next_trigger_monotonic_us": parse_duration(props.get("NextElapseUSecMonotonic"))}
    for root in sorted(roots):
        output["code_roots"].append({"root": classify_root(root), **reader.inspect(root)})
    return output


def main() -> None:
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
        return
    if sys.argv[1:]:
        raise SystemExit("unsupported arguments")  # Never echo arbitrary CLI values.
    print(json.dumps(collect_metadata(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
