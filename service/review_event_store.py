"""Independent persisted notification state for PR review events."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any


STORE_SCHEMA = "qsl.review_event_store.v1"
STORE_PATH_ENV = "CODEX_AUDIT_SERVICE_REVIEW_EVENT_STORE_PATH"
DEFAULT_MAX_EVENTS = 5_000
MAX_STORE_BYTES = 2 * 1024 * 1024
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9._:/-]{1,512}")
_STATUSES = frozenset({"pending", "sent", "failed", "skipped"})


class ReviewEventStoreError(RuntimeError):
    """Raised when persisted review notification state is unsafe or unreadable."""


class ReviewEventStore:
    def __init__(self, *, storage_path: Path, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        self._storage_path = storage_path
        self._max_events = max(1, int(max_events))
        self._lock = threading.Lock()
        self._events = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._storage_path.exists():
            return {}
        try:
            raw = self._storage_path.read_bytes()
        except OSError as exc:
            raise ReviewEventStoreError("review event store is unreadable") from exc
        if len(raw) > MAX_STORE_BYTES:
            raise ReviewEventStoreError("review event store exceeds size limit")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewEventStoreError("review event store is invalid") from exc
        if type(payload) is not dict or set(payload) != {"schema", "events"}:
            raise ReviewEventStoreError("review event store shape is invalid")
        if payload["schema"] != STORE_SCHEMA or type(payload["schema"]) is not str:
            raise ReviewEventStoreError("review event store schema is invalid")
        events = payload["events"]
        if type(events) is not dict or len(events) > self._max_events:
            raise ReviewEventStoreError("review event store entries are invalid")
        validated: dict[str, dict[str, Any]] = {}
        for event_id, entry in events.items():
            if type(event_id) is not str or _EVENT_ID_RE.fullmatch(event_id) is None:
                raise ReviewEventStoreError("review event id is invalid")
            if type(entry) is not dict or set(entry) != {"status", "updated_at"}:
                raise ReviewEventStoreError("review event entry shape is invalid")
            status = entry["status"]
            updated_at = entry["updated_at"]
            if type(status) is not str or status not in _STATUSES:
                raise ReviewEventStoreError("review event status is invalid")
            if (
                type(updated_at) not in {int, float}
                or isinstance(updated_at, bool)
                or not math.isfinite(float(updated_at))
                or updated_at < 0
            ):
                raise ReviewEventStoreError("review event timestamp is invalid")
            validated[event_id] = {"status": status, "updated_at": float(updated_at)}
        return validated

    def _persist_locked(self) -> None:
        payload = {"schema": STORE_SCHEMA, "events": self._events}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_STORE_BYTES:
            raise ReviewEventStoreError("review event store exceeds size limit")
        temporary_path: Path | None = None
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=self._storage_path.parent,
                prefix=f".{self._storage_path.name}.",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._storage_path)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ReviewEventStoreError("review event store cannot be persisted") from exc

    def get_status(self, event_id: str) -> str | None:
        if type(event_id) is not str or _EVENT_ID_RE.fullmatch(event_id) is None:
            raise ReviewEventStoreError("review event id is invalid")
        with self._lock:
            entry = self._events.get(event_id)
            return str(entry["status"]) if entry is not None else None

    def set_status(self, event_id: str, status: str) -> None:
        if type(event_id) is not str or _EVENT_ID_RE.fullmatch(event_id) is None:
            raise ReviewEventStoreError("review event id is invalid")
        if type(status) is not str or status not in _STATUSES:
            raise ReviewEventStoreError("review event status is invalid")
        with self._lock:
            previous = dict(self._events)
            self._events[event_id] = {"status": status, "updated_at": time.time()}
            if len(self._events) > self._max_events:
                oldest = min(
                    self._events,
                    key=lambda key: (float(self._events[key]["updated_at"]), key),
                )
                self._events.pop(oldest)
            try:
                self._persist_locked()
            except ReviewEventStoreError:
                self._events = previous
                raise


def default_review_event_store_path() -> Path:
    configured = os.environ.get(STORE_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    base_dir = Path(
        os.environ.get("CODEX_AUDIT_SERVICE_JOB_DIR")
        or os.environ.get("CODEX_AUDIT_SERVICE_STATE_DIR")
        or (Path.home() / ".local/state/codex-audit-service")
    ).expanduser()
    return base_dir / "review_event_notifications.json"


_review_event_store: ReviewEventStore | None = None
_review_event_store_path: Path | None = None


def get_review_event_store() -> ReviewEventStore:
    global _review_event_store, _review_event_store_path
    path = default_review_event_store_path()
    if _review_event_store is None or _review_event_store_path != path:
        _review_event_store = ReviewEventStore(storage_path=path)
        _review_event_store_path = path
    return _review_event_store
