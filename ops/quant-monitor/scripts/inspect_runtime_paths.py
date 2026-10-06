#!/usr/bin/env python3
"""Fixed read-only path/override snapshot; never source files or print raw env.

Only reviewed public helper bytes are modeled. Unknown syntax, unsupported paths,
permissions and symlinks remain unknown. No selectors, sudo or service actions.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "fixed_metadata", Path(__file__).with_name("inspect_runtime_metadata.py")
)
assert _spec and _spec.loader
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

NAMES = base.ENV_PATHS
CONTROLS = (
    "QUANT_SENTINEL_ENV_FILE",
    "HOME",
    "PATH",
    "PYTHONPATH",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "BASHOPTS",
)
ALIASES = ("ROOT", "AAB", "AAB_ROOT", "QPK_ROOT", "VENV")
KEYS = NAMES + CONTROLS
TELEGRAM = "/run/quant-monitor/telegram.env"
PROJECTS = "/home/ubuntu/Projects"
UNKNOWN = object()
CODE_NAMES = (
    "health_check.sh",
    "common_env.sh",
    "source_telegram_env.sh",
    "sync_strategy_repos.sh",
    "sync_lifecycle_artifacts.py",
    "sync_binance_live_runs.py",
    "health_cycle.py",
    "publish_strategy_health.sh",
    "daily_briefing_pipeline.sh",
    "daily_briefing.sh",
    "daily_briefing_builder.py",
    "load_telegram_env.sh",
)
QPK_NAMES = ("drift_detector.py", "health_dashboard.py", "performance_monitor.py", "return_collector.py")
DAILY_IMPORT_FILES = (
    "service/__init__.py", "service/briefing_consumer.py",
    "service/briefing_dispatch.py", "service/runtime_digest.py",
    "service/dual_review_briefing.py", "service/dual_review_dispatch.py",
    "service/dual_review_orchestrator.py",
)
HEALTH_IMPORT_FILES = ("service/__init__.py", "service/briefing_dispatch.py",
                       "service/strategy_watch.py", "scripts/run_strategy_optimization_watcher.py")
IDENTITY_RELATIVE_FILES = DAILY_IMPORT_FILES + HEALTH_IMPORT_FILES + (base.MONITOR + "/.venv/pyvenv.cfg",)
EXPECTED = {
    "health_check.sh": {
        "2d9ec4a9240b7547f17886015c525f43826d8d7e749b5b61a904accd52408241"
    },
    "common_env.sh": {
        "23d7349718fb61cd8e841641f2197dddf90ae04f1b8b9e5ccd9ac3417eb0165f"
    },
    "source_telegram_env.sh": {
        "684e6c0a1f1834cfd388a81145debd2708cc2d25e926e9f438f2eed026525b37"
    },
    "daily_briefing_pipeline.sh": {
        "03ea8cbdbbd98ea1e419f6cfd9183787653d8c7c3c6d04366fcc492deda66f9a",
        "def3f6075059c4b3efcebadd320bf7e1a819f236166bd4295c85b6be8b893fef",
        # Receipt-only revision preserves ROOT/AAB capture and child/consumer call roots.
        "552efade3039e0223d2f918e2e6af923ba37de15d36abac63b9a3f1ff7cc1345",
    },
    "daily_briefing.sh": {
        "ac7694b5c64650d793779384609c0a236c127333fbd1ea0a2d89ce5acd098530"
    },
}
INVOCATION = (
    "MainPID",
    "ExecMainPID",
    "InvocationID",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "ActiveEnterTimestamp",
    "InactiveEnterTimestamp",
)
PROPERTIES = (
    base.SERVICE_PROPERTIES
    + ("Environment", "EnvironmentFiles", "UnsetEnvironment", "PassEnvironment")
    + INVOCATION
)


def monitor_root(path):
    return (
        base.split_code_path(path, "/" + base.MONITOR)
        if isinstance(path, str)
        else None
    )


def classify(value):
    if value is UNKNOWN:
        return {"status": "unknown", "category": None, "root": None}
    if value is None or value == "":
        return {
            "status": "absent" if value is None else "empty",
            "category": None,
            "root": None,
        }
    if value == PROJECTS:
        return {"status": "known", "category": "legacy_projects", "root": None}
    for suffix, category in (
        ("", "aab"),
        ("/" + base.MONITOR, "monitor"),
        ("/" + base.MONITOR + "/.venv", "venv"),
        ("/" + base.MONITOR + "/data/lifecycle-projects", "mirrors"),
        ("/" + base.MONITOR + "/data/lifecycle-store", "lifecycle_store"),
        (
            "/" + base.MONITOR + "/data/lifecycle-projects/QuantPlatformKit",
            "qpk_mirror",
        ),
    ):
        root = base.classify_root(
            value[: -len(suffix)]
            if suffix and value.endswith(suffix)
            else value
            if not suffix
            else ""
        )
        if root:
            return {"status": "known", "category": category, "root": root}
    if value == PROJECTS + "/AIAuditBridge":
        return {"status": "known", "category": "legacy_aab", "root": None}
    if value == PROJECTS + "/QuantPlatformKit":
        return {"status": "known", "category": "legacy_qpk", "root": None}
    return {"status": "unknown", "category": None, "root": None}


def env_category(path):
    if path == TELEGRAM:
        return "runtime_telegram"
    if isinstance(path, str) and path.endswith("/.env") and monitor_root(path[:-5]):
        return "monitor_dotenv"
    return None


def code_allowed(path):
    for relative in IDENTITY_RELATIVE_FILES:
        if path.endswith("/" + relative) and base.classify_root(path[: -len("/" + relative)]):
            return True
    for name in CODE_NAMES:
        if path.endswith(
            "/" + base.MONITOR + "/scripts/" + name
        ) and base.classify_root(path[: -len("/" + base.MONITOR + "/scripts/" + name)]):
            return True
    for suffix in (
        "/" + base.MONITOR + "/qpk-runtime.sha",
        "/scripts/consume_daily_briefing.py",
    ):
        if path.endswith(suffix) and base.classify_root(path[: -len(suffix)]):
            return True
    for name in QPK_NAMES:
        suffix = (
            "/"
            + base.MONITOR
            + "/data/lifecycle-projects/QuantPlatformKit/src/quant_platform_kit/strategy_lifecycle/"
            + name
        )
        if path.endswith(suffix) and base.classify_root(path[: -len(suffix)]):
            return True
    return False


class Reader:
    """Fixed absolute files only; no symlink traversal or directory enumeration."""

    def __init__(self):
        self._metadata_active = False

    def begin_metadata_collection(self):
        self._metadata_active = True
        self._metadata_bindings = {}
        self._metadata_cache = {}
        self._metadata_qpk = {}
        self._metadata_stamps = {}
        self._metadata_changed = False

    def end_metadata_collection(self):
        self._metadata_active = False
        self._metadata_bindings = {}
        self._metadata_cache = {}
        self._metadata_qpk = {}
        self._metadata_stamps = {}

    @staticmethod
    def _stamp(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    def _remember_metadata(self, path, info):
        if not getattr(self, "_metadata_active", False):
            return
        stamp = self._stamp(info) if info is not None else None
        previous = self._metadata_stamps.setdefault(path, stamp)
        if previous != stamp:
            self._metadata_changed = True

    def metadata_snapshot_stable(self):
        if self._metadata_changed:
            return False
        for path, expected in self._metadata_stamps.items():
            parent = None
            try:
                parts = path.split("/")
                parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                for part in parts[1:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent)
                    parent = child
                actual = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if self._stamp(actual) != expected:
                    return False
            except FileNotFoundError:
                if expected is not None:
                    return False
            except OSError:
                return False
            finally:
                if parent is not None:
                    os.close(parent)
        return True

    def path_metadata(self, path):
        if classify(path)["status"] != "known" or classify(path)["category"].startswith(
            "legacy"
        ):
            return {"kind": "unknown", "symlink_traversal": False}
        parts = path.split("/")
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[1:-1]:
                new = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                os.close(parent)
                parent = new
            entry = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            return {
                "kind": "directory"
                if stat.S_ISDIR(entry.st_mode)
                else "symlink_unknown"
                if stat.S_ISLNK(entry.st_mode)
                else "other",
                "symlink_traversal": False,
            }
        finally:
            os.close(parent)

    def read(self, path, *, environment=False):
        if not isinstance(path, str) or (
            env_category(path) is None if environment else not code_allowed(path)
        ):
            raise ValueError("outside fixed readset")
        limit = (
            65536
            if environment
            else 4096
            if path.endswith("/.venv/pyvenv.cfg")
            else 256
            if path.endswith("/qpk-runtime.sha")
            else 2 * 1024 * 1024
        )
        try:
            if getattr(self, "_metadata_active", False) and path.endswith("/strategy_lifecycle/return_collector.py"):
                key = ("file", path)
                if key not in self._metadata_cache:
                    self._metadata_cache[key] = self._read_regular(path, limit)
                return self._metadata_cache[key]
            return self._read_regular(path, limit)
        except FileNotFoundError:
            self._remember_metadata(path, None)
            raise

    def _read_regular(self, path, limit):
        parts = path.split("/")
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[1:-1]:
                new = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                os.close(parent)
                parent = new
            before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise OSError("unsupported file")
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or opened.st_size > limit
            ):
                if getattr(self, "_metadata_active", False):
                    self._metadata_changed = True
                raise OSError("unstable file")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(data) > limit or (
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                if getattr(self, "_metadata_active", False):
                    self._metadata_changed = True
                raise OSError("unstable file")
            self._remember_metadata(path, after)
            return data

    def identity_entry(self, path):
        """One fixed lstat/readlink, never follow an unknown link or list a directory."""
        allowed = path in ("/usr/bin/python3", "/usr/bin/python3.12")
        for suffix in ("/" + base.MONITOR + "/.venv", "/" + base.MONITOR + "/data",
                       "/" + base.MONITOR + "/.venv/bin/python",
                       "/" + base.MONITOR + "/.venv/bin/python3"):
            allowed = allowed or path.endswith(suffix) and base.classify_root(path[: -len(suffix)]) is not None
        if not allowed:
            raise ValueError("outside fixed identity metadata readset")
        try:
            return self._identity_entry(path)
        except FileNotFoundError:
            self._remember_metadata(path, None)
            raise

    def _identity_entry(self, path):
        parts = path.split("/")
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[1:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = new
            before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            result = {"kind": "directory" if stat.S_ISDIR(before.st_mode) else
                      "regular" if stat.S_ISREG(before.st_mode) else "symlink" if stat.S_ISLNK(before.st_mode) else "other",
                      "executable_mode": bool(before.st_mode & 0o111), "accepted_link_target": None}
            if stat.S_ISLNK(before.st_mode):
                raw = os.readlink(parts[-1], dir_fd=parent)
                target = os.path.normpath(os.path.join(os.path.dirname(path), raw))
                after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                    if getattr(self, "_metadata_active", False):
                        self._metadata_changed = True
                    raise OSError("unstable identity metadata")
                fixed = (base.RUNTIME_ROOT + "/" + base.MONITOR + "/.venv",
                         base.RUNTIME_ROOT + "/" + base.MONITOR + "/data",
                         "/usr/bin/python3", "/usr/bin/python3.12")
                # Return only fixed public targets; never return an unrecognized link value.
                if target in fixed:
                    result["accepted_link_target"] = target
            self._remember_metadata(path, before)
            return result
        finally:
            os.close(parent)

    def _metadata_file(self, path, limit):
        if not self._metadata_active or self._metadata_bindings.get(path) != ("file", limit):
            raise ValueError("unbound metadata leaf")
        key = ("file", path)
        if key not in self._metadata_cache:
            try:
                self._metadata_cache[key] = self._read_regular(path, limit)
            except FileNotFoundError:
                self._remember_metadata(path, None)
                raise
        return self._metadata_cache[key]

    def _metadata_entry(self, path):
        if not self._metadata_active or self._metadata_bindings.get(path) != ("entry", None):
            raise ValueError("unbound metadata leaf")
        key = ("entry", path)
        if key not in self._metadata_cache:
            try:
                self._metadata_cache[key] = self._identity_entry(path)
            except FileNotFoundError:
                self._remember_metadata(path, None)
                raise
        return self._metadata_cache[key]

    def declared_candidate_metadata(self, unit, roles, qpk):
        """Bind only this invocation's selected declarations, then read fixed leaves."""
        result = declared_metadata_output()
        allowed_roles = {"consumer"} if unit == "codex-quant.service" else {"consumer", "builder"}
        if not self._metadata_active or unit not in base.SERVICES or not set(roles) <= allowed_roles:
            return result
        self._metadata_bindings = {}
        selected = {}
        for role, values in roles.items():
            selected[role] = {}
            declaration = values["declaration"]
            venv = values["venv"]
            actual = interpreter_declaration_path(venv, venv=True)
            if (role == "builder" or unit == "codex-quant.service") and (
                actual["category"] == "external_venv_candidate"
                and declaration["venv"]["selection"] == "explicit"
                and declaration["venv"]["declaration"] == actual
            ):
                selected[role]["venv"] = venv
                self._metadata_bindings[venv + "/pyvenv.cfg"] = ("file", 4096)
                for name in ("python", "python3"):
                    self._metadata_bindings[venv + "/bin/" + name] = ("entry", None)
            first = declaration["path"]["first_unrecognized_declared_prefix"]
            path = values["path"]
            if first and isinstance(path, str) and values["interpreter"].get("stop_reason") == "unclassified_path_prefix":
                prefixes = path.split(":")
                index = first["index"]
                if 0 <= index < len(prefixes) <= 16:
                    prefix = prefixes[index]
                    actual = interpreter_declaration_path(prefix)
                    if (actual["path_sha256"] and actual == {k: v for k, v in first.items() if k != "index"}
                        and all(p in ("/usr/bin", "/bin") for p in prefixes[:index])):
                        selected[role]["prefix"] = prefix
                        self._metadata_bindings[prefix + "/python3"] = ("entry", None)
        if isinstance(qpk, str) and classify(qpk)["category"] == "qpk_mirror":
            self._metadata_bindings[qpk + "/.git/HEAD"] = ("file", 256)
            self._metadata_bindings[qpk + "/src/quant_platform_kit/strategy_lifecycle/return_collector.py"] = ("file", 2 * 1024 * 1024)
        else:
            qpk = None
        try:
            for role, paths in selected.items():
                item = {}
                if "venv" in paths:
                    venv = paths["venv"]
                    item["venv"] = {"path_sha256": base.digest(venv),
                        "pyvenv_cfg": metadata_file_result(self, venv + "/pyvenv.cfg", 4096),
                        "python_links": {name: metadata_entry_result(self, venv + "/bin/" + name)
                                         for name in ("python", "python3")}}
                if "prefix" in paths:
                    path = paths["prefix"] + "/python3"
                    item["path_prefix"] = {"path_sha256": base.digest(path), **metadata_entry_result(self, path)}
                result["roles"][role] = item
            if qpk is not None:
                if qpk not in self._metadata_qpk:
                    self._metadata_qpk[qpk] = {"root_path_sha256": base.digest(qpk),
                        "head": metadata_file_result(self, qpk + "/.git/HEAD", 256, head=True),
                        "return_collector": metadata_file_result(self, qpk + "/src/quant_platform_kit/strategy_lifecycle/return_collector.py", 2 * 1024 * 1024),
                        "clean_checkout_proof": None}
                result["qpk"] = dict(self._metadata_qpk[qpk])
            result.update(status="metadata_only", reason=None)
            return result
        finally:
            self._metadata_bindings = {}


