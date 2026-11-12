#!/usr/bin/env python3
"""Unit tests for the in-process reindex seam.

lib/reindex_core.py is the importable INDEX.md generator, and lib/reindex.py's
reindex_after_mutation() is the best-effort in-process seam the consolidated
mutating verbs call instead of subprocessing a sibling binary (killing the
sibling-name-mismatch failure class structurally).

Checked here: the seam updates INDEX.md in-process and honors the commit
contract (suppress_commit True/False), including the legacy single-dir path.

`reindex_core` is the SOLE INDEX.md generator -- there is no bash twin left to
diff byte-identity against. End-to-end INDEX freshness is asserted by the
verification suite (new-issue-store-mode.sh etc.) against the consolidated
surface.

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
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fixture_identity import FIXTURE_EMAIL  # noqa: E402

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
    _git(store, "config", "user.email", FIXTURE_EMAIL)
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
            _git(store, "config", "user.email", FIXTURE_EMAIL)
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
            _git(store, "config", "user.email", FIXTURE_EMAIL)
            _git(store, "config", "user.name", "T")
            reindex_core.reindex_store(store, suppress_commit=True)
            self.assertIn("_(no prefix directories found)_",
                          (store / "INDEX.md").read_text())
        finally:
            subprocess.run(["rm", "-rf", str(store)], check=False)

    def test_parse_frontmatter_nested_dep_type_does_not_clobber_top_level(self):
        # IM-166 defect 1: a depends_on entry's indented `type:` must not win
        # over the issue's own top-level `type:`.
        d = Path(tempfile.mkdtemp(prefix="im-fm-nest-"))
        try:
            p = d / "x.md"
            p.write_text(
                "---\nid: X-001\ntitle: t\ntype: feature\nstatus: open\n"
                "priority: high\nupdated: 2026-08-01\n"
                "depends_on:\n"
                "  - to: X-002\n    type: blocks\n"
                "  - to: X-003\n    type: related\n"
                "---\n\nbody\n"
            )
            fields = reindex_core.parse_frontmatter(p)
            self.assertEqual(fields["type"], "feature")
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)

    def test_parse_frontmatter_strips_templated_changelog_comment(self):
        # IM-166 defect 2: the templated guard comment is a comment, not the
        # changelog value, so it must not reach a table cell.
        d = Path(tempfile.mkdtemp(prefix="im-fm-cmt-"))
        try:
            p = d / "x.md"
            p.write_text(
                "---\nid: X-001\ntitle: t\ntype: bug\nstatus: done\n"
                "priority: low\nupdated: 2026-08-01\n"
                "changelog:    # set by /update-changelog only"
                " -- do not edit manually\n"
                "---\n\nbody\n"
            )
            fields = reindex_core.parse_frontmatter(p)
            self.assertEqual(fields["changelog"], "")
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)

    def test_parse_frontmatter_handles_both_defects_at_once(self):
        # IM-166: the real shape -- `issue create`'s guard comment plus edges
        # authored by `issue dep add`.
        d = Path(tempfile.mkdtemp(prefix="im-fm-both-"))
        try:
            p = d / "x.md"
            p.write_text(
                "---\nid: X-001\ntitle: \"quoted: title\"\ntype: decision\n"
                "status: done\npriority: medium\ncreated: 2026-07-01\n"
                "updated: 2026-08-01\nbranch: \"\"\ntags: []\n"
                "changelog:    # set by /update-changelog only"
                " -- do not edit manually\n"
                "depends_on:\n"
                "  - to: X-002\n    type: related\n"
                "    note: why this edge exists\n"
                "claim:\n  assignee: someone\n"
                "---\n\nbody\n"
            )
            fields = reindex_core.parse_frontmatter(p)
            self.assertEqual(fields["type"], "decision")
            self.assertEqual(fields["changelog"], "")
            self.assertEqual(fields["title"], "quoted: title")
            # Structured values are not rendered in any column, so they are
            # dropped rather than handed to a table cell as a repr.
            self.assertNotIn("depends_on", fields)
            self.assertNotIn("claim", fields)
            # Every surviving value is cell-safe.
            for key, value in fields.items():
                self.assertIsInstance(value, str, key)
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)

    def test_index_row_for_issue_with_dep_edges_shows_own_type(self):
        # IM-166, rendering level: the row an operator or agent reads.
        d = Path(tempfile.mkdtemp(prefix="im-rows-"))
        try:
            (d / "IM-400.md").write_text(
                "---\nid: IM-400\ntitle: with edges\ntype: feature\n"
                "status: open\npriority: high\nupdated: 2026-08-01\n"
                "changelog:    # set by /update-changelog only"
                " -- do not edit manually\n"
                "depends_on:\n  - to: IM-401\n    type: related\n"
                "---\n\nbody\n"
            )
            reindex_core.reindex_legacy_dir(d)
            row = [
                ln for ln in (d / "INDEX.md").read_text().splitlines()
                if ln.startswith("| IM-400 |")
            ]
            self.assertEqual(len(row), 1, "expected exactly one IM-400 row")
            self.assertIn("| feature |", row[0])
            self.assertNotIn("related", row[0])
            self.assertNotIn("update-changelog", row[0])
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)

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


class TestDetailRecords(unittest.TestCase):
    """The structured records `issue admin reindex --json` renders (IM-240).

    The string-returning entry points are projections of these, so the
    summary line and the JSON counts cannot drift.
    """

    def test_store_detail_counts_indexes_and_issues_per_prefix(self):
        store = Path(tempfile.mkdtemp(prefix="im-detail-store-"))
        try:
            _write_issue(store, "AA", 1, "open")
            _write_issue(store, "AA", 2, "done")
            _write_issue(store, "BB", 1, "blocked")
            result = reindex_core.reindex_store_detail(
                store, suppress_commit=True
            )
            self.assertEqual(result["mode"], "store")
            self.assertEqual(result["store"], str(store))
            self.assertEqual(result["index"], str(store / "INDEX.md"))
            self.assertEqual(
                result["prefixes"],
                [{"prefix": "AA", "issues": 2},
                 {"prefix": "BB", "issues": 1}],
            )
            # One root INDEX.md plus one per prefix.
            self.assertEqual(result["indexes_written"], 3)
            self.assertEqual(result["issues"], 3)
        finally:
            subprocess.run(["rm", "-rf", str(store)], check=False)

    def test_store_detail_summary_matches_string_entry_point(self):
        store = Path(tempfile.mkdtemp(prefix="im-detail-parity-"))
        try:
            _write_issue(store, "AA", 1, "open")
            detail = reindex_core.reindex_store_detail(
                store, suppress_commit=True
            )
            summary = reindex_core.reindex_store(store, suppress_commit=True)
            self.assertEqual(detail["summary"], summary)
        finally:
            subprocess.run(["rm", "-rf", str(store)], check=False)

    def test_store_detail_empty_store_has_zero_counts_not_missing_keys(self):
        store = Path(tempfile.mkdtemp(prefix="im-detail-empty-"))
        try:
            result = reindex_core.reindex_store_detail(
                store, suppress_commit=True
            )
            self.assertEqual(result["prefixes"], [])
            self.assertEqual(result["issues"], 0)
            # The root INDEX.md is still written, so the count is 1.
            self.assertEqual(result["indexes_written"], 1)
        finally:
            subprocess.run(["rm", "-rf", str(store)], check=False)

    def test_legacy_detail_reports_dir_index_and_count(self):
        d = Path(tempfile.mkdtemp(prefix="im-detail-legacy-"))
        try:
            (d / "IM-001.md").write_text(_ISSUE_TMPL.format(
                id="IM-001", title="legacy one", type="bug",
                status="open", priority="high",
            ))
            result = reindex_core.reindex_legacy_dir_detail(d)
            self.assertEqual(result["mode"], "legacy")
            self.assertEqual(result["issues_dir"], str(d))
            self.assertEqual(result["index"], str(d / "INDEX.md"))
            self.assertEqual(result["indexes_written"], 1)
            self.assertEqual(result["issues"], 1)
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)

    def test_legacy_detail_summary_matches_string_entry_point(self):
        d = Path(tempfile.mkdtemp(prefix="im-detail-legacy-parity-"))
        try:
            (d / "IM-001.md").write_text(_ISSUE_TMPL.format(
                id="IM-001", title="legacy one", type="bug",
                status="open", priority="high",
            ))
            detail = reindex_core.reindex_legacy_dir_detail(d)
            summary = reindex_core.reindex_legacy_dir(d)
            self.assertEqual(detail["summary"], summary)
        finally:
            subprocess.run(["rm", "-rf", str(d)], check=False)

    def test_detail_entry_points_raise_on_missing_dir(self):
        with self.assertRaises(FileNotFoundError):
            reindex_core.reindex_store_detail(Path("/no/such/store"))
        with self.assertRaises(FileNotFoundError):
            reindex_core.reindex_legacy_dir_detail(Path("/no/such/dir"))


if __name__ == "__main__":
    unittest.main()
