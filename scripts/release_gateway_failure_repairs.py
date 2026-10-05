"""One create-only release of PR299's fixed gateway file, never a historical retry.

An explicit acknowledgement accepts possible interruption. Bounded job metadata
may be unknown/truncated and excludes synchronous work; it never proves drain.
Only gateway code can be restored to this release's 60012ad preimage. Startup
can fail orphaned jobs/service_restart; job/quota/receipt state and provider
consumption cannot be rewound. No state cleanup, request/model replay, config,
unit, environment, credential, permission or provider changes are implemented.
The adapter is read-only throughout. Historical 8dd1 backups stay untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from types import ModuleType
from pathlib import Path

REVIEWED_SOURCE_COMMIT = "c80f2d5255e6ed58907fe55405029320f0bfcd8d"
REVIEWED_SOURCE_TREE = "5e5484d5db2e79458ea0683d6fbc604c5c77e5c2"
SAFETY_OPERATOR_SHA256 = "e0e4367eefe8c6fb9ea6c973716890145957b3466e22c60e2714035e292424c8"
GATEWAY_FILE = Path("/opt/codex-audit-bridge/service/ai_gateway_service.py")
ADAPTER_FILE = Path("/opt/codex-audit-bridge/service/adapters/codex_adapter.py")
TARGET_FILE = Path(__file__).resolve().parents[1] / "service/ai_gateway_service.py"
PREIMAGE_SHA256 = "60012ad523cd0fbef0996a6d879e3625ace3a3c52a0c84957973d6ae01b95cdf"
TARGET_SHA256 = "60fc288f0135720e1a26daf897ad5c898cada7a884753a6e6495aaf6de4f7ec0"
ADAPTER_SHA256 = "e5175a5fb2c132b24dfc59a0af365c5af8ba2f3b8f6079fe398f97111c347cd0"
RECORD_DIRECTORY = Path("/opt/codex-audit-bridge/.audit-gateway-release-c80f2d52")


def _load_safety() -> ModuleType:
    """Execute only the same bounded fixed-path helper bytes verified below.

    No script-directory import search is needed, including under Python -I.
    This proves the helper input bytes, never the runtime Python environment.
    """
    path = Path(__file__).resolve().with_name("manage_codex_audit_service_patch.py")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 128 * 1024:
            raise ValueError("unsupported release safety helper")
        data = bytearray()
        while len(data) <= 128 * 1024:
            chunk = os.read(fd, min(65536, 128 * 1024 + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode, info.st_nlink)
        if (len(data) > 128 * 1024 or identity(before) != identity(after)
                or identity(current) != identity(after) or hashlib.sha256(data).hexdigest() != SAFETY_OPERATOR_SHA256):
            raise ValueError("release safety helper mismatch")
    finally:
        os.close(fd)
    module = ModuleType("gateway_failure_repairs_safety")
    module.__file__ = str(path)
    # Compile and execute the verified in-memory snapshot, never a second read.
    exec(compile(bytes(data), str(path), "exec"), module.__dict__)
    return module


safety = _load_safety()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owner_mode(info: os.stat_result) -> dict:
    return {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}


def _adapter_unchanged(metadata: os.stat_result) -> bool:
    try:
        data, current = safety._file_snapshot(ADAPTER_FILE)
        return _sha(data) == ADAPTER_SHA256 and safety._file_identity(current) == safety._file_identity(metadata)
    except (OSError, ValueError):
        return False


def _inputs(result: dict) -> tuple | str:
    # The helper was verified before execution. A later disk change also
    # refuses this release; neither check attests the Python environment.
    if safety.source_hash(Path(safety.__file__).resolve()) != SAFETY_OPERATOR_SHA256:
        return "safety_operator_mismatch"
    try:
        target, _ = safety._file_snapshot(TARGET_FILE)
        if _sha(target) != TARGET_SHA256:
            return "target_source_mismatch"
        original, metadata = safety._file_snapshot(GATEWAY_FILE)
        if _sha(original) != PREIMAGE_SHA256:
            return "preimage_mismatch"
        adapter, adapter_metadata = safety._file_snapshot(ADAPTER_FILE)
        if _sha(adapter) != ADAPTER_SHA256:
            return "adapter_mismatch"
        unit = safety.unit_metadata()
        process = safety.service_process_metadata(unit)
        if (unit.get("identity_verified") is not True or unit.get("known_working_directory") is not True
                or unit.get("active_state") != "active" or unit.get("sub_state") != "running"
                or not all(process.get(key) is True for key in (
                    "snapshot_coherent", "entrypoint_matches_gateway", "initial_environment_known_cli_choice"))):
            return "service_identity_unproven"
        result["job_counts"] = safety.job_counts(safety.JOB_DIRECTORY)
        return target, original, metadata, adapter_metadata, unit
    except (OSError, ValueError, KeyError):
        return "release_inputs_unavailable"


def _gateway_replace(data: bytes, metadata: os.stat_result) -> None:
    """Gateway-only same-directory CAS; no path, release spec or unit arguments.

    The forward replacement pins the pre-stop file identity. Restoration only
    admits this release's two hashes and original UID/GID/mode; a changed
    preimage inode is also refused. Create-only staging never reuses old stages.
    """
    path = GATEWAY_FILE
    if path.parent.resolve() != path.parent:
        raise ValueError("noncanonical gateway parent")
    stage = f".{path.name}.audit-gateway-release-c80f2d52-stage"
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created = None
    try:
        created = safety._write_exclusive(stage, data, metadata, directory_fd=parent_fd, cleanup_on_failure=True)
        parent = os.fstat(parent_fd)
        current_parent = path.parent.stat(follow_symlinks=False)
        if (current_parent.st_dev, current_parent.st_ino) != (parent.st_dev, parent.st_ino):
            raise ValueError("gateway directory changed")
        current_data, current = safety._file_snapshot(path)
        wanted_hash, current_hash = _sha(data), _sha(current_data)
        if (_owner_mode(current) != _owner_mode(metadata)
                or wanted_hash not in (PREIMAGE_SHA256, TARGET_SHA256)
                or current_hash not in (PREIMAGE_SHA256, TARGET_SHA256)
                or ((wanted_hash == TARGET_SHA256 or current_hash == PREIMAGE_SHA256)
                    and safety._file_identity(current) != safety._file_identity(metadata))):
            raise ValueError("gateway compare-and-swap mismatch")
        os.replace(stage, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            if created is not None:
                try:
                    current = os.stat(stage, dir_fd=parent_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                        os.unlink(stage, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)


def _gateway_matches(sha: str, metadata: os.stat_result) -> bool:
    try:
        data, current = safety._file_snapshot(GATEWAY_FILE)
        return _sha(data) == sha and _owner_mode(current) == _owner_mode(metadata)
    except (OSError, ValueError):
        return False


def _result() -> dict:
    return {"operation": "release_gateway_failure_repairs", "status": "refused", "reason": None,
            "failure_phase": None, "reviewed_source_commit": REVIEWED_SOURCE_COMMIT,
            "reviewed_source_tree": REVIEWED_SOURCE_TREE,
            "release_slot_consumed": False, "admission_receipt_written": False, "terminal_receipt_written": False,
            "requests_may_be_interrupted": True, "drain_proven": False, "rollback_scope": "gateway_code_only",
            "all_builtin_tools_disabled_proven": False, "running_source_identity_verified": False,
            "runtime_python_environment_verified": False,
            "startup_status": {"start_command_ok": False, "disk_hashes_match": False, **safety._startup_status()}}


def _phases(target: bytes, original: bytes, metadata: os.stat_result,
            adapter_metadata: os.stat_result, unit: dict, result: dict) -> dict:
    result["status"] = "maintenance_failed"
    if not safety._unit_action("stop"):
        return {**result, "reason": "stop_failed"}
    if not safety._stopped():
        return {**result, "reason": "stop_not_confirmed"}
    try:
        data, current = safety._file_snapshot(GATEWAY_FILE)
        if data != original or safety._file_identity(current) != safety._file_identity(metadata):
            raise ValueError("gateway changed")
    except (OSError, ValueError):
        return {**result, "status": "recovery_required", "reason": "preimage_changed_after_stop"}
    if not _adapter_unchanged(adapter_metadata):
        return {**result, "status": "recovery_required", "reason": "adapter_changed_after_stop"}
    reason = "replacement_failed"
    result["failure_phase"] = "replacement"
    try:
        _gateway_replace(target, metadata)
        if not _gateway_matches(TARGET_SHA256, metadata) or not _adapter_unchanged(adapter_metadata):
            raise ValueError("release readback mismatch")
        reason = "start_failed"
        result["failure_phase"] = "start_command"
        startup = result["startup_status"]
        startup["start_command_ok"] = safety._unit_action("start")
        if startup["start_command_ok"]:
            accepted = safety._started(unit["main_pid"], diagnostics=startup)
            result["failure_phase"] = startup["failure_phase"]
            if accepted:
                result["failure_phase"] = "target_hash_readback"
                startup["disk_hashes_match"] = _gateway_matches(TARGET_SHA256, metadata) and _adapter_unchanged(adapter_metadata)
                if startup["disk_hashes_match"]:
                    return {**result, "status": "applied", "reason": None, "failure_phase": None,
                            "source_sha256": {"gateway": TARGET_SHA256, "codex_adapter": ADAPTER_SHA256}}
                startup["failure_phase"] = "target_hash_readback"
    except (Exception, KeyboardInterrupt):
        pass  # fixed outcomes only; no source, provider or authentication values
    # Even a failed/timed-out START may have succeeded. Stop and confirm before
    # restoring code; uncertainty never grants permission to write or replay.
    if not safety._stopped() and (not safety._unit_action("stop") or not safety._stopped()):
        return {**result, "status": "recovery_required", "reason": "rollback_stop_unconfirmed", "failure_phase": "rollback_stop"}
    if not _adapter_unchanged(adapter_metadata):
        return {**result, "status": "recovery_required", "reason": "rollback_adapter_changed"}
    try:
        backup, _ = safety._file_snapshot(RECORD_DIRECTORY / "gateway.py")
        if _sha(backup) != PREIMAGE_SHA256:
            raise ValueError("release backup mismatch")
        _gateway_replace(backup, metadata)
        if not _gateway_matches(PREIMAGE_SHA256, metadata):
            raise ValueError("restored gateway mismatch")
    except (Exception, KeyboardInterrupt):
        return {**result, "status": "recovery_required", "reason": "rollback_failed"}
    rollback = {"code_restored": True, "start_command_ok": safety._unit_action("start"), **safety._startup_status()}
    result["rollback_status"] = rollback
    if (not rollback["start_command_ok"] or not safety._started(unit["main_pid"], diagnostics=rollback)
            or not _gateway_matches(PREIMAGE_SHA256, metadata) or not _adapter_unchanged(adapter_metadata)):
        stopped = safety._unit_action("stop") and safety._stopped()
        return {**result, "status": "recovery_required", "reason": "rollback_start_failed", "stopped_after_rollback": stopped}
    return {**result, "status": "rolled_back", "reason": reason,
            "source_sha256": {"gateway": PREIMAGE_SHA256, "codex_adapter": ADAPTER_SHA256}}


def release_service(*, acknowledge_interruption: bool = False) -> dict:
    """Consume this distinct release once; failed/unknown attempts stay consumed."""
    result = _result()
    if acknowledge_interruption is not True:
        return {**result, "reason": "interruption_not_acknowledged"}
    # Check the fixed namespace before preflight, including incomplete records
    # and symlinks. Its contents are never read for eligibility or overwritten.
    try:
        RECORD_DIRECTORY.stat(follow_symlinks=False)
        return {**result, "reason": "release_already_consumed", "release_slot_consumed": True}
    except FileNotFoundError:
        pass
    except OSError:
        return {**result, "reason": "release_admission_unavailable"}
    prepared = _inputs(result)
    if isinstance(prepared, str):
        return {**result, "reason": prepared}
    target, original, metadata, adapter_metadata, unit = prepared
    try:
        if RECORD_DIRECTORY.parent.resolve() != RECORD_DIRECTORY.parent:
            raise ValueError("noncanonical release record parent")
        RECORD_DIRECTORY.mkdir(mode=0o700)
        result["release_slot_consumed"] = True
        operator_hash = safety.source_hash(Path(__file__).resolve())
        if operator_hash is None:
            raise ValueError("release operator hash unavailable")
        safety._write_exclusive(RECORD_DIRECTORY / "gateway.py", original)
        admission = {"reviewed_source_commit": REVIEWED_SOURCE_COMMIT, "reviewed_source_tree": REVIEWED_SOURCE_TREE,
                     "operator_disk_sha256": operator_hash, "preimage_sha256": PREIMAGE_SHA256,
                     "target_sha256": TARGET_SHA256, "adapter_sha256": ADAPTER_SHA256,
                     "metadata": {"gateway": _owner_mode(metadata), "codex_adapter": _owner_mode(adapter_metadata)},
                     "requests_may_be_interrupted": True, "drain_proven": False, "rollback_scope": "gateway_code_only"}
        safety._write_exclusive(RECORD_DIRECTORY / "admission.json", json.dumps(admission, sort_keys=True).encode())
        safety._sync_directory(RECORD_DIRECTORY)
        safety._sync_directory(RECORD_DIRECTORY.parent)
        if safety.source_hash(RECORD_DIRECTORY / "gateway.py") != PREIMAGE_SHA256:
            raise ValueError("release backup readback mismatch")
        result["admission_receipt_written"] = True
    except FileExistsError:
        return {**result, "reason": "release_already_consumed", "release_slot_consumed": True}
    except (OSError, ValueError):
        outcome = {**result, "reason": "release_admission_unavailable"}
        if not result["release_slot_consumed"]:
            return outcome
    else:
        if safety.unit_metadata() != unit:
            outcome = {**result, "reason": "service_changed_before_stop"}
        else:
            try:
                outcome = _phases(target, original, metadata, adapter_metadata, unit, result)
            except (Exception, KeyboardInterrupt):
                outcome = {**result, "status": "recovery_required", "reason": "release_state_unconfirmed",
                           "failure_phase": "release_exception"}
    try:
        # Persist the runtime outcome. The return-only write-confirmation flag
        # is known only after write/fsync and must not falsely describe itself.
        terminal = {key: value for key, value in outcome.items() if key != "terminal_receipt_written"}
        safety._write_exclusive(RECORD_DIRECTORY / "terminal.json", json.dumps(terminal, sort_keys=True).encode())
        safety._sync_directory(RECORD_DIRECTORY)
        return {**outcome, "terminal_receipt_written": True}
    except (OSError, ValueError):
        return {**outcome, "terminal_receipt_written": False, "receipt_error": "terminal_receipt_unavailable"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("release",))
    parser.add_argument("--acknowledge-interruption", action="store_true")
    args = parser.parse_args(argv)
    result = release_service(acknowledge_interruption=args.acknowledge_interruption)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "applied" and result["terminal_receipt_written"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