def declared_metadata_output(reason="metadata_unavailable"):
    return {"scope": "same_snapshot_declared_candidate_metadata", "status": "unknown",
            "reason": reason, "interpreter_execution_proof": False, "import_execution_proof": False,
            "roles": {}, "qpk": None}


def metadata_file_result(reader, path, limit, *, head=False):
    result = {"status": "unknown", "sha256": None}
    if head:
        result["commit"] = None
    try:
        raw = reader._metadata_file(path, limit)
        result.update(status="readable", sha256=base.digest(raw))
        if head and re.fullmatch(rb"[0-9a-f]{40}\n?", raw):
            result["commit"] = raw.decode("ascii").strip()
    except FileNotFoundError:
        result["status"] = "missing"
    except (OSError, ValueError):
        pass
    return result


def metadata_entry_result(reader, path):
    result = {"status": "unknown", "kind": "unknown", "executable_mode": None,
              "link_target_kind": "unknown"}
    try:
        entry = reader._metadata_entry(path)
        kind = entry.get("kind")
        if kind in {"regular", "symlink", "missing"}:
            result.update(status="metadata_only", kind=kind,
                          executable_mode=entry.get("executable_mode") if isinstance(entry.get("executable_mode"), bool) else None)
            result["link_target_kind"] = {"/usr/bin/python3": "system_python3", "/usr/bin/python3.12": "system_python312"}.get(entry.get("accepted_link_target"), "unknown")
    except FileNotFoundError:
        result.update(status="missing", kind="missing")
    except (OSError, ValueError):
        pass
    return result


