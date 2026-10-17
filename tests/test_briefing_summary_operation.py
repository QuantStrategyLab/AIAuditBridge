"""Only the synthetic summary caller: real SDK/runtime, in-process transports."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from ai_service.auth import Principal
from ai_service.client import TaskClient
from ai_service.gemini import GeminiProvider
from ai_service.runtime import Policy, TaskRuntime
from ai_service.server import Application
from ai_service.transport import HttpReply, TransportError
from quant_monitor_domain import briefing_summary_operation as operation


PROJECT = "QuantStrategyLab/SyntheticSummary"
MODEL = "synthetic-model"
RESOURCE = "https://summary.invalid"


@pytest.fixture(autouse=True)
def no_external_io():
    with patch("socket.socket", side_effect=AssertionError("network forbidden")) as socket, patch(
        "subprocess.Popen", side_effect=AssertionError("process forbidden")
    ) as process:
        yield
    assert socket.call_count == process.call_count == 0


class Service:
    def __init__(self, root):
        self.root = root
        self.now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        self.calls = []
        self.provider_calls = 0
        self.auto_tick = False
        self.lose_submit = False
        self.provider_hook = None
        self.record_hook = None
        self.cancel_hook = None
        self.context = None
        self.reopen()

    def provider_request(self, url, **kwargs):
        assert url.startswith("https://generativelanguage.googleapis.com/")
        assert kwargs["timeout"] <= 60
        self.provider_calls += 1
        if self.provider_hook:
            self.provider_hook()
        return HttpReply(200, json.dumps({"modelVersion": MODEL, "candidates": [{
            "finishReason": "STOP", "content": {"parts": [{"text": '{"summary":"synthetic advisory"}'}]},
        }]}).encode())

    def reopen(self):
        transport = type("ProviderTransport", (), {"request": self.provider_request})()
        provider = GeminiProvider(lambda: "synthetic-only", model=MODEL,
                                  api_usage_authorized=True, transport=transport)
        self.runtime = TaskRuntime(self.root / "service.sqlite", {"gemini": provider}, {
            PROJECT: Policy({"api": "gemini"}, {"gemini": {MODEL}}, max_timeout_seconds=60),
        })
        self.app = Application(self.runtime, None)
        self.client = TaskClient(RESOURCE, "synthetic-only", transport=self)

    def request(self, url, *, method, headers, body):
        parsed = urlsplit(url)
        assert parsed.netloc == "summary.invalid" and parsed.path.startswith("/v1/tasks")
        if parsed.path.endswith("/cancel") and self.cancel_hook:
            self.cancel_hook()
        status, result = self.app.dispatch(method, parsed.path, headers,
            json.loads(body) if body else None,
            principal=Principal("synthetic", PROJECT, frozenset({"tasks:read", "tasks:submit"})))
        self.calls.append((method, parsed.path))
        if method == "POST" and parsed.path == "/v1/tasks":
            if self.auto_tick:
                self.tick()
            if self.lose_submit:
                raise TransportError("synthetic lost reply")
        if method == "GET" and not parsed.path.endswith("/events") and self.record_hook:
            self.record_hook(result)
        return HttpReply(status, json.dumps(result).encode())

    def tick(self):
        self.runtime.tick()
        self.runtime.tick()

    def make_context(self, state_root, **changes):
        return replace(operation.SyntheticSummaryContext(
            project=PROJECT, instance="synthetic-summary", occurrence="2026-10-17",
            state_root=state_root.resolve(), resource=RESOURCE, provider="gemini",
            model=MODEL, profile="default", timeout_seconds=60,
            deadline=datetime(2026, 10, 17, 12, 1, tzinfo=timezone.utc),
            now=lambda: self.now, client=self.client,
        ), **changes)

    @property
    def submissions(self):
        return sum(call == ("POST", "/v1/tasks") for call in self.calls)


@pytest.fixture
def rig(tmp_path):
    service = Service(tmp_path)
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    with patch("ai_service.runtime.time.time", side_effect=lambda: service.now.timestamp()):
        yield service, state


def test_pending_reopens_from_disk_and_accepts_real_v2_output(rig):
    service, state = rig
    first = operation.execute_summary(service.make_context(state), "synthetic counts")
    assert first["status"] == "deferred"
    journal = next(state.glob("*.jsonl"))
    assert journal.stat().st_mode & 0o777 == 0o600
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert records[0]["binding"]["request"]["mode"] == "api"
    assert records[0]["binding"]["deadline"] == "2026-10-17T12:01:00+00:00"
    service.reopen()
    service.tick()
    result = operation.execute_summary(service.make_context(state), "synthetic counts")
    assert result["status"] == "available" and result["text"] == "synthetic advisory"
    assert result["task_id"] == first["task_id"]
    assert service.submissions == service.provider_calls == 1


def test_lost_submit_reply_keeps_unknown_after_real_reopen(rig):
    service, state = rig
    service.lose_submit = True
    result = operation.execute_summary(service.make_context(state), "synthetic counts")
    assert result["status"] == "unknown"
    calls = list(service.calls)
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls and service.submissions == 1


@pytest.mark.parametrize("change", ["request", "model", "deadline", "resource", "provider"])
def test_same_slot_drift_never_creates_a_new_operation(rig, change):
    service, state = rig
    context = service.make_context(state)
    operation.execute_summary(context, "synthetic counts")
    before = next(state.glob("*.jsonl")).read_bytes()
    changes = {"model": {"model": "other"}, "deadline": {"deadline": context.deadline + timedelta(seconds=1)},
               "resource": {"resource": "https://other.invalid"}, "provider": {"provider": "other"}}
    altered = replace(context, **changes.get(change, {}))
    result = operation.execute_summary(altered, "changed" if change == "request" else "synthetic counts")
    assert result["status"] in {"unknown", "unavailable"}
    assert service.submissions == 1 and next(state.glob("*.jsonl")).read_bytes() == before


@pytest.mark.parametrize("damage", [b"", b"{partial", b'{}\n'])
def test_corrupt_existing_state_never_resubmits(rig, damage):
    service, state = rig
    operation.execute_summary(service.make_context(state), "synthetic counts")
    next(state.glob("*.jsonl")).write_bytes(damage)
    calls = list(service.calls)
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls


def test_cancel_intent_is_durable_before_request_and_not_retried(rig):
    service, state = rig
    operation.execute_summary(service.make_context(state), "synthetic counts")
    def check_intent():
        entries = next(state.glob("*.jsonl")).read_text()
        assert '"kind":"cancel_intent"' in entries and '"kind":"cancel_dispatch"' in entries
    service.cancel_hook = check_intent
    result = operation.execute_summary(service.make_context(state), "synthetic counts", cancel=True)
    assert result["status"] != "available"
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] != "available"
    assert sum(path.endswith("/cancel") for _, path in service.calls) == 1


def test_late_completed_and_past_cancel_history_are_never_adopted(rig):
    service, state = rig
    first = operation.execute_summary(service.make_context(state), "synthetic counts")
    def late():
        service.now += timedelta(seconds=61)
    service.provider_hook = late
    service.tick()
    assert service.runtime.get(PROJECT, first["task_id"])["status"] == "completed"
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] != "available"


def test_cancel_requested_then_completed_is_rejected_by_events(rig):
    service, state = rig
    first = operation.execute_summary(service.make_context(state), "synthetic counts")
    service.provider_hook = lambda: service.runtime.cancel(PROJECT, first["task_id"])
    service.tick()
    assert service.runtime.get(PROJECT, first["task_id"])["status"] == "completed"
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] != "available"


@pytest.mark.parametrize("field,value", [("id", "wrong"), ("owner", "Pigbibi/PersonalAIService"),
    ("provider", "dot"), ("revision", 2), ("worker", "wrong"), ("attempt", "wrong"),
    ("output", {"summary": ""}), ("output", {"extra": "bad"}),
    ("request", {}), ("handle", {"model": MODEL, "model_verification": "unavailable"})])
def test_mismatched_readback_never_available(rig, field, value):
    service, state = rig
    service.auto_tick = True
    service.record_hook = lambda record: record.update({field: value})
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] != "available"


def test_concurrent_same_slot_submits_once(rig):
    service, state = rig
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: operation.execute_summary(service.make_context(state), "synthetic counts"), range(2)))
    assert service.submissions == 1
    assert all(result["status"] in {"deferred", "unknown"} for result in results)
    assert len({result.get("task_id") for result in results if result.get("task_id")}) == 1


@pytest.mark.parametrize("kind", ["submit_intent", "submitted"])
def test_write_failure_blocks_remote_or_poisoned_resume(rig, kind):
    service, state = rig
    original = operation._Journal.append
    def fail(journal, record):
        if record["kind"] == kind:
            with patch.object(operation.os, "fsync", side_effect=OSError("synthetic write failure")):
                return original(journal, record)
        return original(journal, record)
    with patch.object(operation._Journal, "append", fail):
        assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    calls = list(service.calls)
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls and service.submissions == (1 if kind == "submitted" else 0)


def test_missing_sdk_or_context_never_uses_transport(rig):
    service, state = rig
    assert operation.execute_summary(None, "synthetic counts")["status"] == "unavailable"
    with patch.dict("sys.modules", {"ai_service.client": None}):
        assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unavailable"
    assert service.calls == [] and list(state.iterdir()) == []


@pytest.mark.parametrize("point", ["before_intent", "after_remote_submit"])
def test_interruption_reopens_without_task_id_and_never_retries(rig, point):
    service, state = rig
    if point == "before_intent":
        original = operation._Journal.append
        def interrupted(journal, record):
            if record["kind"] == "submit_intent":
                raise SystemExit("synthetic process interruption")
            return original(journal, record)
        patcher = patch.object(operation._Journal, "append", interrupted)
    else:
        original = service.request
        def interrupted(url, **kwargs):
            result = original(url, **kwargs)
            if kwargs["method"] == "POST":
                raise SystemExit("synthetic process interruption")
            return result
        patcher = patch.object(service, "request", interrupted)
    with patcher, pytest.raises(SystemExit):
        operation.execute_summary(service.make_context(state), "synthetic counts")
    calls = list(service.calls)
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls
    assert service.submissions == (1 if point == "after_remote_submit" else 0)


def test_partial_post_submit_write_leaves_sentinel_and_never_recovers_from_cache(rig):
    service, state = rig
    append, write = operation._Journal.append, operation.os.write
    def failing_append(journal, record):
        if record["kind"] == "submitted":
            def partial(fd, data):
                write(fd, data[:7])
                raise OSError("synthetic partial write")
            with patch.object(operation.os, "write", partial):
                return append(journal, record)
        return append(journal, record)
    with patch.object(operation._Journal, "append", failing_append):
        assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert len(list(state.glob("*.pending"))) == 1
    calls = list(service.calls)
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls and service.submissions == 1


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "mode"])
def test_unsafe_journal_is_not_read_or_replaced(rig, damage):
    service, state = rig
    operation.execute_summary(service.make_context(state), "synthetic counts")
    journal = next(state.glob("*.jsonl"))
    if damage == "mode":
        journal.chmod(0o644)
    else:
        saved = state / "original"
        journal.rename(saved)
        if damage == "symlink":
            journal.symlink_to(saved)
        else:
            operation.os.link(saved, journal)
    calls = list(service.calls)
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls


def test_callable_token_is_not_invoked_for_synthetic_context(rig):
    service, state = rig
    with patch.object(service.client, "token") as token:
        assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unavailable"
    token.assert_not_called()
    assert service.calls == [] and list(state.iterdir()) == []


@pytest.mark.parametrize("history", [None, {"events": []}, {"events": [{"seq": True, "created": 1, "status": "completed"}]}])
def test_unknown_history_is_persisted_without_later_polling(rig, history):
    service, state = rig
    service.auto_tick = True
    original = service.request
    def wrong_history(url, **kwargs):
        if url.endswith("/events"):
            if history is None:
                raise TransportError("synthetic lost history")
            return HttpReply(200, json.dumps(history).encode())
        return original(url, **kwargs)
    with patch.object(service, "request", wrong_history):
        assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    calls = list(service.calls)
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls


def test_duplicate_binding_fields_are_corruption_not_a_new_operation(rig):
    service, state = rig
    operation.execute_summary(service.make_context(state), "synthetic counts")
    journal = next(state.glob("*.jsonl"))
    journal.write_text(journal.read_text().replace('"version":1', '"version":99,"version":1', 1))
    calls = list(service.calls)
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls


@pytest.mark.parametrize("field,value", [("model", "wrong-model"),
    ("model_verification", "unavailable"), ("task_id", "wrong-task"), ("status", "running")])
def test_each_model_evidence_field_is_checked_with_other_fields_valid(rig, field, value):
    service, state = rig
    service.auto_tick = True
    service.record_hook = lambda record: record["handle"].update({field: value})
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unavailable"


def test_boolean_schema_drift_does_not_equal_numeric_zero(rig):
    service, state = rig
    service.auto_tick = True
    def drift(record):
        record["request"]["output_schema"]["additionalProperties"] = 0
    service.record_hook = drift
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"


def test_changed_previously_verified_output_seals_unknown(rig):
    service, state = rig
    service.auto_tick = True
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "available"
    service.record_hook = lambda record: record.update(output={"summary": "changed valid text"})
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    calls = list(service.calls)
    service.record_hook = None
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unknown"
    assert service.calls == calls


@pytest.mark.parametrize("expired", [False, True])
def test_cancel_before_submission_stays_unavailable_without_remote_work(rig, expired):
    service, state = rig
    if expired:
        service.now += timedelta(seconds=60)
    context = service.make_context(state)
    assert operation.execute_summary(context, "synthetic counts", cancel=not expired)["status"] == "unavailable"
    service.reopen()
    assert operation.execute_summary(service.make_context(state), "synthetic counts")["status"] == "unavailable"
    assert service.calls == [] and service.provider_calls == 0


@pytest.mark.parametrize("boundary", ["submit_intent", "verified"])
def test_deadline_crossed_during_durable_append_blocks_dispatch_or_adoption(rig, boundary):
    service, state = rig
    service.auto_tick = boundary == "verified"
    context = service.make_context(state)
    original_append = operation._Journal.append
    crossed = []
    def append_then_expire(journal, record):
        original_append(journal, record)  # Real write/fsync precedes the clock change.
        if record["kind"] == boundary:
            service.now = context.deadline
            crossed.append(boundary)
    with patch.object(operation._Journal, "append", append_then_expire):
        result = operation.execute_summary(context, "synthetic counts")
    assert crossed == [boundary]
    assert result["status"] != "available" and "text" not in result
    if boundary == "submit_intent":
        assert result["status"] == "unknown"
        assert service.submissions == service.provider_calls == 0
    else:
        assert service.submissions == service.provider_calls == 1
    records = [json.loads(line) for line in next(state.glob("*.jsonl")).read_text().splitlines()]
    original_binding = records[0]
    assert any(entry["kind"] == "cancel_intent" for entry in records)
    calls = list(service.calls)
    service.reopen()
    # Restore the explicit clock to prove persisted intent, not the old object
    # or a still-expired clock, prevents adoption/re-submission after reopening.
    service.now = context.deadline - timedelta(seconds=1)
    resumed = operation.execute_summary(service.make_context(state), "synthetic counts")
    assert resumed["status"] != "available" and "text" not in resumed
    assert service.submissions == (0 if boundary == "submit_intent" else 1)
    if boundary == "submit_intent":
        assert service.calls == calls
        assert resumed["status"] == "unknown"
    else:
        submitted = next(entry for entry in records if entry["kind"] == "submitted")
        assert resumed["task_id"] == result["task_id"] == submitted["task_id"]
    reopened_records = [json.loads(line) for line in next(state.glob("*.jsonl")).read_text().splitlines()]
    assert reopened_records[0] == original_binding
