#!/usr/bin/env python3
"""Unit tests for lib/editcore.py (unit-coverage restore).

editcore is the reusable edit-issue data path -- relocated verbatim from the
flat `scripts/edit-issue` binary when that binary became a v3.0.0 tombstone
stub. The consolidated `issue edit` / `issue note` / `issue dep` verbs import
it directly. It carries:

  - argv interpretation (interpret_section_actions) and the
    escaping-header refusal (_check_body_nests)
  - the lock+snapshot+atomic-write+commit lifecycle (apply_edits),
    both the store-mode autocommit path and the legacy no-commit path
  - the --if-unchanged-since guard (_check_section_unchanged_since)
  - the --claim-generation fencing (_check_generation_guard)
  - the dep-edge engine (_apply_dep_actions) and the authoring
    cycle guard (delegated to graph.detect_blocks_cycle)
  - path resolution (find_project_anchor / resolve_store_root /
    resolve_issue_paths) and body-input reading (_read_op_file)

These tests exercise the module's functions directly with tempfile / git
fixtures, matching the pattern in tests/test_transition.py and
tests/test_reindex_core.py. End-to-end CLI behavior (exit codes, --json,
lock contention) lives in tests/verification/*.sh.

Run:
    python3 -m unittest tests.test_editcore -v
"""

import argparse
import datetime
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fixture_identity import FIXTURE_EMAIL  # noqa: E402

import concurrency  # noqa: E402
import deps  # noqa: E402
import editcore  # noqa: E402
import errors  # noqa: E402
import frontmatter  # noqa: E402
import lease  # noqa: E402


SAMPLE = """\
---
id: IM-999
title: "Sample"
type: feature
status: open
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: [a]
---

## Summary

Body of summary.

## Implementation Notes

## Archived Plan Content
"""


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


# ----------------------------------------------------------------------
# interpret_section_actions -- the argv folder
# ----------------------------------------------------------------------


