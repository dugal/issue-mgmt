#!/usr/bin/env python3
"""Unit tests for lib/deps.py.

deps.py owns the block-aware read/write primitives for the `depends_on:`
frontmatter block sequence -- the frontmatter-native dependency edge
model. All functions are pure (operate on line lists / edge dicts), so
these tests need no filesystem or git fixtures.

The canonical serialized shape: `depends_on:` at column 0, each edge
`  - to: <ID>` (2-space list marker) then `    type: <KIND>` (4-space
continuation), `to` before `type`; an empty edge set serializes to the
single inline line `depends_on: []`.

End-to-end CLI behavior (issue-dep / edit-issue --add-dep/--remove-dep)
lives in tests/verification/.

Run:
    python3 -m unittest tests.test_deps -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import deps  # noqa: E402  -- path set above


# A minimal frontmatter line list (as produced by splitting the region
# between the `---` delimiters). Lines carry their trailing newline, matching
# how the CLIs feed fm_lines into these primitives.
def fm(*lines):
    return [ln if ln.endswith("\n") else ln + "\n" for ln in lines]


BASE = fm(
    "id: IM-001",
    "title: \"x\"",
    "type: feature",
    "status: open",
)


class TestGetDeps(unittest.TestCase):
    def test_absent_key_returns_none(self):
        # None signals "unmanaged" -- leave any existing edges alone.
        self.assertIsNone(deps.get_deps(list(BASE)))

    def test_empty_inline_list_returns_empty_list(self):
        lines = BASE + fm("depends_on: []")
        self.assertEqual(deps.get_deps(lines), [])

    def test_well_formed_block_returns_edges(self):
        lines = BASE + fm(
            "depends_on:",
            "  - to: IM-101",
            "    type: blocks",
            "  - to: RM-107",
            "    type: related",
        )
        self.assertEqual(
            deps.get_deps(lines),
            [
                {"to": "IM-101", "type": "blocks"},
                {"to": "RM-107", "type": "related"},
            ],
        )

    def test_note_and_created_lines_preserved(self):
        lines = BASE + fm(
            "depends_on:",
            "  - to: IM-101",
            "    type: blocks",
            "    note: discovered mid-task",
            "    created: 2026-07-11",
        )
        self.assertEqual(
            deps.get_deps(lines),
            [{
                "to": "IM-101",
                "type": "blocks",
                "note": "discovered mid-task",
                "created": "2026-07-11",
            }],
        )

    def test_bare_string_item_yields_empty_edge(self):
        # A `- IM-101` list item has no `key: value` colon, so `_split_kv`
        # returns None and the edge is emitted empty rather than dropped:
        # get_deps records the list marker but captures no key.
        lines = BASE + fm(
            "depends_on:",
            "  - IM-101",
        )
        self.assertEqual(deps.get_deps(lines), [{}])

    def test_block_terminates_at_next_top_level_key(self):
        lines = BASE + fm(
            "depends_on:",
            "  - to: IM-101",
            "    type: blocks",
            "priority: high",
        )
        self.assertEqual(deps.get_deps(lines), [{"to": "IM-101", "type": "blocks"}])

    def test_comment_and_blank_child_lines_ignored(self):
        lines = fm(
            "id: IM-001",
            "depends_on:",
            "  # a comment",
            "  - to: IM-101",
            "    type: blocks",
        )
        self.assertEqual(deps.get_deps(lines), [{"to": "IM-101", "type": "blocks"}])

    def test_indented_and_blank_lines_before_key_skipped(self):
        # Leading indented/blank lines exercise the whitespace-skip branch of
        # _top_level_index before `depends_on` is matched.
        lines = fm(
            "",
            "  stray_indented: line",
            "id: IM-001",
            "depends_on:",
            "  - to: IM-101",
            "    type: blocks",
        )
        self.assertEqual(deps.get_deps(lines), [{"to": "IM-101", "type": "blocks"}])

    def test_nonempty_inline_scalar_returns_empty(self):
        # A non-`[]` inline value is not a block sequence we author; get_deps
        # reports zero edges so callers rewrite it into canonical shape.
        lines = BASE + fm("depends_on: garbage")
        self.assertEqual(deps.get_deps(lines), [])

    def test_quoted_inline_empty_list_returns_empty_list(self):
        lines = BASE + fm('depends_on: "[]"')
        self.assertEqual(deps.get_deps(lines), [])


class TestSetDeps(unittest.TestCase):
    def test_empty_edges_serialize_to_inline_empty_list(self):
        lines = list(BASE)
        deps.set_deps(lines, [])
        self.assertEqual(lines[-1], "depends_on: []\n")

    def test_none_edges_serialize_to_inline_empty_list(self):
        lines = list(BASE)
        deps.set_deps(lines, None)
        self.assertEqual(lines[-1], "depends_on: []\n")

    def test_canonical_block_shape_is_byte_exact(self):
        lines = list(BASE)
        deps.set_deps(lines, [{"to": "IM-101", "type": "blocks"}])
        # Exact indentation + key order (to before type).
        self.assertEqual(
            lines[-3:],
            [
                "depends_on:\n",
                "  - to: IM-101\n",
                "    type: blocks\n",
            ],
        )

    def test_key_order_is_to_then_type_even_when_dict_reversed(self):
        lines = list(BASE)
        # Feed the dict with type first; serialization must still emit `to`
        # on the list-marker line and `type` on the continuation.
        deps.set_deps(lines, [{"type": "blocks", "to": "IM-101"}])
        self.assertEqual(lines[-2], "  - to: IM-101\n")
        self.assertEqual(lines[-1], "    type: blocks\n")

    def test_note_and_created_serialize_after_type(self):
        lines = list(BASE)
        deps.set_deps(lines, [{
            "to": "IM-101",
            "type": "blocks",
            "note": "n",
            "created": "2026-07-11",
        }])
        self.assertEqual(
            lines[-4:],
            [
                "  - to: IM-101\n",
                "    type: blocks\n",
                "    note: n\n",
                "    created: 2026-07-11\n",
            ],
        )

    def test_unexpected_keys_serialize_after_known_ones(self):
        lines = list(BASE)
        deps.set_deps(lines, [{"to": "IM-101", "type": "blocks", "custom": "v"}])
        self.assertEqual(lines[-1], "    custom: v\n")

    def test_replaces_existing_block_in_place(self):
        lines = fm(
            "id: IM-001",
            "depends_on:",
            "  - to: IM-101",
            "    type: blocks",
            "priority: high",
        )
        deps.set_deps(lines, [{"to": "RM-200", "type": "related"}])
        # The old block is gone; the trailing `priority:` line is preserved.
        self.assertEqual(
            lines,
            fm(
                "id: IM-001",
                "depends_on:",
                "  - to: RM-200",
                "    type: related",
                "priority: high",
            ),
        )

    def test_appends_when_absent(self):
        lines = list(BASE)
        n = len(lines)
        deps.set_deps(lines, [{"to": "IM-101", "type": "blocks"}])
        self.assertEqual(lines[:n], BASE)
        self.assertGreater(len(lines), n)

    def test_edge_with_no_known_keys_is_skipped(self):
        lines = list(BASE)
        deps.set_deps(lines, [{}])
        # Header line only; the empty edge contributes nothing.
        self.assertEqual(lines[-1], "depends_on:\n")

    def test_round_trip_recovers_edges(self):
        edges = [
            {"to": "IM-101", "type": "blocks"},
            {"to": "RM-107", "type": "related"},
        ]
        lines = list(BASE)
        deps.set_deps(lines, edges)
        self.assertEqual(deps.get_deps(lines), edges)

    def test_round_trip_empty_recovers_empty(self):
        lines = list(BASE)
        deps.set_deps(lines, [])
        self.assertEqual(deps.get_deps(lines), [])


class TestAddDep(unittest.TestCase):
    def test_adds_new_edge(self):
        result = deps.add_dep([], "blocks", "IM-101")
        self.assertEqual(result, [{"to": "IM-101", "type": "blocks"}])

    def test_dedup_on_to_and_type(self):
        existing = [{"to": "IM-101", "type": "blocks"}]
        result = deps.add_dep(existing, "blocks", "IM-101")
        self.assertEqual(result, existing)

    def test_same_target_different_kind_is_added(self):
        existing = [{"to": "IM-101", "type": "blocks"}]
        result = deps.add_dep(existing, "related", "IM-101")
        self.assertEqual(
            result,
            [
                {"to": "IM-101", "type": "blocks"},
                {"to": "IM-101", "type": "related"},
            ],
        )

    def test_none_edges_treated_as_empty(self):
        result = deps.add_dep(None, "blocks", "IM-101")
        self.assertEqual(result, [{"to": "IM-101", "type": "blocks"}])

    def test_does_not_mutate_input(self):
        existing = [{"to": "IM-101", "type": "blocks"}]
        deps.add_dep(existing, "related", "IM-200")
        self.assertEqual(existing, [{"to": "IM-101", "type": "blocks"}])


class TestRemoveDep(unittest.TestCase):
    def test_removes_matching_target(self):
        edges = [{"to": "IM-101", "type": "blocks"}]
        self.assertEqual(deps.remove_dep(edges, "IM-101"), [])

    def test_removes_only_matching_kind_when_given(self):
        edges = [
            {"to": "IM-101", "type": "blocks"},
            {"to": "IM-101", "type": "related"},
        ]
        result = deps.remove_dep(edges, "IM-101", kind="blocks")
        self.assertEqual(result, [{"to": "IM-101", "type": "related"}])

    def test_removing_last_yields_empty_list(self):
        edges = [{"to": "IM-101", "type": "blocks"}]
        result = deps.remove_dep(edges, "IM-101", kind="blocks")
        self.assertEqual(result, [])

    def test_removing_absent_target_unchanged(self):
        edges = [{"to": "IM-101", "type": "blocks"}]
        result = deps.remove_dep(edges, "IM-999")
        self.assertEqual(result, edges)

    def test_none_edges_returns_empty_list(self):
        self.assertEqual(deps.remove_dep(None, "IM-101"), [])

    def test_target_matches_but_kind_differs_kept(self):
        edges = [{"to": "IM-101", "type": "related"}]
        result = deps.remove_dep(edges, "IM-101", kind="blocks")
        self.assertEqual(result, edges)


class TestValidateKind(unittest.TestCase):
    def test_each_valid_kind_passes(self):
        for kind in deps.IM_DEP_KINDS:
            with self.subTest(kind=kind):
                # No exception.
                self.assertIsNone(deps.validate_kind(kind))

    def test_unknown_kind_raises_value_error(self):
        with self.assertRaises(ValueError):
            deps.validate_kind("bogus")

    def test_error_message_names_the_kind(self):
        with self.assertRaises(ValueError) as ctx:
            deps.validate_kind("bogus")
        self.assertIn("bogus", str(ctx.exception))


class TestValidateTarget(unittest.TestCase):
    def test_valid_target_passes(self):
        self.assertIsNone(deps.validate_target("IM-101"))

    def test_cross_prefix_target_passes(self):
        self.assertIsNone(deps.validate_target("RM-107"))

    def test_eight_char_prefix_passes(self):
        self.assertIsNone(deps.validate_target("ABCDEFGH-1"))

    def test_lowercase_prefix_passes(self):
        # validate_target is shape-only and case-insensitive; the
        # lowercase prefix is canonicalized to IM-101 at write time.
        self.assertIsNone(deps.validate_target("im-101"))

    def test_unpadded_number_passes(self):
        # The loose PREFIX-<int> form is accepted (canonicalized to
        # IM-007 at write time).
        self.assertIsNone(deps.validate_target("IM-7"))

    def test_prefix_without_number_raises(self):
        with self.assertRaises(ValueError):
            deps.validate_target("IM-")

    def test_single_char_prefix_raises(self):
        with self.assertRaises(ValueError):
            deps.validate_target("A-1")

    def test_none_target_raises(self):
        with self.assertRaises(ValueError):
            deps.validate_target(None)

    def test_empty_target_raises(self):
        with self.assertRaises(ValueError):
            deps.validate_target("")


if __name__ == "__main__":
    unittest.main()
