from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from scripts.consume_daily_briefing import main
from service.runtime_digest import prepare_runtime_digest, runtime_digest_event_id


def _record(status: str, *, service: str = "lb-svc", scope: str = "paper", day: str = "2026-09-28", lane: str = "paper", completeness: str = "complete", runs: list | None = None, conflicts: list | None = None) -> dict:
    return {
        "platform": "longbridge",
        "target_key": f"{service}|rot|*{scope}",
        "target": {"service": service, "strategy_profile": "rot", "account_scope": scope},
        "business_date": day,
        "timezone": "Asia/Hong_Kong",
        "status": status,
        "kind": "schedule" if status in {"market_closed", "not_due", "outside_window"} else "run",
        "completeness": completeness,
        "execution_lane": lane,
        "runs": runs or [],
        "conflicts": conflicts or [],
        "fills": {"source": "not_connected", "records": [], "count": None},
    }


def _projection(*records: dict, completeness: str = "complete", observed_at: str = "2026-09-28T08:40:00+00:00", read_errors: list | None = None) -> dict:
    return {
        "platform": "longbridge",
        "observed_at": observed_at,
        "completeness": completeness,
        "read_errors": read_errors or [],
        "records": list(records),
        "unmatched_reports": [],
    }


def test_closed_not_due_and_outside_window_stay_distinct() -> None:
    closed = prepare_runtime_digest(_projection(_record("market_closed")))
    waiting = prepare_runtime_digest(_projection(_record("not_due")))
    outside = prepare_runtime_digest(_projection(_record("outside_window")))
    assert closed["ok"] and "休市" in closed["text"] and "未到运行时间" not in closed["text"]
    assert "未到运行时间" in waiting["text"]
    assert "窗口外" in outside["text"]
    assert closed["text"].count("成交明细暂缺") == 1
    assert "0 笔" not in closed["text"]
    assert "complete" not in closed["text"]
    assert "确定性" not in closed["text"]


def test_missing_partial_and_drill_are_not_a_normal_close() -> None:
    missing = prepare_runtime_digest(_projection(
        _record("missing_report", completeness="incomplete"),
        completeness="incomplete",
    ))
    partial = prepare_runtime_digest(_projection(_record("partially_filled", lane="live")))
    drill = prepare_runtime_digest(_projection(_record("dry_run", lane="dry_run")))
    assert "到期缺报告" in missing["text"]
    assert "休市" not in missing["text"]
    assert "部分成交" in partial["text"]
    assert "演练 dry-run" in drill["text"]
    assert "实盘" not in drill["text"]


def test_schedule_status_cannot_hide_unknown_or_cross_day_conflict() -> None:
    hidden = _projection(_record(
        "market_closed",
        runs=[{"activity": "unknown", "execution_lane": "paper", "source_object": "gs://private/secret"}],
    ))
    refused = prepare_runtime_digest(hidden)
    assert refused["ok"] is False
    assert refused["reason"] == "schedule_hides_anomaly"
    assert refused["text"] == ""

    first = _record("market_closed", service="lb-a", scope="paper", day="2026-09-28")
    second = _record("unknown", service="lb-b", scope="live", day="2026-09-29", completeness="incomplete")
    conflict = prepare_runtime_digest(_projection(first, second, completeness="incomplete"))
    assert conflict["ok"] is False
    assert conflict["reason"] == "business_date_conflict"


def test_duplicate_target_invalid_time_and_count_zero_are_refused() -> None:
    duplicate = _projection(_record("market_closed"), _record("no_submission"))
    assert prepare_runtime_digest(duplicate)["reason"] == "duplicate_target"
    naive = _projection(_record("market_closed"), observed_at="2026-09-28T08:40:00")
    assert prepare_runtime_digest(naive)["reason"] == "invalid_observed_at"
    zero = _projection(_record("no_submission"))
    zero["records"][0]["fills"]["count"] = 0
    assert prepare_runtime_digest(zero)["reason"] == "fills_not_connected"