def query_systemd(unit):
    if unit not in base.SERVICES:
        return None
    try:
        proc = subprocess.run(
            [
                "/usr/bin/systemctl",
                "--no-pager",
                "show",
                unit,
                "--property=" + ",".join(PROPERTIES),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "SYSTEMD_COLORS": "0",
            },
        )
        if proc.returncode or len(proc.stdout) > 128 * 1024:
            return None
        props = {}
        for line in proc.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep and key in PROPERTIES:
                if key in props:
                    return None
                props[key] = value
        return (
            props
            if all(
                k in props
                for k in (
                    "Environment",
                    "EnvironmentFiles",
                    "UnsetEnvironment",
                    "PassEnvironment",
                )
            )
            else None
        )
    except (OSError, subprocess.SubprocessError):
        return None


def words(raw):
    if (
        not isinstance(raw, str)
        or len(raw) > 65536
        or "\\" in raw
        or "\n" in raw
        or "\r" in raw
    ):
        return None
    try:
        return shlex.split(raw, comments=False, posix=True)
    except ValueError:
        return None


def literal(raw):
    if re.fullmatch(r"[A-Za-z0-9_./:@%+,=-]*", raw):
        return raw
    if (
        len(raw) >= 2
        and raw[0] == raw[-1] == "'"
        and "'" not in raw[1:-1]
        and all(32 <= ord(c) <= 126 for c in raw)
    ):
        return raw[1:-1]
    if (
        len(raw) >= 2
        and raw[0] == raw[-1] == '"'
        and not any(c in raw[1:-1] for c in '$`\\"')
        and all(32 <= ord(c) <= 126 for c in raw)
    ):
        return raw[1:-1]
    return UNKNOWN


def parse_file(data, *, shell=True):
    values = {}
    aliases = False
    unsupported = False
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return {}, {
            "unsupported_syntax": True,
            "shell_alias_override": False,
            "path_or_pythonpath_override": False,
        }
    if "\x00" in text or len(text.splitlines()) > 1024:
        unsupported = True
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export )?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            unsupported = True
            continue
        name, raw = match.groups()
        value = literal(raw)
        if not shell and line.startswith("export "):
            unsupported = True
        if value is UNKNOWN:
            unsupported = True
        if (
            name in {"SHELLOPTS", "BASHOPTS", "EUID", "UID", "PPID", "BASH_VERSINFO"}
            or name in {"BASH_ENV", "ENV"}
            and value not in (None, "")
        ):
            unsupported = True
        if name == "PATH" and value not in ("/usr/bin:/bin", "/bin:/usr/bin"):
            unsupported = True
        if name in ALIASES:
            aliases = True
        if name in KEYS:
            values[name] = value
        # All other values, including credentials, are discarded immediately.
    return values, {
        "unsupported_syntax": unsupported,
        "shell_alias_override": aliases,
        "path_or_pythonpath_override": any(k in values for k in ("PATH", "PYTHONPATH")),
    }


def mark_unknown(env, origin, source):
    for name in KEYS:
        env[name] = UNKNOWN
        origin[name] = source


