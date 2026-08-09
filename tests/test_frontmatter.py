#!/usr/bin/env python3
"""Unit tests for lib/frontmatter.py.

Covers:
  - read_issue / parse_issue / serialize_issue round-trip preservation
  - get_field with bare, quoted, empty, and inline-commented values
  - set_field: in-place replace; quoting-style preservation; comment
    preservation; appended-key path when field is absent
  - find_section / edit_section: heading bounds, append, prepend, replace
  - edit_section preserves bytes outside the targeted section

Run:
    python3 -m unittest tests.test_frontmatter -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import frontmatter  # noqa: E402


SAMPLE = """---
id: IM-001
title: "Example issue"
type: feature
status: open
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: [a, b]
changelog:    # set by /update-changelog -- do not edit manually
---

## Summary

First paragraph of summary.

Second paragraph.

## Implementation Notes

## Archived Plan Content
"""


class ParseAndSerialize(unittest.TestCase):
    def test_round_trip_byte_for_byte(self):
        fm, body = frontmatter.parse_issue(SAMPLE)
        self.assertEqual(frontmatter.serialize_issue(fm, body), SAMPLE)

    def test_missing_opening_fence(self):
        with self.assertRaises(ValueError):
            frontmatter.parse_issue("id: IM-001\n---\nbody\n")

    def test_missing_closing_fence(self):
        with self.assertRaises(ValueError):
            frontmatter.parse_issue("---\nid: IM-001\n")

    def test_empty_body(self):
        text = "---\nid: IM-001\n---\n"
        fm, body = frontmatter.parse_issue(text)
        self.assertEqual(fm, ["id: IM-001\n"])
        self.assertEqual(body, "")
        self.assertEqual(frontmatter.serialize_issue(fm, body), text)


class GetField(unittest.TestCase):
    def test_bare_value(self):
        fm, _ = frontmatter.parse_issue(SAMPLE)
        self.assertEqual(frontmatter.get_field(fm, "id"), "IM-001")
        self.assertEqual(frontmatter.get_field(fm, "priority"), "high")

    def test_double_quoted_value(self):
        fm, _ = frontmatter.parse_issue(SAMPLE)
        self.assertEqual(
            frontmatter.get_field(fm, "title"), "Example issue"
        )

    def test_empty_quoted_value(self):
        fm, _ = frontmatter.parse_issue(SAMPLE)
        self.assertEqual(frontmatter.get_field(fm, "branch"), "")

    def test_inline_value_with_comment(self):
        fm, _ = frontmatter.parse_issue(SAMPLE)
        # changelog has no value before the '#' comment.
        self.assertEqual(frontmatter.get_field(fm, "changelog"), "")

    def test_absent_returns_none(self):
        fm, _ = frontmatter.parse_issue(SAMPLE)
        self.assertIsNone(frontmatter.get_field(fm, "nonexistent"))

    def test_list_kept_as_raw_string(self):
        fm, _ = frontmatter.parse_issue(SAMPLE)
        self.assertEqual(frontmatter.get_field(fm, "tags"), "[a, b]")


class SetField(unittest.TestCase):
    def test_replace_bare_value(self):
        fm, body = frontmatter.parse_issue(SAMPLE)
        frontmatter.set_field(fm, "priority", "medium")
        out = frontmatter.serialize_issue(fm, body)
        self.assertIn("priority: medium\n", out)
        self.assertNotIn("priority: high", out)

    def test_replace_double_quoted_preserves_quotes(self):
        fm, body = frontmatter.parse_issue(SAMPLE)
        frontmatter.set_field(fm, "title", "Renamed")
        out = frontmatter.serialize_issue(fm, body)
        self.assertIn('title: "Renamed"\n', out)

    def test_replace_empty_quoted_preserves_quotes(self):
        fm, body = frontmatter.parse_issue(SAMPLE)
        frontmatter.set_field(fm, "branch", "phase-doug035-p1b")
        out = frontmatter.serialize_issue(fm, body)
        self.assertIn('branch: "phase-doug035-p1b"\n', out)

    def test_replace_preserves_inline_comment(self):
        # Original line is `changelog:    # set by ...` (4 spaces between
        # ':' and '#'). After inserting a value, one of those spaces moves
        # to the colon-value gap and the rest stays before the comment.
        fm, body = frontmatter.parse_issue(SAMPLE)
        frontmatter.set_field(fm, "changelog", "skip")
        out = frontmatter.serialize_issue(fm, body)
        self.assertIn(
            "changelog: skip   # set by /update-changelog "
            "-- do not edit manually\n",
            out,
        )

    def test_unrelated_lines_byte_for_byte(self):
        fm, body = frontmatter.parse_issue(SAMPLE)
        frontmatter.set_field(fm, "priority", "low")
        out = frontmatter.serialize_issue(fm, body)
        # All other frontmatter lines must be untouched.
        for line in (
            "id: IM-001\n",
            'title: "Example issue"\n',
            "type: feature\n",
            "status: open\n",
            "created: 2026-05-12\n",
            "updated: 2026-05-12\n",
            'branch: ""\n',
            "tags: [a, b]\n",
        ):
            self.assertIn(line, out)
        # And body is identical.
        self.assertTrue(out.endswith(body))

    def test_append_when_absent(self):
        fm, body = frontmatter.parse_issue(SAMPLE)
        frontmatter.set_field(fm, "newfield", "newvalue")
        out = frontmatter.serialize_issue(fm, body)
        self.assertIn("newfield: newvalue\n", out)

    def test_append_with_special_chars_gets_quoted(self):
        fm, body = frontmatter.parse_issue(SAMPLE)
        frontmatter.set_field(fm, "note", "has: colon")
        out = frontmatter.serialize_issue(fm, body)
        self.assertIn('note: "has: colon"\n', out)


class FindSection(unittest.TestCase):
    def test_simple_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        bounds = frontmatter.find_section(body, "Summary")
        self.assertIsNotNone(bounds)
        heading, start, end = bounds
        lines = body.splitlines(keepends=True)
        self.assertEqual(lines[heading].rstrip(), "## Summary")
        self.assertGreater(end, start)

    def test_empty_section_bounded_by_next(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        bounds = frontmatter.find_section(body, "Implementation Notes")
        self.assertIsNotNone(bounds)
        heading, start, end = bounds
        lines = body.splitlines(keepends=True)
        self.assertEqual(lines[heading].rstrip(), "## Implementation Notes")
        # the section has only a blank line, then the next ## starts
        self.assertEqual(lines[end].rstrip(), "## Archived Plan Content")

    def test_last_section_runs_to_eof(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        bounds = frontmatter.find_section(body, "Archived Plan Content")
        self.assertIsNotNone(bounds)
        _, _, end = bounds
        lines = body.splitlines(keepends=True)
        self.assertEqual(end, len(lines))

    def test_not_found(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        self.assertIsNone(frontmatter.find_section(body, "Nonexistent"))

    def test_ignores_h3(self):
        body = "## A\n\nfoo\n\n### Not me\n\nbar\n\n## B\n\nbaz\n"
        bounds = frontmatter.find_section(body, "A")
        self.assertIsNotNone(bounds)
        _, _, end = bounds
        lines = body.splitlines(keepends=True)
        # The next ## (level-2) is "## B", not "### Not me".
        self.assertEqual(lines[end].rstrip(), "## B")


class EditSection(unittest.TestCase):
    def test_append_to_populated_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.edit_section(body, "Summary", "append", "Third line.")
        self.assertIn("First paragraph of summary.", out)
        self.assertIn("Second paragraph.", out)
        self.assertIn("Third line.", out)
        # adjacency to next heading preserved
        self.assertIn("Third line.\n\n## Implementation Notes\n", out)

    def test_append_to_empty_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.edit_section(
            body, "Implementation Notes", "append", "2026-05-13: started"
        )
        self.assertIn(
            "## Implementation Notes\n\n2026-05-13: started\n\n"
            "## Archived Plan Content\n",
            out,
        )

    def test_append_to_eof_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.edit_section(
            body, "Archived Plan Content", "append", "Archived bit."
        )
        # No following section -- ends with single trailing newline.
        self.assertTrue(out.endswith("## Archived Plan Content\n\nArchived bit.\n"))

    def test_prepend_to_populated_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.edit_section(
            body, "Summary", "prepend", "Top line."
        )
        # New text appears before old content within the section
        summary_block = out.split("## Summary", 1)[1].split("##", 1)[0]
        self.assertLess(
            summary_block.index("Top line."),
            summary_block.index("First paragraph of summary."),
        )

    def test_replace_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.edit_section(
            body, "Summary", "replace", "Brand new summary."
        )
        self.assertIn("## Summary\n\nBrand new summary.\n\n## Implementation Notes\n", out)
        self.assertNotIn("First paragraph of summary.", out)
        self.assertNotIn("Second paragraph.", out)

    def test_adjacent_sections_byte_for_byte_on_append(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        # The body BEFORE the targeted section
        prefix = body.split("## Implementation Notes", 1)[0]
        suffix = body.split("## Archived Plan Content", 1)[1]
        out = frontmatter.edit_section(
            body, "Implementation Notes", "append", "Note."
        )
        # Prefix unchanged
        self.assertTrue(out.startswith(prefix))
        # The suffix following the *Archived Plan Content* heading is unchanged
        self.assertTrue(out.endswith(suffix))

    def test_section_not_found_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.edit_section(body, "Nope", "append", "x")

    def test_invalid_op_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.edit_section(body, "Summary", "wat", "x")


class ListSectionNames(unittest.TestCase):
    def test_lists_in_document_order(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        self.assertEqual(
            frontmatter.list_section_names(body),
            ["Summary", "Implementation Notes", "Archived Plan Content"],
        )

    def test_empty_body_returns_empty(self):
        self.assertEqual(frontmatter.list_section_names(""), [])

    def test_ignores_h3_headings(self):
        body = "## A\n\n### Not me\n\n## B\n"
        self.assertEqual(frontmatter.list_section_names(body), ["A", "B"])


class AddSection(unittest.TestCase):
    def test_at_end_default(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.add_section(body, "Approach", "Some approach.")
        self.assertTrue(
            out.endswith("## Approach\n\nSome approach.\n"),
            f"output did not end with new section: {out!r}",
        )
        # Original prefix preserved up through the prior last section
        self.assertIn("## Archived Plan Content\n\n## Approach", out)

    def test_after_anchor(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.add_section(
            body, "Approach", "Approach text.", after="Summary"
        )
        # New section sits between Summary's body and Implementation Notes
        self.assertIn(
            "## Summary", out
        )
        self.assertLess(
            out.index("## Approach"),
            out.index("## Implementation Notes"),
        )
        # Separation between Approach body and the next heading
        self.assertIn("Approach text.\n\n## Implementation Notes\n", out)

    def test_before_anchor(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.add_section(
            body, "New", "x", before="Implementation Notes"
        )
        # New appears immediately before Implementation Notes
        self.assertLess(out.index("## New"), out.index("## Implementation Notes"))
        # And after Summary
        self.assertLess(out.index("## Summary"), out.index("## New"))

    def test_to_position_zero(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.add_section(body, "First", "x", to_position=0)
        # First is the very first H2 now
        self.assertEqual(
            frontmatter.list_section_names(out),
            ["First", "Summary", "Implementation Notes", "Archived Plan Content"],
        )

    def test_to_position_end(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        n = len(frontmatter.list_section_names(body))
        out = frontmatter.add_section(body, "Last", "x", to_position=n)
        self.assertEqual(
            frontmatter.list_section_names(out)[-1], "Last"
        )

    def test_empty_body_renders_heading_only(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.add_section(body, "Empty", "")
        # The new section consists of just the heading (no extra blank in
        # the body block); a blank line separator still precedes it.
        self.assertTrue(
            out.endswith("## Empty\n"),
            f"output did not end with empty section heading: {out!r}",
        )

    def test_duplicate_name_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.add_section(body, "Summary", "x")

    def test_missing_after_anchor_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.add_section(body, "X", "x", after="Nonexistent")

    def test_missing_before_anchor_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.add_section(body, "X", "x", before="Nonexistent")

    def test_to_position_out_of_range_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.add_section(body, "X", "x", to_position=99)

    def test_multiple_position_selectors_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.add_section(
                body, "X", "x", after="Summary", before="Implementation Notes"
            )

    def test_preserves_bytes_outside_inserted_block(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.add_section(
            body, "Approach", "x", after="Summary"
        )
        # The Summary heading and its body are unchanged before insertion
        prefix = body.split("## Implementation Notes", 1)[0]
        self.assertTrue(out.startswith(prefix))
        # The Implementation Notes ... EOF block remains intact in the output
        tail = body.split("## Implementation Notes", 1)[1]
        self.assertTrue(out.rstrip("\n").endswith(tail.rstrip("\n")))


class RenameSection(unittest.TestCase):
    def test_renames_heading_only(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.rename_section(
            body, "Implementation Notes", "Notes"
        )
        self.assertIn("## Notes\n", out)
        self.assertNotIn("## Implementation Notes\n", out)

    def test_body_preserved_byte_for_byte(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.rename_section(body, "Summary", "Overview")
        # Summary body content stays intact
        self.assertIn("First paragraph of summary.", out)
        self.assertIn("Second paragraph.", out)
        # Surrounding sections untouched
        self.assertIn("## Implementation Notes\n", out)
        self.assertIn("## Archived Plan Content\n", out)

    def test_rename_to_same_name_is_noop(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.rename_section(body, "Summary", "Summary")
        self.assertEqual(out, body)

    def test_missing_old_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.rename_section(body, "Nonexistent", "New")

    def test_new_already_exists_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.rename_section(body, "Summary", "Implementation Notes")


class RemoveSection(unittest.TestCase):
    def test_removes_middle_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.remove_section(body, "Implementation Notes")
        self.assertNotIn("## Implementation Notes", out)
        # The other sections remain
        self.assertIn("## Summary\n", out)
        self.assertIn("## Archived Plan Content\n", out)

    def test_removes_eof_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.remove_section(body, "Archived Plan Content")
        self.assertNotIn("## Archived Plan Content", out)
        self.assertIn("## Implementation Notes", out)

    def test_removes_first_section(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.remove_section(body, "Summary")
        self.assertNotIn("## Summary", out)
        self.assertNotIn("First paragraph of summary.", out)
        # First remaining section is now Implementation Notes
        self.assertEqual(
            frontmatter.list_section_names(out)[0], "Implementation Notes"
        )

    def test_missing_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.remove_section(body, "Nonexistent")


class MoveSection(unittest.TestCase):
    def test_move_to_position_zero(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.move_section(
            body, "Implementation Notes", to_position=0
        )
        self.assertEqual(
            frontmatter.list_section_names(out)[0], "Implementation Notes"
        )

    def test_move_after_anchor(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.move_section(
            body, "Archived Plan Content", after="Summary"
        )
        names = frontmatter.list_section_names(out)
        self.assertEqual(
            names, ["Summary", "Archived Plan Content", "Implementation Notes"]
        )

    def test_move_before_anchor(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.move_section(
            body, "Archived Plan Content", before="Summary"
        )
        names = frontmatter.list_section_names(out)
        self.assertEqual(names[0], "Archived Plan Content")
        self.assertEqual(names[1], "Summary")

    def test_move_to_eof_preserves_body_content(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        out = frontmatter.move_section(body, "Summary", to_position=2)
        # Summary content preserved at new EOF position
        self.assertIn("First paragraph of summary.", out)
        self.assertIn("Second paragraph.", out)
        self.assertEqual(
            frontmatter.list_section_names(out)[-1], "Summary"
        )

    def test_missing_section_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.move_section(
                body, "Nonexistent", to_position=0
            )

    def test_missing_after_anchor_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.move_section(
                body, "Summary", after="Nonexistent"
            )

    def test_no_destination_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.move_section(body, "Summary")

    def test_two_destinations_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.move_section(
                body, "Summary", after="Implementation Notes", to_position=0
            )

    def test_destination_anchor_is_self_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.move_section(body, "Summary", after="Summary")

    def test_to_position_out_of_range_raises(self):
        _, body = frontmatter.parse_issue(SAMPLE)
        with self.assertRaises(ValueError):
            frontmatter.move_section(body, "Summary", to_position=99)


class FindEscapingHeader(unittest.TestCase):
    """detect body lines that would escape a level-2 section."""

    def test_h2_line_is_flagged(self):
        result = frontmatter.find_escaping_header("## phase-1: foo\n\n- item\n")
        self.assertEqual(result, "## phase-1: foo")

    def test_h1_line_is_flagged(self):
        result = frontmatter.find_escaping_header("intro\n# Title\n")
        self.assertEqual(result, "# Title")

    def test_h3_and_deeper_pass(self):
        self.assertIsNone(
            frontmatter.find_escaping_header("### nested\n#### deeper\n")
        )

    def test_first_offending_line_returned(self):
        result = frontmatter.find_escaping_header("# first\n## second\n")
        self.assertEqual(result, "# first")

    def test_empty_and_plain_text_pass(self):
        self.assertIsNone(frontmatter.find_escaping_header(""))
        self.assertIsNone(frontmatter.find_escaping_header("plain\ntext\n"))

    def test_bare_hashes_without_space_pass(self):
        # Matches _is_any_h2: a header requires '# '/'## ' with the space.
        self.assertIsNone(frontmatter.find_escaping_header("##\n##foo\n#bar\n"))

    def test_h2_inside_code_fence_is_still_flagged(self):
        # Deliberately fence-naive: find_section treats a fenced '## ' line
        # as a section boundary too, so it genuinely fragments the file.
        text = "```markdown\n## fenced heading\n```\n"
        self.assertEqual(
            frontmatter.find_escaping_header(text), "## fenced heading"
        )

    def test_trailing_whitespace_and_crlf_normalized(self):
        result = frontmatter.find_escaping_header("## heading  \r\n")
        self.assertEqual(result, "## heading")

    def test_mid_line_hashes_pass(self):
        self.assertIsNone(
            frontmatter.find_escaping_header("see the ## marker inline\n")
        )

    def test_level_1_only_flags_h1(self):
        self.assertIsNone(
            frontmatter.find_escaping_header("## fine at level 1\n", level=1)
        )
        self.assertEqual(
            frontmatter.find_escaping_header("# not fine\n", level=1),
            "# not fine",
        )


if __name__ == "__main__":
    unittest.main()
