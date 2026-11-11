#!/usr/bin/env python3
"""Unit tests for the shared store gate in lib/cli/common.py (IM-253).

`require_store` is the one place the whole read surface decides that a
store is unusable. It exists because an enumeration read's exit 1 is
already spoken for by the grep convention (`output.exit-codes`), so a
store that never resolved has to SAY so -- `issue list` used to exit 1
with zero bytes on both streams, indistinguishable from "matched
nothing".

The per-verb wiring and the legacy-corpus behavior are exercised
end-to-end in tests/verification/issue-list.sh, search-issues.sh,
issue-index.sh and issue-ready.sh; these tests pin the helper's own
decision table and payload.

Run:
    python3 -m unittest tests.test_cli_common_store -v
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import errors  # noqa: E402
from cli import common  # noqa: E402


class RequireStoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def call(self, store_root, **kw):
        """Run require_store, returning (rc, stderr)."""
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = common.require_store(store_root, prog="issue list", **kw)
        return rc, buf.getvalue()


class TestUsableStorePasses(RequireStoreBase):
    def test_existing_store_dir_returns_none(self):
        rc, err = self.call(self.tmp)
        self.assertIsNone(rc)
        self.assertEqual(err, "")

    def test_existing_store_accepts_a_plain_string_path(self):
        # Verbs resolve the root through several helpers; not all of them
        # hand back a Path.
        rc, _ = self.call(str(self.tmp))
        self.assertIsNone(rc)

    def test_existing_legacy_anchor_is_a_usable_corpus(self):
        anchor = self.tmp / ".issues"
        anchor.mkdir()
        rc, err = self.call(None, legacy_anchor=anchor)
        self.assertIsNone(rc)
        self.assertEqual(err, "")


class TestUnusableStoreRefuses(RequireStoreBase):
    def test_unresolved_store_is_a_precondition_failure(self):
        rc, err = self.call(None)
        self.assertEqual(rc, errors.EXIT_PRECONDITION)
        self.assertIn("error=precondition-failed", err)
        self.assertIn("reason=store-unresolvable", err)

    def test_refusal_is_never_silent_on_stderr(self):
        # The bug itself: exit 1 with nothing to read on either stream.
        _, err = self.call(None)
        self.assertTrue(err.strip())

    def test_refusal_names_the_calling_verb(self):
        _, err = self.call(None)
        self.assertTrue(err.startswith("issue list:"), err)

    def test_resolved_but_absent_path_refuses(self):
        rc, _ = self.call(self.tmp / "nope")
        self.assertEqual(rc, errors.EXIT_PRECONDITION)

    def test_resolved_but_absent_path_is_named_in_the_payload(self):
        missing = self.tmp / "nope"
        _, err = self.call(missing)
        self.assertIn("store=%s" % missing, err)

    def test_a_file_is_not_a_store(self):
        f = self.tmp / "a-file"
        f.write_text("", encoding="utf-8")
        rc, _ = self.call(f)
        self.assertEqual(rc, errors.EXIT_PRECONDITION)

    def test_absent_legacy_anchor_is_not_a_corpus(self):
        # find_project_anchor() falls back to cwd/.issues whether or not it
        # exists, which is precisely what made the failure silent.
        rc, _ = self.call(None, legacy_anchor=self.tmp / ".issues")
        self.assertEqual(rc, errors.EXIT_PRECONDITION)

    def test_legacy_anchor_does_not_excuse_an_absent_store_root(self):
        # An explicitly named store that is missing is an error even where
        # a legacy corpus happens to sit beside it.
        anchor = self.tmp / ".issues"
        anchor.mkdir()
        rc, _ = self.call(self.tmp / "nope", legacy_anchor=anchor)
        self.assertEqual(rc, errors.EXIT_PRECONDITION)


class TestJsonPayload(RequireStoreBase):
    def test_json_mode_emits_one_parseable_object(self):
        _, err = self.call(None, json_mode=True)
        obj = json.loads(err)
        self.assertEqual(obj["exit"], 1)
        self.assertEqual(obj["reason"], "store-unresolvable")

    def test_json_mode_object_names_the_tool(self):
        _, err = self.call(None, json_mode=True)
        self.assertEqual(json.loads(err)["tool"], "issue list")


class TestStorePreconditionError(RequireStoreBase):
    def test_unresolved_case_carries_no_store_field(self):
        err = common.store_precondition_error(None)
        self.assertNotIn("store", err.fields)

    def test_absent_case_points_at_the_scaffolder(self):
        err = common.store_precondition_error(self.tmp / "nope")
        self.assertIn("issue admin init-store", err.message)

    def test_both_cases_share_one_reason_so_callers_branch_once(self):
        self.assertEqual(
            common.store_precondition_error(None).fields["reason"],
            common.store_precondition_error(self.tmp / "x").fields["reason"],
        )

    def test_exit_code_is_the_precondition_code(self):
        self.assertEqual(
            common.store_precondition_error(None).exit_code,
            errors.EXIT_PRECONDITION,
        )


if __name__ == "__main__":
    unittest.main()
