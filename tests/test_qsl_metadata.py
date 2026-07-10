"""Validate qsl governance metadata for control-plane repos."""

from __future__ import annotations

from pathlib import Path
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


class TestQslMetadata(unittest.TestCase):
    """AIAuditBridge is a control-plane bridge, not runtime dependency."""

    def _load_qsl(self) -> dict:
        with Path("qsl.toml").open("rb") as handle:
            return tomllib.load(handle)

    def test_qsl_file_exists(self) -> None:
        self.assertTrue(Path("qsl.toml").exists())

    def test_qsl_metadata_values(self) -> None:
        qsl = self._load_qsl()
        self.assertEqual(qsl.get("tier"), "ops/tooling")
        self.assertEqual(qsl.get("upgrade_ring"), "ring_e")
        self.assertIs(qsl.get("runtime_dependency"), False)

        compat = qsl.get("compat")
        self.assertIsInstance(compat, dict)
        self.assertEqual(compat.get("bundle"), "2026.07.0")
