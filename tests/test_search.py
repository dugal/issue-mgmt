#!/usr/bin/env python3
"""Unit tests for lib/search.py.

Covers the in-process surface:
  - SubstringMatcher (case-insensitive default, --case-sensitive, line numbers)
  - RegexMatcher (re.search semantics, bad-regex ValueError, case-sensitivity)
  - NoBodyFilter (selected on empty query; is_filter=False)
  - build_matcher dispatch + unknown-syntax error
  - parse_csv_set (None, empty, whitespace, dedup)
  - FrontmatterFilter.accepts (prefix / status / type, AND-of-fields)
  - enumerate_issue_files: store mode (multi-prefix dirs, prefix allowlist,
    skips INDEX.md and non-prefix dirs), legacy mode (single anchor,
    prefix filter by stem, skips INDEX.md + ISSUE-TEMPLATE.md)
  - match_issue (body + frontmatter combine, malformed-frontmatter skip)
  - search() integration (scan order, body_matches populated)
  - BodyMatch / IssueMatch to_dict shape

End-to-end CLI behavior (exit codes, --help, --json, perf timing)
lives in tests/verification/search-issues.sh.

Run:
    python3 -m unittest tests.test_search -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import search  # noqa: E402  -- path set above


def write_issue(path, fm, body=""):
    """Write a minimal valid issue file with the given frontmatter dict."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---\n"]
    for key, value in fm.items():
        lines.append(f"{key}: {value}\n")
    lines.append("---\n\n")
    lines.append(body if body.endswith("\n") or body == "" else body + "\n")
    path.write_text("".join(lines), encoding="utf-8")


def standard_fm(id_, **overrides):
    fm = {
        "id": id_,
        "title": f'"Issue {id_}"',
        "type": "feature",
        "status": "open",
        "priority": "medium",
        "created": "2026-05-14",
        "updated": "2026-05-14",
        "branch": '""',
        "tags": "[]",
    }
    fm.update(overrides)
    return fm


# ----------------------------------------------------------------------
# SubstringMatcher
# ----------------------------------------------------------------------


class TestSubstringMatcher(unittest.TestCase):
    def test_matches_case_insensitive_by_default(self):
        m = search.SubstringMatcher("concurrent")
        hits = m.search("a Concurrent thing\nNo match\nCONCURRENT again")
        self.assertEqual([h.line for h in hits], [1, 3])

    def test_case_sensitive_flag_respects_case(self):
        m = search.SubstringMatcher("concurrent", case_sensitive=True)
        hits = m.search("Concurrent line\nconcurrent line\nCONCURRENT line")
        self.assertEqual([h.line for h in hits], [2])

    def test_no_matches_returns_empty_list(self):
        m = search.SubstringMatcher("xyzzy")
        self.assertEqual(m.search("no such thing here"), [])

    def test_records_full_line_text(self):
        m = search.SubstringMatcher("foo")
        hits = m.search("the foo is here\nbar")
        self.assertEqual(hits[0].text, "the foo is here")

    def test_metacharacters_are_literal(self):
        m = search.SubstringMatcher(".")
        # only lines containing literal "." should match
        hits = m.search("abc\nfoo.bar\nbaz")
        self.assertEqual([h.line for h in hits], [2])

    def test_is_filter_is_true(self):
        self.assertTrue(search.SubstringMatcher("x").is_filter)


# ----------------------------------------------------------------------
# RegexMatcher
# ----------------------------------------------------------------------