def test_producer_dry_run_previewed_run_is_accepted() -> None:
    projected = _projection(_record(
        "dry_run",
        service="lb-us",
        scope="us",
        lane="dry_run",
        runs=[{
            "run_id": "run-dry-1",
            "source_object": "gs://runtime-reports/longbridge/2026-09/run-dry-1.json",
            "source_objects": ["gs://runtime-reports/longbridge/2026-09/run-dry-1.json"],
            "started_at": "2026-09-28T08:06:00Z",
            "finished_at": "2026-09-28T08:07:00Z",
            "object_updated_at": "2026-09-28T08:08:00Z",
            "report_status": "ok",
            "execution_lane": "dry_run",
            "activity": "previewed",
            "run_time_known": True,
            "evidence": {"execution_status": "previewed"},
        }],
    ))
    prepared = prepare_runtime_digest(projected)
    assert prepared["ok"] is True
    assert prepared["accounts"][0]["status"] == "dry_run"
    assert prepared["accounts"][0]["runs"][0]["activity"] == "previewed"
    assert "演练 dry-run" in prepared["text"]
    assert "gs://" not in prepared["text"]


def test_cross_day_unknown_keeps_run_identity_and_time() -> None:
    carried = _record(
        "unknown",
        completeness="incomplete",
        runs=[{
            "run_id": "run-yday",
            "started_at": "2026-09-27T08:10:00Z",
            "finished_at": "2026-09-27T08:12:00Z",
            "source_object": "gs://runtime-reports/longbridge/2026-09/run-yday.json",
            "source_objects": ["gs://runtime-reports/longbridge/2026-09/run-yday.json"],
            "execution_lane": "paper",
            "activity": "unknown",
        }, {
            "run_id": "run-today",
            "activity": "unknown",
            "execution_lane": "paper",
        }],
    )
    prepared = prepare_runtime_digest(_projection(carried, completeness="incomplete"))
    runs = prepared["accounts"][0]["runs"]
    assert [(run["run_id"], run["started_at"], run["finished_at"]) for run in runs] == [
        ("run-yday", "2026-09-27T08:10:00Z", "2026-09-27T08:12:00Z"),
        ("run-today", None, None),
    ]
    assert runs[0]["source_object"].endswith("run-yday.json")
    assert "gs://" not in prepared["text"]
    assert "未知" in prepared["text"]
    naive = _projection(_record("unknown", runs=[{
        "run_id": "run-bad",
        "activity": "unknown",
        "execution_lane": "paper",
        "started_at": "2026-09-27T08:10:00",
    }]))
    assert prepare_runtime_digest(naive)["reason"] == "invalid_run_time"


def test_event_id_uses_target_scope_not_order_or_observed_at() -> None:
    first = _record("no_submission", service="lb-a", scope="paper")
    second = _record("market_closed", service="lb-b", scope="live")
    forward = prepare_runtime_digest(_projection(first, second))
    reverse = prepare_runtime_digest(_projection(second, first, observed_at="2026-09-28T18:40:00+00:00"))
    keys = ["lb-a|rot|*paper", "lb-b|rot|*live"]
    assert forward["ok"] and reverse["ok"]
    assert forward["event_id"] == reverse["event_id"]
    assert forward["event_id"] == runtime_digest_event_id("longbridge", "2026-09-28", keys)
    assert forward["target_scope"] == sorted(keys)
    with_runs = prepare_runtime_digest(_projection(
        _record("no_submission", service="lb-a", scope="paper", runs=[{
            "run_id": "run-1",
            "activity": "no_submission",
            "execution_lane": "paper",
            "source_object": "/Users/private/report.json",
        }]),
        second,
    ))
    assert with_runs["event_id"] == forward["event_id"]
    assert with_runs["accounts"][0]["runs"][0]["source_object"].endswith("report.json")
    assert "/Users/" not in with_runs["text"]
    other_scope = prepare_runtime_digest(_projection(first))
    assert other_scope["event_id"] != forward["event_id"]
    changed = copy.deepcopy(forward)
    assert "observed_at" not in changed["event_id"]


