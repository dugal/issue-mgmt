#!/usr/bin/env python3
"""Unit tests for lib/reindex.py (sibling-regenerator resolution).

resolve_reindex() picks the sibling reindex script from a bin directory,
preferring the suffixed dev-tree name (reindex-issues.sh) over the
installed no-suffix name (reindex-issues). run_reindex() invokes it, or
warns on stderr when neither name is present (a broken install, not a
transient reindex hiccup).

The subprocess side is exercised against a tiny executable stub so the
env-var (suppress_commit) and stdout/stderr-routing (to_stderr) contracts
are checked without a real store. End-to-end installed-layout behavior
(mutate-then-INDEX against a synthetic install prefix) lives in
tests/verification/im-029-homebrew.sh Block 5.

Run:
    python3 -m unittest tests.test_reindex -v
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import reindex  # noqa: E402


def _touch_exec(path):
    path.write_text("#!/usr/bin/env bash\n")
    path.chmod(0o755)


class TestResolveReindex(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bin_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_suffixed_dev_name_resolves(self):
        _touch_exec(self.bin_dir / "reindex-issues.sh")
        got = reindex.resolve_reindex(self.bin_dir)
        assert got == self.bin_dir / "reindex-issues.sh"

    def test_installed_no_suffix_name_resolves(self):
        _touch_exec(self.bin_dir / "reindex-issues")
        got = reindex.resolve_reindex(self.bin_dir)
        assert got == self.bin_dir / "reindex-issues"

    def test_suffixed_name_preferred_when_both_present(self):
        _touch_exec(self.bin_dir / "reindex-issues.sh")
        _touch_exec(self.bin_dir / "reindex-issues")
        got = reindex.resolve_reindex(self.bin_dir)
        assert got == self.bin_dir / "reindex-issues.sh"

    def test_neither_name_present_returns_none(self):
        assert reindex.resolve_reindex(self.bin_dir) is None


class TestRunReindex(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bin_dir = Path(self._tmp.name)
        # A stub regenerator that records how it was invoked: it appends
        # its argv (minus argv0) and the suppress-commit env flag to a
        # marker file, and prints to both streams so to_stderr routing is
        # observable.
        self.marker = self.bin_dir / "invoked.log"
        stub = self.bin_dir / "reindex-issues"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "args=$* commit=${{ISSUE_MGMT_NO_AUTOCOMMIT:-unset}}" >> "{self.marker}"\n'
            'echo "stub-stdout"\n'
            'echo "stub-stderr" >&2\n'
        )
        stub.chmod(0o755)
        self.stub = stub

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_sibling_warns_on_stderr_and_returns(self):
        empty = Path(tempfile.mkdtemp())
        try:
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                reindex.run_reindex(empty, "STORE", "ANCHOR")
            assert "reindex-issues regenerator not found" in err.getvalue()
            assert str(empty) in err.getvalue()
        finally:
            os.rmdir(empty)

    def test_store_mode_invokes_regenerator_without_anchor_arg(self):
        # store_root not None -> full store reindex, no positional arg.
        reindex.run_reindex(self.bin_dir, "STORE_ROOT", "ANCHOR")
        logged = self.marker.read_text()
        assert "args= commit=unset" in logged

    def test_legacy_mode_passes_anchor_as_arg(self):
        # store_root None -> legacy single-dir target passed positionally.
        reindex.run_reindex(self.bin_dir, None, "/some/anchor")
        logged = self.marker.read_text()
        assert "args=/some/anchor" in logged

    def test_suppress_commit_sets_env_flag(self):
        reindex.run_reindex(self.bin_dir, "STORE_ROOT", "ANCHOR",
                            suppress_commit=True)
        logged = self.marker.read_text()
        assert "commit=1" in logged

    def test_to_stderr_routes_child_stdout_to_stderr(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            reindex.run_reindex(self.bin_dir, "STORE_ROOT", "ANCHOR",
                                to_stderr=True)
        captured = err.getvalue()
        # Both the child's stdout and stderr are folded onto our stderr so
        # a --json caller's stdout stays a single clean JSON document.
        assert "stub-stdout" in captured
        assert "stub-stderr" in captured

    def test_oserror_from_subprocess_is_swallowed(self):
        # A runtime OSError launching the child must not mask a successful
        # mutation -- run_reindex returns without raising.
        with mock.patch("reindex.subprocess.run", side_effect=OSError("boom")):
            reindex.run_reindex(self.bin_dir, "STORE_ROOT", "ANCHOR")


if __name__ == "__main__":
    unittest.main()