def read_env(reader, path, *, shell=True):
    category = env_category(path)
    info = {
        "category": category,
        "status": "unknown",
        "overrides": [],
        "unsupported_syntax": None,
        "shell_alias_override": None,
        "path_or_pythonpath_override": None,
    }
    if category is None:
        return None, info
    try:
        data = reader.read(path, environment=True)
        values, flags = parse_file(data, shell=shell)
        info.update(
            status="parsed" if not flags["unsupported_syntax"] else "unsupported",
            overrides=[x for x in NAMES if x in values],
            **flags,
        )
        return values if not flags["unsupported_syntax"] and not flags[
            "shell_alias_override"
        ] else None, info
    except FileNotFoundError:
        info["status"] = "missing"
        return {}, info
    except (OSError, ValueError):
        return None, info


def system_environment(props, reader):
    env = dict.fromkeys(KEYS)
    origins = {x: "absent" for x in KEYS}
    info = {
        "environment_syntax": "unknown",
        "environment_files": [],
        "unset_variables": [],
        "pass_environment_affects_paths": None,
    }
    tokens = words(props.get("Environment"))
    passed = words(props.get("PassEnvironment"))
    unset = words(props.get("UnsetEnvironment"))
    rawfiles = props.get("EnvironmentFiles")
    matches = list(
        re.finditer(r"([^\s]+) \(ignore_errors=(yes|no)\)(?: |$)", rawfiles or "")
    )
    files_known = (
        isinstance(rawfiles, str)
        and "".join(m[0] for m in matches) == rawfiles
        and len(matches) <= 16
    )
    if tokens is None or passed is None or unset is None or not files_known:
        mark_unknown(env, origins, "systemd_unsupported")
        return env, origins, info
    info["environment_syntax"] = "supported"
    affect = any(x in KEYS for x in passed)
    info["pass_environment_affects_paths"] = affect
    for name in passed:
        if name in KEYS:
            env[name] = UNKNOWN
            origins[name] = "systemd_pass_environment_unknown"
    for token in tokens:
        name, sep, value = token.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            mark_unknown(env, origins, "systemd_unsupported")
            return env, origins, info
        if name in KEYS:
            env[name] = value
            origins[name] = "systemd_environment"
    for index, match in enumerate(matches):
        values, fileinfo = read_env(reader, match[1], shell=False)
        fileinfo["order"] = index
        fileinfo["optional"] = match[2] == "yes"
        info["environment_files"].append(fileinfo)
        if values is None or fileinfo["status"] == "missing" and match[2] != "yes":
            mark_unknown(env, origins, "systemd_environment_file_unknown")
        else:
            for name, value in values.items():
                env[name] = value
                origins[name] = "systemd_environment_file"
    for token in unset:
        name, sep, value = token.partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            mark_unknown(env, origins, "systemd_unset_unknown")
            break
        if name in KEYS:
            if not sep or env[name] is UNKNOWN:
                env[name] = None if not sep else UNKNOWN
                origins[name] = "systemd_unset"
            elif env[name] == value:
                env[name] = None
                origins[name] = "systemd_unset"
            if name in NAMES:
                info["unset_variables"].append(name)
    return env, origins, info


def projection(env, origins):
    return {
        name: {**classify(env.get(name)), "provenance": origins.get(name, "absent")}
        for name in NAMES
    }


def default(env, origins, name, value, source):
    if env.get(name) is None or env.get(name) == "":
        env[name] = value
        origins[name] = source


def add_suffix(value, suffix):
    return value + suffix if isinstance(value, str) else UNKNOWN


def public_hash(reader, path):
    result = {"status": "unknown", "sha256": None, "matches_reviewed_helper": None}
    try:
        data = reader.read(path)
        sha = hashlib.sha256(data).hexdigest()
        name = path.rsplit("/", 1)[-1]
        result.update(
            status="readable",
            sha256=sha,
            matches_reviewed_helper=sha in EXPECTED[name] if name in EXPECTED else None,
        )
        if name == "qpk-runtime.sha":
            pin = data.decode("ascii").strip()
            result["pin"] = pin if base.SHA_RE.fullmatch(pin) else None
    except FileNotFoundError:
        result["status"] = "missing"
    except (OSError, ValueError, UnicodeError):
        pass
    return result


def add_monitor_files(reader, monitor, output, names, *, pin=False):
    root = monitor_root(monitor)
    if root is None:
        return {}
    category = root["kind"] + (":" + root["release_sha"] if root["release_sha"] else "")
    if category not in output:
        output[category] = {"root": root, "files": {}}
    for name in names:
        if name in CODE_NAMES and name not in output[category]["files"]:
            output[category]["files"][name] = public_hash(
                reader, monitor + "/scripts/" + name
            )
    if pin and "qpk-runtime.sha" not in output[category]["files"]:
        output[category]["files"]["qpk-runtime.sha"] = public_hash(
            reader, monitor + "/qpk-runtime.sha"
        )
    return output[category]["files"]


def add_qpk_files(reader, qpk, output):
    if isinstance(qpk, str) and classify(qpk)["category"] == "qpk_mirror":
        key = "qpk:" + qpk
        if key not in output:
            output[key] = {
                "root": classify(qpk)["root"],
                "files": {
                    name: public_hash(
                        reader,
                        qpk + "/src/quant_platform_kit/strategy_lifecycle/" + name,
                    )
                    for name in QPK_NAMES
                },
            }


def helper_known(files, name):
    return files.get(name, {}).get("matches_reviewed_helper") is True


def helper_model_gate(env, props, entryfiles, rootfiles, unit):
    """Existing pre-model predicates only; null means an input is unknown."""
    checks = {
        "entrypoint_helper_reviewed": entryfiles.get(base.SERVICES[unit], {}).get(
            "matches_reviewed_helper"
        ),
        "telegram_helper_reviewed": rootfiles.get("source_telegram_env.sh", {}).get(
            "matches_reviewed_helper"
        ),
        "path_absent_or_canonical": None
        if env.get("PATH") is UNKNOWN
        else env.get("PATH") in (None, "/usr/bin:/bin", "/bin:/usr/bin"),
        "user_is_ubuntu": None
        if props.get("User") is None
        else props.get("User") == "ubuntu",
    }
    for name in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
        checks[name.lower() + "_absent_or_empty"] = (
            None if env[name] is UNKNOWN else env[name] in (None, "")
        )
    if unit == "codex-quant.service":
        checks["common_helper_reviewed"] = rootfiles.get("common_env.sh", {}).get(
            "matches_reviewed_helper"
        )
    return all(value is True for value in checks.values()), checks