def _write_projection(tmp_path: Path, *records: dict) -> Path:
    path = tmp_path / "projection.json"
    path.write_text(json.dumps(_projection(*records)), encoding="utf-8")
    return path


def _runtime_argv(path: Path, *extra: str) -> list[str]:
    return ["--runtime-projection", str(path), *extra]


def test_runtime_dispatch_rejects_day_and_target_scope_before_send(tmp_path: Path, capsys) -> None:
    path = _write_projection(tmp_path, _record("market_closed"))
    key = "lb-svc|rot|*paper"
    other = "lb-svc|rot|*live"
    cases = [
        (["--day", "2026-09-27", "--dispatch", "--expected-target-key", key], "business_date_mismatch"),
        (["--day", "2026-01-02", "--dispatch", "--expected-target-key", key], "business_date_mismatch"),
        (["--day", "2026-9-28", "--dispatch", "--expected-target-key", key], "invalid_business_day"),
        (["--day", "28-09-2026", "--dispatch", "--expected-target-key", key], "invalid_business_day"),
        (["--day", "2026-09-28", "--dispatch", "--expected-target-key", other], "expected_target_mismatch"),
        (["--day", "2026-09-28", "--dispatch", "--expected-target-key", key, "--expected-target-key", other], "expected_target_mismatch"),
        (["--day", "2026-09-28", "--dispatch", "--expected-target-key", key, "--expected-target-key", key], "duplicate_expected_target"),
        (["--day", "2026-09-28", "--dispatch", "--expected-target-key", " "], "invalid_expected_target"),
        (["--day", "2026-09-28", "--dispatch", "--expected-target-key", "lb-svc|rot|"], "invalid_expected_target"),
        (["--day", "2026-09-28", "--dispatch", "--expected-target-key", "LB-SVC|rot|*paper"], "invalid_expected_target"),
        (["--dispatch", "--expected-target-key", key], "missing_dispatch_day"),
        (["--day", "2026-09-28", "--dispatch"], "missing_expected_target"),
        (["--day", "2026-09-27", "--dispatch", "--dry-run", "--expected-target-key", key], "business_date_mismatch"),
        (["--dispatch", "--dry-run"], "missing_dispatch_day"),
    ]
    for extra, reason in cases:
        with patch("scripts.consume_daily_briefing.dispatch_runtime_digest", return_value={"errors": []}) as dispatch:
            code = main(_runtime_argv(path, *extra))
        captured = json.loads(capsys.readouterr().out)
        assert code == 2
        assert captured == {"ok": False, "error": "runtime_projection_rejected", "reason": reason}
        assert str(path) not in json.dumps(captured)
        dispatch.assert_not_called()

    fewer = _write_projection(tmp_path, _record("market_closed", service="lb-a"), _record("no_submission", service="lb-b", scope="live"))
    with patch("scripts.consume_daily_briefing.dispatch_runtime_digest", return_value={"errors": []}) as dispatch:
        code = main(_runtime_argv(
            fewer,
            "--day", "2026-09-28",
            "--dispatch",
            "--expected-target-key", "lb-a|rot|*paper",
        ))
    captured = json.loads(capsys.readouterr().out)
    assert code == 2
    assert captured["reason"] == "expected_target_mismatch"
    dispatch.assert_not_called()


