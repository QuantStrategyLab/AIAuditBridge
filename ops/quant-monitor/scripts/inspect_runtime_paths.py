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
QPK_NAMES = ("drift_detector.py", "health_dashboard.py", "performance_monitor.py")
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
            else 256
            if path.endswith("/qpk-runtime.sha")
            else 2 * 1024 * 1024
        )
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
                raise OSError("unstable file")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(data) > limit or (
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError("unstable file")
            return data


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


def collect(*, query=query_systemd, reader=None):
    reader = reader or Reader()
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