class TestInterpretSectionActions(unittest.TestCase):
    def _interp(self, flags):
        return editcore.interpret_section_actions(flags)

    def test_section_append(self):
        actions, dep_actions = self._interp(
            [("--section", "Notes"), ("--append", "hi")]
        )
        self.assertEqual(actions, [("edit", "Notes", "append", "hi")])
        self.assertEqual(dep_actions, [])

    def test_section_prepend_and_replace(self):
        actions, _ = self._interp(
            [("--section", "A"), ("--prepend", "x")]
        )
        self.assertEqual(actions, [("edit", "A", "prepend", "x")])
        actions, _ = self._interp(
            [("--section", "B"), ("--replace", "y")]
        )
        self.assertEqual(actions, [("edit", "B", "replace", "y")])

    def test_section_without_op_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--section", "Notes")])
        self.assertIn("requires one of --append/--prepend/--replace",
                      str(cm.exception))

    def test_body_op_without_anchor_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--append", "hi")])
        self.assertIn("requires a preceding --section", str(cm.exception))

    def test_two_body_ops_on_one_section_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([
                ("--section", "A"), ("--append", "x"), ("--replace", "y"),
            ])
        self.assertIn("already has a body op", str(cm.exception))

    def test_escaping_header_in_body_refused(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--section", "A"), ("--append", "## Escaped\n")])
        msg = str(cm.exception)
        self.assertIn("escape", msg)
        self.assertIn("demote", msg)

    def test_nested_h3_header_allowed(self):
        actions, _ = self._interp(
            [("--section", "A"), ("--append", "### fine\n")]
        )
        self.assertEqual(actions, [("edit", "A", "append", "### fine\n")])

    def test_add_section_with_body_default_position(self):
        actions, _ = self._interp(
            [("--add-section", "New"), ("--body", "text")]
        )
        self.assertEqual(actions, [("add", "New", "text", {})])

    def test_add_section_after(self):
        actions, _ = self._interp(
            [("--add-section", "New"), ("--after", "Summary")]
        )
        self.assertEqual(actions, [("add", "New", "", {"after": "Summary"})])

    def test_add_section_before_and_to_position(self):
        actions, _ = self._interp(
            [("--add-section", "N"), ("--before", "Summary")]
        )
        self.assertEqual(actions, [("add", "N", "", {"before": "Summary"})])
        actions, _ = self._interp(
            [("--add-section", "N"), ("--to-position", 2)]
        )
        self.assertEqual(actions, [("add", "N", "", {"to_position": 2})])

    def test_add_section_at_end_flag_records_empty_position(self):
        actions, _ = self._interp(
            [("--add-section", "N"), ("--body", "b"), ("--at-end", [])]
        )
        self.assertEqual(actions, [("add", "N", "b", {})])

    def test_two_positions_on_one_anchor_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([
                ("--add-section", "N"), ("--after", "A"), ("--before", "B"),
            ])
        self.assertIn("position already set", str(cm.exception))

    def test_at_end_then_after_raises_position_conflict(self):
        with self.assertRaises(ValueError):
            self._interp([
                ("--add-section", "N"), ("--at-end", []), ("--after", "A"),
            ])

    def test_body_without_add_section_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--body", "x")])
        self.assertIn("requires a preceding --add-section", str(cm.exception))

    def test_duplicate_body_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([
                ("--add-section", "N"), ("--body", "x"), ("--body", "y"),
            ])
        self.assertIn("already has --body", str(cm.exception))

    def test_rename_section(self):
        actions, _ = self._interp([("--rename-section", ("Old", "New"))])
        self.assertEqual(actions, [("rename", "Old", "New")])

    def test_remove_section(self):
        actions, _ = self._interp([("--remove-section", "Gone")])
        self.assertEqual(actions, [("remove", "Gone")])

    def test_move_section_with_position(self):
        actions, _ = self._interp(
            [("--move-section", "M"), ("--after", "Summary")]
        )
        self.assertEqual(actions, [("move", "M", {"after": "Summary"})])

    def test_move_section_without_position_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--move-section", "M")])
        self.assertIn("requires one of --after/--before/--to-position",
                      str(cm.exception))

    def test_add_dep(self):
        _, dep_actions = self._interp([("--add-dep", "blocks:IM-101")])
        self.assertEqual(dep_actions, [("add-dep", "IM-101", "blocks", None)])

    def test_add_dep_with_note(self):
        _, dep_actions = self._interp([
            ("--add-dep", "parent-child:IM-101"), ("--dep-note", "spun out"),
        ])
        self.assertEqual(
            dep_actions,
            [("add-dep", "IM-101", "parent-child", "spun out")],
        )

    def test_add_dep_malformed_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--add-dep", "IM-101")])  # no KIND:
        self.assertIn("KIND:TARGET", str(cm.exception))

    def test_add_dep_bad_kind_raises(self):
        with self.assertRaises(ValueError):
            self._interp([("--add-dep", "bogus:IM-101")])

    def test_add_dep_lowercase_target_accepted(self):
        # A lowercase target is a valid loose form; it is accepted
        # at parse time and canonicalized (to IM-101) at write time.
        _, dep_actions = self._interp([("--add-dep", "blocks:im-101")])
        self.assertEqual(dep_actions, [("add-dep", "im-101", "blocks", None)])

    def test_add_dep_malformed_target_raises(self):
        with self.assertRaises(ValueError):
            self._interp([("--add-dep", "blocks:not_an_id")])

    def test_remove_dep_default_kind_none(self):
        _, dep_actions = self._interp([("--remove-dep", "IM-101")])
        self.assertEqual(dep_actions, [("remove-dep", "IM-101", None)])

    def test_remove_dep_with_type(self):
        _, dep_actions = self._interp([
            ("--remove-dep", "IM-101"), ("--dep-type", "blocks"),
        ])
        self.assertEqual(dep_actions, [("remove-dep", "IM-101", "blocks")])

    def test_remove_dep_bad_type_raises(self):
        with self.assertRaises(ValueError):
            self._interp([
                ("--remove-dep", "IM-101"), ("--dep-type", "bogus"),
            ])

    def test_dep_type_without_remove_dep_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--dep-type", "blocks")])
        self.assertIn("requires a preceding --remove-dep", str(cm.exception))

    def test_dep_type_twice_raises(self):
        # IM-241: --dep-type is the retired spelling of --kind. It still
        # parses, and the diagnostic names the CURRENT form so a caller is
        # never told to retry with a retired flag (surface.self-naming).
        with self.assertRaises(ValueError) as cm:
            self._interp([
                ("--remove-dep", "IM-101"),
                ("--dep-type", "blocks"), ("--dep-type", "related"),
            ])
        self.assertIn("--kind already set", str(cm.exception))

    def test_kind_twice_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([
                ("--remove-dep", "IM-101"),
                ("--kind", "blocks"), ("--kind", "related"),
            ])
        self.assertIn("--kind already set", str(cm.exception))

    def test_kind_and_retired_dep_type_are_one_concept(self):
        # Mixing the two spellings for one anchor is the same collision,
        # not two independent qualifiers.
        with self.assertRaises(ValueError) as cm:
            self._interp([
                ("--remove-dep", "IM-101"),
                ("--kind", "blocks"), ("--dep-type", "related"),
            ])
        self.assertIn("--kind already set", str(cm.exception))

    def test_dep_note_without_add_dep_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--dep-note", "n")])
        self.assertIn("requires a preceding --add-dep", str(cm.exception))

    def test_dep_note_twice_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([
                ("--add-dep", "blocks:IM-101"),
                ("--dep-note", "a"), ("--dep-note", "b"),
            ])
        self.assertIn("--dep-note already set", str(cm.exception))

    def test_unknown_flag_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._interp([("--bogus", "x")])
        self.assertIn("unknown ordered flag", str(cm.exception))

    def test_multiple_anchors_compose_in_order(self):
        actions, dep_actions = self._interp([
            ("--section", "A"), ("--append", "x"),
            ("--remove-section", "B"),
            ("--add-dep", "blocks:IM-101"),
        ])
        self.assertEqual(
            actions,
            [("edit", "A", "append", "x"), ("remove", "B")],
        )
        self.assertEqual(dep_actions, [("add-dep", "IM-101", "blocks", None)])

    def test_append_file_reads_file(self):
        d = Path(tempfile.mkdtemp(prefix="im-opfile-"))
        try:
            p = d / "body.md"
            p.write_text("from file\n")
            actions, _ = self._interp(
                [("--section", "A"), ("--append-file", str(p))]
            )
            self.assertEqual(actions, [("edit", "A", "append", "from file\n")])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_append_file_missing_raises_operand_input_error(self):
        with self.assertRaises(editcore.OperandInputError):
            self._interp([("--section", "A"), ("--append-file", "/no/such")])

    def test_stdin_consumed_twice_raises(self):
        fake_stdin = mock.MagicMock()
        fake_stdin.buffer.read.return_value = b"data"
        with mock.patch.object(editcore.sys, "stdin", fake_stdin):
            with self.assertRaises(ValueError) as cm:
                self._interp([
                    ("--section", "A"), ("--append-file", "-"),
                    ("--section", "B"), ("--append-file", "-"),
                ])
        self.assertIn("stdin already consumed", str(cm.exception))

    def test_body_file_after_body_op_raises(self):
        d = Path(tempfile.mkdtemp(prefix="im-opfile2-"))
        try:
            p = d / "b.md"
            p.write_text("x\n")
            with self.assertRaises(ValueError) as cm:
                self._interp([
                    ("--section", "A"), ("--append", "text"),
                    ("--append-file", str(p)),
                ])
            self.assertIn("already has a body op", str(cm.exception))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestOrderedFlagAction(unittest.TestCase):
    """The argparse Action that records flag occurrences in argv order."""

    def test_records_flags_in_argv_order(self):
        p = argparse.ArgumentParser()
        p.add_argument("--section", action=editcore._OrderedFlagAction)
        p.add_argument("--append", action=editcore._OrderedFlagAction)
        ns = p.parse_args(["--section", "A", "--append", "x"])
        self.assertEqual(
            ns.ordered_section_flags,
            [("--section", "A"), ("--append", "x")],
        )

    def test_nargs_zero_records_empty_values(self):
        p = argparse.ArgumentParser()
        p.add_argument("--at-end", nargs=0, action=editcore._OrderedFlagAction)
        ns = p.parse_args(["--at-end"])
        self.assertEqual(ns.ordered_section_flags, [("--at-end", [])])


# ----------------------------------------------------------------------
# parse_set_args
# ----------------------------------------------------------------------


class TestParseSetArgs(unittest.TestCase):
    def test_valid_specs(self):
        self.assertEqual(
            editcore.parse_set_args(["priority=high", "branch=b"]),
            [("priority", "high"), ("branch", "b")],
        )

    def test_value_may_contain_equals(self):
        self.assertEqual(
            editcore.parse_set_args(["title=a=b"]),
            [("title", "a=b")],
        )

    def test_missing_equals_raises(self):
        with self.assertRaises(ValueError) as cm:
            editcore.parse_set_args(["priority"])
        self.assertIn("FIELD=VALUE", str(cm.exception))

    def test_empty_field_raises(self):
        with self.assertRaises(ValueError) as cm:
            editcore.parse_set_args(["=value"])
        self.assertIn("empty field name", str(cm.exception))


# ----------------------------------------------------------------------
# _read_op_file
# ----------------------------------------------------------------------


class TestReadOpFile(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="im-readop-"))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_empty_path_raises(self):
        with self.assertRaises(editcore.OperandInputError) as cm:
            editcore._read_op_file("")
        self.assertIn("expected a path or '-'", str(cm.exception))

    def test_reads_file_verbatim(self):
        p = self.d / "b.md"
        p.write_text("hello\n")
        self.assertEqual(editcore._read_op_file(str(p)), "hello\n")

    def test_empty_file_yields_empty_string(self):
        p = self.d / "empty.md"
        p.write_text("")
        self.assertEqual(editcore._read_op_file(str(p)), "")

    def test_missing_file_raises(self):
        with self.assertRaises(editcore.OperandInputError) as cm:
            editcore._read_op_file(str(self.d / "nope.md"))
        self.assertIn("file not found", str(cm.exception))

    def test_directory_raises(self):
        with self.assertRaises(editcore.OperandInputError) as cm:
            editcore._read_op_file(str(self.d))
        self.assertIn("is a directory", str(cm.exception))

    def test_non_utf8_raises(self):
        p = self.d / "bin.md"
        p.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(editcore.OperandInputError) as cm:
            editcore._read_op_file(str(p))
        self.assertIn("not valid utf-8", str(cm.exception))

    def test_dash_reads_stdin(self):
        fake_stdin = mock.MagicMock()
        fake_stdin.buffer.read.return_value = b"from stdin\n"
        with mock.patch.object(editcore.sys, "stdin", fake_stdin):
            self.assertEqual(editcore._read_op_file("-"), "from stdin\n")