def test_runtime_dispatch_matching_scope_calls_dispatch_once(tmp_path: Path) -> None:
    first = _record("market_closed", service="lb-a")
    second = _record("no_submission", service="lb-b", scope="live")
    path = _write_projection(tmp_path, first, second)
    payload = json.loads(path.read_text(encoding="utf-8"))
    with patch("scripts.consume_daily_briefing.dispatch_runtime_digest", return_value={"errors": []}) as dispatch:
        code = main([
            "--runtime-projection", str(path),
            "--day", "2026-09-28",
            "--dispatch",
            "--dry-run",
            "--expected-target-key", "lb-b|rot|*live",
            "--expected-target-key", "lb-a|rot|*paper",
        ])
    assert code == 0
    dispatch.assert_called_once_with(payload, dry_run=True, send_dry_run=False)
    assert dispatch.call_args.args[0]["records"][0]["status"] == "market_closed"
    with patch("scripts.consume_daily_briefing.dispatch_runtime_digest", return_value={"errors": []}) as send:
        code = main([
            "--runtime-projection", str(path),
            "--day", "2026-09-28",
            "--dispatch",
            "--expected-target-key", "lb-a|rot|*paper",
            "--expected-target-key", "lb-b|rot|*live",
        ])
    assert code == 0
    send.assert_called_once_with(payload, dry_run=False, send_dry_run=False)


def test_runtime_preview_can_omit_scope_but_checks_constraints(tmp_path: Path, capsys) -> None:
    path = _write_projection(tmp_path, _record("market_closed"))
    with patch("scripts.consume_daily_briefing.dispatch_runtime_digest") as dispatch:
        code = main(["--runtime-projection", str(path)])
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    assert body["kind"] == "runtime_digest"
    assert "dispatch" not in body
    dispatch.assert_not_called()
    with patch("scripts.consume_daily_briefing.dispatch_runtime_digest") as dispatch:
        code = main(["--runtime-projection", str(path), "--day", "2026-09-27"])
    rejected = json.loads(capsys.readouterr().out)
    assert code == 2
    assert rejected["reason"] == "business_date_mismatch"
    dispatch.assert_not_called()


def test_domain_briefing_path_ignores_runtime_scope_flags(tmp_path: Path, capsys) -> None:
    (tmp_path / "us_equity.json").write_text(json.dumps({
        "domain": "us_equity",
        "ok": True,
        "data_status": "ready",
        "as_of": "2026-09-28T08:00:00+00:00",
        "strategies": [{"strategy_profile": "rot", "status": "healthy", "overall_score": 80, "as_of": "2026-09-28"}],
    }), encoding="utf-8")
    with patch("scripts.consume_daily_briefing.dispatch_runtime_digest") as dispatch:
        code = main(["--report-dir", str(tmp_path), "--day", "2026-09-28"])
    captured = json.loads(capsys.readouterr().out)
    assert code == 0
    assert captured["action"] == "quiet"
    assert "runtime_digest" not in captured
    dispatch.assert_not_called()


def _paper_bytes(day: str = "2026-09-28", *, scope: str = "paper", status: str = "market_closed") -> bytes:
    record = _record(status, scope=scope, day=day)
    record["target_key"] = f"lb-svc|rot|{scope}"
    record["target"]["account_scope"] = scope
    payload = _projection(record)
    payload["records"][0]["fills"]["count"] = None
    return json.dumps(payload).encode("utf-8")


def _gcs_uri(day: str = "2026-09-28") -> str:
    return f"gs://bucket/runtime_daily/longbridge/paper/{day}.json"


def test_gcs_preview_and_dispatch_keep_day_and_scope_guards(capsys) -> None:
    uri = _gcs_uri()
    key = "lb-svc|rot|paper"
    raw = _paper_bytes()
    with patch("scripts.consume_daily_briefing._read_gcs_object", return_value=(raw, None)) as reader, patch(
        "scripts.consume_daily_briefing.dispatch_runtime_digest",
        return_value={"errors": []},
    ) as dispatch:
        code = main(["--runtime-projection-gcs", uri, "--day", "2026-09-28", "--expected-target-key", key])
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "休市" in body["text"]
    assert "0 笔" not in body["text"]
    assert "dispatch" not in body
    reader.assert_called_once_with(uri)
    dispatch.assert_not_called()

    with patch("scripts.consume_daily_briefing._read_gcs_object", return_value=(raw, None)), patch(
        "scripts.consume_daily_briefing.dispatch_runtime_digest",
        return_value={"errors": []},
    ) as dispatch:
        code = main([
            "--runtime-projection-gcs", uri,
            "--day", "2026-09-28",
            "--expected-target-key", key,
            "--dispatch",
        ])
    assert code == 0
    dispatch.assert_called_once()
    sent = dispatch.call_args.args[0]
    assert sent["records"][0]["status"] == "market_closed"
    assert sent["records"][0]["fills"]["count"] is None
    assert dispatch.call_args.kwargs == {"dry_run": False, "send_dry_run": False}


