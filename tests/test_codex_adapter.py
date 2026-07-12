from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from service.adapters.codex_adapter import CodexAdapter


class CodexAdapterDispatchTests(unittest.TestCase):
    @staticmethod
    def _process(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
        process = Mock(returncode=returncode)
        process.communicate.return_value = (stdout, stderr)
        return process

    def test_timeout_is_pending_uncertain_not_confirmed_dispatch(self) -> None:
        process = self._process()
        process.communicate.side_effect = subprocess.TimeoutExpired("codex", 1)
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.Popen", return_value=process),
        ):
            result = CodexAdapter().execute(prompt="review", timeout=1)

        self.assertTrue(result.dispatch_started)
        self.assertTrue(result.dispatch_uncertain)

    def test_dispatch_callback_runs_after_popen(self) -> None:
        events: list[str] = []
        process = self._process(stdout="done")
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "service.adapters.codex_adapter.subprocess.Popen",
                side_effect=lambda *args, **kwargs: events.append("popen") or process,
            ),
        ):
            result = CodexAdapter().execute(
                prompt="review", on_dispatch_start=lambda: events.append("dispatch")
            )

        self.assertEqual(events, ["popen", "dispatch"])
        self.assertTrue(result.dispatch_started)

    def test_popen_creation_failure_is_not_dispatched(self) -> None:
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.Popen", side_effect=OSError("permission denied")),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertFalse(result.dispatch_started)
        self.assertFalse(result.dispatch_uncertain)

    def test_callback_failure_after_popen_is_pending_uncertain(self) -> None:
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.Popen", return_value=self._process()),
        ):
            result = CodexAdapter().execute(
                prompt="review", on_dispatch_start=lambda: (_ for _ in ()).throw(OSError("job store unavailable"))
            )

        self.assertTrue(result.dispatch_started)
        self.assertTrue(result.dispatch_uncertain)

    def test_post_dispatch_decode_failure_is_confirmed_dispatch(self) -> None:
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch(
                "service.adapters.codex_adapter.subprocess.Popen",
                return_value=Mock(
                    communicate=Mock(
                        side_effect=UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "invalid start byte")
                    )
                ),
            ),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertFalse(result.success)
        self.assertTrue(result.dispatch_started)
        self.assertTrue(result.dispatch_uncertain)

    def test_nonzero_exit_is_pending_uncertain_not_confirmed_dispatch(self) -> None:
        completed = self._process(returncode=1, stdout="upstream request interrupted")
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.Popen", return_value=completed),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertTrue(result.dispatch_started)
        self.assertTrue(result.dispatch_uncertain)

    def test_parser_exit_is_pending_uncertain_without_structured_signal(self) -> None:
        completed = self._process(returncode=2, stderr="error: unknown option --bad-flag")
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.Popen", return_value=completed),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertTrue(result.dispatch_started)
        self.assertTrue(result.dispatch_uncertain)

    def test_parser_exit_with_stdout_is_pending_uncertain(self) -> None:
        completed = self._process(returncode=2, stdout="progress", stderr="error: unknown option --bad-flag")
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.Popen", return_value=completed),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertTrue(result.dispatch_uncertain)

    def test_local_prefix_in_stdout_is_not_treated_as_prelaunch_evidence(self) -> None:
        completed = self._process(returncode=2, stdout="error: unknown option --remote", stderr="remote failed")
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.Popen", return_value=completed),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertTrue(result.dispatch_uncertain)

    def test_prelaunch_command_failure_is_not_dispatched(self) -> None:
        with patch(
            "service.adapters.codex_adapter._codex_command",
            side_effect=RuntimeError("codex CLI was not found on the service host"),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertFalse(result.dispatch_started)
        self.assertFalse(result.dispatch_uncertain)


if __name__ == "__main__":
    unittest.main()