# ----------------------------------------------------------------------
# Path resolution
# ----------------------------------------------------------------------


class TestPathResolution(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._prev_store = os.environ.pop("ISSUE_STORE_ROOT", None)
        self._prev_root = os.environ.pop("ISSUE_MGMT_ROOT", None)
        self.tmp = Path(tempfile.mkdtemp(prefix="im-paths-"))

    def tearDown(self):
        os.chdir(self._cwd)
        for k, v in (("ISSUE_STORE_ROOT", self._prev_store),
                     ("ISSUE_MGMT_ROOT", self._prev_root)):
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_find_project_anchor_walks_up_to_issues_dir(self):
        proj = self.tmp / "proj"
        (proj / ".issues").mkdir(parents=True)
        sub = proj / "src" / "deep"
        sub.mkdir(parents=True)
        os.chdir(sub)
        self.assertEqual(
            editcore.find_project_anchor().resolve(),
            (proj / ".issues").resolve(),
        )

    def test_find_project_anchor_uses_issue_mgmt_root_env(self):
        # No .issues anywhere up the /var/folders tempdir chain.
        bare = self.tmp / "bare"
        bare.mkdir()
        os.chdir(bare)
        os.environ["ISSUE_MGMT_ROOT"] = str(self.tmp / "ws")
        self.assertEqual(
            editcore.find_project_anchor(), self.tmp / "ws" / ".issues"
        )

    def test_find_project_anchor_final_fallback_is_cwd_issues(self):
        bare = self.tmp / "bare2"
        bare.mkdir()
        os.chdir(bare)
        # No .issues in tree, no ISSUE_MGMT_ROOT -> cwd/.issues.
        self.assertEqual(
            editcore.find_project_anchor().resolve(),
            (bare / ".issues").resolve(),
        )

    def test_resolve_store_root_env_wins(self):
        os.environ["ISSUE_STORE_ROOT"] = "/some/store"
        anchor = self.tmp / ".issues"
        anchor.mkdir()
        self.assertEqual(
            editcore.resolve_store_root(anchor), Path("/some/store")
        )

    def test_resolve_store_root_sentinel(self):
        anchor = self.tmp / ".issues"
        anchor.mkdir()
        (anchor / ".migrated-to-store").write_text("/path/to/store\n")
        self.assertEqual(
            editcore.resolve_store_root(anchor), Path("/path/to/store")
        )

    def test_resolve_store_root_none_when_legacy(self):
        anchor = self.tmp / ".issues"
        anchor.mkdir()
        self.assertIsNone(editcore.resolve_store_root(anchor))

    def test_resolve_issue_paths_invalid_id_raises(self):
        with self.assertRaises(ValueError):
            editcore.resolve_issue_paths("not-an-id")

    def test_resolve_issue_paths_store_mode(self):
        os.environ["ISSUE_STORE_ROOT"] = str(self.tmp / "store")
        issue_file, lock_path, store_root, _anchor = \
            editcore.resolve_issue_paths("IM-042")
        self.assertEqual(issue_file, self.tmp / "store" / "IM" / "IM-042.md")
        self.assertEqual(
            lock_path, self.tmp / "store" / "IM" / ".locks" / "IM-042.lock"
        )
        self.assertEqual(store_root, self.tmp / "store")

    def test_resolve_issue_paths_legacy_mode(self):
        proj = self.tmp / "legacy"
        (proj / ".issues").mkdir(parents=True)
        os.chdir(proj)
        issue_file, lock_path, store_root, anchor = \
            editcore.resolve_issue_paths("IM-042")
        self.assertIsNone(store_root)
        self.assertEqual(issue_file, anchor / "IM-042.md")
        self.assertEqual(lock_path, anchor / ".locks" / "IM-042.lock")


# ----------------------------------------------------------------------
# _section_body
# ----------------------------------------------------------------------


class TestSectionBody(unittest.TestCase):
    BODY = (
        "## Summary\n\nSummary text.\n\n"
        "## Implementation Notes\n\nNote line.\n"
    )

    def test_absent_section_returns_none(self):
        self.assertIsNone(editcore._section_body(self.BODY, "Nope"))

    def test_present_section_returns_body(self):
        got = editcore._section_body(self.BODY, "Implementation Notes")
        self.assertIn("Note line.", got)
        self.assertNotIn("Summary text.", got)


# ----------------------------------------------------------------------
# Action summaries and single-action dispatch
# ----------------------------------------------------------------------


class TestSummaries(unittest.TestCase):
    def test_summarize_edit(self):
        self.assertEqual(
            editcore._summarize_section_action(("edit", "A", "append", "x")),
            {"kind": "edit", "section": "A", "op": "append"},
        )

    def test_summarize_add_with_position(self):
        self.assertEqual(
            editcore._summarize_section_action(
                ("add", "A", "b", {"after": "S"})
            ),
            {"kind": "add", "section": "A", "position": {"after": "S"}},
        )

    def test_summarize_add_default_position(self):
        self.assertEqual(
            editcore._summarize_section_action(("add", "A", "b", {})),
            {"kind": "add", "section": "A", "position": {"at_end": True}},
        )

    def test_summarize_rename_remove_move(self):
        self.assertEqual(
            editcore._summarize_section_action(("rename", "O", "N")),
            {"kind": "rename", "old": "O", "new": "N"},
        )
        self.assertEqual(
            editcore._summarize_section_action(("remove", "A")),
            {"kind": "remove", "section": "A"},
        )
        self.assertEqual(
            editcore._summarize_section_action(("move", "A", {"before": "S"})),
            {"kind": "move", "section": "A", "position": {"before": "S"}},
        )

    def test_summarize_unknown_section_action_raises(self):
        with self.assertRaises(ValueError):
            editcore._summarize_section_action(("bogus", "x"))

    def test_summarize_dep_add_and_remove(self):
        self.assertEqual(
            editcore._summarize_dep_action(("add-dep", "IM-1", "blocks", None)),
            {"kind": "add-dep", "to": "IM-1", "type": "blocks"},
        )
        self.assertEqual(
            editcore._summarize_dep_action(("remove-dep", "IM-1", "blocks")),
            {"kind": "remove-dep", "to": "IM-1", "type": "blocks"},
        )

    def test_summarize_unknown_dep_action_raises(self):
        with self.assertRaises(ValueError):
            editcore._summarize_dep_action(("bogus", "x"))


class TestApplyOneSectionAction(unittest.TestCase):
    BODY = (
        "## Summary\n\nSummary text.\n\n"
        "## Implementation Notes\n\nNote line.\n"
    )

    def test_edit_append(self):
        out = editcore._apply_one_section_action(
            self.BODY, ("edit", "Implementation Notes", "append", "added\n")
        )
        self.assertIn("added", out)

    def test_add(self):
        out = editcore._apply_one_section_action(
            self.BODY, ("add", "New", "content\n", {})
        )
        self.assertIn("## New", out)
        self.assertIn("content", out)

    def test_rename(self):
        out = editcore._apply_one_section_action(
            self.BODY, ("rename", "Summary", "Overview")
        )
        self.assertIn("## Overview", out)
        self.assertNotIn("## Summary", out)

    def test_remove(self):
        out = editcore._apply_one_section_action(
            self.BODY, ("remove", "Implementation Notes")
        )
        self.assertNotIn("## Implementation Notes", out)

    def test_move(self):
        out = editcore._apply_one_section_action(
            self.BODY, ("move", "Implementation Notes", {"before": "Summary"})
        )
        self.assertLess(
            out.index("## Implementation Notes"), out.index("## Summary")
        )

    def test_unknown_action_raises(self):
        with self.assertRaises(ValueError):
            editcore._apply_one_section_action(self.BODY, ("bogus", "x"))


# ----------------------------------------------------------------------
# _apply_dep_actions
# ----------------------------------------------------------------------


class TestApplyDepActions(unittest.TestCase):
    def _fm(self):
        fm_lines, _ = frontmatter.parse_issue(SAMPLE)
        return fm_lines

    def test_add_dep_appends_edge(self):
        fm_lines = self._fm()
        summaries = editcore._apply_dep_actions(
            fm_lines, [("add-dep", "IM-101", "blocks", None)]
        )
        self.assertEqual(
            deps.get_deps(fm_lines), [{"to": "IM-101", "type": "blocks"}]
        )
        self.assertEqual(summaries[0]["kind"], "add-dep")

    def test_add_dep_with_note(self):
        fm_lines = self._fm()
        editcore._apply_dep_actions(
            fm_lines, [("add-dep", "IM-101", "blocks", "seen")]
        )
        edge = deps.get_deps(fm_lines)[0]
        self.assertEqual(edge["note"], "seen")

    def test_add_dep_dedup_is_noop(self):
        fm_lines = self._fm()
        editcore._apply_dep_actions(
            fm_lines, [("add-dep", "IM-101", "blocks", None)]
        )
        editcore._apply_dep_actions(
            fm_lines, [("add-dep", "IM-101", "blocks", None)]
        )
        self.assertEqual(len(deps.get_deps(fm_lines)), 1)

    def test_remove_dep(self):
        fm_lines = self._fm()
        editcore._apply_dep_actions(
            fm_lines, [("add-dep", "IM-101", "blocks", None)]
        )
        editcore._apply_dep_actions(
            fm_lines, [("remove-dep", "IM-101", None)]
        )
        self.assertEqual(deps.get_deps(fm_lines), [])


# ----------------------------------------------------------------------
# _check_generation_guard (fencing)
# ----------------------------------------------------------------------


class TestGenerationGuard(unittest.TestCase):
    def _claimed_fm(self, generation, *, expired=False):
        fm_lines, _ = frontmatter.parse_issue(SAMPLE)
        now = lease.now_utc()
        if expired:
            until = "2020-01-01T00:00:00Z"
        else:
            until = lease.format_ts(now + datetime.timedelta(hours=1))
        lease.set_claim(
            fm_lines, "carol", lease.format_ts(now), until, generation
        )
        return fm_lines

    def test_none_generation_is_noop(self):
        fm_lines, _ = frontmatter.parse_issue(SAMPLE)
        self.assertIsNone(
            editcore._check_generation_guard(fm_lines, None)
        )

    def test_matching_generation_active_lease_passes(self):
        fm_lines = self._claimed_fm(2)
        self.assertIsNone(
            editcore._check_generation_guard(fm_lines, 2)
        )

    def test_mismatched_generation_raises_stale_view(self):
        fm_lines = self._claimed_fm(2)
        with self.assertRaises(errors.StaleViewError) as cm:
            editcore._check_generation_guard(fm_lines, 1)
        self.assertEqual(cm.exception.current_generation, 2)
        self.assertEqual(cm.exception.caller_generation, 1)

    def test_matching_generation_expired_lease_raises_lease_expired(self):
        fm_lines = self._claimed_fm(2, expired=True)
        with self.assertRaises(errors.LeaseExpiredError) as cm:
            editcore._check_generation_guard(fm_lines, 2)
        self.assertEqual(cm.exception.actor, "carol")

    def test_generation_zero_never_claimed_passes(self):
        fm_lines, _ = frontmatter.parse_issue(SAMPLE)
        self.assertIsNone(
            editcore._check_generation_guard(fm_lines, 0)
        )


# ----------------------------------------------------------------------
# apply_edits -- legacy (no-commit) path
# ----------------------------------------------------------------------


class TestApplyEditsLegacy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".locks").mkdir()
        self.issue = self.root / "IM-999.md"
        self.lock = self.root / ".locks" / "IM-999.lock"
        self.issue.write_text(SAMPLE, encoding="utf-8")
        self.today = datetime.date.today().isoformat()

    def _read(self):
        return self.issue.read_text(encoding="utf-8")

    def test_set_field_updates_and_stamps(self):
        summary = editcore.apply_edits(
            self.issue, self.lock, [("priority", "low")], [], 2.0
        )
        text = self._read()
        self.assertIn("priority: low\n", text)
        self.assertIn(f"updated: {self.today}\n", text)
        self.assertEqual(summary["field_changes"][0]["new"], "low")

    def test_section_append(self):
        editcore.apply_edits(
            self.issue, self.lock, [],
            [("edit", "Implementation Notes", "append", "2026-08-01: hi\n")],
            2.0,
        )
        self.assertIn("2026-08-01: hi", self._read())

    def test_guarded_field_raises(self):
        with self.assertRaises(editcore.GuardedFieldError) as cm:
            editcore.apply_edits(
                self.issue, self.lock, [("status", "done")], [], 2.0
            )
        self.assertEqual(cm.exception.field, "status")

    def test_missing_file_raises(self):
        self.issue.unlink()
        with self.assertRaises(FileNotFoundError):
            editcore.apply_edits(
                self.issue, self.lock, [("priority", "low")], [], 2.0
            )

    def test_reindex_fn_invoked_with_suppress_false(self):
        seen = {}

        def reindex_fn(*, suppress_commit=False):
            seen["suppress"] = suppress_commit

        editcore.apply_edits(
            self.issue, self.lock, [("priority", "low")], [], 2.0,
            reindex_fn=reindex_fn,
        )
        self.assertEqual(seen, {"suppress": False})

    def test_no_op_does_not_stamp_updated(self):
        summary = editcore.apply_edits(self.issue, self.lock, [], [], 2.0)
        self.assertEqual(summary["field_changes"], [])
        self.assertIn("updated: 2026-05-12\n", self._read())

    def test_dep_add_in_legacy_mode(self):
        summary = editcore.apply_edits(
            self.issue, self.lock, [], [], 2.0,
            dep_actions=[("add-dep", "IM-101", "blocks", None)],
        )
        self.assertEqual(len(summary["dep_actions"]), 1)
        fm_lines, _ = frontmatter.read_issue(self.issue)
        self.assertEqual(
            deps.get_deps(fm_lines), [{"to": "IM-101", "type": "blocks"}]
        )

    def test_generation_guard_blocks_stale_edit(self):
        # Claim the issue at generation 1 so a caller_generation=0 is stale.
        fm_lines, body = frontmatter.read_issue(self.issue)
        now = lease.now_utc()
        lease.set_claim(
            fm_lines, "carol", lease.format_ts(now),
            lease.format_ts(now + datetime.timedelta(hours=1)), 1,
        )
        self.issue.write_text(
            frontmatter.serialize_issue(fm_lines, body), encoding="utf-8"
        )
        with self.assertRaises(errors.StaleViewError):
            editcore.apply_edits(
                self.issue, self.lock, [("priority", "low")], [], 2.0,
                caller_generation=0,
            )