def test_gcs_bad_input_times_out_or_rejects_before_send(capsys) -> None:
    uri = _gcs_uri()
    key = "lb-svc|rot|paper"
    secret = "gs://private/secret-stderr"
    cases = [
        (["--runtime-projection-gcs", "gs://bucket/runtime_daily/longbridge/paper/*.json", "--day", "2026-09-28", "--expected-target-key", key], "invalid_runtime_object", False),
        (["--runtime-projection-gcs", "gs://user:pass@bucket/runtime_daily/longbridge/paper/2026-09-28.json", "--day", "2026-09-28", "--expected-target-key", key], "invalid_runtime_object", False),
        (["--runtime-projection-gcs", "gs://bucket/../runtime_daily/longbridge/paper/2026-09-28.json", "--day", "2026-09-28", "--expected-target-key", key], "invalid_runtime_object", False),
        (["--runtime-projection-gcs", "gs://bucket/not_runtime_daily/longbridge/paper/2026-09-28.json", "--day", "2026-09-28", "--expected-target-key", key], "invalid_runtime_object", False),
        (["--runtime-projection-gcs", _gcs_uri("2026-09-27"), "--day", "2026-09-28", "--expected-target-key", key], "runtime_object_day_mismatch", False),
        (["--runtime-projection-gcs", uri, "--day", "2026-09-28", "--expected-target-key", "lb-svc|rot|live"], "expected_scope_not_paper", False),
        (["--runtime-projection-gcs", uri, "--day", "2026-09-28", "--expected-target-key", "lb-svc|rot|*paper"], "expected_scope_not_paper", False),
        (["--runtime-projection-gcs", uri, "--expected-target-key", key], "missing_dispatch_day", False),
        (["--runtime-projection-gcs", uri, "--day", "2026-09-28", "--expected-target-key", key], "business_date_mismatch", True),
        (["--runtime-projection-gcs", uri, "--day", "2026-09-28", "--expected-target-key", "other-svc|rot|paper", "--dispatch"], "expected_target_mismatch", True),
    ]
    for argv, reason, reads in cases:
        payload = _paper_bytes("2026-09-27" if reason == "business_date_mismatch" else "2026-09-28")
        with patch("scripts.consume_daily_briefing._read_gcs_object", return_value=(payload, None)) as reader, patch(
            "scripts.consume_daily_briefing.dispatch_runtime_digest",
            return_value={"errors": []},
        ) as dispatch:
            code = main(argv)
        captured = json.loads(capsys.readouterr().out)
        assert code == 2
        assert captured["reason"] == reason
        assert secret not in json.dumps(captured)
        assert "gs://" not in json.dumps(captured)
        dispatch.assert_not_called()
        if reads:
            reader.assert_called_once()
        else:
            reader.assert_not_called()

    for reason in ("runtime_projection_too_large", "runtime_projection_unreadable", "runtime_projection_timeout"):
        with patch("scripts.consume_daily_briefing._read_gcs_object", return_value=(None, reason)) as reader, patch(
            "scripts.consume_daily_briefing.dispatch_runtime_digest",
        ) as dispatch:
            code = main(["--runtime-projection-gcs", uri, "--day", "2026-09-28", "--expected-target-key", key, "--dispatch"])
        captured = json.loads(capsys.readouterr().out)
        assert code == 2
        assert captured == {"ok": False, "error": "runtime_projection_rejected", "reason": reason}
        assert secret not in json.dumps(captured)
        dispatch.assert_not_called()
        reader.assert_called_once()


