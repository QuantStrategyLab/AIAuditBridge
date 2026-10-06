"""Offline-only diagnostics tests: guards are active before the helper is imported."""

import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import unittest
from unittest import mock
import urllib.request


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/inspect_monitor_failures.py"
SECRET = "SECRET_MARKER_ACCOUNT_token_https://private.invalid/private/path"
QUANT = "codex-quant.service"
DAILY = "codex-daily-briefing.service"
QID = "a" * 32
DID = "b" * 32
RECORDED_DID = "ff223a65dbf049cc9e9280f49c7519d5"


class ExternalAttempt(BaseException):
    """Cannot be mistaken for a production read error."""


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.attempts = []
        self.guards = contextlib.ExitStack()
        self.addCleanup(self.guards.close)
        self.addCleanup(self.assert_no_external_attempts)

        def blocked(*args, **kwargs):
            self.attempts.append("blocked_external_attempt")
            raise ExternalAttempt("external action prohibited in offline tests")

        for target, attribute in (
            (subprocess, "Popen"),
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (socket, "socket"),
            (urllib.request, "urlopen"),
            (http.client.HTTPConnection, "connect"),
            (http.client.HTTPConnection, "request"),
            (os, "system"),
            (os, "posix_spawn"),
            (os, "posix_spawnp"),
            (os, "fork"),
            (os, "forkpty"),
            (os, "execv"),
            (os, "execve"),
            (os, "execvp"),
            (os, "execvpe"),
            (os, "spawnv"),
            (os, "spawnve"),
            (os, "spawnvp"),
            (os, "spawnvpe"),
        ):
            self.guards.enter_context(mock.patch.object(target, attribute, blocked))
        spec = importlib.util.spec_from_file_location("inspect_monitor_failures_tested", SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        self.guards.enter_context(mock.patch.dict(sys.modules, {spec.name: self.m}))
        spec.loader.exec_module(self.m)

    def assert_no_external_attempts(self):
        self.assertEqual(self.attempts, [], "an unexpected external attempt was blocked")

    def properties(self, unit, invocation=None, **changes):
        m = self.m
        values = {key: "0" for key in m.properties_for(unit)}
        values.update(
            Id=unit, LoadState="loaded", ActiveState="failed" if unit.endswith(".service") else "active",
            SubState="failed" if unit.endswith(".service") else "waiting", UnitFileState="disabled",
            InvocationID=invocation or (QID if unit == QUANT else DID), Result="exit-code",
        )
        if unit.endswith(".service"):
            values.update(ExecMainCode="1", ExecMainStatus="2" if unit == QUANT else "1",
                          ExecMainStartTimestampMonotonic="100", ExecMainExitTimestampMonotonic="200")
        else:
            values.update(LastTriggerUSec="Tue 2026-10-06 04:30:00 UTC",
                          NextElapseUSecRealtime="Tue 2026-10-06 05:00:00 UTC")
        values.update(changes)
        return "".join(f"{key}={values[key]}\n" for key in m.properties_for(unit)).encode()

    def records(self, unit, messages, invocation=None):
        invocation = invocation or (QID if unit == QUANT else DID)
        return b"".join((json.dumps({"_SYSTEMD_UNIT": unit, "_SYSTEMD_INVOCATION_ID": invocation,
                                     "MESSAGE": message}) + "\n").encode() for message in messages)

    def collect(self, *, journals=None, override=None, daily_only=False):
        m = self.m
        requests = []
        counts = {}

        def runner(argv):
            argv = tuple(argv)
            requests.append(argv)
            counts[argv] = counts.get(argv, 0) + 1
            if override:
                replacement = override(argv, counts[argv])
                if replacement is not None:
                    return replacement
            if argv[0] == m.SYSTEMCTL:
                return m.ReadResult(data=self.properties(argv[3]), returncode=0)
            unit = argv[-2].split("=", 1)[1]
            return m.ReadResult(data=(journals or {}).get(unit, b""), returncode=0)

        return (m.collect(runner=runner, daily_only=True) if daily_only else m.collect(runner=runner)), requests

    def recorded(self, *, messages=None, data=None, flags=None, before_changes=None, after_changes=None):
        requests = []
        snapshots = 0

        def runner(argv):
            nonlocal snapshots
            requests.append(tuple(argv))
            if argv[0] == self.m.SYSTEMCTL:
                snapshots += 1
                changes = (before_changes or {}) if snapshots == 1 else (after_changes or {})
                return self.m.ReadResult(data=self.properties(DAILY, RECORDED_DID, **changes), returncode=0)
            body = self.records(DAILY, messages or [], RECORDED_DID) if data is None else data
            return self.m.ReadResult(data=body, **{"returncode": 0, **(flags or {})})

        return self.m.collect_recorded_daily_errors(runner=runner), requests

    def test_recorded_exact_command_filter_and_exception_projection(self):
        result, calls = self.recorded(messages=["Traceback (most recent call last):", "ModuleNotFoundError: " + SECRET,
                                               "json.decoder.JSONDecodeError: " + SECRET])
        self.assertEqual(self.m.RECORDED_DAILY_INVOCATION, RECORDED_DID)
        self.assertEqual(calls, [self.m.systemd_command(DAILY), self.m.RECORDED_DAILY_JOURNAL, self.m.systemd_command(DAILY)])
        self.assertEqual(calls[1][-2:], ("_SYSTEMD_UNIT=" + DAILY, "_SYSTEMD_INVOCATION_ID=" + RECORDED_DID))
        self.assertIn("--grep=" + self.m.RECORDED_DAILY_ERROR_GREP, calls[1])
        self.assertIn("--case-sensitive=yes", calls[1])
        self.assertIn("--reverse", calls[1])
        self.assertFalse(any("priority" in item for item in calls[1]))
        self.assertEqual(set(result["units"]), {DAILY})
        unit = result["units"][DAILY]
        self.assertTrue(unit["bound_invocation_current"])
        self.assertTrue(unit["snapshot_stable"])
        filtered = unit["filtered_evidence"]
        self.assertTrue(filtered["available"])
        self.assertFalse(filtered["complete"])
        self.assertFalse(filtered["absence_proven"])
        self.assertEqual(filtered["exception_counts"]["ModuleNotFoundError"], 1)
        self.assertEqual(filtered["exception_counts"]["JSONDecodeError"], 1)
        self.assertEqual(filtered["stage_counts"]["python_traceback"], 1)
        self.assertEqual(unit["diagnosis"], "unknown")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(RECORDED_DID, json.dumps(result))

    def test_recorded_pipeline_markers_and_only_legal_exit_codes(self):
        message = json.dumps({"ok": False, "error": "report_dir_not_found: /private/" + SECRET})
        result, _ = self.recorded(messages=[message,
            "[briefing-pipeline] runtime digest rejected: invalid_runtime_timezone",
            "[briefing-pipeline] domain_exit=1 runtime_exit=2"])
        filtered = result["units"][DAILY]["filtered_evidence"]
        self.assertTrue(filtered["available"])
        self.assertEqual(filtered["stage_counts"]["briefing_directory_check"], 1)
        self.assertEqual(filtered["stage_counts"]["daily_pipeline"], 2)
        self.assertEqual(filtered["exit_pairs"], [{"domain_exit": 1, "runtime_exit": 2}])
        self.assertNotIn(SECRET, json.dumps(result))
        for line in ("[briefing-pipeline] domain_exit=256 runtime_exit=1",
                     "[briefing-pipeline] domain_exit=-1 runtime_exit=1",
                     "[briefing-pipeline] domain_exit=1 runtime_exit=99999999",
                     "[briefing-pipeline] runtime digest rejected: " + SECRET):
            result, _ = self.recorded(messages=[line])
            self.assertFalse(result["units"][DAILY]["filtered_evidence"]["available"])
            self.assertEqual(result["units"][DAILY]["filtered_evidence"]["exit_pairs"], [])
            self.assertNotIn(SECRET, json.dumps(result))

    def test_recorded_before_wrong_id_stops_without_journal_or_second_read(self):
        for identity in ("", DID, "0" * 32, SECRET):
            result, calls = self.recorded(before_changes={"InvocationID": identity}, messages=["ImportError: " + SECRET])
            self.assertEqual(calls, [self.m.systemd_command(DAILY)])
            self.assertFalse(result["units"][DAILY]["bound_invocation_current"])
            self.assertFalse(result["units"][DAILY]["filtered_evidence"]["available"])
            self.assertNotIn(SECRET, json.dumps(result))

    def test_recorded_after_race_or_unknown_metadata_discards_all_counts(self):
        for changes in ({"InvocationID": DID}, {"MainPID": "42"}, {"ExecMainStatus": "3"}, {"ActiveState": SECRET}):
            result, calls = self.recorded(messages=["PermissionError: " + SECRET], after_changes=changes)
            self.assertEqual(len(calls), 3)
            filtered = result["units"][DAILY]["filtered_evidence"]
            self.assertFalse(filtered["available"])
            self.assertFalse(any(filtered["exception_counts"].values()))
            self.assertFalse(any(filtered["stage_counts"].values()))
            self.assertNotIn(SECRET, json.dumps(result))

    def test_recorded_no_hits_and_unclassified_grep_words_stop_unknown(self):
        for messages in ([], ["untrusted source mentions RuntimeError with " + SECRET],
                         ["report_dir_not_found " + SECRET], ["domain_exit=" + SECRET]):
            result, calls = self.recorded(messages=messages)
            self.assertEqual(len(calls), 3)
            unit = result["units"][DAILY]
            self.assertEqual(unit["diagnosis"], "unknown")
            self.assertFalse(unit["filtered_evidence"]["available"])
            self.assertFalse(unit["filtered_evidence"]["absence_proven"])
            self.assertNotIn(SECRET, json.dumps(result))

    def test_recorded_malformed_wrong_unit_id_or_non_grep_record_rejects_whole_read(self):
        prefix = self.records(DAILY, ["ImportError: " + SECRET], RECORDED_DID)
        cases = [prefix + b"{\n", prefix + b"\xff\n", prefix + self.records(QUANT, ["ValueError: " + SECRET], RECORDED_DID),
                 prefix + self.records(DAILY, ["ValueError: " + SECRET], DID),
                 prefix + self.records(DAILY, ["no reviewed filter word " + SECRET], RECORDED_DID),
                 prefix + self.records(DAILY, ["ValueError: " + SECRET], RECORDED_DID).replace(b'"MESSAGE":', b'"bad": NaN, "MESSAGE":'),
                 prefix + self.records(DAILY, [[SECRET]], RECORDED_DID)]
        for data in cases:
            result, _ = self.recorded(data=data)
            filtered = result["units"][DAILY]["filtered_evidence"]
            self.assertFalse(filtered["available"])
            self.assertFalse(any(filtered["exception_counts"].values()))
            self.assertNotIn(SECRET, json.dumps(result))

    def test_recorded_all_limits_or_permission_failures_discard_instead_of_sampling(self):
        for count in (64, 65):
            result, _ = self.recorded(messages=["ImportError: " + SECRET] * count)
            self.assertTrue(result["units"][DAILY]["journal"]["truncated"])
            self.assertFalse(result["units"][DAILY]["filtered_evidence"]["available"])
        for flags in ({"returncode": 1}, {"timed_out": True}, {"stderr_seen": True}, {"truncated": True}):
            result, _ = self.recorded(messages=["ImportError: " + SECRET], flags=flags)
            self.assertFalse(result["units"][DAILY]["filtered_evidence"]["available"])
        result, _ = self.recorded(data=self.records(DAILY, ["ImportError: " + SECRET], RECORDED_DID) + b" " * 65536)
        self.assertFalse(result["units"][DAILY]["filtered_evidence"]["available"])
        result, _ = self.recorded(messages=["ImportError: " + SECRET] * 63)
        self.assertEqual(result["units"][DAILY]["filtered_evidence"]["exception_counts"]["ImportError"], 63)

    def test_recorded_command_validator_accepts_only_reviewed_exact_grep_binding(self):
        command = self.m.RECORDED_DAILY_JOURNAL
        self.assertEqual(self.m.command_limit(command), self.m.JOURNAL_BYTES)
        for before, after in ((RECORDED_DID, DID), (DAILY, QUANT),
                              (self.m.RECORDED_DAILY_ERROR_GREP, "Error"), ("--case-sensitive=yes", "--case-sensitive=no"),
                              ("--lines=64", "--lines=65")):
            changed = tuple(value.replace(before, after) for value in command)
            self.assertIsNone(self.m.command_limit(changed))
            self.assertIsNone(self.m.run_bounded(changed).returncode)

    def test_recorded_reader_uses_the_existing_bounded_stream_with_exact_argv(self):
        data = self.records(DAILY, ["ImportError: " + SECRET], RECORDED_DID)
        _, popen, sizes = self.fake_process(data)
        read = self.m.run_bounded(self.m.RECORDED_DAILY_JOURNAL)
        self.assertEqual(read.returncode, 0)
        self.assertEqual(sum(sizes), len(data))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], self.m.RECORDED_DAILY_JOURNAL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["env"], self.m.CLEAN_ENV)

    def test_recorded_before_metadata_read_failure_stops_at_first_command(self):
        for flags in ({"returncode": 1}, {"timed_out": True}, {"stderr_seen": True}, {"truncated": True}):
            calls = []
            def runner(argv):
                calls.append(tuple(argv))
                return self.m.ReadResult(data=self.properties(DAILY, RECORDED_DID), **{"returncode": 0, **flags})
            result = self.m.collect_recorded_daily_errors(runner=runner)
            self.assertEqual(calls, [self.m.systemd_command(DAILY)])
            self.assertFalse(result["units"][DAILY]["filtered_evidence"]["available"])

    def test_recorded_cli_fixed_no_free_invocation_or_combined_modes(self):
        for argv in (["--inspect-recorded-daily-errors", DID], ["--inspect-recorded-daily-errors", "--inspect"],
                     ["--inspect-recorded-daily-errors", "--inspect-daily-only"], ["--grep=" + SECRET], []):
            with mock.patch.object(self.m, "collect_recorded_daily_errors", side_effect=ExternalAttempt("invalid CLI read")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.m.main(argv), 64)
        with mock.patch.object(self.m, "collect_recorded_daily_errors", return_value={}) as collect, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.main(["--inspect-recorded-daily-errors"]), 0)
        collect.assert_called_once_with()

    def test_daily_sample_validates_all_64_and_keeps_diagnosis_unknown(self):
        messages = ["unclassified " + SECRET] * 62 + [
            '  "failure_stage": "briefing_input_processing",', '  "failure_category": "io_error",',
        ]
        result, calls = self.collect(daily_only=True, journals={DAILY: self.records(DAILY, messages)})
        self.assertEqual(set(result["units"]), {DAILY})
        self.assertEqual(calls, [self.m.systemd_command(DAILY), self.m.journal_command(DAILY, DID), self.m.systemd_command(DAILY)])
        unit = result["units"][DAILY]
        self.assertEqual(unit["diagnosis"], "unknown")
        self.assertTrue(unit["journal"]["truncated"])
        self.assertTrue(unit["journal"]["current_invocation_matched"])
        sample = unit["sampled_evidence"]
        self.assertTrue(sample["available"])
        self.assertFalse(sample["complete"])
        self.assertFalse(sample["absence_proven"])
        self.assertEqual(sample["record_count"], 64)
        self.assertEqual(sample["stage_counts"]["briefing_input_processing"], 1)
        self.assertEqual(sample["error_counts"]["io_error"], 1)
        self.assertFalse(any(unit["stage_counts"].values()))
        self.assertFalse(any(unit["error_counts"].values()))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_daily_63_records_keep_normal_classification_and_no_partial_sample(self):
        messages = ["unclassified"] * 62 + ["ImportError: " + SECRET]
        result, calls = self.collect(daily_only=True, journals={DAILY: self.records(DAILY, messages)})
        unit = result["units"][DAILY]
        self.assertEqual(len(calls), 3)
        self.assertEqual(unit["diagnosis"], "observed_error")
        self.assertFalse(unit["journal"]["truncated"])
        self.assertEqual(unit["error_counts"]["import_error"], 1)
        self.assertFalse(unit["sampled_evidence"]["available"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_daily_64_zero_counts_never_prove_error_absence(self):
        result, _ = self.collect(daily_only=True, journals={DAILY: self.records(DAILY, [SECRET] * 64)})
        sample = result["units"][DAILY]["sampled_evidence"]
        self.assertTrue(sample["available"])
        self.assertFalse(sample["absence_proven"])
        self.assertFalse(sample["complete"])
        self.assertFalse(any(sample["stage_counts"].values()))
        self.assertFalse(any(sample["error_counts"].values()))
        self.assertEqual(result["units"][DAILY]["diagnosis"], "unknown")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_daily_sample_rejects_any_malformed_or_mismatched_record(self):
        prefix = self.records(DAILY, ["ImportError: " + SECRET] * 63)
        cases = [prefix + b"{\n", prefix + b"[]\n", prefix + b"\xff\n",
                 prefix + self.records(QUANT, ["ImportError: " + SECRET], DID),
                 prefix + self.records(DAILY, ["ImportError: " + SECRET], QID),
                 prefix + self.records(DAILY, [[SECRET]]),
                 prefix + self.records(DAILY, [None]),
                 prefix + self.records(DAILY, ["ImportError: " + SECRET]).replace(b'"MESSAGE":', b'"MESSAGE": "a", "MESSAGE":'),
                 prefix + self.records(DAILY, ["ImportError: " + SECRET]).replace(b'"MESSAGE":', b'"untrusted": NaN, "MESSAGE":'),
                 prefix + self.records(DAILY, ["ImportError: " + SECRET]).replace(b'"MESSAGE":', b'"untrusted": Infinity, "MESSAGE":')]
        for data in cases:
            result, _ = self.collect(daily_only=True, journals={DAILY: data})
            unit = result["units"][DAILY]
            self.assertEqual(unit["diagnosis"], "unknown")
            self.assertFalse(unit["sampled_evidence"]["available"])
            self.assertFalse(any(unit["sampled_evidence"]["error_counts"].values()))
            self.assertNotIn(SECRET, json.dumps(result))

    def test_daily_sample_never_parses_more_than_64_or_byte_capped_input(self):
        for data in (self.records(DAILY, ["ImportError: " + SECRET] * 65),
                     self.records(DAILY, ["ImportError: " + SECRET] * 64) + b" " * 65536):
            with mock.patch.object(self.m, "evidence", side_effect=ExternalAttempt("sample over budget")):
                result, _ = self.collect(daily_only=True, journals={DAILY: data})
            self.assertFalse(result["units"][DAILY]["sampled_evidence"]["available"])
            self.assertNotIn(SECRET, json.dumps(result))

    def test_daily_sample_timeout_stderr_permission_and_byte_truncation_discard_counts(self):
        data = self.records(DAILY, ["ImportError: " + SECRET] * 64)
        for flags in ({"timed_out": True}, {"stderr_seen": True}, {"returncode": 1}, {"truncated": True}):
            result, _ = self.collect(daily_only=True, override=lambda args, _: self.m.ReadResult(
                data=data, **{"returncode": 0, **flags}
            ) if args[0] == self.m.JOURNALCTL else None)
            unit = result["units"][DAILY]
            self.assertFalse(unit["sampled_evidence"]["available"])
            self.assertFalse(any(unit["sampled_evidence"]["error_counts"].values()))
            self.assertEqual(unit["diagnosis"], "unknown")
            self.assertNotIn(SECRET, json.dumps(result))

    def test_daily_sample_race_or_unknown_systemd_metadata_discards_counts(self):
        for changes, changed_read in (({"InvocationID": QID}, 2), ({"MainPID": "42"}, 2),
                                      ({"ExecMainStatus": "3"}, 2), ({"ActiveState": SECRET}, None)):
            result, _ = self.collect(daily_only=True,
                journals={DAILY: self.records(DAILY, ["ImportError: " + SECRET] * 64)},
                override=lambda args, count: self.m.ReadResult(data=self.properties(DAILY, **changes), returncode=0)
                if args[0] == self.m.SYSTEMCTL and (changed_read is None or count == changed_read) else None)
            unit = result["units"][DAILY]
            self.assertFalse(unit["sampled_evidence"]["available"])
            self.assertFalse(any(unit["sampled_evidence"]["stage_counts"].values()))
            self.assertFalse(any(unit["sampled_evidence"]["error_counts"].values()))
            self.assertEqual(unit["diagnosis"], "unknown")
            self.assertNotIn(SECRET, json.dumps(result))

    def test_daily_only_unknown_invocation_never_reads_any_other_unit(self):
        result, calls = self.collect(daily_only=True, override=lambda args, _: self.m.ReadResult(
            data=self.properties(DAILY, InvocationID=""), returncode=0
        ))
        self.assertEqual(calls, [self.m.systemd_command(DAILY), self.m.systemd_command(DAILY)])
        self.assertEqual(set(result["units"]), {DAILY})
        self.assertFalse(result["units"][DAILY]["sampled_evidence"]["available"])
        for unsupported in (DAILY, [DAILY], 1, None):
            with self.assertRaises(ValueError):
                self.m.collect(daily_only=unsupported)

    def test_daily_only_cli_is_strict_and_old_inspect_route_stays_unchanged(self):
        for argv in ([], ["--daily-only"], ["--inspect", "--inspect-daily-only"],
                     ["--inspect-daily-only", SECRET], ["--inspect-daily-only", "--fixture-test"]):
            with mock.patch.object(self.m, "collect", side_effect=ExternalAttempt("invalid CLI read")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.m.main(argv), 64)
        for argv, expected in ((["--inspect"], {}), (["--inspect-daily-only"], {"daily_only": True})):
            with mock.patch.object(self.m, "collect", return_value={}) as collect, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.m.main(argv), 0)
            collect.assert_called_once_with(**expected)
        result, _ = self.collect(journals={DAILY: self.records(DAILY, ["ImportError: " + SECRET] * 64)})
        self.assertNotIn("sampled_evidence", result["units"][DAILY])
        self.assertEqual(result["units"][DAILY]["diagnosis"], "unknown")

    def test_daily_sample_malicious_message_cannot_escape_fixed_enums(self):
        messages = [json.dumps({"failure_stage": SECRET, "failure_category": SECRET,
                               "instructions": "restart " + QUANT, "path": SECRET})] * 64
        result, calls = self.collect(daily_only=True, journals={DAILY: self.records(DAILY, messages)})
        unit = result["units"][DAILY]
        self.assertTrue(unit["sampled_evidence"]["available"])
        self.assertFalse(any(unit["sampled_evidence"]["stage_counts"].values()))
        self.assertFalse(any(unit["sampled_evidence"]["error_counts"].values()))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(len(calls), 3)

    def test_monitor_alert_exit_two_is_evidence_not_python_damage(self):
        message = json.dumps({"ok": False, "telegram_alerts": [SECRET], "data_errors": [],
                              "collector_payload_valid": True, "snapshot_count": 1,
                              "optimization_findings": 0, "optimization_issue_errors": 0})
        result, calls = self.collect(journals={QUANT: self.records(QUANT, [message])})
        unit = result["units"][QUANT]
        self.assertEqual(unit["diagnosis"], "observed_monitor_alert")
        self.assertEqual(unit["error_counts"]["monitor_alert"], 1)
        self.assertEqual(unit["error_counts"]["import_error"], 0)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(len(calls), 10)

    def test_exit_two_without_matching_evidence_is_unknown(self):
        result, _ = self.collect(journals={QUANT: self.records(QUANT, ["unclassified exit 2"])})
        self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")

    def test_daily_fixed_failure_stage_and_category(self):
        messages = ['  "failure_stage": "briefing_input_processing",',
                    '  "failure_category": "io_error",', SECRET]
        result, _ = self.collect(journals={DAILY: self.records(DAILY, messages)})
        unit = result["units"][DAILY]
        self.assertEqual(unit["stage_counts"]["briefing_input_processing"], 1)
        self.assertEqual(unit["error_counts"]["io_error"], 1)
        self.assertEqual(unit["diagnosis"], "observed_error")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_pre_start_or_start_failure_does_not_claim_exact_pre_command(self):
        result, _ = self.collect(
            journals={DAILY: self.records(DAILY, ["PermissionError: " + SECRET])},
            override=lambda args, _: self.m.ReadResult(
                data=self.properties(DAILY, ExecMainStartTimestampMonotonic="0"), returncode=0
            ) if args[0] == self.m.SYSTEMCTL and args[3] == DAILY else None,
        )
        self.assertEqual(result["units"][DAILY]["execution_phase"], "pre_start_or_start")
        self.assertEqual(result["units"][DAILY]["error_counts"]["permission_denied"], 1)

    def test_each_journal_query_and_record_matches_its_own_invocation(self):
        result, calls = self.collect(journals={
            QUANT: self.records(QUANT, ["ModuleNotFoundError: " + SECRET]),
            DAILY: self.records(DAILY, ["FileNotFoundError: " + SECRET]),
        })
        journals = [args for args in calls if args[0] == self.m.JOURNALCTL]
        self.assertEqual([(args[-2], args[-1]) for args in journals], [
            ("_SYSTEMD_UNIT=" + QUANT, "_SYSTEMD_INVOCATION_ID=" + QID),
            ("_SYSTEMD_UNIT=" + DAILY, "_SYSTEMD_INVOCATION_ID=" + DID),
        ])
        self.assertTrue(result["units"][QUANT]["journal"]["current_invocation_matched"])
        self.assertTrue(result["units"][DAILY]["journal"]["current_invocation_matched"])

    def test_wrong_unit_or_invocation_is_unknown_and_discards_all_evidence(self):
        for unit, invocation in ((DAILY, QID), (QUANT, DID), (QUANT, SECRET)):
            with self.subTest(unit=unit, invocation_known=invocation in (QID, DID)):
                result, _ = self.collect(journals={QUANT: self.records(unit, ["ImportError: " + SECRET], invocation)})
                q = result["units"][QUANT]
                self.assertEqual(q["diagnosis"], "unknown")
                self.assertEqual(q["error_counts"]["import_error"], 0)
                self.assertFalse(q["journal"]["current_invocation_matched"])
                self.assertNotIn(SECRET, json.dumps(result))

    def test_unknown_invocation_never_queries_journal(self):
        for invocation in ("", "0" * 32, SECRET):
            result, calls = self.collect(override=lambda args, _: self.m.ReadResult(
                data=self.properties(QUANT, InvocationID=invocation), returncode=0
            ) if args[0] == self.m.SYSTEMCTL and args[3] == QUANT else None)
            self.assertFalse(any(args[0] == self.m.JOURNALCTL and args[-2] == "_SYSTEMD_UNIT=" + QUANT for args in calls))
            self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")
            self.assertNotIn(SECRET, json.dumps(result))

    def test_changed_snapshot_discards_evidence(self):
        for changes in ({"InvocationID": DID}, {"ExecMainStatus": "1"}, {"MainPID": "42"}):
            result, _ = self.collect(
                journals={QUANT: self.records(QUANT, ["ImportError: " + SECRET])},
                override=lambda args, n: self.m.ReadResult(data=self.properties(QUANT, **changes), returncode=0)
                if args[0] == self.m.SYSTEMCTL and args[3] == QUANT and n == 2 else None,
            )
            self.assertFalse(result["units"][QUANT]["snapshot_stable"])
            self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")
            self.assertEqual(result["units"][QUANT]["error_counts"]["import_error"], 0)

    def test_empty_journal_cannot_prove_health_or_allow_deployment(self):
        result, _ = self.collect()
        self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")
        self.assertFalse(result["units"][QUANT]["journal"]["visible"])
        self.assertFalse(result["business_recovery_proven"])
        self.assertFalse(result["deployment_authorized"])

    def test_timeout_permission_and_truncation_are_unknown(self):
        for kwargs in ({"timed_out": True}, {"returncode": 1}, {"stderr_seen": True}, {"truncated": True}):
            result, _ = self.collect(override=lambda args, _: self.m.ReadResult(
                data=self.records(QUANT, ["ImportError: " + SECRET]), **{"returncode": 0, **kwargs}
            ) if args[0] == self.m.JOURNALCTL and args[-2] == "_SYSTEMD_UNIT=" + QUANT else None)
            self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")
            self.assertEqual(result["units"][QUANT]["error_counts"]["import_error"], 0)
            self.assertNotIn(SECRET, json.dumps(result))

    def test_byte_and_record_limits_are_unknown_even_with_valid_prefix(self):
        for data in (self.records(QUANT, ["ImportError: " + SECRET] * 64),
                     self.records(QUANT, ["ImportError: " + SECRET] * 65),
                     self.records(QUANT, ["ImportError: " + SECRET]) + b" " * 65536):
            result, _ = self.collect(journals={QUANT: data})
            self.assertTrue(result["units"][QUANT]["journal"]["truncated"])
            self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")

    def test_malformed_duplicate_and_non_string_journal_fields_are_unknown(self):
        valid = self.records(QUANT, ["ImportError: " + SECRET])
        cases = [b"{", b"[]\n", b'\xff\n', valid + b"{", valid.replace(b'"MESSAGE":', b'"MESSAGE": "a", "MESSAGE":'),
                 self.records(QUANT, [[SECRET]]), self.records(QUANT, [None])]
        for data in cases:
            result, _ = self.collect(journals={QUANT: data})
            self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")
            self.assertNotIn(SECRET, json.dumps(result))

    def test_systemd_invalid_identity_or_properties_are_unknown(self):
        cases = [self.properties(QUANT, Id=DAILY), self.properties(QUANT) + b"Id=" + QUANT.encode() + b"\n",
                 b"LoadState=loaded\n", self.properties(QUANT, LoadState="not-found"), b"x" * 16384]
        for data in cases:
            result, calls = self.collect(override=lambda args, _: self.m.ReadResult(data=data, returncode=0)
                                        if args[0] == self.m.SYSTEMCTL and args[3] == QUANT else None)
            self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")
            self.assertFalse(any(args[0] == self.m.JOURNALCTL and args[-2] == "_SYSTEMD_UNIT=" + QUANT for args in calls))

    def test_unknown_status_and_schedule_text_never_echoes(self):
        result, _ = self.collect(override=lambda args, _: self.m.ReadResult(
            data=self.properties(args[3], ActiveState=SECRET, **({"NextElapseUSecRealtime": SECRET} if args[3].endswith(".timer") else {})),
            returncode=0
        ) if args[0] == self.m.SYSTEMCTL else None)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["units"][QUANT]["systemd"]["active_state"], "unknown")
        self.assertEqual(result["units"]["codex-quant.timer"]["systemd"]["next_elapse_utc"], None)
        self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")

    def test_quant_monotonic_timer_template_shape_preserves_schedule_evidence(self):
        # codex-quant.timer.example uses OnBootSec=2min/OnUnitActiveSec=30min.
        # CLI USecMonotonic properties are timespans, not DBus raw integers.
        timer = "codex-quant.timer"
        result, _ = self.collect(override=lambda args, _: self.m.ReadResult(
            data=self.properties(timer, NextElapseUSecRealtime="", LastTriggerUSecMonotonic="1d 2h 12.000001s",
                                 NextElapseUSecMonotonic="1d 2h 30min 12.123456s"), returncode=0
        ) if args[0] == self.m.SYSTEMCTL and args[3] == timer else None)
        state = result["units"][timer]["systemd"]
        self.assertIsNone(state["next_elapse_utc"])
        self.assertEqual(state["next_elapse_monotonic"], {
            "status": "known", "clock": "monotonic", "elapsed_timespan": "1d 2h 30min 12.123456s",
        })
        self.assertEqual(state["last_trigger_monotonic"]["elapsed_timespan"], "1d 2h 12.000001s")
        self.assertEqual(self.m.CLEAN_ENV["TZ"], "UTC")

    def test_monotonic_timespan_sentinels_and_unsupported_are_safe(self):
        for value, status in (("0", "unset"), ("infinity", "infinite"), ("", "unknown"),
                              ("1800000000", "unknown"), ("[not set]", "unknown"), (SECRET, "unknown"),
                              ("1s 2min", "unknown"), ("1s 2s", "unknown"), ("1.25h", "unknown"),
                              ("-1s", "unknown"), ("1us 2s", "unknown")):
            projected = self.m.monotonic_timespan(value)
            self.assertEqual(projected["status"], status)
            self.assertIsNone(projected["elapsed_timespan"])
            self.assertNotIn(SECRET, json.dumps(projected))
        for value in ("2min", "30min", "1w 2d 3h 4min 5.123456s", "1.123ms", "1us"):
            self.assertEqual(self.m.monotonic_timespan(value)["elapsed_timespan"], value)

    def test_unrecognized_metadata_cannot_support_journal_diagnosis(self):
        for changes in ({"ActiveState": SECRET}, {"ExecMainStatus": "9999999999999999999"},
                        {"ExecMainCode": "4"}, {"ExecMainStartTimestampMonotonic": SECRET}):
            result, _ = self.collect(
                journals={QUANT: self.records(QUANT, ["ImportError: " + SECRET])},
                override=lambda args, _: self.m.ReadResult(data=self.properties(QUANT, **changes), returncode=0)
                if args[0] == self.m.SYSTEMCTL and args[3] == QUANT else None,
            )
            self.assertEqual(result["units"][QUANT]["diagnosis"], "unknown")
            self.assertEqual(result["units"][QUANT]["error_counts"]["import_error"], 0)
            self.assertNotIn(SECRET, json.dumps(result))

    def fake_process(self, stdout, stderr=b"", wait_timeout=False):
        m = self.m
        buffers = {101: bytearray(stdout), 102: bytearray(stderr)}
        read_sizes = []

        class Stream:
            def __init__(self, fd):
                self.fd = fd
                self.closed = False

            def fileno(self):
                return self.fd

            def close(self):
                self.closed = True

        class Process:
            def __init__(self):
                self.stdout = Stream(101)
                self.stderr = Stream(102)
                self.returncode = None
                self.kills = 0

            def poll(self):
                return self.returncode

            def kill(self):
                self.kills += 1
                self.returncode = -9

            def wait(self, timeout):
                if wait_timeout and self.returncode is None:
                    raise subprocess.TimeoutExpired("fixed_metadata_command", timeout)
                self.returncode = 0 if self.returncode is None else self.returncode
                return self.returncode

        class Selector:
            def __init__(self):
                self.registered = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def register(self, stream, event):
                self.registered[stream.fd] = mock.Mock(fileobj=stream)

            def unregister(self, stream):
                self.registered.pop(stream.fd)

            def get_map(self):
                return self.registered

            def select(self, timeout):
                return [(key, m.selectors.EVENT_READ) for key in list(self.registered.values())]

        def read(fd, size):
            chunk = bytes(buffers[fd][:size])
            del buffers[fd][:size]
            read_sizes.append(len(chunk))
            return chunk

        proc = Process()
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        popen = patches.enter_context(mock.patch.object(m.subprocess, "Popen", return_value=proc))
        patches.enter_context(mock.patch.object(m.selectors, "DefaultSelector", Selector))
        patches.enter_context(mock.patch.object(m.os, "set_blocking"))
        patches.enter_context(mock.patch.object(m.os, "read", read))
        return proc, popen, read_sizes

    def test_actual_stream_runner_caps_bytes_without_unbounded_capture(self):
        for stderr in (b"", b"x" * 65536):
            with self.subTest(stderr_present=bool(stderr)):
                proc, popen, sizes = self.fake_process(b"x" * 70000, stderr)
                result = self.m.run_bounded(self.m.journal_command(QUANT, QID))
                self.assertTrue(result.truncated)
                self.assertEqual(sum(sizes), 65536)
                self.assertLessEqual(len(result.data), 65536)
                self.assertEqual(proc.kills, 1)
                self.assertTrue(proc.stdout.closed and proc.stderr.closed)
                args, kwargs = popen.call_args
                self.assertEqual(args[0], self.m.journal_command(QUANT, QID))
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertIs(kwargs["shell"], False)
                self.assertEqual(kwargs["env"], self.m.CLEAN_ENV)
                self.assertEqual(set(kwargs), {"stdin", "stdout", "stderr", "shell", "env", "close_fds"})

    def test_actual_stream_runner_handles_eof_stderr_and_deadline(self):
        proc, _, sizes = self.fake_process(b"fixed_fixture", b"private-stderr-" + SECRET.encode())
        result = self.m.run_bounded(self.m.systemd_command(QUANT))
        self.assertEqual(result.data, b"fixed_fixture")
        self.assertTrue(result.stderr_seen)
        self.assertEqual(proc.kills, 0)
        self.assertNotIn(SECRET, repr(result))
        self.assertLess(sum(sizes), self.m.SYSTEMD_BYTES)
        proc, _, sizes = self.fake_process(b"unread", b"unread")
        with mock.patch.object(self.m.time, "monotonic", side_effect=[0, 6]):
            result = self.m.run_bounded(self.m.journal_command(QUANT, QID))
        self.assertTrue(result.timed_out)
        self.assertEqual(sum(sizes), 0)
        self.assertEqual(proc.kills, 1)

    def test_actual_stream_runner_process_wait_timeout_and_spawn_denial_are_safe(self):
        proc, _, _ = self.fake_process(b"", wait_timeout=True)
        result = self.m.run_bounded(self.m.systemd_command(QUANT))
        self.assertTrue(result.timed_out)
        self.assertEqual(proc.kills, 1)
        with mock.patch.object(self.m.subprocess, "Popen", side_effect=PermissionError(SECRET)):
            result = self.m.run_bounded(self.m.systemd_command(QUANT))
        self.assertIsNone(result.returncode)
        self.assertNotIn(SECRET, repr(result))

    def test_fixed_data_errors_do_not_echo_extra_fields(self):
        message = json.dumps({"data_errors": [{"code": "trusted_artifact_unavailable", "path": SECRET}]})
        result, _ = self.collect(journals={QUANT: self.records(QUANT, [message])})
        self.assertEqual(result["units"][QUANT]["error_counts"]["data_or_artifact_unavailable"], 1)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_legacy_coarse_unavailable_reason_does_not_invent_failure_stage(self):
        for message in (json.dumps({"ai_summary": {"reason": "briefing_input_unavailable", "error": SECRET}}),
                        '  "reason": "briefing_input_unavailable",'):
            result, _ = self.collect(journals={DAILY: self.records(DAILY, [message])})
            unit = result["units"][DAILY]
            self.assertEqual(unit["error_counts"]["data_or_artifact_unavailable"], 1)
            self.assertFalse(any(unit["stage_counts"].values()))
            self.assertEqual(unit["error_counts"]["executable_or_file_missing"], 0)
            self.assertNotIn(SECRET, json.dumps(result))

    def test_fixed_missing_file_permission_and_import_categories(self):
        for message, category, stage in (
            ("ImportError: " + SECRET, "import_error", "python_import"),
            ("bash: /private/" + SECRET + ": No such file or directory", "executable_or_file_missing", "process_start"),
            ("python3: can't open file '/private/" + SECRET + "': [Errno 2] No such file or directory", "executable_or_file_missing", "process_start"),
            ("bash: /private/" + SECRET + ": Permission denied", "permission_denied", "process_start"),
        ):
            result, _ = self.collect(journals={DAILY: self.records(DAILY, [message])})
            self.assertEqual(result["units"][DAILY]["error_counts"][category], 1)
            self.assertEqual(result["units"][DAILY]["stage_counts"][stage], 1)
            self.assertNotIn(SECRET, json.dumps(result))

    def test_unknown_stage_category_and_callback_error_do_not_echo(self):
        message = json.dumps({"failure_stage": SECRET, "failure_category": SECRET})
        result, _ = self.collect(journals={DAILY: self.records(DAILY, [message])})
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["units"][DAILY]["diagnosis"], "unknown")
        def failing(_):
            raise OSError(SECRET)
        self.assertNotIn(SECRET, json.dumps(self.m.collect(runner=failing)))

    def test_cli_requires_explicit_inspect_and_fixture_test_is_offline(self):
        for argv in ([], [SECRET], ["--inspect", SECRET]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(self.m.main(argv), 64)
            self.assertNotIn(SECRET, output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.m.main(["--fixture-test"]), 0)
        self.assertEqual(json.loads(output.getvalue())["fixture_test"], "passed")
        output = io.StringIO()
        with mock.patch.object(self.m, "collect", side_effect=RuntimeError(SECRET)), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.m.main(["--inspect"]), 1)
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_no_arbitrary_commands_units_paths_or_invocations(self):
        for argv in (["/usr/bin/systemctl", "restart", QUANT], ["/bin/sh", "-c", SECRET],
                     [self.m.SYSTEMCTL, "--no-pager", "show", SECRET, "--property=Environment"],
                     [self.m.JOURNALCTL, "--no-pager", SECRET]):
            self.assertIsNone(self.m.run_bounded(argv).returncode)

    def test_workflow_is_separate_manual_protected_and_secret_free(self):
        workflow = (SCRIPT.parents[3] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        job = workflow.split("  inspect-monitor-failures:\n", 1)[1].split("\n  inspect-quant-runtime:", 1)[0]
        for text in ("inputs.mode == 'inspect-monitor-failures'", "github.event_name == 'workflow_dispatch'",
                     "environment: codex-vps-ops", "persist-credentials: false", 'checkout_status="$(git status --porcelain --untracked-files=all)"',
                     '[ "$RUN_WORKFLOW_SHA" = "$RUN_SHA" ]', '[ "$current_main" = "$RUN_SHA" ]',
                     "env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C", "/usr/bin/python3 -I -B", 'inspect_monitor_failures.py" --inspect'):
            self.assertIn(text, job)
        for text in ("sudo", "secrets.", "OPENAI", "ANTHROPIC", "health_check.sh", "daily_briefing_pipeline.sh", "acknowledge_interruption"):
            self.assertNotIn(text, job)
        self.assertIn("inputs.mode != 'inspect-monitor-failures'", workflow.split("  inspect-monitor-failures:", 1)[0])
        self.assertIn("default: inspect\n", workflow)
        self.assertIn("group: vps-codex-service-ops\n  cancel-in-progress: false", workflow)
        self.assertNotIn("  schedule:", workflow)
        self.assertNotIn("  push:", workflow)

    def test_workflow_mode_routes_only_to_its_own_job(self):
        workflow = (SCRIPT.parents[3] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        conditions = dict(re.findall(r"^  ([a-z-]+):\n    if: ([^\n]+)", workflow, re.MULTILINE))
        expected = {
            "inspect": "vps-codex-service-ops", "deploy": "vps-codex-service-ops",
            "repair-ssh": "vps-codex-service-ops", "install-org-health-token": "org-health-token",
            "inspect-quant-runtime": "inspect-quant-runtime", "inspect-quant-paths": "inspect-quant-paths",
            "stage-quant-runtime-inactive": "stage-quant-runtime-inactive",
            "install-quant-release-inactive": "install-quant-release-inactive",
            "inspect-audit-patch": "inspect-audit-patch", "apply-audit-patch": "apply-audit-patch",
            "retry-audit-patch-once": "retry-audit-patch-once",
            "release-gateway-failure-repairs": "release-gateway-failure-repairs",
            "inspect-monitor-failures": "inspect-monitor-failures",
            "inspect-daily-failure-sample": "inspect-daily-failure-sample",
            "inspect-recorded-daily-errors": "inspect-recorded-daily-errors",
        }

        def routed(mode, event="workflow_dispatch", ref="refs/heads/main", repository="QuantStrategyLab/AIAuditBridge",
                   acknowledge_interruption=True, ssh_unban_ip=""):
            values = {"inputs.mode": mode, "github.event_name": event, "github.ref": ref,
                      "github.repository": repository, "inputs.acknowledge_interruption": acknowledge_interruption,
                      "inputs.ssh_unban_ip": ssh_unban_ip}
            jobs = []
            for job, condition in conditions.items():
                terms = []
                for term in condition.split(" && "):
                    comparison = re.fullmatch(r"([a-z_.]+) (==|!=) '([^']*)'", term)
                    if comparison:
                        key, operator, value = comparison.groups()
                        self.assertIn(key, values)
                        terms.append((values[key] == value) if operator == "==" else (values[key] != value))
                    else:
                        self.assertIn(term, ("inputs.acknowledge_interruption", "!inputs.acknowledge_interruption"))
                        terms.append(not acknowledge_interruption if term.startswith("!") else acknowledge_interruption)
                if all(terms):
                    jobs.append(job)
            return jobs

        self.assertEqual(set(conditions), set(expected.values()))
        for mode, job in expected.items():
            self.assertEqual(routed(mode, acknowledge_interruption=mode not in {"stage-quant-runtime-inactive", "install-quant-release-inactive"}), [job])
        self.assertEqual(routed("stage-quant-runtime-inactive", acknowledge_interruption=True), [])
        self.assertEqual(routed("stage-quant-runtime-inactive", acknowledge_interruption=False,
                                ssh_unban_ip="192.0.2.1"), [])
        self.assertEqual(routed("install-quant-release-inactive", acknowledge_interruption=True), [])
        self.assertEqual(routed("install-quant-release-inactive", acknowledge_interruption=False,
                                ssh_unban_ip="192.0.2.1"), [])
        for changes in ({"event": "push"}, {"ref": "refs/heads/other"}, {"repository": "other/repo"}):
            self.assertEqual(routed("inspect-monitor-failures", **changes), [])
            self.assertEqual(routed("inspect-daily-failure-sample", **changes), [])
            self.assertEqual(routed("inspect-recorded-daily-errors", **changes), [])
            self.assertEqual(routed("stage-quant-runtime-inactive", acknowledge_interruption=False, **changes), [])
            self.assertEqual(routed("install-quant-release-inactive", acknowledge_interruption=False, **changes), [])

    def test_recorded_workflow_is_separate_exact_main_and_fixed_cli(self):
        workflow = (SCRIPT.parents[3] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        job = workflow.split("  inspect-recorded-daily-errors:\n", 1)[1].split("\n  inspect-daily-failure-sample:", 1)[0]
        for text in ("inputs.mode == 'inspect-recorded-daily-errors'", "github.event_name == 'workflow_dispatch'",
                     "github.repository == 'QuantStrategyLab/AIAuditBridge'", "github.ref == 'refs/heads/main'",
                     "environment: codex-vps-ops", "persist-credentials: false", '[ "$RUN_MODE" = inspect-recorded-daily-errors ]',
                     '[ "$RUN_EVENT_NAME" = workflow_dispatch ]', '[ "$RUN_WORKFLOW_SHA" = "$RUN_SHA" ]',
                     '[ "$current_main" = "$RUN_SHA" ]', 'checkout_status="$(git status --porcelain --untracked-files=all)"',
                     "env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C", "/usr/bin/python3 -I -B", 'inspect_monitor_failures.py" --inspect-recorded-daily-errors'):
            self.assertIn(text, job)
        for text in ("sudo", "secrets.", "OPENAI", "ANTHROPIC", "health_check.sh", "daily_briefing_pipeline.sh",
                     "acknowledge_interruption", "systemctl", "journalctl", " --inspect\n", " --inspect-daily-only\n"):
            self.assertNotIn(text, job)
        self.assertIn("inputs.mode != 'inspect-recorded-daily-errors'", workflow.split("  inspect-recorded-daily-errors:", 1)[0])

    def test_daily_workflow_is_exact_manual_gate_and_runs_only_strict_flag(self):
        workflow = (SCRIPT.parents[3] / ".github/workflows/vps_codex_service_ops.yml").read_text()
        job = workflow.split("  inspect-daily-failure-sample:\n", 1)[1].split("\n  inspect-monitor-failures:", 1)[0]
        for text in ("inputs.mode == 'inspect-daily-failure-sample'", "github.event_name == 'workflow_dispatch'",
                     "github.repository == 'QuantStrategyLab/AIAuditBridge'", "github.ref == 'refs/heads/main'",
                     "environment: codex-vps-ops", "persist-credentials: false", '[ "$RUN_MODE" = inspect-daily-failure-sample ]',
                     '[ "$RUN_EVENT_NAME" = workflow_dispatch ]', '[ "$RUN_WORKFLOW_SHA" = "$RUN_SHA" ]',
                     '[ "$current_main" = "$RUN_SHA" ]', 'checkout_status="$(git status --porcelain --untracked-files=all)"',
                     "env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C", "/usr/bin/python3 -I -B", 'inspect_monitor_failures.py" --inspect-daily-only'):
            self.assertIn(text, job)
        for text in ("sudo", "secrets.", "OPENAI", "ANTHROPIC", "health_check.sh", "daily_briefing_pipeline.sh",
                     "acknowledge_interruption", " --inspect\n", "systemctl", "journalctl"):
            self.assertNotIn(text, job)
        self.assertIn("inputs.mode != 'inspect-daily-failure-sample'", workflow.split("  inspect-daily-failure-sample:", 1)[0])
        self.assertIn("default: inspect\n", workflow)
        self.assertNotIn("  schedule:", workflow)
        self.assertNotIn("  push:", workflow)


if __name__ == "__main__":
    unittest.main()
