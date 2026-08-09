#!/usr/bin/env python3
"""Unit tests for lib/jsonfields.py.

jsonfields owns the gh-style `--json <fields>` projection shared by every
read verb: the parse of `args.json_mode` into an ordered field list, strict
validation against a per-verb valid-field set, and the flat projection of a
per-object dict (or a list of them) to those fields in caller order.

All functions are pure (operate on the parsed `json_mode` value and plain
dicts), so these tests need no filesystem or CLI fixtures. End-to-end verb
wiring (exit codes, per-verb valid sets, wrapper-metadata preservation) lives
in tests/verification/*.sh.

Run:
    python3 -m unittest tests.test_jsonfields -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import jsonfields  # noqa: E402  -- path set above


VALID = ("id", "file", "title", "status", "priority", "type", "body_matches")


class TestJsonEnabled(unittest.TestCase):
    def test_none_is_disabled(self):
        # Flag absent -> not JSON mode.
        self.assertFalse(jsonfields.json_enabled(None))

    def test_bare_true_is_enabled(self):
        # Bare --json (const True) -> JSON mode.
        self.assertTrue(jsonfields.json_enabled(True))

    def test_field_string_is_enabled(self):
        self.assertTrue(jsonfields.json_enabled("id,title"))

    def test_empty_string_is_enabled(self):
        # --json '' is still "requested" -- it must reach parse_fields (which
        # rejects it) rather than silently falling back to text output.
        self.assertTrue(jsonfields.json_enabled(""))


class TestParseFields(unittest.TestCase):
    def test_none_returns_none_full_shape(self):
        self.assertIsNone(jsonfields.parse_fields(None, VALID, prog="t"))

    def test_bare_true_returns_none_full_shape(self):
        self.assertIsNone(jsonfields.parse_fields(True, VALID, prog="t"))

    def test_single_field(self):
        self.assertEqual(jsonfields.parse_fields("id", VALID, prog="t"), ["id"])

    def test_multiple_fields_preserve_caller_order(self):
        # Order follows the caller, NOT the valid-set order.
        self.assertEqual(
            jsonfields.parse_fields("title,id,status", VALID, prog="t"),
            ["title", "id", "status"],
        )

    def test_whitespace_around_names_is_stripped(self):
        self.assertEqual(
            jsonfields.parse_fields(" id , title ", VALID, prog="t"),
            ["id", "title"],
        )

    def test_duplicates_are_deduped_keeping_first(self):
        self.assertEqual(
            jsonfields.parse_fields("id,title,id", VALID, prog="t"),
            ["id", "title"],
        )

    def test_unknown_field_raises(self):
        with self.assertRaises(jsonfields.FieldSelectionError):
            jsonfields.parse_fields("id,bogus", VALID, prog="t")

    def test_unknown_field_message_names_field_and_valid_set(self):
        try:
            jsonfields.parse_fields("bogus", VALID, prog="issue list")
        except jsonfields.FieldSelectionError as e:
            self.assertIn("bogus", e.message)
            self.assertIn("issue list", e.message)
            self.assertIn("id", e.message)
        else:
            self.fail("expected FieldSelectionError")

    def test_empty_string_raises(self):
        with self.assertRaises(jsonfields.FieldSelectionError):
            jsonfields.parse_fields("", VALID, prog="t")

    def test_only_commas_raises_empty(self):
        with self.assertRaises(jsonfields.FieldSelectionError):
            jsonfields.parse_fields(",,", VALID, prog="t")

    def test_only_whitespace_raises_empty(self):
        with self.assertRaises(jsonfields.FieldSelectionError):
            jsonfields.parse_fields("   ", VALID, prog="t")


class TestProjectObject(unittest.TestCase):
    def setUp(self):
        self.obj = {
            "id": "IM-001",
            "file": "IM/IM-001.md",
            "title": "T",
            "status": "open",
        }

    def test_none_fields_returns_object_unchanged(self):
        self.assertIs(jsonfields.project_object(self.obj, None), self.obj)

    def test_projects_requested_fields(self):
        self.assertEqual(
            jsonfields.project_object(self.obj, ["id", "status"]),
            {"id": "IM-001", "status": "open"},
        )

    def test_output_order_follows_fields_not_object(self):
        # object key order is id/file/title/status; ask for the reverse.
        result = jsonfields.project_object(self.obj, ["status", "id"])
        self.assertEqual(list(result.keys()), ["status", "id"])

    def test_valid_but_absent_field_is_omitted(self):
        # `priority` is not present on this particular object -> omitted, not
        # emitted as null (the shapes are conditional for some verbs).
        self.assertEqual(
            jsonfields.project_object(self.obj, ["id", "priority"]),
            {"id": "IM-001"},
        )

    def test_projection_does_not_mutate_source(self):
        jsonfields.project_object(self.obj, ["id"])
        self.assertEqual(set(self.obj), {"id", "file", "title", "status"})


class TestProjectList(unittest.TestCase):
    def test_none_fields_returns_list_copy(self):
        objs = [{"id": "IM-001"}, {"id": "IM-002"}]
        out = jsonfields.project_list(objs, None)
        self.assertEqual(out, objs)

    def test_projects_each_element(self):
        objs = [
            {"id": "IM-001", "title": "A", "status": "open"},
            {"id": "IM-002", "title": "B", "status": "done"},
        ]
        self.assertEqual(
            jsonfields.project_list(objs, ["id", "title"]),
            [{"id": "IM-001", "title": "A"}, {"id": "IM-002", "title": "B"}],
        )

    def test_accepts_a_generator(self):
        gen = (o for o in [{"id": "IM-001", "x": 1}])
        self.assertEqual(
            jsonfields.project_list(gen, ["id"]), [{"id": "IM-001"}]
        )

    def test_show_multiid_mixed_success_projection(self):
        # `issue show` batch: a success object and an error object share the
        # same projection. `error` is a valid show field; requesting id,error
        # keeps the error on the failed element and drops it from the success
        # element (which has no error key).
        results = [
            {"id": "IM-001", "file": "IM/IM-001.md", "full_text": "body"},
            {"id": "IM-999", "file": None, "store": "/s", "error": "not found"},
        ]
        self.assertEqual(
            jsonfields.project_list(results, ["id", "error"]),
            [{"id": "IM-001"}, {"id": "IM-999", "error": "not found"}],
        )


if __name__ == "__main__":
    unittest.main()