def common(env, origins, monitor):
    default(env, origins, "QUANT_MONITOR_ROOT", monitor, "common_default")
    root = monitor_root(monitor)
    aab = base.root_path(root) if root else UNKNOWN
    default(env, origins, "AIAUDIT_BRIDGE_ROOT", aab, "common_default")
    home = env.get("HOME")
    default(
        env,
        origins,
        "PROJECTS_ROOT",
        "/home/ubuntu/Projects" if home == "/home/ubuntu" else UNKNOWN,
        "common_default",
    )
    default(
        env,
        origins,
        "QUANT_PROJECTS_ROOT",
        add_suffix(monitor, "/data/lifecycle-projects"),
        "common_default",
    )
    default(
        env,
        origins,
        "LIFECYCLE_LOCAL_ROOT",
        add_suffix(monitor, "/data/lifecycle-store"),
        "common_default",
    )
    default(
        env,
        origins,
        "QUANT_PLATFORM_KIT_ROOT",
        add_suffix(env["QUANT_PROJECTS_ROOT"], "/QuantPlatformKit"),
        "common_default",
    )
    venv = (
        env["QUANT_MONITOR_VENV"]
        if env["QUANT_MONITOR_VENV"] not in (None, "")
        else add_suffix(monitor, "/.venv")
    )
    return {
        "aab": classify(env["AIAUDIT_BRIDGE_ROOT"]),
        "qpk": classify(env["QUANT_PLATFORM_KIT_ROOT"]),
        "venv": classify(venv),
    }


def telegram(env, origins, monitor, reader):
    selector = env.get("QUANT_SENTINEL_ENV_FILE")
    path = TELEGRAM if selector is None or selector == "" else selector
    values, info = read_env(reader, path)
    if info["status"] == "missing":
        values, info = read_env(reader, add_suffix(monitor, "/.env"))
    if values is None:
        mark_unknown(env, origins, "shell_environment_unknown")
    else:
        for name, value in values.items():
            env[name] = value
            origins[name] = "source_telegram_env"
    return info


def selected_path_metadata(env, reader):
    result = {}
    for name in NAMES:
        value = env.get(name)
        meta = {"kind": "unknown", "symlink_traversal": False}
        if isinstance(value, str) and classify(value)["status"] == "known":
            try:
                meta = reader.path_metadata(value)
            except FileNotFoundError:
                meta["kind"] = "missing"
            except (OSError, ValueError):
                pass
        result[name] = meta
    return result


def invocation(props):
    output = {"state": base.states(props)}
    for name in INVOCATION:
        value = props.get(name)
        if name.endswith("Timestamp"):
            output[name] = base.parse_timestamp(value)
        elif name in ("MainPID", "ExecMainPID", "ExecMainCode", "ExecMainStatus"):
            output[name] = (
                int(value)
                if value and re.fullmatch(r"[0-9]{1,10}", value) and int(value) < 2**32
                else None
            )
        elif name == "InvocationID":
            output[name] = (
                value if value and re.fullmatch(r"[0-9a-f]{32}", value) else None
            )
        else:
            output[name] = (
                value
                if value
                in {
                    "success",
                    "exit-code",
                    "signal",
                    "core-dump",
                    "timeout",
                    "watchdog",
                    "start-limit-hit",
                    "resources",
                    "protocol",
                    "oom-kill",
                }
                else None
            )
    return output


def identity_metadata(reader, path):
    try:
        return reader.identity_entry(path)
    except FileNotFoundError:
        return {"kind": "missing"}
    except (OSError, ValueError, AttributeError):
        return {"kind": "unknown"}


def identity_alias(reader, path, category):
    """Only the installer's exact shared data/venv link can be mapped once."""
    if not isinstance(path, str):
        return None
    descriptor = classify(path)
    if descriptor["category"] != category:
        return None
    suffix = "/" + base.MONITOR + ("/.venv" if category == "venv" else "/data")
    prefix = path if category == "venv" else path.split("/data/", 1)[0] + "/data"
    meta = identity_metadata(reader, prefix)
    if meta["kind"] == "directory":
        return path
    target = base.RUNTIME_ROOT + suffix
    if meta["kind"] == "symlink" and meta.get("accepted_link_target") == target:
        return target + path[len(prefix):]
    return None


def static_interpreter(reader, path_value, venv=None, *, common_prepend=True):
    result = {"status": "unknown", "stop_reason": None, "candidate_kind": None,
              "candidate_path_sha256": None, "binary_bytes_verified": False,
              "candidate_executable_metadata_verified": False, "pyvenv_cfg": None}
    prefix = None
    if venv is not None:
        if classify(venv)["category"] != "venv":
            result["stop_reason"] = "unclassified_venv"
            return result
        selected = identity_alias(reader, venv, "venv")
        if selected is None:
            result["stop_reason"] = "metadata_unavailable"
            return result
        result["pyvenv_cfg"] = public_hash(reader, selected + "/pyvenv.cfg")
        enabled = identity_metadata(reader, selected + "/bin/python")
        if enabled["kind"] == "unknown" or enabled["kind"] == "other":
            result["stop_reason"] = "venv_python_metadata_unknown"
            return result
        if enabled["kind"] == "regular" and enabled.get("executable_mode"):
            prefix = selected + "/bin"
        elif enabled["kind"] == "symlink":
            target = enabled.get("accepted_link_target")
            if target not in ("/usr/bin/python3", "/usr/bin/python3.12"):
                result["stop_reason"] = "venv_python_link_unknown"
                return result
            # Symlink mode is irrelevant to -x. Check only its exact fixed target.
            target_meta = identity_metadata(reader, target)
            if target_meta["kind"] != "regular" or not target_meta.get("executable_mode"):
                result["stop_reason"] = "venv_python_target_unknown"
                return result
            prefix = selected + "/bin"
    if path_value is UNKNOWN or path_value is None:
        result["stop_reason"] = "inherited_path_unknown"
        return result
    paths = path_value.split(":") if isinstance(path_value, str) else []
    if prefix and common_prepend:
        paths.insert(0, prefix)
    if not paths or len(paths) > 16:
        result["stop_reason"] = "path_shape_unsupported"
        return result
    for directory in paths:
        if directory not in ("/usr/bin", "/bin", prefix) or not directory:
            result["stop_reason"] = "unclassified_path_prefix"
            return result
        # Do not traverse /bin's symlink; its alias is not proven by this readset.
        if directory == "/bin":
            result["stop_reason"] = "system_bin_alias_unverified"
            return result
        candidate = directory + "/python3"
        meta = identity_metadata(reader, candidate)
        if meta["kind"] == "missing":
            continue
        if meta["kind"] == "regular" and not meta.get("executable_mode"):
            continue
        if meta["kind"] not in ("regular", "symlink"):
            result["stop_reason"] = "python3_metadata_unknown"
            return result
        if meta["kind"] == "symlink" and meta.get("accepted_link_target") not in ("/usr/bin/python3", "/usr/bin/python3.12"):
            result["stop_reason"] = "python3_link_unknown"
            return result
        if meta["kind"] == "symlink":
            target_meta = identity_metadata(reader, meta["accepted_link_target"])
            if target_meta["kind"] != "regular" or not target_meta.get("executable_mode"):
                result["stop_reason"] = "python3_target_metadata_unknown"
                return result
        result.update(status="candidate_metadata_only", candidate_kind="venv" if directory == prefix else "system",
                      candidate_path_sha256=base.digest(candidate), candidate_executable_metadata_verified=True)
        return result
    result["stop_reason"] = "python3_not_found_in_fixed_paths"
    return result


