#!/usr/bin/env python3
"""Unit tests for lib/validate.py.

Covers the structural schema:
  - required fields present
  - allowed values for type / status / priority
  - id format (PREFIX-NNN)
  - id <-> filename consistency
  - id-prefix <-> parent-directory consistency
  - date-field formatting (YYYY-MM-DD)
  - frontmatter parser edge cases (quotes, inline comments, blank lines)
  - serialization (Violation.to_dict, ValidationResult.to_dict, .format)

Corpus-level smoke-tests (validate-issue running against every issue file
in a local projects tree) live in tests/verification/validate-issue.sh.

Run:
    python3 -m unittest tests.test_validate -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from validate import (  # noqa: E402  -- path set above
    ALLOWED_STATUSES,
    ALLOWED_TYPES,
    ALLOWED_PRIORITIES,
    ValidationResult,
    Violation,
    validate_file,
    validate_text,
)


VALID_FRONTMATTER = """---
id: IM-001
title: "Example issue"
type: feature
status: open
priority: high
created: 2026-05-13
updated: 2026-05-13
branch: ""
tags: []
changelog:
---

## Summary

Body.
"""


def codes(result):
    return [v.code for v in result.violations]


class TestCanonicalFile(unittest.TestCase):
    def test_valid_file_has_no_violations(self):
        r = validate_text(Path("IM-001.md"), VALID_FRONTMATTER)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")
        self.assertEqual(r.violations, [])


class TestRequiredFields(unittest.TestCase):
    def _missing(self, text, expected_field, filename="IM-001.md"):
        r = validate_text(Path(filename), text)
        pairs = {(v.code, v.field) for v in r.violations}
        self.assertIn(("missing-field", expected_field), pairs)
        self.assertFalse(r.ok)

    def test_missing_id_reported(self):
        text = VALID_FRONTMATTER.replace("id: IM-001\n", "")
        self._missing(text, "id", filename="UNKNOWN.md")

    def test_missing_title_reported(self):
        text = VALID_FRONTMATTER.replace('title: "Example issue"\n', "")
        self._missing(text, "title")

    def test_missing_type_reported(self):
        text = VALID_FRONTMATTER.replace("type: feature\n", "")
        self._missing(text, "type")

    def test_missing_status_reported(self):
        text = VALID_FRONTMATTER.replace("status: open\n", "")
        self._missing(text, "status")

    def test_missing_priority_reported(self):
        text = VALID_FRONTMATTER.replace("priority: high\n", "")
        self._missing(text, "priority")

    def test_missing_created_reported(self):
        text = VALID_FRONTMATTER.replace("created: 2026-05-13\n", "")
        self._missing(text, "created")

    def test_missing_updated_reported(self):
        text = VALID_FRONTMATTER.replace("updated: 2026-05-13\n", "")
        self._missing(text, "updated")


class TestOptionalFields(unittest.TestCase):
    def test_missing_branch_is_allowed(self):
        text = VALID_FRONTMATTER.replace('branch: ""\n', "")
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_missing_tags_is_allowed(self):
        text = VALID_FRONTMATTER.replace("tags: []\n", "")
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_missing_changelog_is_allowed(self):
        text = VALID_FRONTMATTER.replace("changelog:\n", "")
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")


class TestAllowedValues(unittest.TestCase):
    def test_underscore_status_reported_invalid(self):
        # The motivating bug: spec.md previously had in_progress (underscore)
        # while every corpus file uses in-progress (hyphen). Validator rejects
        # the underscore form.
        text = VALID_FRONTMATTER.replace("status: open", "status: in_progress")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-status", codes(r))

    def test_unknown_status_reported_invalid(self):
        text = VALID_FRONTMATTER.replace("status: open", "status: archived")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-status", codes(r))

    def test_unknown_type_reported_invalid(self):
        text = VALID_FRONTMATTER.replace("type: feature", "type: refactor")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-type", codes(r))

    def test_unknown_priority_reported_invalid(self):
        text = VALID_FRONTMATTER.replace("priority: high", "priority: urgent")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-priority", codes(r))

    def test_each_canonical_status_validates(self):
        for s in ALLOWED_STATUSES:
            with self.subTest(status=s):
                text = VALID_FRONTMATTER.replace("status: open", f"status: {s}")
                r = validate_text(Path("IM-001.md"), text)
                self.assertTrue(
                    r.ok,
                    f"status={s} should validate; got {r.violations!r}",
                )

    def test_each_canonical_type_validates(self):
        for t in ALLOWED_TYPES:
            with self.subTest(type=t):
                text = VALID_FRONTMATTER.replace("type: feature", f"type: {t}")
                r = validate_text(Path("IM-001.md"), text)
                self.assertTrue(
                    r.ok,
                    f"type={t} should validate; got {r.violations!r}",
                )

    def test_each_canonical_priority_validates(self):
        for p in ALLOWED_PRIORITIES:
            with self.subTest(priority=p):
                text = VALID_FRONTMATTER.replace("priority: high", f"priority: {p}")
                r = validate_text(Path("IM-001.md"), text)
                self.assertTrue(
                    r.ok,
                    f"priority={p} should validate; got {r.violations!r}",
                )


class TestIdFormat(unittest.TestCase):
    def test_lowercase_prefix_reported(self):
        text = VALID_FRONTMATTER.replace("id: IM-001", "id: im-001")
        r = validate_text(Path("im-001.md"), text)
        self.assertIn("invalid-id-format", codes(r))

    def test_prefix_only_no_number_reported(self):
        text = VALID_FRONTMATTER.replace("id: IM-001", "id: IM-")
        r = validate_text(Path("IM-.md"), text)
        self.assertIn("invalid-id-format", codes(r))

    def test_eight_char_prefix_validates(self):
        text = VALID_FRONTMATTER.replace("id: IM-001", "id: ABCDEFGH-001")
        r = validate_text(Path("ABCDEFGH-001.md"), text)
        self.assertNotIn("invalid-id-format", codes(r))

    def test_nine_char_prefix_reported(self):
        text = VALID_FRONTMATTER.replace("id: IM-001", "id: ABCDEFGHI-001")
        r = validate_text(Path("ABCDEFGHI-001.md"), text)
        self.assertIn("invalid-id-format", codes(r))

    def test_single_char_prefix_reported(self):
        text = VALID_FRONTMATTER.replace("id: IM-001", "id: A-001")
        r = validate_text(Path("A-001.md"), text)
        self.assertIn("invalid-id-format", codes(r))


class TestIdFilenameMismatch(unittest.TestCase):
    def test_stem_differs_from_id_reported(self):
        # The ACP-001 case: id: ACP-001 inside ISSUE-001.md.
        text = VALID_FRONTMATTER.replace("id: IM-001", "id: ACP-001")
        r = validate_text(Path("ISSUE-001.md"), text)
        self.assertIn("id-filename-mismatch", codes(r))

    def test_stem_matches_id_not_reported(self):
        text = VALID_FRONTMATTER.replace("id: IM-001", "id: ACP-001")
        r = validate_text(Path("ACP-001.md"), text)
        self.assertNotIn("id-filename-mismatch", codes(r))


class TestPrefixDirectoryMismatch(unittest.TestCase):
    def test_parent_dir_differs_from_id_prefix_reported(self):
        text = VALID_FRONTMATTER.replace("id: IM-001", "id: RM-001")
        r = validate_text(Path("IM/RM-001.md"), text)
        self.assertIn("prefix-directory-mismatch", codes(r))

    def test_parent_dir_matches_id_prefix_not_reported(self):
        r = validate_text(Path("IM/IM-001.md"), VALID_FRONTMATTER)
        self.assertNotIn("prefix-directory-mismatch", codes(r))

    def test_non_prefix_parent_dir_not_checked(self):
        # `.issues` does not match ^[A-Z][A-Z0-9]{1,7}$; the directory check
        # is silent for legacy per-project layouts.
        r = validate_text(Path(".issues/IM-001.md"), VALID_FRONTMATTER)
        self.assertNotIn("prefix-directory-mismatch", codes(r))


class TestDateFormat(unittest.TestCase):
    def test_slash_separated_updated_reported(self):
        text = VALID_FRONTMATTER.replace("updated: 2026-05-13", "updated: 2026/05/13")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-date-format", codes(r))

    def test_freeform_created_reported(self):
        text = VALID_FRONTMATTER.replace("created: 2026-05-13", "created: yesterday")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-date-format", codes(r))


class TestEmptyTitle(unittest.TestCase):
    def test_empty_quoted_title_reported(self):
        text = VALID_FRONTMATTER.replace('title: "Example issue"', 'title: ""')
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("empty-title", codes(r))


class TestMalformedFrontmatter(unittest.TestCase):
    """Each of these should produce exactly one malformed-frontmatter
    violation; the parser short-circuits before per-field validation."""

    def test_no_opening_delimiter_reported(self):
        text = "id: IM-001\n---\n"
        r = validate_text(Path("IM-001.md"), text)
        self.assertEqual(codes(r), ["malformed-frontmatter"])

    def test_no_closing_delimiter_reported(self):
        text = "---\nid: IM-001\n\n## Summary\n"
        r = validate_text(Path("IM-001.md"), text)
        self.assertEqual(codes(r), ["malformed-frontmatter"])

    def test_line_without_colon_reported(self):
        text = "---\nid: IM-001\nnot-a-key-value-line\n---\n"
        r = validate_text(Path("IM-001.md"), text)
        self.assertEqual(codes(r), ["malformed-frontmatter"])

    def test_empty_key_reported(self):
        text = "---\nid: IM-001\n: value\n---\n"
        r = validate_text(Path("IM-001.md"), text)
        self.assertEqual(codes(r), ["malformed-frontmatter"])

    def test_empty_file_reported(self):
        r = validate_text(Path("IM-001.md"), "")
        self.assertEqual(codes(r), ["malformed-frontmatter"])


class TestFrontmatterParsing(unittest.TestCase):
    def test_inline_comment_stripped(self):
        # changelog field uses inline comments in the canonical template.
        text = (
            "---\nid: IM-001\ntitle: \"x\"\ntype: feature\n"
            "status: open\npriority: high\n"
            "created: 2026-05-13\nupdated: 2026-05-13\n"
            "changelog:    # set by /update-changelog\n---\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_blank_lines_inside_frontmatter_skipped(self):
        text = (
            "---\n\nid: IM-001\ntitle: \"x\"\n\ntype: feature\n"
            "status: open\npriority: high\n"
            "created: 2026-05-13\nupdated: 2026-05-13\n---\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_comment_only_line_skipped(self):
        text = (
            "---\nid: IM-001\n# annotation\ntitle: \"x\"\ntype: feature\n"
            "status: open\npriority: high\n"
            "created: 2026-05-13\nupdated: 2026-05-13\n---\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")


class TestMultipleViolations(unittest.TestCase):
    def test_file_with_several_problems_lists_all(self):
        text = (
            "---\nid: bad-id\ntitle: \"\"\ntype: not-a-type\n"
            "status: invalid\npriority: medium\n"
            "created: today\nupdated: 2026-05-13\n---\n"
        )
        r = validate_text(Path("UNKNOWN.md"), text)
        seen = set(codes(r))
        for expected in {
            "invalid-id-format",
            "empty-title",
            "invalid-type",
            "invalid-status",
            "invalid-date-format",
        }:
            self.assertIn(expected, seen)


class TestSerialization(unittest.TestCase):
    def test_violation_to_dict_omits_none_fields(self):
        v = Violation(code="missing-field", message="m", field="title")
        d = v.to_dict()
        self.assertEqual(d, {"code": "missing-field", "message": "m", "field": "title"})
        self.assertNotIn("line", d)
        self.assertNotIn("value", d)

    def test_violation_to_dict_includes_set_fields(self):
        v = Violation(
            code="invalid-status",
            message="m",
            field="status",
            line=5,
            value="foo",
        )
        d = v.to_dict()
        self.assertEqual(d["line"], 5)
        self.assertEqual(d["value"], "foo")

    def test_violation_format_with_line(self):
        v = Violation(code="invalid-status", message="msg", line=5)
        self.assertEqual(
            v.format(Path("/x.md")),
            "/x.md:5: invalid-status: msg",
        )

    def test_violation_format_without_line(self):
        v = Violation(code="missing-field", message="msg", field="title")
        self.assertEqual(
            v.format(Path("/x.md")),
            "/x.md: missing-field: msg",
        )

    def test_result_to_dict_shape(self):
        r = validate_text(Path("IM-001.md"), VALID_FRONTMATTER)
        d = r.to_dict()
        self.assertEqual(set(d.keys()), {"file", "valid", "violations"})
        self.assertTrue(d["valid"])
        self.assertEqual(d["violations"], [])


class TestDependsOnLint(unittest.TestCase):
    """The IM-154 `depends_on` shape lint.

    A non-list `depends_on` reads as unmanaged, so a malformed edge fails
    no loud check on the authoring path. This lint is the out-of-band
    backstop.
    """

    def _with_dep(self, dep_block):
        # Splice a depends_on block in before the closing `---`. dep_block is
        # the text (already newline-terminated per line) to insert.
        return VALID_FRONTMATTER.replace(
            "changelog:\n---", "changelog:\n" + dep_block + "---"
        )

    def test_scalar_value_reported(self):
        text = self._with_dep("depends_on: garbage\n")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-depends-on-shape", codes(r))

    def test_entry_missing_to_reported(self):
        text = self._with_dep(
            "depends_on:\n  - type: blocks\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("dep-missing-to", codes(r))

    def test_type_outside_four_kinds_reported(self):
        text = self._with_dep(
            "depends_on:\n  - to: IM-101\n    type: bogus\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-dep-kind", codes(r))

    def test_malformed_to_id_reported(self):
        text = self._with_dep(
            "depends_on:\n  - to: not-an-id\n    type: blocks\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("malformed-dep-target", codes(r))

    def test_well_formed_block_not_reported(self):
        text = self._with_dep(
            "depends_on:\n  - to: IM-101\n    type: blocks\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_empty_inline_list_not_reported(self):
        text = self._with_dep("depends_on: []\n")
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_absent_key_not_reported(self):
        # The canonical VALID_FRONTMATTER has no depends_on at all.
        r = validate_text(Path("IM-001.md"), VALID_FRONTMATTER)
        dep_codes = [c for c in codes(r) if "dep" in c or "depends" in c]
        self.assertEqual(dep_codes, [])

    def test_bare_key_no_children_not_reported(self):
        # `depends_on:` with nothing under it parses to None -> valid.
        text = self._with_dep("depends_on:\n")
        r = validate_text(Path("IM-001.md"), text)
        dep_codes = [c for c in codes(r) if "dep" in c or "depends" in c]
        self.assertEqual(dep_codes, [])

    def test_cross_prefix_target_not_reported(self):
        # A foreign-prefix id (a non-IM prefix in an IM issue) is shape-valid; the lint
        # checks shape only, not store resolution.
        text = self._with_dep(
            "depends_on:\n  - to: RM-107\n    type: related\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_string_entry_kind_target_form_valid(self):
        # An inline flow-list string entry `KIND:TARGET` is shape-valid.
        text = self._with_dep("depends_on: [blocks:IM-101]\n")
        r = validate_text(Path("IM-001.md"), text)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_string_entry_without_colon_reported(self):
        # A string entry with no colon separator has no target -> missing-to.
        text = self._with_dep("depends_on: [IM-101]\n")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("dep-missing-to", codes(r))

    def test_string_entry_empty_target_reported(self):
        # `blocks:` -- colon present but empty tail -> missing-to.
        text = self._with_dep('depends_on: ["blocks:"]\n')
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("dep-missing-to", codes(r))

    def test_string_entry_bad_kind_reported(self):
        text = self._with_dep("depends_on: [bogus:IM-101]\n")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("invalid-dep-kind", codes(r))

    def test_string_entry_bad_target_reported(self):
        text = self._with_dep("depends_on: [blocks:not-an-id]\n")
        r = validate_text(Path("IM-001.md"), text)
        self.assertIn("malformed-dep-target", codes(r))

    def test_missing_to_with_bad_kind_reports_both(self):
        # A dict entry with a bad `type` and no `to` reports both codes.
        text = self._with_dep(
            "depends_on:\n  - type: bogus\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        seen = set(codes(r))
        self.assertIn("dep-missing-to", seen)
        self.assertIn("invalid-dep-kind", seen)

    def test_multiple_bad_entries_all_reported(self):
        text = self._with_dep(
            "depends_on:\n"
            "  - to: IM-101\n    type: bogus\n"
            "  - to: not-an-id\n    type: blocks\n"
        )
        r = validate_text(Path("IM-001.md"), text)
        seen = set(codes(r))
        self.assertIn("invalid-dep-kind", seen)
        self.assertIn("malformed-dep-target", seen)


class TestValidateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="im026-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_file_on_disk(self):
        p = self.tmp / "IM-001.md"
        p.write_text(VALID_FRONTMATTER, encoding="utf-8")
        r = validate_file(p)
        self.assertTrue(r.ok, f"violations: {r.violations!r}")

    def test_missing_file_raises_filenotfounderror(self):
        with self.assertRaises(FileNotFoundError):
            validate_file(self.tmp / "does-not-exist.md")


if __name__ == "__main__":
    unittest.main()