# ----------------------------------------------------------------------
# apply_edits -- store (autocommit) path + guards needing a git store
# ----------------------------------------------------------------------


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp(prefix="im-editcore-store-"))
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        (self.store / "IM").mkdir()
        (self.store / "IM" / ".locks").mkdir()
        _git(self.store, "init", "-q")
        _git(self.store, "config", "user.email", FIXTURE_EMAIL)
        _git(self.store, "config", "user.name", "T")
        self.today = datetime.date.today().isoformat()

    def _write_issue(self, issue_id, text=None):
        path = self.store / "IM" / f"{issue_id}.md"
        path.write_text(
            (text or SAMPLE).replace("IM-999", issue_id), encoding="utf-8"
        )
        return path

    def _commit_all(self, msg="seed"):
        _git(self.store, "add", "-A")
        _git(self.store, "commit", "-q", "-m", msg)

    def _head_subject(self):
        r = subprocess.run(
            ["git", "-C", str(self.store), "log", "-1", "--format=%s"],
            check=True, capture_output=True, text=True,
        )
        return r.stdout.strip()

    def _paths(self, issue_id):
        return [
            Path("IM") / f"{issue_id}.md",
            Path("IM") / "INDEX.md",
            Path("INDEX.md"),
        ]


class TestApplyEditsStore(StoreFixture):
    def test_autocommit_commits_with_message(self):
        issue = self._write_issue("IM-001")
        self._commit_all()
        lock = self.store / "IM" / ".locks" / "IM-001.lock"
        editcore.apply_edits(
            issue, lock, [("priority", "low")], [], 2.0,
            store_root=self.store,
            commit_paths=self._paths("IM-001"),
            commit_message="edit IM-001",
        )
        self.assertEqual(self._head_subject(), "edit IM-001")
        self.assertIn("priority: low\n", issue.read_text())

    def test_reindex_fn_suppressed_in_store_path(self):
        issue = self._write_issue("IM-002")
        self._commit_all()
        lock = self.store / "IM" / ".locks" / "IM-002.lock"
        seen = {}

        def reindex_fn(*, suppress_commit=False):
            seen["suppress"] = suppress_commit

        editcore.apply_edits(
            issue, lock, [("priority", "low")], [], 2.0,
            store_root=self.store,
            commit_paths=self._paths("IM-002"),
            commit_message="edit IM-002",
            reindex_fn=reindex_fn,
        )
        self.assertEqual(seen, {"suppress": True})