class TestRegexMatcher(unittest.TestCase):
    def test_metacharacters_apply(self):
        m = search.RegexMatcher(r"f.o")
        hits = m.search("foo\nfxo\nbar")
        self.assertEqual([h.line for h in hits], [1, 2])

    def test_anchored_word(self):
        m = search.RegexMatcher(r"\bTODO\b")
        hits = m.search("TODO: fix\nTODOPHILE is not\nfix TODO now")
        self.assertEqual([h.line for h in hits], [1, 3])

    def test_case_insensitive_by_default(self):
        m = search.RegexMatcher(r"todo")
        self.assertEqual(len(m.search("TODO\nTodo\ntodo")), 3)

    def test_case_sensitive_flag(self):
        m = search.RegexMatcher(r"todo", case_sensitive=True)
        self.assertEqual(len(m.search("TODO\nTodo\ntodo")), 1)

    def test_invalid_regex_raises_value_error(self):
        with self.assertRaises(ValueError):
            search.RegexMatcher(r"[unterminated")

    def test_is_filter_is_true(self):
        self.assertTrue(search.RegexMatcher("x").is_filter)


# ----------------------------------------------------------------------
# NoBodyFilter
# ----------------------------------------------------------------------


class TestNoBodyFilter(unittest.TestCase):
    def test_is_filter_is_false(self):
        self.assertFalse(search.NoBodyFilter().is_filter)


# ----------------------------------------------------------------------
# build_matcher
# ----------------------------------------------------------------------


class TestBuildMatcher(unittest.TestCase):
    def test_empty_query_returns_no_body_filter(self):
        m = search.build_matcher("substring", "")
        self.assertIsInstance(m, search.NoBodyFilter)

    def test_empty_query_returns_no_body_filter_even_for_regex(self):
        m = search.build_matcher("regex", "")
        self.assertIsInstance(m, search.NoBodyFilter)

    def test_substring_default(self):
        m = search.build_matcher("substring", "x")
        self.assertIsInstance(m, search.SubstringMatcher)

    def test_regex_dispatch(self):
        m = search.build_matcher("regex", "x")
        self.assertIsInstance(m, search.RegexMatcher)

    def test_unknown_syntax_raises_value_error(self):
        with self.assertRaises(ValueError):
            search.build_matcher("lucene", "x")

    def test_case_sensitive_passes_through(self):
        m = search.build_matcher("substring", "X", case_sensitive=True)
        self.assertTrue(m.case_sensitive)


# ----------------------------------------------------------------------
# parse_csv_set
# ----------------------------------------------------------------------


class TestParseCsvSet(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(search.parse_csv_set(None))

    def test_single_value(self):
        self.assertEqual(search.parse_csv_set("open"), {"open"})

    def test_comma_separated(self):
        self.assertEqual(search.parse_csv_set("open,blocked"), {"open", "blocked"})

    def test_whitespace_trimmed(self):
        self.assertEqual(search.parse_csv_set("open , blocked "), {"open", "blocked"})

    def test_empty_string_returns_none(self):
        self.assertIsNone(search.parse_csv_set(""))

    def test_only_whitespace_returns_none(self):
        self.assertIsNone(search.parse_csv_set("  ,  ,  "))

    def test_duplicates_deduped(self):
        self.assertEqual(search.parse_csv_set("a,a,b"), {"a", "b"})


# ----------------------------------------------------------------------
# FrontmatterFilter.accepts
# ----------------------------------------------------------------------


class TestFrontmatterFilter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, **fm):
        p = self.tmp / "IM-001.md"
        write_issue(p, standard_fm("IM-001", **fm))
        import frontmatter as fm_mod
        return fm_mod.read_issue(p)[0]

    def test_accept_all_when_filters_none(self):
        fm_lines = self._make()
        ff = search.FrontmatterFilter()
        self.assertTrue(ff.accepts(fm_lines, "IM-001"))

    def test_prefix_match(self):
        fm_lines = self._make()
        ff = search.FrontmatterFilter(prefixes={"IM"})
        self.assertTrue(ff.accepts(fm_lines, "IM-001"))

    def test_prefix_miss(self):
        fm_lines = self._make()
        ff = search.FrontmatterFilter(prefixes={"DOUG"})
        self.assertFalse(ff.accepts(fm_lines, "IM-001"))

    def test_status_match(self):
        fm_lines = self._make(status="blocked")
        ff = search.FrontmatterFilter(statuses={"blocked"})
        self.assertTrue(ff.accepts(fm_lines, "IM-001"))

    def test_status_miss(self):
        fm_lines = self._make(status="open")
        ff = search.FrontmatterFilter(statuses={"done"})
        self.assertFalse(ff.accepts(fm_lines, "IM-001"))

    def test_type_match(self):
        fm_lines = self._make(type="bug")
        ff = search.FrontmatterFilter(types={"bug"})
        self.assertTrue(ff.accepts(fm_lines, "IM-001"))

    def test_type_miss(self):
        fm_lines = self._make(type="feature")
        ff = search.FrontmatterFilter(types={"bug"})
        self.assertFalse(ff.accepts(fm_lines, "IM-001"))

    def test_and_across_fields_all_pass(self):
        fm_lines = self._make(status="open", type="bug")
        ff = search.FrontmatterFilter(
            prefixes={"IM"}, statuses={"open"}, types={"bug"}
        )
        self.assertTrue(ff.accepts(fm_lines, "IM-001"))

    def test_and_across_fields_one_fails(self):
        fm_lines = self._make(status="open", type="bug")
        ff = search.FrontmatterFilter(
            prefixes={"IM"}, statuses={"open"}, types={"feature"}
        )
        self.assertFalse(ff.accepts(fm_lines, "IM-001"))