def test_gcs_reader_is_bounded_argv_and_discards_stderr(monkeypatch) -> None:
    from scripts.consume_daily_briefing import _GCS_OBJECT_LIMIT, _read_gcs_object

    secret = b"gs://private/do-not-print"

    def fake_popen(argv, stdout=None, stderr=None, bufsize=-1):
        assert isinstance(argv, list)
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.PIPE
        read_fd, write_fd = os.pipe()
        err_read, err_write = os.pipe()
        os.write(err_write, secret)
        os.close(err_write)

        def _writer() -> None:
            remaining = _GCS_OBJECT_LIMIT + 1
            while remaining:
                size = min(65536, remaining)
                os.write(write_fd, b"x" * size)
                remaining -= size
            os.close(write_fd)

        threading.Thread(target=_writer, daemon=True).start()

        class Proc:
            def __init__(self) -> None:
                self.stdout = os.fdopen(read_fd, "rb")
                self.stderr = os.fdopen(err_read, "rb")
                self.killed = False
                self.returncode = None

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode if self.returncode is not None else 0

        seen["argv"] = argv
        seen["proc"] = Proc()
        return seen["proc"]

    seen: dict[str, object] = {}
    monkeypatch.setattr("scripts.consume_daily_briefing.subprocess.Popen", fake_popen)
    data, reason = _read_gcs_object("gs://bucket/runtime_daily/longbridge/paper/2026-09-28.json")
    assert data is None
    assert reason == "runtime_projection_too_large"
    assert seen["argv"] == [
        "gcloud", "storage", "cat", "--",
        "gs://bucket/runtime_daily/longbridge/paper/2026-09-28.json",
    ]
    assert seen["proc"].killed is True

    def hang_popen(argv, stdout=None, stderr=None, bufsize=-1):
        read_fd, write_fd = os.pipe()
        err_read, err_write = os.pipe()
        os.write(err_write, secret)
        os.close(err_write)

        class Proc:
            def __init__(self) -> None:
                self.stdout = os.fdopen(read_fd, "rb")
                self.stderr = os.fdopen(err_read, "rb")
                self._write = write_fd
                self.killed = False
                self.returncode = None

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9
                os.close(self._write)

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return -9

        return Proc()

    monkeypatch.setattr("scripts.consume_daily_briefing.subprocess.Popen", hang_popen)
    monkeypatch.setattr("scripts.consume_daily_briefing._GCS_TIMEOUT_SECONDS", 0.05)
    data, reason = _read_gcs_object("gs://bucket/runtime_daily/longbridge/paper/2026-09-28.json")
    assert data is None
    assert reason == "runtime_projection_timeout"


def test_gcs_read_times_out_when_child_writes_one_byte_then_sleeps(monkeypatch) -> None:
    from scripts.consume_daily_briefing import _read_gcs_object

    real_popen = subprocess.Popen
    script = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'x'); sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write(b'y'); sys.stderr.buffer.flush()\n"
        "time.sleep(0.6)\n"
    )

    def launching(argv, stdout=None, stderr=None, bufsize=-1):
        assert isinstance(argv, list) and argv[0] == "gcloud"
        return real_popen(
            [sys.executable, "-c", script],
            stdout=stdout,
            stderr=stderr,
            bufsize=bufsize,
        )

    monkeypatch.setattr("scripts.consume_daily_briefing.subprocess.Popen", launching)
    monkeypatch.setattr("scripts.consume_daily_briefing._GCS_TIMEOUT_SECONDS", 0.08)
    started = time.monotonic()
    data, reason = _read_gcs_object("gs://bucket/runtime_daily/longbridge/paper/2026-09-28.json")
    elapsed = time.monotonic() - started
    assert data is None
    assert reason == "runtime_projection_timeout"
    assert elapsed < 0.45
