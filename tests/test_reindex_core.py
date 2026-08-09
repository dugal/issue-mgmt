#!/usr/bin/env python3
"""Unit tests for the in-process reindex seam.

lib/reindex_core.py is the importable INDEX.md generator extracted from the
embedded Python in scripts/reindex-issues.sh, and lib/reindex.py's
reindex_after_mutation() is the in-process seam the consolidated mutating
verbs call instead of subprocessing a sibling binary (killing the
failure class structurally).

Checked here: the seam updates INDEX.md in-process and honors the commit
contract (suppress_commit True/False), including the legacy single-dir path.

The earlier byte-identity-vs-bash guarantee is retired -- the
flat `reindex-issues.sh` bash generator is now a tombstone stub, so
`reindex_core` is the SOLE INDEX.md generator and there is nothing left to
diff it against. End-to-end INDEX freshness is asserted by the verification
suite (new-issue-store-mode.sh etc.) against the consolidated surface.

Run:
    python3 -m unittest tests.test_reindex_core -v
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import reindex  # noqa: E402
import reindex_core  # noqa: E402

_ISSUE_TMPL = """\
---
id: {id}
title: "{title}"
type: {type}
status: {status}
priority: {priority}
updated: 2026-08-01
---

## Summary

Body for {id}.
"""


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


def _write_issue(store, prefix, num, status, *, itype="feature", prio="medium"):
    d = store / prefix
    d.mkdir(parents=True, exist_ok=True)
    iid = f"{prefix}-{num:03d}"
    (d / f"{iid}.md").write_text(_ISSUE_TMPL.format(
        id=iid, title=f"{iid} title", type=itype, status=status, priority=prio,
    ))


def _make_store():
    store = Path(tempfile.mkdtemp(prefix="im-reindex-core-"))
    _write_issue(store, "AA", 1, "open")
    _write_issue(store, "AA", 2, "in-progress")
    _write_issue(store, "AA", 3, "done")
    _write_issue(store, "BB", 1, "open")
    _write_issue(store, "BB", 2, "cancelled")
    _git(store, "init", "-q")
    _git(store, "config", "user.email", "test@example.invalid")
    _git(store, "config", "user.name", "Test")
    _git(store, "add", "-A")
    _git(store, "commit", "-q", "-m", "seed issues")
    return store


class TestSeamCommitSemantics(unittest.TestCase):
    def setUp(self):
        self.store = _make_store()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.store)], check=False)

    def _head_subject(self):
        r = subprocess.run(
            ["git", "-C", str(self.store), "log", "-1", "--format=%s"],
            check=True, capture_output=True, text=True,
        )
        return r.stdout.strip()

    def _commit_count(self):
        r = subprocess.run(
            ["git", "-C", str(self.store), "rev-list", "--count", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        return int(r.stdout.strip())

    def test_seam_commits_when_not_suppressed(self):
        before = self._commit_count()
        reindex.reindex_after_mutation(self.store, None, suppress_commit=False)
        self.assertEqual(self._head_subject(), "reindex INDEX.md")
        self.assertEqual(self._commit_count(), before + 1)
        # INDEX.md reflects the store: an open AA issue is listed.
        self.assertIn("AA-001", (self.store / "INDEX.md").read_text())

    def test_seam_suppresses_commit_for_outer_mutation(self):
        before = self._commit_count()
        # Flip a status on disk, then reindex under an outer mutation's commit.
        (self.store / "AA" / "AA-001.md").write_text(
            _ISSUE_TMPL.format(
                id="AA-001", title="AA-001 title", type="feature",
                status="blocked", priority="medium",
            )
        )
        reindex.reindex_after_mutation(self.store, None, suppress_commit=True)
        self.assertEqual(self._commit_count(), before, "no commit expected")
        # The regenerated INDEX reflects the new status.
        self.assertIn("AA-001", (self.store / "AA" / "INDEX.md").read_text())


class TestLegacyInProcess(unittest.TestCase):
    def test_legacy_dir_reindex_writes_index(self):
        d = Path(tempfile.mkdtemp(prefix="im-reindex-legacy-"))
        try:
            (d / "IM-001.md").write_text(_ISSUE_TMPL.format(
                id="IM-001", title="legacy one", type="bug",
                status="open", priority="high",
            ))
            reindex_core.reindex_legacy_dir(d)
            idx = (d / "INDEX.md").read_text()
            self.assertIn("# Issues Index", idx)
            self.assertIn("IM-001", idx)
            self.assertIn("## Open", idx)
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)


class TestReindexCoreEdges(unittest.TestCase):
    def test_parse_frontmatter_unreadable_returns_none(self):
        self.assertIsNone(reindex_core.parse_frontmatter(Path("/no/such/file.md")))

    def test_parse_frontmatter_no_frontmatter_returns_none(self):
        d = Path(tempfile.mkdtemp(prefix="im-fm-"))
        try:
            p = d / "x.md"
            p.write_text("no frontmatter here\n")
            self.assertIsNone(reindex_core.parse_frontmatter(p))
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)

    def test_parse_frontmatter_missing_required_field_returns_none(self):
        d = Path(tempfile.mkdtemp(prefix="im-fm-"))
        try:
            p = d / "x.md"
            # Missing 'updated' (a required field).
            p.write_text("---\nid: X-1\ntitle: t\ntype: bug\n"
                         "status: open\npriority: low\n---\n\nbody\n")
            self.assertIsNone(reindex_core.parse_frontmatter(p))
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)

    def test_reindex_store_missing_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            reindex_core.reindex_store(Path("/no/such/store"))

    def test_reindex_legacy_missing_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            reindex_core.reindex_legacy_dir(Path("/no/such/dir"))

    def test_store_skips_index_and_template_and_malformed(self):
        store = Path(tempfile.mkdtemp(prefix="im-skip-"))
        try:
            _write_issue(store, "AA", 1, "open")
            # Files that must be skipped, not indexed:
            (store / "AA" / "INDEX.md").write_text("stale\n")
            (store / "AA" / "AA-TEMPLATE.md").write_text("---\nid: AA-TEMPLATE\n---\n")
            (store / "AA" / "malformed.md").write_text("no frontmatter\n")
            _git(store, "init", "-q")
            _git(store, "config", "user.email", "t@e.invalid")
            _git(store, "config", "user.name", "T")
            _git(store, "add", "-A")
            _git(store, "commit", "-q", "-m", "seed")
            reindex_core.reindex_store(store, suppress_commit=True)
            idx = (store / "AA" / "INDEX.md").read_text()
            self.assertIn("AA-001", idx)
            self.assertNotIn("AA-TEMPLATE", idx)
        finally:
            subprocess.run(["rm", "-rf", str(store)], check=False)

    def test_store_empty_has_no_prefix_dirs_line(self):
        store = Path(tempfile.mkdtemp(prefix="im-empty-"))
        try:
            _git(store, "init", "-q")
            _git(store, "config", "user.email", "t@e.invalid")
            _git(store, "config", "user.name", "T")
            reindex_core.reindex_store(store, suppress_commit=True)
            self.assertIn("_(no prefix directories found)_",
                          (store / "INDEX.md").read_text())
        finally:
            subprocess.run(["rm", "-rf", str(store)], check=False)

    def test_legacy_done_changelog_skip_column(self):
        d = Path(tempfile.mkdtemp(prefix="im-legacy-skip-"))
        try:
            (d / "IM-001.md").write_text(
                "---\nid: IM-001\ntitle: t\ntype: bug\nstatus: done\n"
                "priority: low\nupdated: 2026-08-01\nchangelog: skip\n---\n\nbody\n"
            )
            reindex_core.reindex_legacy_dir(d)
            idx = (d / "INDEX.md").read_text()
            self.assertIn("Changelog", idx)
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)


if __name__ == "__main__":
    unittest.main()
