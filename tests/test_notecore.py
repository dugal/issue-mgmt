#!/usr/bin/env python3
"""Unit tests for lib/notecore.py (unit-coverage restore).

notecore holds the date/text helpers the consolidated `issue note` verb reuses
(relocated verbatim from the flat `scripts/issue-note` binary when that binary
became a v3.0.0 tombstone stub). The functions are pure -- no filesystem, no
git -- except read_text's stdin branch, which is exercised by patching
`notecore.sys.stdin`.

End-to-end `issue note` CLI behavior lives in
tests/verification/im-071-issue-note.sh and tests/test_issue_note.py.

Run:
    python3 -m unittest tests.test_notecore -v
"""

import datetime
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import notecore  # noqa: E402  -- path set above


class TestResolveDate(unittest.TestCase):
    def test_none_returns_today(self):
        self.assertEqual(
            notecore.resolve_date(None),
            datetime.date.today().isoformat(),
        )

    def test_valid_date_passes_through(self):
        self.assertEqual(notecore.resolve_date("2026-08-01"), "2026-08-01")

    def test_malformed_shape_raises_value_error(self):
        with self.assertRaises(ValueError) as cm:
            notecore.resolve_date("2026/08/01")
        self.assertIn("YYYY-MM-DD", str(cm.exception))

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            notecore.resolve_date("2026-8-1")

    def test_impossible_calendar_day_raises(self):
        # Passes the regex shape but is not a real date.
        with self.assertRaises(ValueError) as cm:
            notecore.resolve_date("2026-02-30")
        self.assertIn("valid calendar date", str(cm.exception))

    def test_impossible_month_raises(self):
        with self.assertRaises(ValueError):
            notecore.resolve_date("2026-13-01")


class TestReadText(unittest.TestCase):
    def test_literal_positional_returned_verbatim(self):
        self.assertEqual(notecore.read_text("hello world"), "hello world")

    def test_dash_reads_stdin_stripping_single_trailing_newline(self):
        with mock.patch.object(notecore.sys, "stdin", io.StringIO("note text\n")):
            self.assertEqual(notecore.read_text("-"), "note text")

    def test_dash_preserves_internal_newlines(self):
        with mock.patch.object(
            notecore.sys, "stdin", io.StringIO("line1\nline2\n")
        ):
            self.assertEqual(notecore.read_text("-"), "line1\nline2")

    def test_dash_without_trailing_newline_returned_as_is(self):
        with mock.patch.object(notecore.sys, "stdin", io.StringIO("no newline")):
            self.assertEqual(notecore.read_text("-"), "no newline")

    def test_dash_strips_only_one_trailing_newline(self):
        with mock.patch.object(notecore.sys, "stdin", io.StringIO("x\n\n")):
            self.assertEqual(notecore.read_text("-"), "x\n")


class TestDefaultSection(unittest.TestCase):
    def test_default_section_is_implementation_notes(self):
        self.assertEqual(notecore.DEFAULT_SECTION, "Implementation Notes")


if __name__ == "__main__":
    unittest.main()
