#!/usr/bin/env python3
"""Unit tests for the shared `--prefix` scope resolver (IM-236).

`common.resolve_prefix_scope` is the one place the nine verbs carrying
`--prefix` decide what a scope means. Five verbs each had their own copy
through v3.4.x, and the copies disagreed about the value space, the shape
gate, the order of case-folding versus refusing an absent scope, and the
unresolvable-input fallback -- so these tests pin the resolver's decision
table rather than any one verb's wiring.

The per-verb wiring is exercised end-to-end in
tests/verification/issue-index.sh, issue-list.sh, issue-ready.sh,
search-issues.sh, activity-issues.sh, search-history.sh and im-098-lease.sh.

Framework note: stdlib `unittest`, because the project ships no
third-party packages and `python3 -m unittest discover tests/` is the one
test entry point.

Run:
    python3 -m unittest tests.test_cli_common_prefix_scope -v
"""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import errors  # noqa: E402
from cli import common  # noqa: E402


class PrefixScopeBase(unittest.TestCase):
    """A store carrying the IM and DOUG prefix directories."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for prefix in ("IM", "DOUG"):
            (self.store / prefix).mkdir()
            (self.store / prefix / f"{prefix}-001.md").write_text(
                "---\nid: %s-001\n---\n" % prefix, encoding="utf-8")

    def resolve(self, raw, **kw):
        """Resolve `raw`, returning (prefixes, rc, stderr)."""
        err = StringIO()
        with redirect_stderr(err):
            prefixes, rc = common.resolve_prefix_scope(
                raw, self.store, None, prog="issue list", **kw)
        return prefixes, rc, err.getvalue()


class TestAbsentFlag(PrefixScopeBase):
    def test_none_resolves_to_an_unscoped_read(self):
        prefixes, rc, _ = self.resolve(None)
        self.assertIsNone(prefixes)
        self.assertIsNone(rc)

    def test_none_is_silent(self):
        _, _, err = self.resolve(None)
        self.assertEqual("", err)


class TestValueSpace(PrefixScopeBase):
    """One value space on every verb: repeatable AND comma-separated."""

    def test_single_value_resolves_to_one_prefix(self):
        prefixes, rc, _ = self.resolve(["IM"])
        self.assertEqual({"IM"}, prefixes)
        self.assertIsNone(rc)

    def test_comma_list_resolves_to_every_named_prefix(self):
        prefixes, _, _ = self.resolve(["IM,DOUG"])
        self.assertEqual({"IM", "DOUG"}, prefixes)

    def test_repeated_occurrences_accumulate(self):
        # The IM-269 failure mode on the other axis: last-wins would
        # return {"DOUG"} here, with exit 0 -- a plausible shorter answer.
        prefixes, _, _ = self.resolve(["IM", "DOUG"])
        self.assertEqual({"IM", "DOUG"}, prefixes)

    def test_mixing_both_forms_resolves_to_the_union(self):
        prefixes, _, _ = self.resolve(["IM", "DOUG,IM"])
        self.assertEqual({"IM", "DOUG"}, prefixes)

    def test_a_bare_string_is_accepted_for_callers_that_pass_one(self):
        # parse_csv_set takes either shape, so a verb registering --prefix
        # without the appending action still resolves.
        prefixes, _, _ = self.resolve("IM,DOUG")
        self.assertEqual({"IM", "DOUG"}, prefixes)

    def test_surrounding_whitespace_is_trimmed(self):
        prefixes, _, _ = self.resolve([" IM , DOUG "])
        self.assertEqual({"IM", "DOUG"}, prefixes)


class TestCaseFolding(PrefixScopeBase):
    def test_lowercase_resolves_to_the_canonical_prefix(self):
        prefixes, rc, _ = self.resolve(["im"])
        self.assertEqual({"IM"}, prefixes)
        self.assertIsNone(rc)

    def test_a_changed_value_is_announced_on_stderr(self):
        _, _, err = self.resolve(["im"])
        self.assertIn("issue list: resolved im -> IM", err)

    def test_an_already_canonical_value_is_silent(self):
        # The announcement reports a change, so a caller that typed the
        # canonical form sees nothing (`--json` stdout purity depends on
        # the note being on stderr, and on it not firing needlessly).
        _, _, err = self.resolve(["IM"])
        self.assertEqual("", err)

    def test_each_entry_in_a_list_folds_independently(self):
        prefixes, _, _ = self.resolve(["im,DOUG"])
        self.assertEqual({"IM", "DOUG"}, prefixes)


class TestAbsentPrefixRefused(PrefixScopeBase):
    """IM-238: a scope naming nothing is an argument error, not a zero result."""

    def test_unknown_prefix_is_a_usage_error(self):
        prefixes, rc, _ = self.resolve(["ZZZ"])
        self.assertEqual(errors.EXIT_USAGE, rc)
        self.assertIsNone(prefixes)

    def test_unknown_prefix_payload_names_the_value_and_the_known_set(self):
        _, _, err = self.resolve(["ZZZ"])
        self.assertIn("error=unknown-prefix", err)
        self.assertIn("unknown=ZZZ", err)
        self.assertIn("known=DOUG,IM", err)

    def test_one_unknown_entry_in_a_list_refuses_the_whole_scope(self):
        prefixes, rc, _ = self.resolve(["IM,ZZZ"])
        self.assertEqual(errors.EXIT_USAGE, rc)
        self.assertIsNone(prefixes)

    def test_a_malformed_token_reports_through_the_same_payload(self):
        # A shape gate would be a second error text for the same
        # condition: a token that cannot be a prefix is not a prefix this
        # store has. Four verbs carried one, two carried none, and the
        # reaper matched against a prefix-directory regex.
        _, rc, err = self.resolve(["1x"])
        self.assertEqual(errors.EXIT_USAGE, rc)
        self.assertIn("error=unknown-prefix", err)

    def test_the_diagnostic_names_what_the_caller_typed(self):
        # Enforce runs BEFORE canonicalizing. The history verbs folded
        # first, so an unresolvable lowercase value was reported in a
        # casing the caller never wrote.
        _, _, err = self.resolve(["zzz"])
        self.assertIn("unknown=zzz", err)


class TestEmptyScopeRefused(PrefixScopeBase):
    """A flag given but empty is a usage error, never accept-all."""

    def test_empty_string_is_a_usage_error(self):
        prefixes, rc, _ = self.resolve([""])
        self.assertEqual(errors.EXIT_USAGE, rc)
        self.assertIsNone(prefixes)

    def test_separators_only_is_a_usage_error(self):
        _, rc, _ = self.resolve([","])
        self.assertEqual(errors.EXIT_USAGE, rc)

    def test_payload_carries_a_branchable_reason(self):
        _, _, err = self.resolve([""])
        self.assertIn("error=usage-error", err)
        self.assertIn("reason=empty-prefix-scope", err)

    def test_payload_shows_the_raw_value_not_its_list_wrapper(self):
        # --prefix appends, so the raw value reaching the resolver is a
        # list; a caller reading the diagnostic should see what they typed.
        _, _, err = self.resolve([","])
        self.assertIn("','", err)
        self.assertNotIn("[", err)


class TestJsonMode(PrefixScopeBase):
    def test_unknown_prefix_emits_one_parseable_object(self):
        _, _, err = self.resolve(["ZZZ"], json_mode=True)
        payload = json.loads(err.strip())
        self.assertEqual("unknown-prefix", payload["error"])

    def test_empty_scope_emits_one_parseable_object(self):
        _, _, err = self.resolve([""], json_mode=True)
        payload = json.loads(err.strip())
        self.assertEqual("empty-prefix-scope", payload["reason"])

    def test_json_payload_names_the_calling_verb(self):
        _, _, err = self.resolve([""], json_mode=True)
        self.assertEqual("issue list", json.loads(err.strip())["tool"])


class TestLegacyMode(unittest.TestCase):
    """No store root means no registry to validate a scope against."""

    def test_unknown_prefix_passes_through_unresolved(self):
        err = StringIO()
        with redirect_stderr(err):
            prefixes, rc = common.resolve_prefix_scope(
                ["zz"], None, None, prog="issue list")
        self.assertEqual({"zz"}, prefixes)
        self.assertIsNone(rc)

    def test_an_empty_scope_is_still_refused(self):
        # The refusal is argv-shaped, so it does not depend on a store.
        err = StringIO()
        with redirect_stderr(err):
            _, rc = common.resolve_prefix_scope(
                [""], None, None, prog="issue list")
        self.assertEqual(errors.EXIT_USAGE, rc)
        self.assertIn("reason=empty-prefix-scope", err.getvalue())


if __name__ == "__main__":
    unittest.main()
