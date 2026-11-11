#!/usr/bin/env python3
"""The shipped cheat sheet describes the real CLI (IM-267).

`share/issue-mgmt/CHEATSHEET.md` ships in the formula and installs next to
the skills, so a session reads it as current fact. `dist.cheatsheet` promises
it maps every `issue` verb to its purpose, the direct-FS habit it obviates,
one example, and its `/issue-*` skill -- and nothing checked that. Every test
touching the file asserted only that it gets INSTALLED; not one read its text.
Its inventories were correct when IM-267 was filed, but by luck, and its one
statement about argument POSITION was wrong for four of the five flags it
named.

These tests put `scripts/check-cheatsheet` on the `make test` path and pin
its gate-bites cases against synthetic fixture trees. The flag-POSITION half
cannot honestly be checked by reading text, so it lives in
`tests/verification/cheatsheet.sh`, which executes each claimed placement
against the real dispatcher.

Run:
    python3 -m unittest tests.test_cheatsheet -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check-cheatsheet"


def load_checker(repo_root=None):
    spec = importlib.util.spec_from_loader(
        "check_cheatsheet",
        importlib.machinery.SourceFileLoader("check_cheatsheet", str(CHECKER)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if repo_root is not None:
        root = str(repo_root)
        module.REPO_ROOT = root
        module.SHEET = os.path.join(
            root, "share", "issue-mgmt", "CHEATSHEET.md"
        )
        module.MANIFEST = os.path.join(root, "docs", "cli-surface.json")
        module.SKILLS_DIR = os.path.join(root, ".claude", "skills")
    return module


class TestRealSheetPasses(unittest.TestCase):
    def test_checker_exits_zero_on_the_shipped_sheet(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            result.returncode, 0,
            "the shipped cheat sheet disagrees with the CLI:\n%s%s"
            % (result.stdout, result.stderr),
        )

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("check-cheatsheet", result.stdout)

    def test_every_manifest_verb_is_named(self):
        module = load_checker()
        problems, verbs, skills = module.check()
        self.assertEqual(problems, [])
        # Guard the guard: a manifest that parsed to nothing would make the
        # whole check vacuous, so assert it found a plausible surface.
        self.assertGreater(len(verbs), 20)
        self.assertGreater(len(skills), 10)


class TestGateBites(unittest.TestCase):
    """Inject each violation into a fixture tree; require a report.

    Per the IM-261 taxonomy, a guard is only worth committing once the defect
    it exists to catch has been shown to trip it.
    """

    MINIMAL_EXITS = (
        "Read verbs report emptiness through the exit code, one rule per "
        "class:\n\n"
        "- An ENUMERATION read follows the grep convention.\n"
        "- A POINT read exits 0 when found, 1 when not.\n"
        "- `--prefix` the store lacks is an argument error "
        "(`error=unknown-prefix`).\n"
        "- An unusable store carries `reason=store-unresolvable`.\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="im267-cheatsheet-")
        self.root = Path(self._tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "share" / "issue-mgmt").mkdir(parents=True)
        (self.root / ".claude" / "skills").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "docs" / "cli-surface.json",
                    self.root / "docs" / "cli-surface.json")
        # One shipped skill, so the skills-dir derivation has ground truth.
        d = self.root / ".claude" / "skills" / "issue-create-dev"
        d.mkdir()
        (d / "SKILL.md").write_text("stub\n", encoding="utf-8")
        self.module = load_checker(self.root)
        self.all_verbs = self.module.manifest_verb_paths()

    def tearDown(self):
        self._tmp.cleanup()

    def write_sheet(self, verbs, skills=("issue-create",), exits=None):
        lines = ["# Cheat sheet", ""]
        for verb in sorted(verbs):
            lines.append("- `issue %s` -- does a thing." % verb)
        for skill in skills:
            lines.append("- Skill: `/%s`" % skill)
        lines.append("")
        lines.append(self.MINIMAL_EXITS if exits is None else exits)
        (self.root / "share" / "issue-mgmt" / "CHEATSHEET.md").write_text(
            "\n".join(lines), encoding="utf-8",
        )

    def problems(self):
        return self.module.check()[0]

    def test_complete_sheet_passes(self):
        self.write_sheet(self.all_verbs)
        self.assertEqual(self.problems(), [])

    def test_missing_verb_is_reported(self):
        dropped = sorted(self.all_verbs)[0]
        self.write_sheet(v for v in self.all_verbs if v != dropped)
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("does not name", problems[0])
        self.assertIn(dropped, problems[0])

    def test_phantom_verb_is_reported(self):
        self.write_sheet(list(self.all_verbs) + ["frobnicate"])
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("frobnicate", problems[0])
        self.assertIn("not a verb", problems[0])

    def test_phantom_skill_is_reported(self):
        self.write_sheet(self.all_verbs,
                         skills=("issue-create", "issue-frobnicate"))
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("issue-frobnicate", problems[0])

    def test_unnamed_shipped_skill_is_reported(self):
        d = self.root / ".claude" / "skills" / "issue-note-dev"
        d.mkdir()
        (d / "SKILL.md").write_text("stub\n", encoding="utf-8")
        self.write_sheet(self.all_verbs, skills=("issue-create",))
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("issue-note", problems[0])

    def test_grep_only_exit_guidance_is_reported(self):
        """The pre-IM-267 wording: grep convention and nothing else."""
        self.write_sheet(
            self.all_verbs,
            exits="Read verbs follow the grep convention: exit 0 with "
                  "matches, exit 1 on an empty result.\n",
        )
        problems = self.problems()
        # Three of the four required rules are absent.
        self.assertEqual(len(problems), 3, problems)
        joined = "\n".join(problems)
        self.assertIn("point-read", joined)
        self.assertIn("absent-prefix", joined)
        self.assertIn("unusable store", joined)

    def test_prose_mention_is_not_read_as_an_invocation(self):
        """Only code spans and fences name verbs.

        "the per-issue lock" and "an open issue passes through in-progress"
        are prose. A deny-list of words to skip would itself be the
        hand-maintained declaration this check exists to make unnecessary,
        so detection is scoped to code instead.
        """
        self.write_sheet(self.all_verbs)
        path = self.root / "share" / "issue-mgmt" / "CHEATSHEET.md"
        path.write_text(
            path.read_text()
            + "\nThe per-issue lock is advisory, and an open issue passes "
              "through in-progress atomically.\n",
            encoding="utf-8",
        )
        self.assertEqual(self.problems(), [])

    def test_group_prefix_is_not_a_phantom_verb(self):
        """`issue admin <sub>` in the tier overview is real surface."""
        self.write_sheet(self.all_verbs)
        path = self.root / "share" / "issue-mgmt" / "CHEATSHEET.md"
        path.write_text(
            path.read_text()
            + "\n```\nissue history <sub>   git archaeology\n"
              "issue admin <sub>     maintenance\n```\n",
            encoding="utf-8",
        )
        self.assertEqual(self.problems(), [])

    def test_empty_skills_dir_refuses_rather_than_reporting_clean(self):
        shutil.rmtree(self.root / ".claude" / "skills")
        (self.root / ".claude" / "skills").mkdir()
        self.write_sheet(self.all_verbs)
        with self.assertRaises(SystemExit) as cm:
            self.problems()
        self.assertEqual(cm.exception.code, 2)

    def test_missing_sheet_refuses_rather_than_reporting_clean(self):
        with self.assertRaises(SystemExit) as cm:
            self.problems()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
