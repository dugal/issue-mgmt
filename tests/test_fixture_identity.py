#!/usr/bin/env python3
"""One fixture git identity across the tree (IM-268).

Nine test scripts each invented their own throwaway git identity, named
after whichever issue needed it, and `.publish.yaml` `leaks.allow` had to
enumerate every one of them by full address -- the publish detector
suppresses an email hit only on a full-span match, so a domain token buys
nothing. Three of four authors forgot the allowlist step, twice inside this
phase, and both times the feedback arrived at the publish gate rather than
at `make test`.

These tests put `scripts/check-fixture-emails` on the `make test` path and
pin its gate-bites cases against synthetic fixture trees, so the guard is
committed only with the defects it exists to catch shown to trip it.

NOTE ON THIS FILE'S OWN CONTENT: it writes NO email-shaped literal and
names no `leaks.deny` token. Every synthetic address is assembled at
runtime by `addr()`. The leak detector reports a literal wherever it
appears, prose and test fixtures included (IM-262), so spelling one here
would make the publish gate red from the very guard that exists to keep it
green -- and the first draft of this file did exactly that, caught by
running the checker against the tree.

Run:
    python3 -m unittest tests.test_fixture_identity -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fixture_identity import FIXTURE_EMAIL  # noqa: E402

CHECKER = REPO_ROOT / "scripts" / "check-fixture-emails"
LIB_SH = REPO_ROOT / "tests" / "verification" / "lib.sh"


def addr(local, domain="issue-mgmt.invalid"):
    """Assemble a synthetic address so no literal appears in this source."""
    return "%s@%s" % (local, domain)


def load_checker(repo_root=None):
    spec = importlib.util.spec_from_loader(
        "check_fixture_emails",
        importlib.machinery.SourceFileLoader(
            "check_fixture_emails", str(CHECKER)
        ),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if repo_root is not None:
        module.REPO_ROOT = str(repo_root)
    return module


class TestRealTreePasses(unittest.TestCase):
    def test_checker_exits_zero_on_the_real_tree(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            result.returncode, 0,
            "the tree does not carry one fixture identity:\n%s%s"
            % (result.stdout, result.stderr),
        )

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("check-fixture-emails", result.stdout)

    def test_shared_identity_is_allowlisted(self):
        """The one address authors are pointed at must not trip the gate."""
        module = load_checker()
        _, allow, _ = module.publish_config()
        self.assertIn(FIXTURE_EMAIL, allow)

    def test_the_tree_carries_at_most_two_addresses(self):
        """The shared fixture identity, plus the selftest store identity.

        Asserted as a ceiling rather than a list: naming the second one
        here would put a literal in this file.
        """
        module = load_checker()
        private, allow, _ = module.publish_config()
        found = module.scan(module.public_files(private))
        self.assertLessEqual(len(found), 2, sorted(found))
        self.assertIn(FIXTURE_EMAIL, found)
        for address in found:
            self.assertIn(address, allow, "not allowlisted: %s" % address)

    def test_guard_found_a_plausible_tree(self):
        """Guard the guard: an empty scan would make every check vacuous."""
        module = load_checker()
        private, _, deny = module.publish_config()
        self.assertGreater(len(module.public_files(private)), 100)
        self.assertGreaterEqual(len(deny), 3)

    def test_most_verification_scripts_use_the_shared_variable(self):
        """Complements the checker.

        The checker proves the per-script literals are gone; this proves
        the scripts went through `lib.sh` rather than dropping the identity
        altogether.
        """
        users = [
            p.name
            for p in sorted((REPO_ROOT / "tests" / "verification")
                            .glob("*.sh"))
            if "FIXTURE_EMAIL" in p.read_text(encoding="utf-8")
        ]
        self.assertGreater(len(users), 20, users)

    def test_lib_sh_derives_the_identity_rather_than_restating_it(self):
        """The shell half reads the python constant; it does not copy it."""
        lib = LIB_SH.read_text(encoding="utf-8")
        self.assertNotIn(FIXTURE_EMAIL, lib)
        self.assertIn("fixture_identity.py", lib)
        out = subprocess.run(
            ["bash", "-c",
             '. "%s"; printf "%%s" "$FIXTURE_EMAIL"' % LIB_SH],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(out.stdout, FIXTURE_EMAIL, out.stderr)

    def test_lib_sh_refuses_when_the_identity_cannot_be_read(self):
        """An empty FIXTURE_EMAIL would surface much later.

        `git config user.email ""` succeeds, so the failure would land on
        an unrelated commit rather than here.
        """
        with tempfile.TemporaryDirectory(prefix="im268-norootid-") as tmp:
            fake = Path(tmp)
            (fake / "tests" / "verification").mkdir(parents=True)
            (fake / "tests").joinpath("fixture_identity.py").write_text(
                "# no assignment here\n", encoding="utf-8")
            shim = fake / "tests" / "verification" / "lib.sh"
            shim.write_text(LIB_SH.read_text(encoding="utf-8"),
                            encoding="utf-8")
            out = subprocess.run(
                ["bash", "-c", '. "%s"; echo SHOULD-NOT-REACH' % shim],
                capture_output=True, text=True,
            )
            self.assertNotEqual(out.returncode, 0)
            self.assertNotIn("SHOULD-NOT-REACH", out.stdout)
            self.assertIn("fixture_identity.py", out.stderr)


class TestGateBites(unittest.TestCase):
    """Inject each violation into a fixture tree; require a report."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="im268-fixture-")
        self.root = Path(self._tmp.name)
        (self.root / "tests" / "verification").mkdir(parents=True)
        (self.root / "docs").mkdir()
        self.write_identity(FIXTURE_EMAIL)
        self.write_config(allow=(FIXTURE_EMAIL,))
        # A consumer that references the constant, never an address.
        (self.root / "tests" / "test_thing.py").write_text(
            "EMAIL = FIXTURE_EMAIL\n", encoding="utf-8")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.module = load_checker(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def write_identity(self, address):
        (self.root / "tests" / "fixture_identity.py").write_text(
            'FIXTURE_EMAIL = "%s"\n' % address, encoding="utf-8")

    def write_config(self, allow=(), deny=("sekrituser",), private=()):
        """A synthetic config.

        `deny` defaults to a made-up token rather than the project's real
        one, for the same reason this file spells no address: naming a deny
        token here would trip the real gate on this file.
        """
        lines = ["backend: gitea", "", "private_paths:"]
        lines += ["  - %s" % p for p in (private or ("docs/plan.md",))]
        lines += ["", "leaks:", "  deny:"]
        lines += ["    - %s" % d for d in deny]
        lines += ["  allow:"]
        lines += ["    - %s" % a for a in allow]
        (self.root / ".publish.yaml").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def write(self, relpath, text):
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def problems(self):
        return self.module.check()[0]

    # -- the clean baseline ------------------------------------------------

    def test_clean_tree_passes(self):
        self.assertEqual(self.problems(), [])

    # -- an invented address ----------------------------------------------

    def test_invented_address_is_reported(self):
        """The IM-240 / IM-263 defect, which reached the publish gate."""
        invented = addr("im999")
        self.write("tests/verification/im-999.sh",
                   "export GIT_AUTHOR_EMAIL=%s\n" % invented)
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(invented, problems[0])
        self.assertIn("im-999.sh:1", problems[0])
        self.assertIn("FIXTURE_EMAIL", problems[0])

    def test_invented_address_in_prose_is_reported_too(self):
        """The detector reports a literal wherever it appears (IM-262).

        So a consolidation cannot leave retired addresses named in a
        comment or in changelog prose explaining the change.
        """
        invented = addr("im999")
        self.write("docs/note.md",
                   "We retired %s for one shared address.\n" % invented)
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(invented, problems[0])

    def test_multiple_uses_of_one_address_report_once(self):
        invented = addr("x", "y.invalid")
        self.write("tests/a.py", "A = '%s'\n" % invented)
        self.write("tests/b.py", "B = '%s'\n" % invented)
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tests/a.py:1", problems[0])
        self.assertIn("tests/b.py:1", problems[0])

    def test_untracked_file_is_scanned(self):
        """A just-written test is when the fix is free.

        The mirror ships tracked files only, but waiting for the commit
        would put the feedback back at publish time, which is the defect.
        """
        invented = addr("brand", "new.invalid")
        self.write("tests/test_new.py", "E = '%s'\n" % invented)
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(invented, problems[0])

    # -- the allowlist itself ---------------------------------------------

    def test_shared_identity_missing_from_allow_is_reported(self):
        """Pointing authors at an address the gate reports is worse than
        pointing them nowhere."""
        self.write_config(allow=())
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("not in", problems[0])
        self.assertIn(FIXTURE_EMAIL, problems[0])

    def test_inline_comment_on_an_allow_entry_is_reported(self):
        """The parser quirk this project has been bitten by before.

        `.publish.yaml` strips FULL-LINE comments only, so an inline
        `# ...` stays part of the token and the entry silently never
        matches. Reproducing the quirk is the point: a tolerant parser here
        would read a mis-authored entry as working when the gate reads it
        as inert.
        """
        self.write_config(allow=("%s  # the shared one" % FIXTURE_EMAIL,))
        problems = self.problems()
        self.assertTrue(
            any("not in" in p and FIXTURE_EMAIL in p for p in problems),
            problems)

    def test_stale_allow_entry_is_reported(self):
        """A retired fixture address must leave with its entry."""
        retired = addr("retired", "example.invalid")
        self.write_config(allow=(FIXTURE_EMAIL, retired))
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(retired, problems[0])
        self.assertIn("no public file uses", problems[0])

    def test_allow_entry_spanning_a_deny_token_is_reported(self):
        """An allow entry overrides `deny` inside its span -- IM-218's shape.

        A bare-username address at a fixture domain reports only as
        `email`, so allowing the address quiets the deny token entirely.
        """
        overriding = addr("sekrituser")
        self.write("tests/c.py", "C = '%s'\n" % overriding)
        self.write_config(allow=(FIXTURE_EMAIL, overriding))
        problems = self.problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("spans the `leaks.deny` token", problems[0])
        self.assertIn("sekrituser", problems[0])

    def test_non_email_allow_tokens_are_not_treated_as_addresses(self):
        """A substring token for the path and deny categories is neither an
        address to match nor a stale entry to report."""
        self.write_config(
            allow=(FIXTURE_EMAIL, "somebrand", "me", "linuxbrew"))
        self.assertEqual(self.problems(), [])

    # -- scope ------------------------------------------------------------

    def test_private_path_is_not_scanned(self):
        """The mirror never ships it, so an address there is not a leak.

        Without this, the plan file -- which discusses the retired
        addresses by name -- would make the guard permanently red.
        """
        self.write("docs/plan.md", "IM-240 used %s.\n" % addr("im999"))
        self.assertEqual(self.problems(), [])

    def test_private_glob_and_directory_forms_are_honored(self):
        self.write_config(allow=(FIXTURE_EMAIL,),
                          private=("glob:.issues/*.md", "docs/design/"))
        self.write(".issues/IM-001.md", "claimed by %s\n" % addr("im999"))
        self.write("docs/design/old.md", "see %s\n" % addr("im189"))
        self.assertEqual(self.problems(), [])

    def test_publish_config_itself_is_not_scanned(self):
        """Its own allow entries are email literals, and it never ships."""
        self.assertEqual(self.problems(), [])
        _, allow, _ = self.module.publish_config()
        self.assertIn(FIXTURE_EMAIL, allow)

    def test_gitignored_output_is_not_scanned(self):
        self.write(".gitignore", "htmlcov-unit/\n")
        self.write("htmlcov-unit/index.html",
                   "generated for %s\n" % addr("someone", "elsewhere.com"))
        self.assertEqual(self.problems(), [])

    # -- refusals, rather than a clean report -----------------------------

    def test_missing_identity_file_refuses(self):
        (self.root / "tests" / "fixture_identity.py").unlink()
        with self.assertRaises(SystemExit) as cm:
            self.problems()
        self.assertEqual(cm.exception.code, 2)

    def test_unparseable_identity_file_refuses(self):
        self.write("tests/fixture_identity.py",
                   "FIXTURE_EMAIL = os.environ['ADDR']\n")
        with self.assertRaises(SystemExit) as cm:
            self.problems()
        self.assertEqual(cm.exception.code, 2)

    def test_unparseable_publish_config_refuses(self):
        """A parser that stopped matching the file would read clean."""
        self.write(".publish.yaml", "backend: gitea\n")
        with self.assertRaises(SystemExit) as cm:
            self.problems()
        self.assertEqual(cm.exception.code, 2)

    def test_empty_file_list_refuses(self):
        empty = self.root / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=empty, check=True)
        module = load_checker(empty)
        with self.assertRaises(SystemExit) as cm:
            module.public_files([])
        self.assertEqual(cm.exception.code, 2)

    def test_non_repo_root_refuses(self):
        """A root outside any git repo, not merely an empty subdirectory.

        A subdirectory of this fixture repo would resolve to the repo above
        it and exercise the empty-scan path instead.
        """
        with tempfile.TemporaryDirectory(prefix="im268-norepo-") as outside:
            module = load_checker(outside)
            with self.assertRaises(SystemExit) as cm:
                module.public_files([])
            self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
