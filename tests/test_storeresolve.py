#!/usr/bin/env python3
"""Unit tests for lib/storeresolve.py (IM-280).

storeresolve is the one store-root resolution chain: the 4-step precedence
(--store -> ISSUE_STORE_ROOT -> sentinel -> None) that common.py, create.py,
ready.py, and editcore.py all delegate to, plus the shared config-file
reader the three deviating admin verbs (init-project, migrate, snapshot)
consume.

Run:
    python3 -m unittest tests.test_storeresolve -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import errors  # noqa: E402
import storeresolve  # noqa: E402


class ResolveStoreRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.anchor = self.tmp / ".issues"
        self.anchor.mkdir()
        self._saved_env = os.environ.get("ISSUE_STORE_ROOT")
        os.environ.pop("ISSUE_STORE_ROOT", None)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("ISSUE_STORE_ROOT", None)
        else:
            os.environ["ISSUE_STORE_ROOT"] = self._saved_env
        self._tmp.cleanup()

    def test_cli_store_wins_over_env_and_sentinel(self):
        os.environ["ISSUE_STORE_ROOT"] = "/env/store"
        (self.anchor / ".migrated-to-store").write_text("/sentinel/store\n")
        self.assertEqual(
            storeresolve.resolve_store_root(self.anchor, "/flag/store"),
            Path("/flag/store"),
        )

    def test_env_wins_over_sentinel(self):
        os.environ["ISSUE_STORE_ROOT"] = "/env/store"
        (self.anchor / ".migrated-to-store").write_text("/sentinel/store\n")
        self.assertEqual(
            storeresolve.resolve_store_root(self.anchor, None),
            Path("/env/store"),
        )

    def test_sentinel_payload_is_trimmed(self):
        (self.anchor / ".migrated-to-store").write_text(
            "  /sentinel/store  \n"
        )
        self.assertEqual(
            storeresolve.resolve_store_root(self.anchor, None),
            Path("/sentinel/store"),
        )

    def test_none_when_nothing_resolves(self):
        self.assertIsNone(storeresolve.resolve_store_root(self.anchor, None))

    def test_each_leg_expands_tilde(self):
        self.assertEqual(
            storeresolve.resolve_store_root(self.anchor, "~/flag-store"),
            Path.home() / "flag-store",
        )
        os.environ["ISSUE_STORE_ROOT"] = "~/env-store"
        self.assertEqual(
            storeresolve.resolve_store_root(self.anchor, None),
            Path.home() / "env-store",
        )
        os.environ.pop("ISSUE_STORE_ROOT", None)
        (self.anchor / ".migrated-to-store").write_text("~/sentinel-store\n")
        self.assertEqual(
            storeresolve.resolve_store_root(self.anchor, None),
            Path.home() / "sentinel-store",
        )


class CorruptSentinelTests(unittest.TestCase):
    """A present-but-empty sentinel is corrupt routing state (IM-294).

    ``Path('')`` is ``Path('.')``, so silently accepting an empty payload
    rerouted the store root to the current directory; the chain now fails
    loud naming the sentinel path.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.anchor = self.tmp / ".issues"
        self.anchor.mkdir()
        self.sentinel = self.anchor / ".migrated-to-store"
        self._saved_env = os.environ.get("ISSUE_STORE_ROOT")
        os.environ.pop("ISSUE_STORE_ROOT", None)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("ISSUE_STORE_ROOT", None)
        else:
            os.environ["ISSUE_STORE_ROOT"] = self._saved_env
        self._tmp.cleanup()

    def test_empty_sentinel_raises_corrupt_sentinel(self):
        self.sentinel.write_text("")
        with self.assertRaises(errors.PreconditionError) as ctx:
            storeresolve.resolve_store_root(self.anchor, None)
        err = ctx.exception
        self.assertEqual(err.fields.get("reason"), "corrupt-sentinel")
        self.assertEqual(err.fields.get("sentinel"), str(self.sentinel))
        self.assertEqual(err.exit_code, 1)

    def test_whitespace_only_sentinel_raises_corrupt_sentinel(self):
        self.sentinel.write_text("  \n\t\n")
        with self.assertRaises(errors.PreconditionError) as ctx:
            storeresolve.resolve_store_root(self.anchor, None)
        self.assertEqual(
            ctx.exception.fields.get("reason"), "corrupt-sentinel"
        )

    def test_flag_leg_short_circuits_a_corrupt_sentinel(self):
        self.sentinel.write_text("")
        self.assertEqual(
            storeresolve.resolve_store_root(self.anchor, "/flag/store"),
            Path("/flag/store"),
        )

    def test_env_leg_short_circuits_a_corrupt_sentinel(self):
        self.sentinel.write_text("")
        os.environ["ISSUE_STORE_ROOT"] = "/env/store"
        self.assertEqual(
            storeresolve.resolve_store_root(self.anchor, None),
            Path("/env/store"),
        )

    def test_read_sentinel_payload_returns_trimmed_payload(self):
        self.sentinel.write_text("  /sentinel/store  \n")
        self.assertEqual(
            storeresolve.read_sentinel_payload(self.sentinel),
            "/sentinel/store",
        )


class ReadConfigStoreRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name) / "config"
        self._patch = mock.patch.object(
            storeresolve, "CONFIG_PATH", self.config
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_absent_file_returns_none(self):
        self.assertIsNone(storeresolve.read_config_store_root())

    def test_store_root_key_is_read(self):
        self.config.write_text("store_root = /cfg/store\n")
        self.assertEqual(
            storeresolve.read_config_store_root(), "/cfg/store"
        )

    def test_comments_blanks_and_other_keys_are_skipped(self):
        self.config.write_text(
            "# a comment\n\nother = x\nstore_root = /cfg/store\n"
        )
        self.assertEqual(
            storeresolve.read_config_store_root(), "/cfg/store"
        )

    def test_empty_value_returns_none(self):
        self.config.write_text("store_root =\n")
        self.assertIsNone(storeresolve.read_config_store_root())

    def test_unreadable_file_returns_none(self):
        self.config.write_text("store_root = /cfg/store\n")
        with mock.patch.object(
            Path, "read_text", side_effect=OSError("denied")
        ):
            self.assertIsNone(storeresolve.read_config_store_root())


if __name__ == "__main__":
    unittest.main()
