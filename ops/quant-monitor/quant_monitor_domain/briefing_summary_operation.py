"""One synthetic briefing summary operation; no production defaults or scheduler."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Callable
from urllib.parse import quote, urlsplit


SUMMARY_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string", "minLength": 1, "maxLength": 4000}},
    "required": ["summary"], "additionalProperties": False}


@dataclass(frozen=True)
class SyntheticSummaryContext:
    project: str
    instance: str
    occurrence: str
    state_root: Path
    resource: str
    provider: str
    model: str
    profile: str
    timeout_seconds: int
    deadline: datetime
    now: Callable[[], datetime]
    client: object


class _Unknown(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Unknown("duplicate operation field")
        result[key] = value
    return result


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("explicit aware time required")
    if not math.isfinite(value.timestamp()):
        raise ValueError("invalid time")
    return value.astimezone(timezone.utc)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_context(context):
    """This batch accepts only explicitly supplied synthetic contexts/transports."""
    if not isinstance(context, SyntheticSummaryContext):
        return False
    try:
        from ai_service.client import TaskClient
        from ai_service.transport import PublicHTTPS
        parsed = urlsplit(context.resource)
        return (isinstance(context.client, TaskClient)
            and not isinstance(context.client.transport, PublicHTTPS)
            and isinstance(context.client.token, str) and context.client.token.startswith("synthetic-")
            and context.client.resource == context.resource
            and parsed.scheme == "https" and bool(parsed.hostname)
            and parsed.hostname.endswith(".invalid")
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            and parsed.path in {"", "/"}
            and isinstance(context.project, str)
            and re.fullmatch(r"QuantStrategyLab/[A-Za-z0-9][A-Za-z0-9_.-]*", context.project) is not None
            and all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value)
                    for value in (context.instance, context.occurrence, context.provider, context.model, context.profile))
            and ":" not in context.profile
            and type(context.timeout_seconds) is int and 1 <= context.timeout_seconds <= 60
            and isinstance(context.state_root, Path) and context.state_root.is_absolute()
            and context.state_root.resolve() == context.state_root
            and _time(context.deadline) is not None and _time(context.now()) is not None)
    except Exception:
        return False


def _private(info, directory=False):
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        raise _Unknown("private operation state required")


class _Journal:
    def __init__(self, fd, directory, name, binding, fresh):
        self.fd, self.directory, self.name = fd, directory, name
        self.pending = name + ".pending"
        self.binding, self.fresh = binding, fresh
        self.records = []

    def sync_directory(self):
        os.fsync(self.directory)

    def append(self, record):
        # A failed write leaves a persistent sentinel. A valid-looking prefix
        # must never hide an fsync failure and grant another submission/adoption.
        marker = os.open(self.pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.directory)
        try:
            os.fsync(marker)
            self.sync_directory()
            data = (_json(record) + "\n").encode()
            os.lseek(self.fd, 0, os.SEEK_END)
            while data:
                count = os.write(self.fd, data)
                if count <= 0:
                    raise OSError("operation write failed")
                data = data[count:]
            os.fsync(self.fd)
            os.unlink(self.pending, dir_fd=self.directory)
            try:
                self.sync_directory()
            except OSError:
                # Best effort preserves visible uncertainty if directory sync
                # fails after removal. No failure authorizes remote work.
                poison = os.open(self.pending, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                                 0o600, dir_fd=self.directory)
                os.close(poison)
                raise
            self.records.append(record)
        finally:
            os.close(marker)

    def load(self):
        try:
            os.stat(self.pending, dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise _Unknown("incomplete operation write")
        if os.fstat(self.fd).st_size > 2_000_000:
            raise _Unknown("operation state too large")
        os.lseek(self.fd, 0, os.SEEK_SET)
        raw = os.read(self.fd, 2_000_001)
        if self.fresh and not raw:
            self.append({"kind": "binding", "version": 1, "binding": self.binding})
        else:
            if not raw or not raw.endswith(b"\n"):
                raise _Unknown("incomplete operation state")
            self.records = [json.loads(line, object_pairs_hook=_unique_pairs) for line in raw.splitlines()]
        if _json(self.records[0]) != _json({"kind": "binding", "version": 1, "binding": self.binding}):
            raise _Unknown("operation binding conflict")
        self.state()  # Validate the entire persisted sequence before any I/O.

    def state(self):
        state = {"intent": False, "submitted": None, "cancel": False,
                 "cancel_dispatch": False, "unknown": False, "verified": None}
        for entry in self.records[1:]:
            kind = entry.get("kind") if isinstance(entry, dict) else None
            if kind == "submit_intent" and set(entry) == {"kind"} and not state["intent"]:
                state["intent"] = True
            elif (kind == "submitted" and set(entry) == {"kind", "task_id", "created", "service_deadline"}
                  and state["intent"] and state["submitted"] is None
                  and isinstance(entry["task_id"], str) and 0 < len(entry["task_id"]) <= 200
                  and _number(entry["created"]) and _number(entry["service_deadline"])):
                state["submitted"] = entry
            elif kind == "cancel_intent" and set(entry) == {"kind"} and not state["cancel"]:
                state["cancel"] = True
            elif (kind == "cancel_dispatch" and set(entry) == {"kind"} and state["cancel"]
                  and state["submitted"] and not state["cancel_dispatch"]):
                state["cancel_dispatch"] = True
            elif kind == "unknown" and set(entry) == {"kind"} and not state["unknown"]:
                state["unknown"] = True
            elif (kind == "verified" and set(entry) == {"kind", "output_sha256"}
                  and state["submitted"] and not (state["cancel"] or state["unknown"])
                  and state["verified"] is None and isinstance(entry["output_sha256"], str)
                  and re.fullmatch(r"[0-9a-f]{64}", entry["output_sha256"])):
                state["verified"] = entry["output_sha256"]
            else:
                raise _Unknown("invalid operation transition")
        return state


@contextmanager
def _journal(context, request):
    slot = {"project": context.project, "instance": context.instance, "occurrence": context.occurrence}
    key = "briefing-summary:" + _digest(slot)
    binding = {**slot, "resource": context.resource, "provider": context.provider,
               "operation_key": key, "request": request, "request_sha256": _digest(request),
               "deadline": _time(context.deadline).isoformat()}
    directory = os.open(context.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        _private(os.fstat(directory), directory=True)
        name = _digest(slot) + ".jsonl"
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            fresh = True
        except FileExistsError:
            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
            fresh = False
        _private(os.fstat(fd))
        fcntl.flock(fd, fcntl.LOCK_EX)
        journal = _Journal(fd, directory, name, binding, fresh)
        journal.load()
        yield journal
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _check_record(record, context, request, submitted=None):
    if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
            or not 0 < len(record["id"]) <= 200 or record.get("owner") != context.project
            or record.get("provider") != context.provider or type(record.get("revision")) is not int
            or record["revision"] != 1 or record.get("worker", "missing") is not None
            or record.get("attempt", "missing") is not None or _digest(record.get("request")) != _digest(request)
            or record.get("result_kind") != "advisory" or not _number(record.get("created"))
            or not _number(record.get("deadline"))
            or not 0 < record["deadline"] - record["created"] <= context.timeout_seconds
            or record.get("status") not in {"queued", "submitting", "running", "outcome_unknown",
                "cancel_requested", "cancelled", "failed", "invalid_output", "completed"}):
        raise _Unknown("task readback identity mismatch")
    if submitted and any(record[key] != submitted[stored] for key, stored in (
        ("id", "task_id"), ("created", "created"), ("deadline", "service_deadline"))):
        raise _Unknown("original task changed")


def _result(status, reason, task_id=None):
    return {"status": status, "reason": reason, "advisory_only": True,
            **({"task_id": task_id} if task_id else {})}


def execute_summary(context, prompt, *, cancel=False):
    if (not valid_context(context) or not isinstance(prompt, str) or not prompt.strip()
            or len(prompt.encode()) > 64_000 or type(cancel) is not bool):
        return _result("unavailable", "summary_context_or_sdk_unavailable")
    request = {"mode": "api", "profile": context.profile, "model": context.model,
               "objective": prompt, "materials": [], "output_schema": SUMMARY_SCHEMA,
               "timeout_seconds": context.timeout_seconds}
    task_id = None
    try:
        with _journal(context, request) as journal:
            state = journal.state()
            expired = _time(context.now()) >= _time(context.deadline)
            if (cancel or expired) and not state["cancel"]:
                journal.append({"kind": "cancel_intent"})
                state = journal.state()
            if state["unknown"]:
                return _result("unknown", "summary_outcome_unknown")
            if journal.fresh and not state["cancel"]:
                journal.append({"kind": "submit_intent"})
                if _time(context.now()) >= _time(context.deadline):
                    journal.append({"kind": "cancel_intent"})
                    return _result("unknown", "summary_submit_outcome_unknown")
                try:
                    record = context.client.submit(request, journal.binding["operation_key"])
                    _check_record(record, context, request)
                except Exception:
                    journal.append({"kind": "unknown"})
                    return _result("unknown", "summary_submit_outcome_unknown")
                journal.append({"kind": "submitted", "task_id": record["id"],
                                "created": record["created"], "service_deadline": record["deadline"]})
                state = journal.state()
            if not state["submitted"]:
                if state["cancel"] and not state["intent"]:
                    return _result("unavailable", "summary_cancelled_before_submit")
                return _result("unknown", "summary_submit_outcome_unknown")
            task_id = state["submitted"]["task_id"]
            if state["cancel"] and not state["cancel_dispatch"]:
                journal.append({"kind": "cancel_dispatch"})
                try:
                    context.client.cancel(task_id)
                except Exception:
                    return _result("unavailable", "summary_cancellation_unconfirmed", task_id)
            try:
                record = context.client.get(task_id)
                _check_record(record, context, request, state["submitted"])
            except Exception:
                journal.append({"kind": "unknown"})
                return _result("unknown", "summary_readback_unknown", task_id)
            if record["status"] == "outcome_unknown":
                journal.append({"kind": "unknown"})
                return _result("unknown", "summary_outcome_unknown", task_id)
            if (journal.state()["cancel"] or _time(context.now()).timestamp() >= min(
                    _time(context.deadline).timestamp(), record["deadline"])):
                if not journal.state()["cancel"]:
                    journal.append({"kind": "cancel_intent"})
                return _result("unavailable", "summary_cancelled_or_expired", task_id)
            if record["status"] in {"queued", "submitting", "running"}:
                return _result("deferred", "summary_task_pending", task_id)
            if record["status"] != "completed":
                if record["status"] in {"cancel_requested", "cancelled"}:
                    journal.append({"kind": "cancel_intent"})
                return _result("unavailable", "summary_result_unavailable", task_id)
            try:
                events = context.client.request("GET", "/v1/tasks/" + quote(task_id, safe="") + "/events")
                events = events.get("events") if isinstance(events, dict) else None
                history_valid = (isinstance(events, list) and bool(events) and len(events) <= 4096
                    and all(isinstance(event, dict) and type(event.get("seq")) is int
                            and event["seq"] > 0 and _number(event.get("created")) for event in events)
                    and events[0].get("status") == "queued" and events[-1].get("status") == "completed"
                    and all(event.get("status") in {"submitting", "running", "cancel_requested"}
                            for event in events[1:-1])
                    and all(a["seq"] < b["seq"] for a, b in zip(events, events[1:])))
                if not history_valid:
                    raise _Unknown("task history unavailable")
            except Exception:
                journal.append({"kind": "unknown"})
                return _result("unknown", "summary_history_unknown", task_id)
            if any(event.get("status") in {"cancel_requested", "cancelled"} for event in events):
                journal.append({"kind": "cancel_intent"})
                return _result("unavailable", "summary_cancelled_or_expired", task_id)
            output, handle = record.get("output"), record.get("handle")
            if (not isinstance(output, dict) or set(output) != {"summary"}
                    or not isinstance(output["summary"], str) or not output["summary"].strip()
                    or len(output["summary"]) > 4000 or not isinstance(handle, dict)
                    or handle.get("task_id") != task_id or handle.get("model") != context.model
                    or handle.get("model_verification") != "provider_reported"
                    or handle.get("status") != "completed"
                    or events[-1]["created"] >= min(_time(context.deadline).timestamp(), record["deadline"])
                    or any(event["created"] > _time(context.now()).timestamp() for event in events)
                    or _time(context.now()).timestamp() >= min(_time(context.deadline).timestamp(), record["deadline"])):
                return _result("unavailable", "summary_result_unavailable", task_id)
            digest = _digest(output)
            if state["verified"] is not None and state["verified"] != digest:
                journal.append({"kind": "unknown"})
                return _result("unknown", "summary_output_changed", task_id)
            if state["verified"] is None:
                journal.append({"kind": "verified", "output_sha256": digest})
            if _time(context.now()).timestamp() >= min(_time(context.deadline).timestamp(), record["deadline"]):
                journal.append({"kind": "cancel_intent"})
                return _result("unavailable", "summary_cancelled_or_expired", task_id)
            return {"status": "available", "advisory_only": True, "text": output["summary"],
                    "task_id": task_id, "model": context.model, "model_verification": "provider_reported"}
    except Exception:
        return _result("unknown", "summary_state_or_readback_unknown", task_id)
