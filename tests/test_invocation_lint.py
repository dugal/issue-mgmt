#!/usr/bin/env python3
"""The operational-doc invocation linter (IM-283).

`scripts/invocation_lint.py` resolves every prescribed `issue <verb>
--flag` / `make <target>` invocation in `docs/plan.md` and `CLAUDE.md`
against `docs/cli-surface.json` and the Makefile target list. These tests
are the gate: the first group asserts the live docs are clean, and the
rest inject one synthetic defect at a time and assert the lint goes red --
a check nobody has watched fail is a check nobody knows works (the IM-261
lesson). The gate-bites fixture re-introduces a removed flag into a
prescriptive line, per the IM-283 acceptance criteria.

The false-positive shapes each have a passing fixture because they are the
cost that decides whether a prose check is worth having at all: a fixture
that stops passing means the lint started biting narration, which is how a
guard like this gets deleted.

Run:
    python3 -m unittest tests.test_invocation_lint -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import invocation_lint  # noqa: E402


VERB_TABLE = invocation_lint.load_verb_table()
MAKE_TARGETS = invocation_lint.load_make_targets()


def lint(text):
    return invocation_lint.lint_text("fixture.md", text, VERB_TABLE,
                                     MAKE_TARGETS)


class LiveDocsTests(unittest.TestCase):
    """The committed operational docs pass the lint."""

    def test_live_docs_are_clean(self):
        findings = invocation_lint.lint_docs()
        self.assertEqual(
            [], [f.as_dict() for f in findings],
            "a prescribed invocation in the operational docs does not "
            "resolve against the surface manifest or the Makefile")

    def test_scope_is_the_two_operational_docs(self):
        # docs/releases.md and CHANGELOG.md are history by construction
        # and must stay out of scope (IM-283 scope decision).
        self.assertEqual(("docs/plan.md", "CLAUDE.md"),
                         invocation_lint.DOC_PATHS)


class GateBitesTests(unittest.TestCase):
    """Synthetic defects turn the lint red."""

    def test_removed_flag_on_prescriptive_line_is_a_finding(self):
        # The original IM-283 instance: `--force` was removed from
        # `issue admin init-store` in adbd376 and a plan.md instruction
        # kept prescribing it.
        findings = lint("Then run `issue admin init-store --store X "
                        "--force` to converge the store.")
        self.assertEqual(1, len(findings))
        self.assertIn("unknown flag --force", findings[0].detail)

    def test_unknown_verb_is_a_finding(self):
        findings = lint("Run `issue reindex-issues --all` once.")
        self.assertEqual(1, len(findings))
        self.assertIn("unknown verb", findings[0].detail)

    def test_unknown_admin_subverb_is_a_finding(self):
        findings = lint("Run `issue admin gc` weekly.")
        self.assertEqual(1, len(findings))
        self.assertIn("unknown admin subverb", findings[0].detail)

    def test_retired_make_target_is_a_finding(self):
        # The instance this issue's own adoption sweep found: plan.md
        # step 3 prescribed `make publish`, retired many schemas ago.
        findings = lint("`make publish` is a Release checklist item.")
        self.assertEqual(1, len(findings))
        self.assertIn("no such Makefile target", findings[0].detail)

    def test_fenced_block_line_is_checked(self):
        findings = lint("```bash\nissue list --nonexistent-flag\n```\n")
        self.assertEqual(1, len(findings))
        self.assertIn("unknown flag --nonexistent-flag",
                      findings[0].detail)

    def test_unknown_flag_in_compound_span_is_a_finding(self):
        findings = lint("`make test && issue ready --frobnicate`")
        self.assertEqual(1, len(findings))
        self.assertIn("unknown flag --frobnicate", findings[0].detail)


class HistoricalMentionTests(unittest.TestCase):
    """A qualified historical mention passes as narration."""

    def test_qualifier_passes_a_removed_flag(self):
        self.assertEqual([], lint(
            "the retired `issue admin init-store --force` flag exits 2"))

    def test_qualifier_passes_a_removed_make_target(self):
        self.assertEqual([], lint(
            "`make publish` was the schema-2 publish step"))

    def test_unqualified_line_still_fails(self):
        # The qualifier is per line: the same span without one is live.
        self.assertEqual(1, len(lint(
            "run `issue admin init-store --force` to converge")))


class FalsePositiveShapeTests(unittest.TestCase):
    """The shapes the adoption sweep measured must pass."""

    def test_prose_starting_with_make_is_not_a_candidate(self):
        self.assertEqual([], lint("make a check stop failing."))

    def test_placeholder_verb_passes(self):
        self.assertEqual([], lint(
            "routes `issue <verb>` / `issue history <subverb>` to a "
            "handler"))

    def test_placeholder_operand_passes(self):
        self.assertEqual([], lint(
            "run `issue show <id> --section <name>` to read one"))

    def test_slash_joined_verb_list_passes(self):
        self.assertEqual([], lint(
            "`issue start/complete/cancel/block/unblock` wrap it"))

    def test_slash_joined_flag_pair_passes(self):
        self.assertEqual([], lint(
            "wraps `issue edit --add-dep/--remove-dep` atomically"))

    def test_glob_make_target_passes(self):
        self.assertEqual([], lint("the three `make coverage-*` targets"))

    def test_make_variable_assignment_passes(self):
        self.assertEqual([], lint("`make release-prep VERSION=vX.Y.Z`"))

    def test_dispatcher_help_passes(self):
        self.assertEqual([], lint("```\nissue --help\n```\n"))

    def test_compound_span_resolves_each_piece(self):
        self.assertEqual([], lint(
            "`make coverage-unit && make coverage-black-box && "
            "make coverage-spec`"))

    def test_flag_value_and_operands_are_not_checked(self):
        self.assertEqual([], lint(
            "`issue edit IM-001 --set priority=high` then "
            "`issue admin validate .issues/IM-*.md`"))

    def test_bracketed_optional_flag_passes(self):
        self.assertEqual([], lint(
            "`issue transition X --claim C [--lease-ttl S]`"))


class GroundTruthTests(unittest.TestCase):
    """The lint reads committed ground truth, never a restated list."""

    def test_verb_table_comes_from_the_manifest(self):
        # A spot-check that the table carries real registrations: the
        # claimant synonym pair from IM-243 must be present on ready.
        ready = VERB_TABLE[("tier1", "ready")]
        self.assertIn("--actor", ready)
        self.assertIn("--claim", ready)

    def test_make_targets_come_from_the_makefile(self):
        self.assertIn("release", MAKE_TARGETS)
        self.assertIn("surface-check", MAKE_TARGETS)
        self.assertNotIn("publish", MAKE_TARGETS)
        self.assertNotIn("distribute", MAKE_TARGETS)

    def test_missing_manifest_is_a_lint_error(self):
        with self.assertRaises(invocation_lint.LintError):
            invocation_lint.load_verb_table(Path("/nonexistent/x.json"))

    def test_missing_makefile_is_a_lint_error(self):
        with self.assertRaises(invocation_lint.LintError):
            invocation_lint.load_make_targets(Path("/nonexistent/Makefile"))


if __name__ == "__main__":
    unittest.main()
