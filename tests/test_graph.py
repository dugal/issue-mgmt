#!/usr/bin/env python3
"""Unit tests for lib/graph.py -- the frontmatter-native authoring-time
blocks-cycle guard (IM-172, relocated out of the retired bd bridge in
IM-178).

Pure frontmatter: no bd binary, no .beads/ index, no stub. Each test
writes issue files to a temp store and asserts graph.detect_blocks_cycle
raises errors.DependencyCycleError (exit 1, reason=dependency-cycle,
cycle members named) exactly when a `blocks` edge would close a cycle.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import errors  # noqa: E402
import graph  # noqa: E402


def make_issue_text(issue_id, status="open", deps=None):
    dep_block = ""
    if deps is not None:
        if not deps:
            dep_block = "depends_on: []\n"
        else:
            dep_block = "depends_on:\n"
            for to, kind in deps:
                dep_block += "  - to: %s\n    type: %s\n" % (to, kind)
    return (
        "---\n"
        "id: %s\n"
        'title: "Issue %s"\n'
        "type: chore\n"
        "status: %s\n"
        "priority: medium\n"
        "created: 2026-07-01\n"
        "updated: 2026-07-01\n"
        'branch: ""\n'
        "tags: []\n"
        "%s"
        "---\n\n"
        "## Summary\n\nBody of %s.\n"
        % (issue_id, issue_id, status, dep_block, issue_id)
    )


class TestDetectBlocksCycle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cycle-test.")
        self.store = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, issue_id, deps=None):
        prefix = issue_id.rsplit("-", 1)[0]
        (self.store / prefix).mkdir(parents=True, exist_ok=True)
        (self.store / prefix / ("%s.md" % issue_id)).write_text(
            make_issue_text(issue_id, deps=deps), encoding="utf-8"
        )

    def test_no_cycle_returns_none(self):
        self._write("ZZ-001", deps=[("ZZ-002", "blocks")])
        self._write("ZZ-002")
        self.assertIsNone(
            graph.detect_blocks_cycle(self.store, "ZZ-001", ["ZZ-002"])
        )

    def test_direct_two_cycle_raises_naming_path(self):
        # ZZ-002 already blocks ZZ-001; adding ZZ-001 blocks ZZ-002 closes it.
        self._write("ZZ-001")
        self._write("ZZ-002", deps=[("ZZ-001", "blocks")])
        with self.assertRaises(errors.DependencyCycleError) as ctx:
            graph.detect_blocks_cycle(self.store, "ZZ-001", ["ZZ-002"])
        self.assertEqual(
            ctx.exception.fields.get("cycle"), "ZZ-001 -> ZZ-002 -> ZZ-001"
        )
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(
            ctx.exception.fields.get("reason"), "dependency-cycle"
        )

    def test_transitive_three_cycle_raises(self):
        self._write("ZZ-001", deps=[("ZZ-002", "blocks")])
        self._write("ZZ-002", deps=[("ZZ-003", "blocks")])
        self._write("ZZ-003")
        with self.assertRaises(errors.DependencyCycleError) as ctx:
            graph.detect_blocks_cycle(self.store, "ZZ-003", ["ZZ-001"])
        self.assertEqual(
            ctx.exception.fields.get("cycle"),
            "ZZ-003 -> ZZ-001 -> ZZ-002 -> ZZ-003",
        )

    def test_self_loop_raises(self):
        self._write("ZZ-001")
        with self.assertRaises(errors.DependencyCycleError) as ctx:
            graph.detect_blocks_cycle(self.store, "ZZ-001", ["ZZ-001"])
        self.assertEqual(
            ctx.exception.fields.get("cycle"), "ZZ-001 -> ZZ-001"
        )

    def test_non_blocks_backedge_is_not_a_cycle(self):
        # ZZ-001 --discovered-from--> ZZ-002 on disk; adding ZZ-002 blocks
        # ZZ-001 does NOT close a *blocks* cycle -- provenance is not ordering.
        self._write("ZZ-001", deps=[("ZZ-002", "discovered-from")])
        self._write("ZZ-002")
        self.assertIsNone(
            graph.detect_blocks_cycle(self.store, "ZZ-002", ["ZZ-001"])
        )

    def test_unresolved_cross_prefix_target_skipped(self):
        # A target whose file is absent (cross-prefix write-now/resolve-later)
        # cannot be part of a visible cycle -- skipped, not an error.
        self._write("ZZ-001")
        self.assertIsNone(
            graph.detect_blocks_cycle(self.store, "ZZ-001", ["QQ-999"])
        )

    def test_preexisting_foreign_cycle_terminates(self):
        # A blocks cycle among OTHER issues must not hang the DFS (seen-set
        # guards it) and must not be misattributed to issue_id.
        self._write("ZZ-002", deps=[("ZZ-003", "blocks")])
        self._write("ZZ-003", deps=[("ZZ-002", "blocks")])
        self._write("ZZ-001")
        self.assertIsNone(
            graph.detect_blocks_cycle(self.store, "ZZ-001", ["ZZ-002"])
        )


if __name__ == "__main__":
    unittest.main()
