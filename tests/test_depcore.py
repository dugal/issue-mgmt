#!/usr/bin/env python3
"""Unit tests for lib/depcore.py (unit-coverage restore).

depcore holds the lock-free `list` reader the consolidated `issue dep list`
verb reuses (relocated verbatim from the flat `scripts/issue-dep` binary when
that binary became a v3.0.0 tombstone stub). `cmd_list` resolves the issue
file via editcore.resolve_issue_paths, reads the depends_on block, and prints
it either as `TYPE:TARGET` lines or as JSON. It acquires no lock.

Store resolution is pinned via ISSUE_STORE_ROOT (which wins the resolution
precedence) so these tests do not depend on cwd.

End-to-end `issue dep` CLI behavior lives in tests/verification/im-153-*.sh.

Run:
    python3 -m unittest tests.test_depcore -v
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import depcore  # noqa: E402  -- path set above
import deps  # noqa: E402
import frontmatter  # noqa: E402

_ISSUE = """\
---
id: {id}
title: "{id} title"
type: feature
status: open
priority: medium
created: 2026-08-01
updated: 2026-08-01
---

## Summary

Body.
"""


def _args(issue_id, json_mode=False):
    return types.SimpleNamespace(issue_id=issue_id, json_mode=json_mode)


class CmdListBase(unittest.TestCase):
    def setUp(self):
        self._prev_store = os.environ.pop("ISSUE_STORE_ROOT", None)
        self.store = Path(tempfile.mkdtemp(prefix="im-depcore-"))
        os.environ["ISSUE_STORE_ROOT"] = str(self.store)
        (self.store / "IM").mkdir(parents=True)

    def tearDown(self):
        if self._prev_store is not None:
            os.environ["ISSUE_STORE_ROOT"] = self._prev_store
        else:
            os.environ.pop("ISSUE_STORE_ROOT", None)
        shutil.rmtree(self.store, ignore_errors=True)

    def _write(self, issue_id, edges=None):
        path = self.store / "IM" / f"{issue_id}.md"
        path.write_text(_ISSUE.format(id=issue_id))
        if edges is not None:
            fm_lines, body = frontmatter.read_issue(path)
            deps.set_deps(fm_lines, edges)
            path.write_text(frontmatter.serialize_issue(fm_lines, body))
        return path


class TestCmdListErrors(CmdListBase):
    def test_invalid_id_returns_2(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = depcore.cmd_list(_args("not-an-id"))
        self.assertEqual(rc, 2)
        self.assertIn("issue dep:", buf.getvalue())

    def test_missing_file_returns_1(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = depcore.cmd_list(_args("IM-404"))
        self.assertEqual(rc, 1)
        self.assertIn("not found", buf.getvalue())


class TestCmdListText(CmdListBase):
    def test_no_deps_prints_nothing_returns_0(self):
        self._write("IM-001", edges=[])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = depcore.cmd_list(_args("IM-001"))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_absent_depends_on_key_returns_0(self):
        # No depends_on block at all -> treated as zero edges.
        self._write("IM-002")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = depcore.cmd_list(_args("IM-002"))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_edges_print_type_colon_target(self):
        self._write("IM-003", edges=[
            {"to": "IM-101", "type": "blocks"},
            {"to": "RM-107", "type": "related"},
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = depcore.cmd_list(_args("IM-003"))
        self.assertEqual(rc, 0)
        out = buf.getvalue().splitlines()
        self.assertIn("blocks:IM-101", out)
        self.assertIn("related:RM-107", out)

    def test_bare_string_edge_prints_str(self):
        # A bare `- IM-101` list item (no key: value) is a string edge,
        # exercising the non-dict print branch.
        path = self.store / "IM" / "IM-004.md"
        path.write_text(_ISSUE.format(id="IM-004").replace(
            "## Summary",
            "",  # placeholder, replaced below
        ))
        # Hand-author a bare-string depends_on block.
        text = _ISSUE.format(id="IM-004").replace(
            "updated: 2026-08-01\n",
            "updated: 2026-08-01\ndepends_on:\n  - IM-101\n",
        )
        path.write_text(text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = depcore.cmd_list(_args("IM-004"))
        self.assertEqual(rc, 0)
        self.assertIn("IM-101", buf.getvalue())


class TestCmdListJson(CmdListBase):
    def test_json_shape(self):
        self._write("IM-005", edges=[{"to": "IM-101", "type": "blocks"}])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = depcore.cmd_list(_args("IM-005", json_mode=True))
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["id"], "IM-005")
        self.assertEqual(doc["depends_on"], [{"to": "IM-101", "type": "blocks"}])

    def test_json_empty_deps(self):
        self._write("IM-006", edges=[])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = depcore.cmd_list(_args("IM-006", json_mode=True))
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["depends_on"], [])


if __name__ == "__main__":
    unittest.main()
