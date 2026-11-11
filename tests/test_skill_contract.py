#!/usr/bin/env python3
"""Shipped skills agree with the CLI they drive (IM-265).

`scripts/check-skill-contract` is the condition-level half of the shipped-skill
guard. The IM-228 guard (`tests/verification/skill-exit-codes.sh`) checks the
exit-code CEILING -- no skill may name a code outside the contract -- and that
turned out to be necessary but not sufficient: when IM-238 flipped the
enumeration reads to exit 1 on a zero result, two shipped history skills kept
telling the agent 0, and the guard stayed green because 0 is inside the
contract. Both skills read exit 1 as "store misconfigured", so a normal empty
search was reported to the user as a broken store.

These tests put the checker on the `make test` path (the script itself runs
under the verification suite) and pin the three derivations it depends on, so
the checker cannot quietly become vacuous:

  * the zero-result code comes from the verb's CLASS in the manifest,
  * `concurrent-edit` membership comes from `lib/errors.py`,
  * a documented flag is checked against the verb it is declared passed to.

The gate-bites negative cases are exercised against synthetic skill trees in a
temp directory rather than by editing the real shipped skills.

Run:
    python3 -m unittest tests.test_skill_contract -v
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check-skill-contract"


def load_checker(repo_root=None):
    """Import the checker as a module, optionally rooted at a fixture tree."""
    spec = importlib.util.spec_from_loader(
        "check_skill_contract",
        importlib.machinery.SourceFileLoader(
            "check_skill_contract", str(CHECKER)
        ),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if repo_root is not None:
        module.REPO_ROOT = str(repo_root)
        module.SKILLS_DIR = os.path.join(str(repo_root), ".claude", "skills")
        module.MANIFEST = os.path.join(
            str(repo_root), "docs", "cli-surface.json"
        )
        module.ERRORS_PY = os.path.join(str(repo_root), "lib", "errors.py")
    return module


class TestRealTreePasses(unittest.TestCase):
    def test_checker_exits_zero_on_the_shipped_skills(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            result.returncode, 0,
            "shipped skills disagree with the CLI:\n%s%s"
            % (result.stdout, result.stderr),
        )

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("check-skill-contract", result.stdout)

    def test_every_shipped_skill_resolves_to_a_verb(self):
        """No skill may be silently unchecked for want of a resolved verb."""
        module = load_checker()
        problems, rows = module.check()
        self.assertEqual(problems, [])
        names = sorted(os.path.basename(p).replace("-dev", "")
                       for p in os.listdir(module.SKILLS_DIR)
                       if p.endswith("-dev"))
        self.assertEqual(sorted(r[0] for r in rows), names)
        for skill, target, verb_class, _invoked in rows:
            self.assertTrue(target, "%s resolved to no verb" % skill)
            self.assertIn(verb_class, (
                "enumeration-read", "point-read", "mutating", "scaffolding",
            ))


class TestDerivations(unittest.TestCase):
    """The checker reads ground truth; a literal here would defeat it."""

    def setUp(self):
        self.module = load_checker()

    def test_concurrent_edit_code_comes_from_errors_py(self):
        sys.path.insert(0, str(REPO_ROOT / "lib"))
        try:
            import errors
        finally:
            sys.path.pop(0)
        self.assertEqual(
            self.module.exit_constant("EXIT_CONCURRENT_EDIT"),
            errors.EXIT_CONCURRENT_EDIT,
        )

    def test_unknown_exit_constant_fails_loud(self):
        with self.assertRaises(SystemExit) as cm:
            self.module.exit_constant("EXIT_NOT_A_REAL_CODE")
        self.assertEqual(cm.exception.code, 2)

    def test_zero_result_rule_is_keyed_on_read_classes_only(self):
        """Mutating/scaffolding verbs have no zero-result condition."""
        self.assertEqual(
            sorted(self.module.ZERO_RESULT_BY_CLASS),
            ["enumeration-read", "point-read"],
        )
        self.assertEqual(
            set(self.module.ZERO_RESULT_BY_CLASS.values()), {1},
        )

    def test_history_skill_resolves_to_the_subverb_it_invokes(self):
        """`issue-history` matches three history paths on name alone.

        Name overlap cannot disambiguate it, so invoked-ness breaks the tie.
        A regression here would silently check the wrong verb's class.
        """
        _problems, rows = self.module.check()
        by_skill = dict((r[0], r[1]) for r in rows)
        self.assertEqual(by_skill["issue-history"], "history show")
        self.assertEqual(by_skill["issue-search-history"], "history search")
        self.assertEqual(by_skill["issue-activity"], "history activity")
        self.assertEqual(by_skill["issue-show-at"], "history at")
        # And a wrapper that never names its own verb in a fenced block still
        # resolves to it rather than to a verb it merely composes.
        self.assertEqual(by_skill["issue-start"], "start")
        self.assertEqual(by_skill["issue-list"], "list")


class TestGateBites(unittest.TestCase):
    """Inject each violation into a fixture tree; require the guard to fail.

    Per the IM-261 taxonomy a guard is only worth committing once the defect
    it exists to catch has been shown to trip it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="im265-contract-")
        self.root = Path(self._tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "lib").mkdir()
        (self.root / ".claude" / "skills").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "docs" / "cli-surface.json",
                    self.root / "docs" / "cli-surface.json")
        shutil.copy(REPO_ROOT / "lib" / "errors.py",
                    self.root / "lib" / "errors.py")

    def tearDown(self):
        self._tmp.cleanup()

    def write_skill(self, name, body):
        d = self.root / ".claude" / "skills" / ("%s-dev" % name)
        d.mkdir()
        (d / "SKILL.md").write_text(body, encoding="utf-8")

    def run_check(self):
        return load_checker(self.root).check()[0]

    def test_clean_fixture_passes(self):
        self.write_skill("issue-search-history", (
            "## Invocation\n\n```bash\nissue history search \"<term>\" --json\n"
            "```\n\n## Error semantics\n\n"
            "- `0` -- success, at least one commit.\n"
            "- `1` -- zero matches, or a precondition failure.\n"
        ))
        self.assertEqual(self.run_check(), [])

    def test_wrong_zero_result_code_is_reported(self):
        self.write_skill("issue-search-history", (
            "## Invocation\n\n```bash\nissue history search \"<term>\" --json\n"
            "```\n\n## Error semantics\n\n"
            "- `0` -- success, including zero matches (exits 0).\n"
        ))
        problems = self.run_check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("zero-result", problems[0])
        self.assertIn("enumeration-read", problems[0])

    def test_flag_the_target_verb_rejects_is_reported(self):
        self.write_skill("issue-create", (
            "## Invocation\n\n```bash\nissue create \"<title>\" <type> <pri>\n"
            "```\n\nOptional flags forwarded to `issue create`:\n\n"
            "- `--prefix <PFX>` -- explicit prefix.\n"
            "\n## Error semantics\n\n- `3` -- concurrent-edit, retry once.\n"
        ))
        problems = self.run_check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("--prefix", problems[0])
        self.assertIn("issue create", problems[0])

    def test_skills_own_parameters_are_not_linted_as_cli_flags(self):
        """A composing skill's own argument surface is not a CLI flag.

        `/issue-block-dev --unblock --to open` composes an `issue transition`
        call; linting those against `transition` would report every composing
        skill and the check would be waived wholesale.
        """
        self.write_skill("issue-block", (
            "## Invocation\n\n```bash\nissue transition <id> blocked --reason x\n"
            "```\n\n## Parameters\n\n"
            "- `--unblock` (optional) -- invert the operation.\n"
            "- `--to <state>` (optional) -- state to land in.\n"
            "\n## Error semantics\n\n- `3` -- concurrent-edit, retry once.\n"
        ))
        self.assertEqual(self.run_check(), [])

    def test_mutating_skill_omitting_concurrent_edit_is_reported(self):
        self.write_skill("issue-note", (
            "## Invocation\n\n```bash\nissue note <id> \"<text>\"\n"
            "```\n\n## Error semantics\n\n"
            "- `0` -- success.\n- `2` -- usage error.\n"
        ))
        problems = self.run_check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("concurrent-edit", problems[0])

    def test_skill_with_no_invocation_is_reported_not_skipped(self):
        """An unparseable skill must fail loud, never pass vacuously."""
        self.write_skill("issue-note", "## Notes\n\nNo fenced invocation.\n")
        problems = self.run_check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no `issue <verb>` invocation", problems[0])

    def test_empty_skills_dir_refuses_rather_than_reporting_clean(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_check()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
