#!/usr/bin/env python3
"""Unit tests for lib/reindex.py's in-process reindex seam.

reindex_after_mutation() is the best-effort wrapper the consolidated
mutating verbs call: it dispatches store-vs-legacy to reindex_core, routes
the informational notice to stdout (or stderr for --json purity), and
swallows any reindex error so a hiccup cannot mask a mutation that already
succeeded. reindex_core itself is mocked here -- this exercises the seam's
dispatch/routing/error contract, not the generator.

Run:
    python3 -m unittest tests.test_reindex -v
"""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import reindex  # noqa: E402
import reindex_core  # noqa: E402  (patched per-test; import ensures it is loaded)


class TestReindexAfterMutation(unittest.TestCase):
    def test_store_mode_calls_reindex_store_and_notices_on_stdout(self):
        with mock.patch.object(
            reindex_core, "reindex_store", return_value="Reindexed INDEX.md"
        ) as m:
            out = io.StringIO()
            with redirect_stdout(out):
                reindex.reindex_after_mutation("STORE", "ANCHOR")
        m.assert_called_once_with("STORE", suppress_commit=False)
        assert out.getvalue() == "Reindexed INDEX.md\n"

    def test_legacy_mode_calls_reindex_legacy_dir_with_anchor(self):
        # store_root None -> legacy single-dir path keyed on the anchor.
        with mock.patch.object(
            reindex_core, "reindex_legacy_dir", return_value="Reindexed legacy"
        ) as m:
            out = io.StringIO()
            with redirect_stdout(out):
                reindex.reindex_after_mutation(None, "/some/anchor")
        m.assert_called_once_with("/some/anchor")
        assert out.getvalue() == "Reindexed legacy\n"

    def test_suppress_commit_is_forwarded_to_the_store_reindex(self):
        with mock.patch.object(
            reindex_core, "reindex_store", return_value="msg"
        ) as m:
            with redirect_stdout(io.StringIO()):
                reindex.reindex_after_mutation(
                    "STORE", "ANCHOR", suppress_commit=True
                )
        m.assert_called_once_with("STORE", suppress_commit=True)

    def test_to_stderr_routes_the_notice_off_stdout(self):
        # --json callers pass to_stderr so stdout stays one clean document.
        with mock.patch.object(
            reindex_core, "reindex_store", return_value="notice"
        ):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                reindex.reindex_after_mutation("STORE", "ANCHOR", to_stderr=True)
        assert out.getvalue() == ""
        assert err.getvalue() == "notice\n"

    def test_reindex_error_is_swallowed_and_nothing_is_written(self):
        # A reindex hiccup must not raise (it would mask a mutation that
        # already committed) and must not emit a partial notice.
        with mock.patch.object(
            reindex_core, "reindex_store", side_effect=RuntimeError("boom")
        ):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                reindex.reindex_after_mutation("STORE", "ANCHOR")
        assert out.getvalue() == ""
        assert err.getvalue() == ""

    def test_empty_message_writes_nothing(self):
        # reindex_core returns a falsy message (nothing to report).
        with mock.patch.object(reindex_core, "reindex_store", return_value=""):
            out = io.StringIO()
            with redirect_stdout(out):
                reindex.reindex_after_mutation("STORE", "ANCHOR")
        assert out.getvalue() == ""


if __name__ == "__main__":
    unittest.main()