# ----------------------------------------------------------------------
# enumerate_issue_files -- store mode
# ----------------------------------------------------------------------


class TestEnumerateStoreMode(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp())
        for pfx, n in [("IM", 2), ("DOUG", 1), ("RM", 1)]:
            for i in range(1, n + 1):
                p = self.store / pfx / f"{pfx}-{i:03d}.md"
                write_issue(p, standard_fm(f"{pfx}-{i:03d}"))
        # Decoys
        (self.store / "INDEX.md").write_text("decoy", encoding="utf-8")
        (self.store / "IM" / "INDEX.md").write_text("decoy", encoding="utf-8")
        (self.store / ".repos").write_text("decoy", encoding="utf-8")
        (self.store / "lowercase").mkdir()  # non-prefix dir
        (self.store / "lowercase" / "x-001.md").write_text(
            "decoy", encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.store, ignore_errors=True)

    def test_lists_all_prefix_dirs_when_no_filter(self):
        files = list(search.enumerate_issue_files(self.store, None))
        ids = sorted(f.stem for f in files)
        self.assertEqual(ids, ["DOUG-001", "IM-001", "IM-002", "RM-001"])

    def test_prefix_filter_scopes_scan(self):
        files = list(search.enumerate_issue_files(self.store, None, {"IM", "DOUG"}))
        ids = sorted(f.stem for f in files)
        self.assertEqual(ids, ["DOUG-001", "IM-001", "IM-002"])

    def test_skips_non_prefix_dirs(self):
        files = list(search.enumerate_issue_files(self.store, None))
        self.assertFalse(any("lowercase" in str(f) for f in files))

    def test_skips_index_md_in_prefix_dirs(self):
        files = list(search.enumerate_issue_files(self.store, None))
        self.assertFalse(any(f.name == "INDEX.md" for f in files))

    def test_missing_store_root_yields_nothing(self):
        gone = self.store / "does-not-exist"
        self.assertEqual(list(search.enumerate_issue_files(gone, None)), [])


# ----------------------------------------------------------------------
# enumerate_issue_files -- legacy mode
# ----------------------------------------------------------------------


