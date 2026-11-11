#!/usr/bin/env python3
"""The declared-source-reaches-the-tarball gate (IM-264).

`scripts/check-tarball-sources` runs inside the `make dist` recipe and
fails the build when a path declared in the Makefile's `SOURCES` list has
no content-identical member in the built tarball. It closes the half of
the packaging gap the derived `tar` include list cannot: a file that is
declared but never staged.

Two files shipped broken through this hole -- `issue-ready` (IM-145,
caught only in post-publish verification) and `LICENSE`, which was absent
from every packaging list from v3.1.0 onward while the formula declared
`license "MIT"`. These tests are the gate-bites proof: each asserts the
check FAILS on an injected omission, not merely that it passes today.

Matching is by sha256 of content rather than by path, because the tarball
renames what it stages; a path comparison would need a second copy of the
staging rules, which is the same hand-maintained declaration the check
exists to retire.

Run:
    python3 -m unittest tests.test_tarball_sources -v
"""

import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check-tarball-sources"


def run_check(tarball, sources):
    """Invoke the checker as the Makefile does; return (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(CHECKER), str(tarball)] + [str(s) for s in sources],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TarballSourcesFixture(unittest.TestCase):
    """A synthetic source tree plus a tarball built from a chosen subset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write_source(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def build_tarball(self, members):
        """Pack {archive_name: source_path}, mimicking the staging renames."""
        tarball = self.root / "dist.tar.gz"
        with tarfile.open(tarball, "w:gz") as tf:
            for arcname, src in members.items():
                tf.add(str(src), arcname=arcname)
        return tarball


class TestDeclaredSourcePresent(TarballSourcesFixture):
    def test_source_shipped_under_its_own_name_passes(self):
        src = self.write_source("LICENSE", "MIT text\n")
        tarball = self.build_tarball({"LICENSE": src})

        rc, out, _ = run_check(tarball, [src])

        self.assertEqual(0, rc)
        self.assertIn("all 1 declared sources present", out)

    def test_source_shipped_under_a_renamed_path_passes(self):
        # The real staging renames .claude/skills/<n>-dev/SKILL.md to
        # skills/<n>.md and scripts/issue to issue. Content matching is
        # what makes the check survive those renames.
        src = self.write_source(".claude/skills/issue-dep-dev/SKILL.md", "dep skill\n")
        tarball = self.build_tarball({"skills/issue-dep.md": src})

        rc, _, _ = run_check(tarball, [src])

        self.assertEqual(0, rc)

    def test_every_source_checked_not_just_the_first(self):
        a = self.write_source("a.md", "alpha\n")
        b = self.write_source("b.md", "beta\n")
        tarball = self.build_tarball({"a.md": a})

        rc, _, err = run_check(tarball, [a, b])

        self.assertEqual(1, rc)
        self.assertIn("b.md", err)


class TestGateBites(TarballSourcesFixture):
    """The injected-omission direction -- the reason the check exists."""

    def test_declared_but_unstaged_source_fails(self):
        shipped = self.write_source("README.md", "readme\n")
        omitted = self.write_source("LICENSE", "MIT text\n")
        tarball = self.build_tarball({"README.md": shipped})

        rc, _, err = run_check(tarball, [shipped, omitted])

        self.assertEqual(1, rc)
        self.assertIn("LICENSE", err)
        self.assertIn("NOT in", err)

    def test_failure_names_every_omission_and_counts_them(self):
        kept = self.write_source("kept.md", "kept\n")
        gone_a = self.write_source("gone-a.md", "gone a\n")
        gone_b = self.write_source("gone-b.md", "gone b\n")
        tarball = self.build_tarball({"kept.md": kept})

        rc, _, err = run_check(tarball, [kept, gone_a, gone_b])

        self.assertEqual(1, rc)
        self.assertIn("2 declared source(s) are NOT in", err)
        self.assertIn("gone-a.md", err)
        self.assertIn("gone-b.md", err)

    def test_stale_tarball_content_fails(self):
        # The source changed after the tarball was built: same path, different
        # bytes. Content matching catches it; a path check would not.
        src = self.write_source("CHANGELOG.md", "old\n")
        tarball = self.build_tarball({"CHANGELOG.md": src})
        src.write_text("new\n")

        rc, _, err = run_check(tarball, [src])

        self.assertEqual(1, rc)
        self.assertIn("CHANGELOG.md", err)

    def test_failure_names_the_staging_step_as_the_fix(self):
        omitted = self.write_source("LICENSE", "MIT text\n")
        kept = self.write_source("kept.md", "kept\n")
        tarball = self.build_tarball({"kept.md": kept})

        _, _, err = run_check(tarball, [kept, omitted])

        self.assertIn("staging", err)


class TestUsageErrors(TarballSourcesFixture):
    def test_missing_tarball_is_a_usage_error(self):
        src = self.write_source("a.md", "alpha\n")

        rc, _, err = run_check(self.root / "nope.tar.gz", [src])

        self.assertEqual(2, rc)
        self.assertIn("tarball not found", err)

    def test_nonexistent_declared_source_is_a_usage_error(self):
        kept = self.write_source("kept.md", "kept\n")
        tarball = self.build_tarball({"kept.md": kept})

        rc, _, err = run_check(tarball, [kept, self.root / "ghost.md"])

        self.assertEqual(2, rc)
        self.assertIn("does not exist", err)

    def test_no_sources_is_a_usage_error(self):
        kept = self.write_source("kept.md", "kept\n")
        tarball = self.build_tarball({"kept.md": kept})

        rc, _, err = run_check(tarball, [])

        self.assertEqual(2, rc)
        self.assertIn("usage", err)

    def test_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(CHECKER), "--help"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, proc.returncode)
        self.assertIn("check-tarball-sources", proc.stdout)


class TestCheckerIsWired(unittest.TestCase):
    """The checker only guards anything if `make dist` actually runs it."""

    def test_checker_is_executable(self):
        self.assertTrue(os.access(CHECKER, os.X_OK), f"{CHECKER} is not executable")

    def test_dist_recipe_invokes_the_checker_over_SOURCES(self):
        makefile = (REPO_ROOT / "Makefile").read_text()

        self.assertIn("scripts/check-tarball-sources $(TARBALL) $(SOURCES)", makefile)

    def test_license_is_declared_in_SOURCES(self):
        # The data half of IM-264: LICENSE existed, was referenced by the
        # formula's `license "MIT"`, and was in no packaging list.
        makefile = (REPO_ROOT / "Makefile").read_text()
        sources_line = next(
            line for line in makefile.splitlines() if line.startswith("SOURCES ")
        )

        self.assertIn("LICENSE", sources_line)


if __name__ == "__main__":
    unittest.main()
