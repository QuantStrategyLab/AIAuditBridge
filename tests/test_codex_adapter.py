from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from service.adapters.codex_adapter import CodexAdapter


class CodexAdapterDispatchTests(unittest.TestCase):
    def test_timeout_is_pending_uncertain_not_confirmed_dispatch(self) -> None:
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 1)),
        ):
            result = CodexAdapter().execute(prompt="review", timeout=1)

        self.assertFalse(result.dispatch_started)
        self.assertTrue(result.dispatch_uncertain)

    def test_nonzero_exit_is_pending_uncertain_not_confirmed_dispatch(self) -> None:
        completed = Mock(returncode=1, stdout="upstream request interrupted", stderr="")
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.run", return_value=completed),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertFalse(result.dispatch_started)
        self.assertTrue(result.dispatch_uncertain)

    def test_known_local_nonzero_exit_is_not_dispatched(self) -> None:
        completed = Mock(returncode=2, stdout="", stderr="unknown option --bad-flag")
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.run", return_value=completed),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertFalse(result.dispatch_started)
        self.assertFalse(result.dispatch_uncertain)

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
