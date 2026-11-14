#!/usr/bin/env python3
"""Unit tests for the reap-path index regeneration (IM-271).

A reap is a mutation -- it stamps `updated:` and commits `reap <id>` -- so
every reclaimed row in INDEX.md is stale until the index is regenerated
(`cli.auto-reindex`). `_reap.reindex_after_reap` is that regeneration,
shared by the manual (`issue admin reap`) and opportunistic (`issue ready`)
paths so the two cannot drift.

These tests pin the helper's own contract: whole-store dispatch, the
ISSUE_MGMT_NO_AUTOCOMMIT passthrough, and stream routing. That it is called
ONCE per pass (never once per reclaimed issue), that `--no-reindex`
suppresses it, and that a no-op pass writes nothing are asserted end-to-end
in tests/verification/im-098-lease.sh section [R].

Run:
    python3 -m unittest tests.test_reap_reindex -v
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import reindex_core  # noqa: E402  (patched per-test)
from cli import _reap  # noqa: E402


class TestReindexAfterReap(unittest.TestCase):
    def setUp(self):
        # The helper reads the kill switch from the environment; pin it off
        # so a caller's shell cannot decide the outcome of these tests.
        self._prior = os.environ.pop("ISSUE_MGMT_NO_AUTOCOMMIT", None)
        if self._prior is not None:
            self.addCleanup(
                os.environ.__setitem__,
                "ISSUE_MGMT_NO_AUTOCOMMIT", self._prior,
            )

    def test_regenerates_the_whole_store_and_notices_on_stdout(self):
        with mock.patch.object(
            reindex_core, "reindex_store", return_value="Reindexed INDEX.md"
        ) as m:
            out = io.StringIO()
            with redirect_stdout(out):
                _reap.reindex_after_reap("STORE")
        m.assert_called_once_with("STORE", suppress_commit=False)
        self.assertEqual(out.getvalue(), "Reindexed INDEX.md\n")

    def test_to_stderr_routes_the_notice_off_stdout(self):
        # `--json` stdout carries exactly one document (output.json-stable),
        # and `ready` is a read verb whose stdout is the ready set.
        with mock.patch.object(
            reindex_core, "reindex_store", return_value="Reindexed INDEX.md"
        ):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                _reap.reindex_after_reap("STORE", to_stderr=True)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "Reindexed INDEX.md\n")

    def test_no_autocommit_env_suppresses_the_commit(self):
        # An outer mutation owns the commit, so the index is written bare --
        # the same passthrough every other mutating verb implements.
        os.environ["ISSUE_MGMT_NO_AUTOCOMMIT"] = "1"
        self.addCleanup(os.environ.pop, "ISSUE_MGMT_NO_AUTOCOMMIT", None)
        with mock.patch.object(
            reindex_core, "reindex_store", return_value="msg"
        ) as m:
            with redirect_stdout(io.StringIO()):
                _reap.reindex_after_reap("STORE")
        m.assert_called_once_with("STORE", suppress_commit=True)

    def test_a_reindex_failure_never_reaches_the_caller(self):
        # Best-effort: the reap already committed, so an index hiccup must
        # not turn a successful reclaim into a failure -- and on the
        # opportunistic path it must not break the start/ready operation.
        with mock.patch.object(
            reindex_core, "reindex_store", side_effect=OSError("disk full")
        ):
            with redirect_stdout(io.StringIO()):
                _reap.reindex_after_reap("STORE")  # must not raise


if __name__ == "__main__":
    unittest.main()