def interpreter_declaration_path(value, *, venv=False):
    """Lexical nonsecret selection only; never authorize a new filesystem root."""
    result = {"category": "untrusted_or_unbound", "location_family": None,
              "path_sha256": None, "installer_qualification_proof": False}
    if value is UNKNOWN or value is None or value == "":
        result["category"] = "unknown" if value is UNKNOWN else "absent" if value is None else "empty"
        return result
    if not isinstance(value, str):
        return result
    known = classify(value)
    if known["category"] == "venv":
        category = "existing_fixed_venv"
    elif not venv and value in ("/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin", "/usr/local/sbin"):
        category = "system_command_prefix"
    elif not venv and value.endswith("/bin") and classify(value[:-4])["category"] == "venv":
        category = "existing_fixed_venv_bin"
    else:
        if not isinstance(value, str) or len(value) > 512 or not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value):
            return result
        if any(part in (".", "..") for part in value.split("/")) or value in (base.RUNTIME_ROOT, base.RELEASE_ROOT) or value.startswith((base.RUNTIME_ROOT + "/", base.RELEASE_ROOT + "/")):
            return result
        if not value.startswith(("/opt/quant-monitor/", "/home/ubuntu/")):
            return result
        category = "external_venv_candidate" if venv else "external_command_prefix_candidate"
    family = ("existing_fixed_venv" if category.startswith("existing_fixed_") else "system_command_prefix" if category == "system_command_prefix"
              else "opt_quant_monitor" if value.startswith("/opt/quant-monitor/") else "ubuntu_home")
    result.update(category=category, location_family=family, path_sha256=base.digest(value))
    return result


def interpreter_declarations(env, origins, selected_venv, configured_venv, venv_origin,
                             path_before, path_origin_before, *, common_applies):
    """Already-parsed declaration fields, with one bounded residual evidence request."""
    path = env.get("PATH")
    origin = origins.get("PATH", "absent")
    selection = ("not_used_by_parent" if not common_applies else "inherited_unknown" if configured_venv is UNKNOWN
                 else "helper_default" if configured_venv is None else "empty_uses_helper_default" if configured_venv == "" else "explicit")
    venv = interpreter_declaration_path(selected_venv, venv=True)
    next_venv = ("not_used_by_parent" if not common_applies else "existing_fixed_venv_metadata_only" if venv["category"] == "existing_fixed_venv"
                else "prebind_exact_venv_then_pyvenv_cfg_and_two_python_links" if venv["category"] == "external_venv_candidate"
                else "obtain_trusted_exact_venv_declaration_before_any_file_read")
    status = "unknown" if path is UNKNOWN else "absent" if path is None else "empty" if path == "" else "literal"
    path_info = {"declaration_status": status, "provenance": origin, "prefix_count": None,
                 "search_reachability_proof": False,
                 "first_unrecognized_declared_prefix": None,
                 "literal_source_override": None if origin == path_origin_before == "source_telegram_env" and path == path_before
                    else origin == "source_telegram_env" and (origin != path_origin_before or path != path_before),
                 "next_evidence": "obtain_selected_interpreter_from_existing_operator_no_default_assumption"}
    if isinstance(path, str) and path and len(path.split(":")) <= 16:
        prefixes = path.split(":")
        path_info["prefix_count"] = len(prefixes)
        path_info["next_evidence"] = "existing_fixed_candidates_metadata_only_no_execution"
        for index, prefix in enumerate(prefixes):
            if prefix not in ("/usr/bin", "/bin"):
                declaration = interpreter_declaration_path(prefix)
                path_info["first_unrecognized_declared_prefix"] = {"index": index, **declaration}
                path_info["next_evidence_prerequisite"] = "earlier_fixed_candidates_excluded_and_original_static_stop_matches"
                path_info["next_evidence"] = ("prebind_first_unrecognized_prefix_then_python3_metadata_only" if declaration["path_sha256"]
                                             else "obtain_trusted_path_selection_before_any_candidate_read")
                break
    elif isinstance(path, str) and path:
        path_info["next_evidence"] = "obtain_bounded_trusted_path_declaration_before_any_candidate_read"
    return {"scope": "parsed_declaration_only", "filesystem_identity_proof": False,
            "interpreter_execution_proof": False, "import_execution_proof": False,
            "common_path_rule": "prepend_if_venv_bin_python_executable" if common_applies else "not_applied_by_parent",
            "venv": {"selection": selection, "provenance": "common_default" if selection in ("helper_default", "empty_uses_helper_default") else venv_origin if common_applies else "not_used_by_parent",
                     "configured_provenance": venv_origin if common_applies else "not_used_by_parent",
                     "declaration": venv, "next_evidence": next_venv}, "path": path_info}


def static_identity_output():
    return {"scope": "next_invocation_static_selection", "not_historical_adoption": True,
              "status": "unknown", "stop_reason": None, "interpreter_invoked": False,
              "import_execution_proof": False, "import_resolution_proof": False,
              "roots": {}, "files": {}, "import_candidates": None, "interpreter": None,
              "interpreter_declarations": None,
              "declared_candidate_metadata": declared_metadata_output()}