class TestEnumerateLegacyMode(unittest.TestCase):
    def setUp(self):
        self.anchor = Path(tempfile.mkdtemp())
        for stem in ["IM-001", "IM-002", "DOUG-001"]:
            write_issue(self.anchor / f"{stem}.md", standard_fm(stem))
        (self.anchor / "INDEX.md").write_text("decoy", encoding="utf-8")
        (self.anchor / "ISSUE-TEMPLATE.md").write_text("decoy", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.anchor, ignore_errors=True)

    def test_lists_all_md_files_when_no_filter(self):
        files = list(search.enumerate_issue_files(None, self.anchor))
        ids = sorted(f.stem for f in files)
        self.assertEqual(ids, ["DOUG-001", "IM-001", "IM-002"])

    def test_prefix_filter_by_stem(self):
        files = list(search.enumerate_issue_files(None, self.anchor, {"IM"}))
        ids = sorted(f.stem for f in files)
        self.assertEqual(ids, ["IM-001", "IM-002"])

    def test_skips_template_and_index(self):
        files = list(search.enumerate_issue_files(None, self.anchor))
        self.assertFalse(any(f.name == "INDEX.md" for f in files))
        self.assertFalse(any(f.name == "ISSUE-TEMPLATE.md" for f in files))

    def test_missing_anchor_yields_nothing(self):
        gone = self.anchor / "does-not-exist"
        self.assertEqual(list(search.enumerate_issue_files(None, gone)), [])


# ----------------------------------------------------------------------
# match_issue + search()
# ----------------------------------------------------------------------


class TestMatchAndSearch(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp())
        write_issue(
            self.store / "IM" / "IM-001.md",
            standard_fm("IM-001", type="bug"),
            "## Summary\n\nA concurrent edit happens here.\n",
        )
        write_issue(
            self.store / "IM" / "IM-002.md",
            standard_fm("IM-002", status="done"),
            "## Summary\n\nUnrelated text.\n",
        )
        write_issue(
            self.store / "DOUG" / "DOUG-001.md",
            standard_fm("DOUG-001", type="bug", status="blocked"),
            "## Summary\n\nAnother concurrent thing.\n",
        )

    def tearDown(self):
        shutil.rmtree(self.store, ignore_errors=True)

    def test_body_filter_only(self):
        matcher = search.build_matcher("substring", "concurrent")
        ff = search.FrontmatterFilter()
        results = search.search(self.store, None, matcher, ff)
        self.assertEqual([m.id for m in results], ["DOUG-001", "IM-001"])
        self.assertTrue(all(len(m.body_matches) > 0 for m in results))

    def test_frontmatter_only(self):
        matcher = search.build_matcher("substring", "")
        ff = search.FrontmatterFilter(types={"bug"})
        results = search.search(self.store, None, matcher, ff)
        self.assertEqual([m.id for m in results], ["DOUG-001", "IM-001"])
        # No body_matches because matcher is no-op
        self.assertTrue(all(m.body_matches == [] for m in results))

    def test_body_and_frontmatter_and(self):
        matcher = search.build_matcher("substring", "concurrent")
        ff = search.FrontmatterFilter(statuses={"open"})
        results = search.search(self.store, None, matcher, ff)
        self.assertEqual([m.id for m in results], ["IM-001"])

    def test_prefix_filter_scopes_scan(self):
        matcher = search.build_matcher("substring", "concurrent")
        ff = search.FrontmatterFilter(prefixes={"IM"})
        results = search.search(self.store, None, matcher, ff)
        self.assertEqual([m.id for m in results], ["IM-001"])

    def test_no_match_returns_empty(self):
        matcher = search.build_matcher("substring", "xyzzy")
        ff = search.FrontmatterFilter()
        self.assertEqual(search.search(self.store, None, matcher, ff), [])

    def test_malformed_frontmatter_is_skipped(self):
        bad = self.store / "IM" / "IM-003.md"
        bad.write_text("no frontmatter here at all", encoding="utf-8")
        matcher = search.build_matcher("substring", "")
        ff = search.FrontmatterFilter()
        results = search.search(self.store, None, matcher, ff)
        # Three valid files; the bad one is silently dropped
        self.assertEqual([m.id for m in results], ["DOUG-001", "IM-001", "IM-002"])

    def test_match_issue_populates_all_fields(self):
        matcher = search.build_matcher("substring", "concurrent")
        ff = search.FrontmatterFilter()
        path = self.store / "IM" / "IM-001.md"
        m = search.match_issue(path, matcher, ff)
        self.assertIsNotNone(m)
        self.assertEqual(m.id, "IM-001")
        self.assertEqual(m.status, "open")
        self.assertEqual(m.type, "bug")
        self.assertEqual(m.priority, "medium")
        self.assertEqual(m.title, "Issue IM-001")
        # Body match line is reported file-absolute. Verify by reading
        # that line out of the file and confirming it contains the query.
        file_lines = path.read_text(encoding="utf-8").splitlines()
        for bm in m.body_matches:
            self.assertIn("concurrent", file_lines[bm.line - 1])


