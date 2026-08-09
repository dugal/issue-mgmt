#!/usr/bin/env python3
"""Unit tests for the `issue note` verb call site (regression guard).

Drives the consolidated `issue` dispatcher's `note` verb in-process so
signature drift between the note verb's `apply_edits()` call site
(lib/cli/note.py) and `editcore.apply_edits()`'s definition raises at
unittest time, not only when the slow end-to-end im-071-issue-note.sh
verification script is run.

The flat `scripts/issue-note` became a tombstone stub, so
the guard now loads the dispatcher (`scripts/issue`) and calls
`main(["note", ...])` -- the same call site a `issue note` invocation
takes. The note logic itself lives in lib/notecore.py + lib/cli/note.py +
lib/editcore.py.

End-to-end CLI behavior (exit codes, --json, lock contention,
auto-reindex, store-mode auto-commit) lives in
tests/verification/im-071-issue-note.sh.

Run:
    python3 -m unittest tests.test_issue_note -v
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_issue_dispatcher():
    path = REPO_ROOT / "scripts" / "issue"
    loader = SourceFileLoader("issue_dispatcher", str(path))
    spec = importlib.util.spec_from_loader("issue_dispatcher", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


issue_cli = _load_issue_dispatcher()


SAMPLE = """---
id: IM-001
title: "Sample"
type: feature
status: open
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: []
changelog:
---

## Summary

Initial summary text.

## Implementation Notes

## Archived Plan Content
"""


class IssueNoteCallSiteTests(unittest.TestCase):
    """Guard against IM-094-class regressions: apply_edits signature drift."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._env_store = os.environ.pop("ISSUE_STORE_ROOT", None)
        self._tmp = tempfile.mkdtemp(prefix="im094-unit.")
        self.proj = Path(self._tmp) / "legacy-project"
        (self.proj / ".issues").mkdir(parents=True)
        (self.proj / ".issues" / ".prefix").write_text("IM\n")
        self.issue = self.proj / ".issues" / "IM-001.md"
        self.issue.write_text(SAMPLE)
        os.chdir(self.proj)

    def tearDown(self):
        os.chdir(self._cwd)
        if self._env_store is not None:
            os.environ["ISSUE_STORE_ROOT"] = self._env_store
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_main_append_succeeds_without_signature_mismatch(self):
        rc = issue_cli.main(["note", "IM-001", "smoke note", "--no-reindex"])
        self.assertEqual(rc, 0)
        body = self.issue.read_text()
        # The exact date prefix is today's; only assert the text part is present
        # so the test is date-agnostic.
        self.assertIn(": smoke note", body)
        notes_block = body.split("## Implementation Notes", 1)[1].split("## Archived Plan Content", 1)[0]
        self.assertIn(": smoke note", notes_block)


if __name__ == "__main__":
    unittest.main()