def static_adoption_identity(unit, env, origins, reader, monitor, entry, checks):
    """Independent bounded static evidence, never a relaxation of helper_model_gate."""
    result = static_identity_output()
    if not all(value is True for key, value in checks.items() if key != "path_absent_or_canonical"):
        result["stop_reason"] = "existing_non_path_precondition_unknown_or_false"
        return result
    if monitor_root(monitor) is None:
        result["stop_reason"] = "unclassified_monitor"
        return result
    env, origins = dict(env), dict(origins)
    configured_venv, venv_origin = env.get("QUANT_MONITOR_VENV"), origins.get("QUANT_MONITOR_VENV", "absent")
    path_before, path_origin_before = env.get("PATH"), origins.get("PATH", "absent")
    result["roots"] = {"entrypoint": classify(entry), "monitor": classify(monitor)}
    captured_aab = env.get("AIAUDIT_BRIDGE_ROOT")
    if unit == "codex-quant.service":
        common(env, origins, monitor)
        venv = env["QUANT_MONITOR_VENV"] if env["QUANT_MONITOR_VENV"] not in (None, "") else monitor + "/.venv"
        aab = env.get("AIAUDIT_BRIDGE_ROOT")
        qpk = env.get("QUANT_PLATFORM_KIT_ROOT")
    elif captured_aab in (None, ""):
        captured_aab = PROJECTS + "/AIAuditBridge" if env.get("HOME") == "/home/ubuntu" else UNKNOWN
    source = telegram(env, origins, monitor, reader)
    if source["status"] not in ("parsed", "missing") or source["shell_alias_override"]:
        result["stop_reason"] = "telegram_selector_or_literal_configuration_unknown"
        return result
    result["interpreter_declarations"] = {"consumer": interpreter_declarations(
        env, origins, venv if unit == "codex-quant.service" else None, configured_venv, venv_origin,
        path_before, path_origin_before, common_applies=unit == "codex-quant.service")}
    consumer_venv = venv if unit == "codex-quant.service" else None
    if unit == "codex-daily-briefing.service":
        aab = captured_aab
        result["roots"]["consume_aab"] = classify(aab)
        result["interpreter"] = static_interpreter(reader, env.get("PATH"))
        child = env["QUANT_MONITOR_ROOT"] if env["QUANT_MONITOR_ROOT"] not in (None, "") else monitor
        childfiles = add_monitor_files(reader, child, {}, ("common_env.sh", "source_telegram_env.sh", "daily_briefing_builder.py"))
        parent_script = public_hash(reader, monitor + "/scripts/daily_briefing.sh")
        if parent_script["matches_reviewed_helper"] is not True or not all(helper_known(childfiles, name) for name in ("common_env.sh", "source_telegram_env.sh")):
            result["stop_reason"] = "daily_child_helper_unknown"
            return result
        childenv, childorigins = dict(env), dict(origins)
        child_configured_venv, child_venv_origin = childenv.get("QUANT_MONITOR_VENV"), childorigins.get("QUANT_MONITOR_VENV", "absent")
        child_path_before, child_path_origin_before = childenv.get("PATH"), childorigins.get("PATH", "absent")
        common(childenv, childorigins, child)
        venv = childenv["QUANT_MONITOR_VENV"] if childenv["QUANT_MONITOR_VENV"] not in (None, "") else child + "/.venv"
        qpk = childenv.get("QUANT_PLATFORM_KIT_ROOT")
        childsource = telegram(childenv, childorigins, child, reader)
        if childsource["status"] not in ("parsed", "missing") or childsource["shell_alias_override"]:
            result["stop_reason"] = "daily_child_literal_configuration_unknown"
            return result
        result["roots"]["builder_monitor"] = classify(child)
        result["interpreter_declarations"]["builder"] = interpreter_declarations(
            childenv, childorigins, venv, child_configured_venv, child_venv_origin,
            child_path_before, child_path_origin_before, common_applies=True)
        result["builder_interpreter"] = static_interpreter(reader, childenv.get("PATH"), venv,
            common_prepend=childorigins.get("PATH") != "source_telegram_env")
        result["files"]["daily_briefing_builder.py"] = childfiles["daily_briefing_builder.py"]
        selected_monitor = child
        root_source = "captured_aab_with_pythonpath_dot"
    else:
        result["interpreter"] = static_interpreter(reader, env.get("PATH"), venv,
            common_prepend=origins.get("PATH") != "source_telegram_env")
        selected_monitor = monitor
        root_source = "common_helper_qpk_src_then_aab_if_src_exists"
    result["roots"]["import_aab"] = classify(aab)
    result["roots"]["qpk"] = classify(qpk)
    if not isinstance(aab, str) or base.classify_root(aab) is None:
        result["stop_reason"] = "unclassified_import_aab"
        return result
    # Fixed file candidates only: no package import, finder hooks, .pth or setup code.
    result["import_candidates"] = {"root_source": root_source, "path_precedence_proven": False,
        "files": {relative: public_hash(reader, aab + "/" + relative)
                  for relative in (DAILY_IMPORT_FILES if unit == "codex-daily-briefing.service" else HEALTH_IMPORT_FILES)}}
    if unit == "codex-daily-briefing.service":
        result["files"]["consume_daily_briefing.py"] = public_hash(reader, aab + "/scripts/consume_daily_briefing.py")
    for name in (("health_cycle.py",) if unit == "codex-daily-briefing.service" else ("sync_lifecycle_artifacts.py", "health_cycle.py")):
        result["files"][name] = public_hash(reader, selected_monitor + "/scripts/" + name)
    actual_qpk = identity_alias(reader, qpk, "qpk_mirror")
    result["qpk_import_candidates"] = {name: public_hash(reader, actual_qpk + "/src/quant_platform_kit/strategy_lifecycle/" + name)
                                       for name in QPK_NAMES} if actual_qpk else None
    collect_metadata = getattr(reader, "declared_candidate_metadata", None)
    if collect_metadata is not None:
        roles = {"consumer": {"venv": consumer_venv, "path": env.get("PATH"),
                              "declaration": result["interpreter_declarations"]["consumer"],
                              "interpreter": result["interpreter"]}}
        if unit == "codex-daily-briefing.service":
            roles["builder"] = {"venv": venv, "path": childenv.get("PATH"),
                                "declaration": result["interpreter_declarations"]["builder"],
                                "interpreter": result["builder_interpreter"]}
        result["declared_candidate_metadata"] = collect_metadata(unit, roles, actual_qpk)
    result["status"] = "static_candidates_only"
    return result


