#!/usr/bin/env python3
"""The SHIPPED skill bodies name only commands that resolve (IM-276).

`scripts/check-shipped-skills` is the guard IM-276 asked for: one that derives
its expectation from the artifact that INSTALLS, not from `.claude/skills/*-dev`.
Four guards missed the alias leak because each read a derived view of the dev
tree, so the `-dev` suffix sat on both sides of the comparison and cancelled
out. `test_the_bite_is_invisible_to_the_dev_tree_gate` below is the test that
pins the difference: it damages only the staged copy, and shows the dev-tree
gate stays green while this guard goes red.

These tests put the guard on the `make test` path (the script itself also runs
under the verification suite) and pin its two derivations, so it cannot quietly
become vacuous:

  * the installed-command set comes from the staged tree's own basenames,
  * the verb set comes from the `VERB` / `TIER` constants in `lib/cli/*.py`,
    cross-checked here against the independently-extracted surface manifest.

Run:
    python3 -m unittest tests.test_shipped_skills -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "scripts" / "check-shipped-skills"
GENERATOR = REPO_ROOT / "scripts" / "gen-dev-skills"
MANIFEST = REPO_ROOT / "docs" / "cli-surface.json"


def load_guard():
    spec = importlib.util.spec_from_loader(
        "check_shipped_skills",
        importlib.machinery.SourceFileLoader(
            "check_shipped_skills", str(GUARD)
        ),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StagedTreeCase(unittest.TestCase):
    """A real staged tree, built by the project's own staging step."""

    def setUp(self):
        self.module = load_guard()
        self._tmp = tempfile.TemporaryDirectory(prefix="im276-shipped-")
        self.stage = Path(self._tmp.name)
        self.module.stage_into(str(self.stage))

    def tearDown(self):
        self._tmp.cleanup()

    def problems(self):
        return self.module.check(str(self.stage))[0]

    def append(self, name, line):
        """Append a line to a STAGED body.

        Injections append rather than rewrite existing prose: a fixture that
        anchors on a real sentence breaks when that sentence is legitimately
        reworded, which is the defect class IM-273 tracks.
        """
        path = self.stage / ("%s.md" % name)
        path.write_text(path.read_text(encoding="utf-8") + "\n%s\n" % line,
                        encoding="utf-8")


class TestRealShippedTreePasses(StagedTreeCase):
    def test_no_problems_in_the_staged_bodies(self):
        self.assertEqual(self.problems(), [])

    def test_every_staged_skill_is_checked_not_skipped(self):
        _problems, rows = self.module.check(str(self.stage))
        names = sorted(p.name[: -len(".md")]
                       for p in self.stage.glob("*.md"))
        self.assertEqual(sorted(r[0] for r in rows), names)
        self.assertEqual(len(rows), 15)
        for name, declared, _commands, invoked in rows:
            self.assertEqual(declared, name)
            self.assertTrue(invoked, "%s names no `issue` verb at all" % name)

    def test_guard_exits_zero_as_a_subprocess(self):
        result = subprocess.run(
            [sys.executable, str(GUARD)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(GUARD), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("check-shipped-skills", result.stdout)

    def test_staging_is_a_verbatim_copy_of_the_authored_sources(self):
        """What ships IS what an author edits -- no rename, no rewrite."""
        for staged in sorted(self.stage.glob("*.md")):
            source = (REPO_ROOT / "share" / "issue-mgmt" / "skills"
                      / staged.name)
            self.assertEqual(staged.read_bytes(), source.read_bytes(),
                             "%s diverged in staging" % staged.name)


class TestDerivations(StagedTreeCase):
    """The guard reads ground truth; a literal here would defeat it."""

    def test_verb_set_matches_the_independently_extracted_manifest(self):
        with open(MANIFEST, "r") as fh:
            manifest = json.load(fh)
        from_manifest = set()
        for entry in manifest["verbs"]:
            path = entry["path"]
            from_manifest.add(path[len("issue "):]
                              if path.startswith("issue ") else path)
        self.assertEqual(self.module.verb_paths(), from_manifest)

    def test_installed_command_set_comes_from_the_staged_tree(self):
        """Not from `.claude/skills` -- that is the whole point of the guard.

        Removing a skill from the staged tree must change the set the bodies
        are checked against, which is only true if the set is read from there.
        The reference is appended here rather than borrowed from a real body,
        so the test does not depend on which skill happens to cross-reference
        which (the IM-273 shape: a fixture anchored in live content dies when
        that content legitimately changes -- as the IM-127 reword did).
        """
        self.append("issue-note", "See `/issue-edit <id>` to amend.")
        self.assertEqual(self.problems(), [])
        (self.stage / "issue-edit.md").unlink()
        problems = self.problems()
        self.assertTrue(problems)
        self.assertTrue(any("/issue-edit" in p for p in problems), problems)


class TestGateBites(StagedTreeCase):
    """Inject each violation into the STAGED tree; require a red verdict."""

    def rewrite_name(self, name, declared):
        """Replace the frontmatter `name:` value -- a structural anchor."""
        path = self.stage / ("%s.md" % name)
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            if line.startswith("name:"):
                lines[i] = "name: %s" % declared
                break
        else:
            self.fail("%s has no frontmatter name: line" % name)
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_a_reintroduced_dev_invocation_is_reported(self):
        self.append("issue-complete", "Then run `/issue-edit-dev <id>`.")
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("/issue-edit-dev", problems[0])
        self.assertIn("not an installed skill", problems[0])

    def test_a_dev_aliased_frontmatter_name_is_reported(self):
        self.rewrite_name("issue-activity", "issue-activity-dev")
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("declares `name: issue-activity-dev`", problems[0])

    def test_a_command_that_is_not_a_skill_at_all_is_reported(self):
        """`/issue-validate-dev` shipped for releases; nothing ever had it."""
        self.append("issue-search",
                    "- structural validation is `/issue-validate`'s job.")
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("/issue-validate", problems[0])

    def test_an_invocation_of_a_verb_the_dispatcher_lacks_is_reported(self):
        self.append("issue-note", "```bash\nissue annotate <id>\n```")
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("issue annotate", problems[0])

    def test_a_missing_name_line_is_reported(self):
        path = self.stage / "issue-show.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            "\n".join(l for l in text.split("\n")
                      if not l.startswith("name:")),
            encoding="utf-8")
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no frontmatter `name:` line", problems[0])

    def test_an_empty_shipped_dir_refuses_rather_than_reporting_clean(self):
        for path in self.stage.glob("*.md"):
            path.unlink()
        with self.assertRaises(SystemExit) as cm:
            self.module.check(str(self.stage))
        self.assertEqual(cm.exception.code, 2)

    def test_the_bite_is_invisible_to_the_dev_tree_gate(self):
        """Why this guard had to be written (IM-276, the IM-261 shape).

        The bite below exists only in the staged artifact. A guard deriving
        its expectation from `.claude/skills/*-dev` -- which is what all four
        pre-existing guards did -- sees a perfectly consistent dev tree and
        stays green. This guard goes red on the same tree.
        """
        self.append("issue-complete", "Then run `/issue-edit-dev <id>`.")
        self.assertTrue(self.problems())
        dev_gate = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(dev_gate.returncode, 0,
                         "the dev-tree gate should be blind to a staged-only "
                         "defect: " + dev_gate.stdout + dev_gate.stderr)


if __name__ == "__main__":
    unittest.main()