class TestTitleSearch(unittest.TestCase):
    """The free-text query matches the title, not just the body (IM-232)."""

    def setUp(self):
        self.store = Path(tempfile.mkdtemp())
        # Term lives in the title; body has none of it -- the common
        # just-created, empty-body case.
        write_issue(
            self.store / "MYP" / "MYP-001.md",
            standard_fm("MYP-001", title='"add --dry-run flag"'),
            "## Summary\n\n## Implementation Notes\n",
        )
        # Term lives in the body; the title does not carry it.
        write_issue(
            self.store / "MYP" / "MYP-002.md",
            standard_fm("MYP-002"),
            "## Summary\n\nlisten on the socket.\n",
        )

    def tearDown(self):
        shutil.rmtree(self.store, ignore_errors=True)

    def _search(self, query, syntax="substring"):
        matcher = search.build_matcher(syntax, query)
        return search.search(
            self.store, None, matcher, search.FrontmatterFilter()
        )

    def test_title_only_term_is_found(self):
        results = self._search("dry-run")
        self.assertEqual([m.id for m in results], ["MYP-001"])

    def test_title_match_sets_flag_with_no_body_matches(self):
        results = self._search("flag")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].title_match)
        self.assertEqual(results[0].body_matches, [])

    def test_body_match_leaves_title_match_false(self):
        results = self._search("socket")
        self.assertEqual([m.id for m in results], ["MYP-002"])
        self.assertFalse(results[0].title_match)
        self.assertTrue(len(results[0].body_matches) > 0)

    def test_title_match_is_case_insensitive_by_default(self):
        results = self._search("DRY-RUN")
        self.assertEqual([m.id for m in results], ["MYP-001"])

    def test_regex_syntax_matches_title(self):
        results = self._search(r"dry-\w+", syntax="regex")
        self.assertEqual([m.id for m in results], ["MYP-001"])

    def test_absent_term_matches_neither_title_nor_body(self):
        self.assertEqual(self._search("zzznotpresent"), [])

    def test_empty_query_never_sets_title_match(self):
        matcher = search.build_matcher("substring", "")
        results = search.search(
            self.store, None, matcher, search.FrontmatterFilter()
        )
        self.assertTrue(all(not m.title_match for m in results))


# ----------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------


class TestSerialization(unittest.TestCase):
    def test_body_match_to_dict(self):
        bm = search.BodyMatch(line=7, text="hi")
        self.assertEqual(bm.to_dict(), {"line": 7, "text": "hi"})

    def test_issue_match_to_dict_shape(self):
        im = search.IssueMatch(
            id="IM-001",
            file=Path("/tmp/IM-001.md"),
            title="T",
            status="open",
            priority="high",
            type="bug",
            body_matches=[search.BodyMatch(line=1, text="x")],
            title_match=True,
        )
        d = im.to_dict()
        self.assertEqual(set(d.keys()), {
            "id", "file", "title", "status", "priority", "type",
            "title_match", "body_matches",
        })
        self.assertEqual(d["file"], "/tmp/IM-001.md")
        self.assertEqual(d["title_match"], True)
        self.assertEqual(d["body_matches"], [{"line": 1, "text": "x"}])

    def test_issue_match_default_body_matches_empty(self):
        im = search.IssueMatch(
            id="IM-001", file=Path("/x"), title="t",
            status="open", priority="low", type="bug",
        )
        self.assertEqual(im.body_matches, [])


if __name__ == "__main__":
    unittest.main()