def collect(*, query=query_systemd, reader=None):
    reader = reader or Reader()
    begin_metadata = getattr(reader, "begin_metadata_collection", None)
    if begin_metadata is not None:
        begin_metadata()
    output = {
        "schema_version": 1,
        "scope": "fixed_path_override_snapshot",
        "process_environment_proof": False,
        "next_invocation_environment_proof": False,
        "python_import_root_proof": False,
        "services": {},
        "dependencies": {},
    }
    for unit, script in base.SERVICES.items():
        before = query(unit)
        props = before or {}
        env, origins, info = system_environment(props, reader)
        result = {
            "query_status": "ok" if before else "unknown",
            "invocation_before": invocation(props),
            "configuration": info,
            "unit_source": base.source_metadata(props, unit),
            "environment_declaration_source": "effective_systemd_aggregate",
            "systemd_execution_path_overrides": {
                x: None if env.get(x) is UNKNOWN else env.get(x) is not None
                for x in ("PATH", "PYTHONPATH")
            },
            "systemd_paths": projection(env, origins),
            "telegram_sources": [],
            "effective_paths": None,
            "selected_call_roots": {},
            "helper_model_status": "unknown",
            "helper_model_gate_checks": None,
            "static_adoption_identity": {**static_identity_output(), "stop_reason": "entrypoint_not_supported"},
        }
        output["services"][unit] = result
        start = base.parse_exec(props.get("ExecStart"), script, pre=False)
        pre = base.parse_exec(
            props.get("ExecStartPre"), "load_telegram_env.sh", pre=True
        )
        entry = base.root_path(start["code_root"])
        entry_monitor = add_suffix(entry, "/" + base.MONITOR)
        if start["expected_shape"] and start["expected_entrypoint"]:
            entryfiles = add_monitor_files(
                reader, entry_monitor, output["dependencies"], (script,)
            )
            initial_monitor = (
                env["QUANT_MONITOR_ROOT"]
                if env["QUANT_MONITOR_ROOT"] not in (None, "")
                else entry_monitor
            )
            rootfiles = add_monitor_files(
                reader,
                initial_monitor,
                output["dependencies"],
                (
                    "common_env.sh",
                    "source_telegram_env.sh",
                    "sync_strategy_repos.sh",
                    "sync_lifecycle_artifacts.py",
                    "sync_binance_live_runs.py",
                    "health_cycle.py",
                    "publish_strategy_health.sh",
                )
                if unit == "codex-quant.service"
                else ("source_telegram_env.sh", "daily_briefing.sh"),
                pin=unit == "codex-quant.service",
            )
            safe, result["helper_model_gate_checks"] = helper_model_gate(
                env, props, entryfiles, rootfiles, unit
            )
            result["static_adoption_identity"] = static_adoption_identity(
                unit, env, origins, reader, initial_monitor, entry,
                result["helper_model_gate_checks"],
            )
            supplemental_qpk = result["static_adoption_identity"]["declared_candidate_metadata"]["qpk"]
            if supplemental_qpk is not None:
                declared_pin = rootfiles.get("qpk-runtime.sha", {}).get("pin")
                head = supplemental_qpk["head"]["commit"]
                supplemental_qpk["head_matches_declared_pin"] = (
                    head == declared_pin if head is not None and declared_pin is not None else None)
            if unit == "codex-quant.service":
                if safe:
                    result["common_selected_paths"] = common(
                        env, origins, initial_monitor
                    )
                    result["after_common_paths"] = projection(env, origins)
                    add_qpk_files(
                        reader,
                        env.get("QUANT_PLATFORM_KIT_ROOT"),
                        output["dependencies"],
                    )
            if safe:
                consume_root = env.get("AIAUDIT_BRIDGE_ROOT")
                if unit == "codex-daily-briefing.service" and consume_root in (
                    None,
                    "",
                ):
                    consume_root = (
                        PROJECTS + "/AIAuditBridge"
                        if env.get("HOME") == "/home/ubuntu"
                        else UNKNOWN
                    )
                result["telegram_sources"].append(
                    telegram(env, origins, initial_monitor, reader)
                )
                shell_safe = (
                    result["telegram_sources"][-1]["status"] in {"parsed", "missing"}
                    and not result["telegram_sources"][-1]["shell_alias_override"]
                )
                if not shell_safe:
                    initial_monitor = UNKNOWN
                    consume_root = UNKNOWN
                result["selected_call_roots"]["entrypoint"] = classify(entry)
                result["selected_call_roots"]["shell_monitor"] = classify(
                    initial_monitor
                )
                if unit == "codex-daily-briefing.service":
                    child = (
                        env["QUANT_MONITOR_ROOT"]
                        if env["QUANT_MONITOR_ROOT"] not in (None, "")
                        else initial_monitor
                    )
                    childfiles = add_monitor_files(
                        reader,
                        child,
                        output["dependencies"],
                        (
                            "common_env.sh",
                            "source_telegram_env.sh",
                            "daily_briefing_builder.py",
                        ),
                    )
                    result["selected_call_roots"]["daily_child_monitor"] = classify(
                        child
                    )
                    result["selected_call_roots"]["consume_aab"] = classify(
                        consume_root
                    )
                    if isinstance(consume_root, str) and base.classify_root(
                        consume_root
                    ):
                        output["dependencies"]["consume:" + consume_root] = {
                            "root": base.classify_root(consume_root),
                            "files": {
                                "consume_daily_briefing.py": public_hash(
                                    reader,
                                    consume_root + "/scripts/consume_daily_briefing.py",
                                )
                            },
                        }
                    result["daily_child_helper_model_status"] = "unknown"
                    if (
                        helper_known(rootfiles, "daily_briefing.sh")
                        and helper_known(childfiles, "common_env.sh")
                        and helper_known(childfiles, "source_telegram_env.sh")
                    ):
                        childenv = dict(env)
                        childorigins = dict(origins)
                        result["daily_child_common_selected_paths"] = common(
                            childenv, childorigins, child
                        )
                        add_qpk_files(
                            reader,
                            childenv.get("QUANT_PLATFORM_KIT_ROOT"),
                            output["dependencies"],
                        )
                        result["telegram_sources"].append(
                            telegram(childenv, childorigins, child, reader)
                        )
                        result["daily_child_effective_paths"] = projection(
                            childenv, childorigins
                        )
                        result["daily_child_helper_model_status"] = (
                            "supported"
                            if result["telegram_sources"][-1]["status"]
                            in {"parsed", "missing"}
                            and not result["telegram_sources"][-1][
                                "shell_alias_override"
                            ]
                            else "unknown"
                        )
                result["effective_paths"] = projection(env, origins)
                result["selected_path_metadata"] = selected_path_metadata(env, reader)
                result["helper_model_status"] = (
                    "supported"
                    if all(
                        x["status"] in {"parsed", "missing"}
                        and not x["shell_alias_override"]
                        for x in result["telegram_sources"]
                    )
                    else "unknown"
                )
        if pre["expected_shape"] and pre["expected_entrypoint"]:
            add_monitor_files(
                reader,
                add_suffix(base.root_path(pre["code_root"]), "/" + base.MONITOR),
                output["dependencies"],
                ("load_telegram_env.sh",),
            )
            result["prestart_telegram_refresh_expected"] = True
        else:
            result["prestart_telegram_refresh_expected"] = None
        after = query(unit)
        result["invocation_after"] = invocation(after or {})
        result["invocation_snapshot_stable"] = (
            invocation(props) == invocation(after or {}) if before and after else None
        )
        result["configuration_snapshot_stable"] = (
            all(props.get(x) == after.get(x) for x in PROPERTIES)
            if before and after
            else None
        )
        if result["invocation_snapshot_stable"] is not True or result["configuration_snapshot_stable"] is not True:
            result["static_adoption_identity"]["status"] = "unknown"
            result["static_adoption_identity"]["stop_reason"] = "configuration_or_invocation_changed_or_unavailable"
            result["static_adoption_identity"]["declared_candidate_metadata"] = declared_metadata_output(
                "configuration_or_invocation_changed_or_unavailable")
    metadata_stable = getattr(reader, "metadata_snapshot_stable", None)
    if metadata_stable is not None and not metadata_stable():
        for result in output["services"].values():
            result["static_adoption_identity"]["declared_candidate_metadata"] = declared_metadata_output(
                "metadata_file_snapshot_changed_or_unavailable")
    end_metadata = getattr(reader, "end_metadata_collection", None)
    if end_metadata is not None:
        end_metadata()
    # Dependency identifiers contain only roots already accepted by classify_root.
    return output


def main():
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
        return
    if sys.argv[1:]:
        raise SystemExit("unsupported arguments")
    print(json.dumps(collect(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
