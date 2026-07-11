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
        completed = Mock(returncode=1, stdout="bootstrap failed", stderr="")
        with (
            patch("service.adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("service.adapters.codex_adapter.subprocess.run", return_value=completed),
        ):
            result = CodexAdapter().execute(prompt="review")

        self.assertFalse(result.dispatch_started)
        self.assertTrue(result.dispatch_uncertain)


if __name__ == "__main__":
    unittest.main()