class TestIfUnchangedSince(StoreFixture):
    def _head(self):
        r = subprocess.run(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        return r.stdout.strip()

    def test_replace_succeeds_when_section_unchanged(self):
        issue = self._write_issue("IM-010")
        self._commit_all()
        ref = self._head()
        lock = self.store / "IM" / ".locks" / "IM-010.lock"
        editcore.apply_edits(
            issue, lock, [],
            [("edit", "Summary", "replace", "new summary\n")],
            2.0,
            store_root=self.store,
            commit_paths=self._paths("IM-010"),
            commit_message="edit IM-010",
            if_unchanged_since=ref,
            if_unchanged_since_section="Summary",
        )
        self.assertIn("new summary", issue.read_text())

    def test_replace_raises_concurrent_edit_on_drift(self):
        issue = self._write_issue("IM-011")
        self._commit_all()
        ref = self._head()
        # Drift the Summary section on disk after the ref was taken.
        drifted = issue.read_text().replace(
            "Body of summary.", "DRIFTED body."
        )
        issue.write_text(drifted, encoding="utf-8")
        lock = self.store / "IM" / ".locks" / "IM-011.lock"
        with self.assertRaises(errors.ConcurrentEdit):
            editcore.apply_edits(
                issue, lock, [],
                [("edit", "Summary", "replace", "attempted\n")],
                2.0,
                store_root=self.store,
                commit_paths=self._paths("IM-011"),
                commit_message="edit IM-011",
                if_unchanged_since=ref,
                if_unchanged_since_section="Summary",
            )

    def test_unresolvable_ref_raises_value_error(self):
        issue = self._write_issue("IM-012")
        self._commit_all()
        lock = self.store / "IM" / ".locks" / "IM-012.lock"
        with self.assertRaises(ValueError) as cm:
            editcore.apply_edits(
                issue, lock, [],
                [("edit", "Summary", "replace", "x\n")],
                2.0,
                store_root=self.store,
                commit_paths=self._paths("IM-012"),
                commit_message="edit IM-012",
                if_unchanged_since="deadbeefdeadbeef",
                if_unchanged_since_section="Summary",
            )
        self.assertIn("if-unchanged-since", str(cm.exception))


class TestCheckSectionUnchangedSince(StoreFixture):
    def _head(self):
        r = subprocess.run(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        return r.stdout.strip()

    def test_added_section_since_ref_raises_concurrent_edit(self):
        # Section absent at ref, present now -> concurrent add.
        issue = self._write_issue("IM-020")
        self._commit_all()
        ref = self._head()
        # Add a brand-new section on disk.
        fm_lines, body = frontmatter.read_issue(issue)
        body = frontmatter.add_section(body, "Brand New", "content\n")
        issue.write_text(
            frontmatter.serialize_issue(fm_lines, body), encoding="utf-8"
        )
        _fm_now, body_now = frontmatter.read_issue(issue)
        with self.assertRaises(errors.ConcurrentEdit) as cm:
            editcore._check_section_unchanged_since(
                self.store, issue, body_now, "Brand New", ref
            )
        self.assertIn("did not exist", str(cm.exception))

    def test_both_absent_returns_none(self):
        issue = self._write_issue("IM-021")
        self._commit_all()
        ref = self._head()
        _fm, body_now = frontmatter.read_issue(issue)
        # Section that never existed at ref or now -> no preemptive error.
        self.assertIsNone(
            editcore._check_section_unchanged_since(
                self.store, issue, body_now, "Nonexistent", ref
            )
        )

    def test_file_absent_at_ref_raises_file_not_found(self):
        # Take a ref BEFORE the issue file existed, then ask to compare it.
        (self.store / "placeholder.txt").write_text("x\n")
        self._commit_all("placeholder")
        ref = self._head()
        issue = self._write_issue("IM-022")
        self._commit_all("add IM-022")
        _fm, body_now = frontmatter.read_issue(issue)
        with self.assertRaises(FileNotFoundError) as cm:
            editcore._check_section_unchanged_since(
                self.store, issue, body_now, "Summary", ref
            )
        self.assertIn("if-unchanged-since", str(cm.exception))


class TestReplaceBodyLegacy(unittest.TestCase):
    """apply_edits replace_body kwarg -- legacy no-commit path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".locks").mkdir()
        self.issue = self.root / "IM-999.md"
        self.lock = self.root / ".locks" / "IM-999.lock"
        self.issue.write_text(SAMPLE, encoding="utf-8")
        self.today = datetime.date.today().isoformat()

    def test_replace_body_swaps_whole_body(self):
        new_body = "\n## Rewritten\n\ncontent\n\n### Nested\n\nDECIDED\n"
        summary = editcore.apply_edits(
            self.issue, self.lock, [], [], 2.0, replace_body=new_body,
        )
        _fm, body = frontmatter.read_issue(self.issue)
        self.assertEqual(body, new_body)
        self.assertEqual(
            summary["body_actions"],
            [{"kind": "replace-body",
              "bytes": len(new_body.encode("utf-8"))}],
        )

    def test_replace_body_preserves_frontmatter_bytes_except_updated(self):
        fm_before, _ = frontmatter.read_issue(self.issue)
        editcore.apply_edits(
            self.issue, self.lock, [], [], 2.0, replace_body="new\n",
        )
        fm_after, _ = frontmatter.read_issue(self.issue)
        expected = [
            line if not line.startswith("updated:")
            else f"updated: {self.today}\n"
            for line in fm_before
        ]
        self.assertEqual(fm_after, expected)

    def test_replace_body_stamps_updated(self):
        editcore.apply_edits(
            self.issue, self.lock, [], [], 2.0, replace_body="new\n",
        )
        self.assertIn(f"updated: {self.today}\n", self.issue.read_text())

    def test_empty_replace_body_yields_empty_body(self):
        summary = editcore.apply_edits(
            self.issue, self.lock, [], [], 2.0, replace_body="",
        )
        _fm, body = frontmatter.read_issue(self.issue)
        self.assertEqual(body, "")
        self.assertEqual(summary["body_actions"][0]["bytes"], 0)

    def test_replace_body_composes_with_sets(self):
        summary = editcore.apply_edits(
            self.issue, self.lock, [("priority", "low")], [], 2.0,
            replace_body="new\n",
        )
        text = self.issue.read_text()
        self.assertIn("priority: low\n", text)
        self.assertTrue(text.endswith("---\nnew\n"))
        self.assertEqual(len(summary["field_changes"]), 1)
        self.assertEqual(len(summary["body_actions"]), 1)

    def test_summary_body_actions_empty_when_flag_unused(self):
        summary = editcore.apply_edits(
            self.issue, self.lock, [("priority", "low")], [], 2.0,
        )
        self.assertEqual(summary["body_actions"], [])


class TestReplaceBodyStore(StoreFixture):
    """replace_body + --if-unchanged-since whole-body guard."""

    def _head(self):
        r = subprocess.run(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        return r.stdout.strip()

    def test_replace_body_commits_with_message(self):
        issue = self._write_issue("IM-040")
        self._commit_all()
        lock = self.store / "IM" / ".locks" / "IM-040.lock"
        editcore.apply_edits(
            issue, lock, [], [], 2.0,
            store_root=self.store,
            commit_paths=self._paths("IM-040"),
            commit_message="edit IM-040",
            replace_body="\n## Rewritten\n\nx\n",
        )
        self.assertEqual(self._head_subject(), "edit IM-040")

    def test_replace_body_succeeds_when_body_unchanged_since_ref(self):
        issue = self._write_issue("IM-041")
        self._commit_all()
        ref = self._head()
        lock = self.store / "IM" / ".locks" / "IM-041.lock"
        editcore.apply_edits(
            issue, lock, [], [], 2.0,
            store_root=self.store,
            commit_paths=self._paths("IM-041"),
            commit_message="edit IM-041",
            if_unchanged_since=ref,
            if_unchanged_since_section=None,
            replace_body="new body\n",
        )
        _fm, body = frontmatter.read_issue(issue)
        self.assertEqual(body, "new body\n")

    def test_replace_body_raises_concurrent_edit_on_body_drift(self):
        issue = self._write_issue("IM-042")
        self._commit_all()
        ref = self._head()
        drifted = issue.read_text().replace(
            "Body of summary.", "DRIFTED body."
        )
        issue.write_text(drifted, encoding="utf-8")
        lock = self.store / "IM" / ".locks" / "IM-042.lock"
        with self.assertRaises(errors.ConcurrentEdit):
            editcore.apply_edits(
                issue, lock, [], [], 2.0,
                store_root=self.store,
                commit_paths=self._paths("IM-042"),
                commit_message="edit IM-042",
                if_unchanged_since=ref,
                if_unchanged_since_section=None,
                replace_body="attempted\n",
            )

    def test_frontmatter_drift_does_not_trip_body_guard(self):
        # A lease renew / priority flip between ref and now touches only
        # frontmatter; the whole-body guard must not refuse.
        issue = self._write_issue("IM-043")
        self._commit_all()
        ref = self._head()
        fm_lines, body = frontmatter.read_issue(issue)
        frontmatter.set_field(fm_lines, "priority", "low")
        issue.write_text(
            frontmatter.serialize_issue(fm_lines, body), encoding="utf-8"
        )
        lock = self.store / "IM" / ".locks" / "IM-043.lock"
        editcore.apply_edits(
            issue, lock, [], [], 2.0,
            store_root=self.store,
            commit_paths=self._paths("IM-043"),
            commit_message="edit IM-043",
            if_unchanged_since=ref,
            if_unchanged_since_section=None,
            replace_body="new body\n",
        )
        _fm, body_now = frontmatter.read_issue(issue)
        self.assertEqual(body_now, "new body\n")


class TestCheckBodyUnchangedSince(StoreFixture):
    """_check_body_unchanged_since directly."""

    def _head(self):
        r = subprocess.run(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        return r.stdout.strip()

    def test_identical_body_returns_none(self):
        issue = self._write_issue("IM-050")
        self._commit_all()
        ref = self._head()
        _fm, body_now = frontmatter.read_issue(issue)
        self.assertIsNone(
            editcore._check_body_unchanged_since(
                self.store, issue, body_now, ref
            )
        )

    def test_drifted_body_raises_concurrent_edit(self):
        issue = self._write_issue("IM-051")
        self._commit_all()
        ref = self._head()
        with self.assertRaises(errors.ConcurrentEdit) as cm:
            editcore._check_body_unchanged_since(
                self.store, issue, "different body\n", ref
            )
        self.assertIn("body changed since", str(cm.exception))

    def test_unresolvable_ref_raises_value_error(self):
        issue = self._write_issue("IM-052")
        self._commit_all()
        _fm, body_now = frontmatter.read_issue(issue)
        with self.assertRaises(ValueError) as cm:
            editcore._check_body_unchanged_since(
                self.store, issue, body_now, "deadbeefdeadbeef"
            )
        self.assertIn("if-unchanged-since", str(cm.exception))

    def test_file_absent_at_ref_raises_file_not_found(self):
        (self.store / "placeholder.txt").write_text("x\n")
        self._commit_all("placeholder")
        ref = self._head()
        issue = self._write_issue("IM-053")
        self._commit_all("add IM-053")
        _fm, body_now = frontmatter.read_issue(issue)
        with self.assertRaises(FileNotFoundError):
            editcore._check_body_unchanged_since(
                self.store, issue, body_now, ref
            )


class TestCycleGuard(StoreFixture):
    def test_self_edge_refused(self):
        issue = self._write_issue("IM-030")
        self._commit_all()
        lock = self.store / "IM" / ".locks" / "IM-030.lock"
        with self.assertRaises(errors.DependencyCycleError):
            editcore.apply_edits(
                issue, lock, [], [], 2.0,
                store_root=self.store,
                commit_paths=self._paths("IM-030"),
                commit_message="edit IM-030",
                dep_actions=[("add-dep", "IM-030", "blocks", None)],
            )

    def test_two_node_cycle_refused_and_no_commit(self):
        # IM-040 blocks IM-041 already; adding IM-041 blocks IM-040 closes it.
        a = self._write_issue("IM-040")
        fm_lines, body = frontmatter.read_issue(a)
        deps.set_deps(fm_lines, [{"to": "IM-041", "type": "blocks"}])
        a.write_text(frontmatter.serialize_issue(fm_lines, body))
        self._write_issue("IM-041")
        self._commit_all()
        head_before = self._head_subject()
        b = self.store / "IM" / "IM-041.md"
        lock = self.store / "IM" / ".locks" / "IM-041.lock"
        with self.assertRaises(errors.DependencyCycleError):
            editcore.apply_edits(
                b, lock, [], [], 2.0,
                store_root=self.store,
                commit_paths=self._paths("IM-041"),
                commit_message="edit IM-041",
                dep_actions=[("add-dep", "IM-040", "blocks", None)],
            )
        # No write, no commit: IM-041 still has no depends_on edges and HEAD
        # is unchanged.
        fm_after, _ = frontmatter.read_issue(b)
        self.assertIn(deps.get_deps(fm_after), (None, []))
        self.assertEqual(self._head_subject(), head_before)

    def test_non_cycle_blocks_add_commits(self):
        a = self._write_issue("IM-050")
        self._write_issue("IM-051")
        self._commit_all()
        lock = self.store / "IM" / ".locks" / "IM-050.lock"
        editcore.apply_edits(
            a, lock, [], [], 2.0,
            store_root=self.store,
            commit_paths=self._paths("IM-050"),
            commit_message="edit IM-050",
            dep_actions=[("add-dep", "IM-051", "blocks", None)],
        )
        fm_after, _ = frontmatter.read_issue(a)
        self.assertEqual(
            deps.get_deps(fm_after), [{"to": "IM-051", "type": "blocks"}]
        )


if __name__ == "__main__":
    unittest.main()
