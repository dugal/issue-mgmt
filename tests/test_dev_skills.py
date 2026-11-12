#!/usr/bin/env python3
"""The project-local dev skill copies are derived, and stay derived (IM-276).

`share/issue-mgmt/skills/<name>.md` is the authored canonical skill -- the same
layout and bytes the formula installs -- and `.claude/skills/<name>-dev/SKILL.md`
is generated from it by `scripts/gen-dev-skills`. That inversion is the IM-276
decision: the two variants differ in CONTENT (a dev session must be told the
`-dev` commands, an installed one the canonical names), so one side has to be
derived, and the side that ships is the side that needs a reader.

The accepted cost of generating into a tracked directory is that a hand edit to
a dev copy is discarded on the next regeneration. These tests are the guard for
that failure mode -- the committed dev copies ARE the detector, byte-compared
against what the generator produces, exactly as `tests/test_cli_surface.py`
guards the committed surface manifest. A hand edit turns `make test` red
instead of vanishing silently.

Run:
    python3 -m unittest tests.test_dev_skills -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen-dev-skills"


def load_generator(canonical_dir=None, dev_dir=None):
    """Import the generator as a module, optionally rooted at a fixture tree."""
    spec = importlib.util.spec_from_loader(
        "gen_dev_skills",
        importlib.machinery.SourceFileLoader(
            "gen_dev_skills", str(GENERATOR)
        ),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if canonical_dir is not None:
        module.CANONICAL_DIR = str(canonical_dir)
    if dev_dir is not None:
        module.DEV_DIR = str(dev_dir)
    return module


class TestRealTreeIsCurrent(unittest.TestCase):
    def setUp(self):
        self.module = load_generator()

    def test_every_committed_dev_copy_matches_its_canonical_source(self):
        """The regeneration diff test -- the detector itself."""
        stale = []
        for name, dest, text in self.module.generate():
            with open(dest, "r") as fh:
                if fh.read() != text:
                    stale.append(os.path.relpath(dest, str(REPO_ROOT)))
        self.assertEqual(
            stale, [],
            "dev skill copies are GENERATED; author the change in "
            "share/issue-mgmt/skills/<name>.md and run `make dev-skills`",
        )

    def test_check_mode_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("gen-dev-skills", result.stdout)

    def test_canonical_sources_carry_no_dev_alias_at_all(self):
        """The shipped side, asserted at the source.

        `scripts/check-shipped-skills` asserts this against the STAGED bytes,
        which is the authority; this is the same invariant one step earlier,
        so a reintroduced alias is attributable to the file an author edits.
        """
        offenders = []
        for name in self.module.canonical_names():
            path = self.module.canonical_path(name)
            with open(path, "r") as fh:
                for lineno, line in enumerate(fh, 1):
                    if "-dev" in line:
                        offenders.append("%s:%d: %s"
                                         % (name, lineno, line.strip()))
        self.assertEqual(offenders, [])

    def test_the_dev_set_and_the_canonical_set_are_the_same_skills(self):
        canonical = sorted(self.module.canonical_names())
        dev = sorted(d[: -len("-dev")]
                     for d in os.listdir(self.module.DEV_DIR)
                     if d.endswith("-dev"))
        self.assertEqual(canonical, dev)


class TestTransformIsABijection(unittest.TestCase):
    """A one-way transform could silently drop content; prove it round-trips.

    This is also the evidence that creating the canonical sources from the
    pre-IM-276 dev bodies preserved them: regenerating the dev copies from the
    new canonical sources reproduced the committed files byte for byte.
    """

    def setUp(self):
        self.module = load_generator()
        self.names = self.module.canonical_names()

    def test_canonical_to_dev_and_back_is_the_identity(self):
        for name in self.names:
            with open(self.module.canonical_path(name), "r") as fh:
                canonical = fh.read()
            dev = self.module.to_dev(canonical, name, self.names)
            self.assertEqual(
                self.module.strip_dev(dev, name, self.names), canonical,
                "round trip changed %s" % name,
            )

    def test_dev_to_canonical_and_back_is_the_identity(self):
        for name in self.names:
            with open(self.module.dev_path(name), "r") as fh:
                dev = fh.read()
            canonical = self.module.strip_dev(dev, name, self.names)
            self.assertEqual(
                self.module.to_dev(canonical, name, self.names), dev,
                "round trip changed %s-dev" % name,
            )

    def test_cli_invocations_and_paths_are_not_rewritten(self):
        """Only skill references carry the alias.

        `issue complete` is the same command in both worlds, and
        `.claude/issue-mgmt-sessions` is a path, not a slash command. A
        blanket substitution would corrupt both.
        """
        text = (
            "---\nname: issue-complete\n---\n\n"
            "Run `issue complete <id>` and `issue admin reap`.\n"
            "Then `/issue-edit <id>` and `/issue-show-at <id>`.\n"
            "State lives in $(git rev-parse --show-toplevel)"
            "/.claude/issue-mgmt-sessions/<ppid>.json\n"
        )
        out = self.module.to_dev(text, "issue-complete", self.names)
        self.assertIn("name: issue-complete-dev", out)
        self.assertIn("`issue complete <id>`", out)
        self.assertIn("`issue admin reap`", out)
        self.assertIn("/issue-edit-dev", out)
        self.assertIn("/issue-show-at-dev", out)
        self.assertNotIn("/issue-show-dev-at", out)
        self.assertIn("/.claude/issue-mgmt-sessions/", out)


class TestGateBites(unittest.TestCase):
    """Inject the failure modes into a fixture tree; require a red verdict."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="im276-devskills-")
        self.root = Path(self._tmp.name)
        self.canonical = self.root / "share" / "issue-mgmt" / "skills"
        self.dev = self.root / ".claude" / "skills"
        self.canonical.mkdir(parents=True)
        self.dev.mkdir(parents=True)
        self.module = load_generator(self.canonical, self.dev)

    def tearDown(self):
        self._tmp.cleanup()

    def write_pair(self, name="issue-note", body=None):
        body = body or (
            "---\nname: %s\n---\n\n# Note\n\nSee `/issue-edit <id>`.\n" % name
        )
        (self.canonical / ("%s.md" % name)).write_text(body, encoding="utf-8")
        (self.canonical / "issue-edit.md").write_text(
            "---\nname: issue-edit\n---\n\n# Edit\n", encoding="utf-8")
        # Generate the matching dev copies so the fixture starts clean.
        for n, dest, text in self.module.generate():
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as fh:
                fh.write(text)

    def run_check(self):
        """Exit code of `--check` against the fixture tree."""
        rows = self.module.generate()
        stale = []
        for name, dest, text in rows:
            if not os.path.isfile(dest):
                stale.append(dest)
            else:
                with open(dest, "r") as fh:
                    if fh.read() != text:
                        stale.append(dest)
        return stale

    def test_clean_fixture_passes(self):
        self.write_pair()
        self.assertEqual(self.run_check(), [])

    def test_a_hand_edited_dev_copy_is_reported(self):
        """The accepted cost of generating into a tracked directory."""
        self.write_pair()
        target = self.dev / "issue-note-dev" / "SKILL.md"
        target.write_text(
            target.read_text(encoding="utf-8") + "\nhand edit\n",
            encoding="utf-8")
        self.assertEqual([os.path.basename(os.path.dirname(p))
                          for p in self.run_check()], ["issue-note-dev"])

    def test_a_missing_dev_copy_is_reported(self):
        self.write_pair()
        os.remove(self.dev / "issue-note-dev" / "SKILL.md")
        self.assertEqual(len(self.run_check()), 1)

    def test_a_canonical_name_disagreeing_with_its_filename_fails_loud(self):
        """A mis-declared `name:` is what installs as the wrong directory."""
        (self.canonical / "issue-note.md").write_text(
            "---\nname: issue-notes\n---\n\n# Note\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self.module.generate()
        self.assertEqual(cm.exception.code, 2)

    def test_a_canonical_source_with_no_name_line_fails_loud(self):
        (self.canonical / "issue-note.md").write_text(
            "---\ndescription: x\n---\n\n# Note\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self.module.generate()
        self.assertEqual(cm.exception.code, 2)

    def test_an_empty_canonical_dir_refuses_rather_than_passing(self):
        with self.assertRaises(SystemExit) as cm:
            self.module.canonical_names()
        self.assertEqual(cm.exception.code, 2)

    def test_write_mode_restores_a_hand_edited_copy(self):
        self.write_pair()
        target = self.dev / "issue-note-dev" / "SKILL.md"
        expected = target.read_text(encoding="utf-8")
        target.write_text("clobbered\n", encoding="utf-8")
        for name, dest, text in self.module.generate():
            with open(dest, "w") as fh:
                fh.write(text)
        self.assertEqual(target.read_text(encoding="utf-8"), expected)


if __name__ == "__main__":
    unittest.main()
