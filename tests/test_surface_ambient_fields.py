#!/usr/bin/env python3
"""Ambient-derived `--json` fields must be declared, measured (IM-262).

`surface.explicit-output-mode` permits an ambient-derived value -- a wall-clock
stamp, a resolved relative window, a date written into a record -- only in a
field the verb declares, and the surface manifest records, as one of exactly
two kinds: GENERATION METADATA (describes this invocation's rendering) or
RECORD CONTENT (persisted into the issue being written).

Nothing checked that. `SURFACE_GENERATION_FIELDS` was declared complete on two
verbs while `issue create --json` emitted a clock-read `created` and
`issue note --json` a clock-read `date`, so the tree that introduced the clause
already violated it. The test that was supposed to cover this compared a
hand-written table in the test file against the hand-written constant in the
module -- a comparison between two declarations, which per the IM-261 taxonomy
is not a guard at all.

So measure instead. Every verb that registers `--json` is driven through the
real CLI and every date- or timestamp-shaped value in its output is collected
by dotted path. An AMBIENT value is one carrying the clock reading of this test
run; the fixture store makes that unambiguous:

  * every fixture issue's `created` / `updated` is rewritten to 2020,
  * every fixture git commit is authored and committed at a 2020 date,
  * the read verbs run against that pristine store BEFORE any mutating recipe,
    so no earlier recipe's write can be mistaken for the read verb's own.

Nothing in the fixture therefore carries today's date, and a today-dated value
in a verb's output can only have come from the clock during that invocation.
Both directions fail:

  * measured but not declared -- the IM-262 bug; an ambient value reaching
    `--json` with no record of which kind it is.
  * declared but never observed -- a dead or misspelled declaration, which
    reads as coverage while guarding nothing.

Run:
    python3 -m unittest tests.test_surface_ambient_fields -v
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ISSUE_CLI = REPO_ROOT / "scripts" / "issue"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import cli_surface  # noqa: E402
from fixture_identity import FIXTURE_EMAIL  # noqa: E402

# A date or ISO timestamp, anchored: `2026-11-08` or `2026-11-08T16:35:41Z`.
DATE_SHAPED = re.compile(r"^\d{4}-\d{2}-\d{2}")

# The fixture's own dates. Chosen far from any plausible run date so a
# today-dated output value cannot be fixture data.
FIXTURE_DATE = "2020-01-01"
FIXTURE_COMMIT_DATE = "2020-01-01T00:00:00+00:00"

# Fixture git identity: the project's one shared address, imported from its
# single source of truth (IM-268 consolidated the nine per-script addresses
# onto it, so a new test has none to invent and `leaks.allow` carries one
# entry instead of ten).
FIXTURE_NAME = "fixture"

# Per verb: the argv modes to drive, and whether the mode writes to the store.
#
# A `read` mode runs first, against the pristine 2020-dated store, so the only
# today-dated value it can emit is one it generated itself. A `write` mode
# runs after, and its own write is exactly what we want attributed to it.
#
# Several verbs need more than one mode to reach every ambient field they can
# emit: `transition` writes lease stamps only under `--claim` / `--renew`, and
# would otherwise pass the declared-but-never-observed check vacuously.
#
# Completeness is not trusted to this table: every `--json` verb in the
# manifest must appear here, and `test_every_json_verb_is_exercised` fails
# naming any that does not. A new verb cannot be forgotten silently.
READ = "read"
WRITE = "write"

RECIPES = {
    # --- read verbs, against the pristine fixture ---------------------------
    "issue show": ((READ, ["show", "PRB-001", "--json"]),),
    "issue list": ((READ, ["list", "--json"]),),
    "issue search": ((READ, ["search", "probe", "--json"]),),
    "issue index": ((READ, ["index", "--json"]),),
    "issue ready": ((READ, ["ready", "--json"]),),
    "issue history show": ((READ, ["history", "show", "PRB-001", "--json"]),),
    "issue history search": ((READ, ["history", "search", "probe", "--json"]),),
    "issue history at": ((READ, ["history", "at", "PRB-001", "--at", "HEAD",
                                 "--json"]),),
    "issue history activity": ((READ, ["history", "activity", "--since", "",
                                       "--json"]),),
    "issue admin validate": ((READ, ["admin", "validate", "--json",
                                     "@PRB-001"]),),
    "issue admin reap": ((READ, ["admin", "reap", "--json"]),),
    # --- mutating and scaffolding verbs ------------------------------------
    "issue create": ((WRITE, ["create", "made by the probe", "chore",
                              "medium", "--json"]),),
    "issue note": ((WRITE, ["note", "PRB-001", "probe note", "--json"]),),
    "issue edit": ((WRITE, ["edit", "PRB-001", "--set", "priority=high",
                            "--json"]),),
    "issue dep": ((WRITE, ["dep", "add", "PRB-001", "blocks:PRB-002",
                           "--json"]),
                  (WRITE, ["dep", "list", "PRB-001", "--json"])),
    "issue transition": ((WRITE, ["transition", "PRB-002", "--claim",
                                  "probe-actor", "--json"]),
                         (WRITE, ["transition", "PRB-002", "--renew",
                                  "--json"]),
                         (WRITE, ["transition", "PRB-002", "--release",
                                  "--json"]),
                         (WRITE, ["transition", "PRB-002", "in-progress",
                                  "--json"])),
    "issue start": ((WRITE, ["start", "PRB-003", "--json"]),),
    "issue complete": ((WRITE, ["complete", "PRB-004", "--summary", "ok",
                                "--json"]),),
    "issue block": ((WRITE, ["block", "PRB-005", "--reason", "waiting",
                             "--json"]),),
    "issue unblock": ((WRITE, ["unblock", "PRB-005", "--reason", "cleared",
                               "--json"]),),
    "issue cancel": ((WRITE, ["cancel", "PRB-006", "--reason", "nope",
                              "--json"]),),
    "issue admin reindex": ((WRITE, ["admin", "reindex", "--json"]),),
    "issue admin snapshot": ((WRITE, ["admin", "snapshot", "--json"]),),
    "issue admin init-store": ((WRITE, ["admin", "init-store", "@store2",
                                        "--non-interactive", "--json"]),),
    "issue admin init-project": ((WRITE, ["admin", "init-project", "--store",
                                          "@store", "--prefix", "PRC",
                                          "--non-interactive", "--json"]),),
    "issue admin migrate": ((WRITE, ["admin", "migrate", "--project",
                                     "@legacy", "--store", "@store",
                                     "--prefix", "LEG", "--dry-run",
                                     "--json"]),),
    "issue admin install-skills": ((WRITE, ["admin", "install-skills",
                                            "--json"]),),
    "issue admin selftest": ((WRITE, ["admin", "selftest", "--json"]),),
}


def date_shaped_paths(doc):
    """{dotted path: [value, ...]} for every date-shaped scalar in `doc`.

    List indices collapse to `[]`, so a per-row field reads as
    `issues[].latest.date` however many rows there are -- the path names the
    FIELD, which is what a declaration can address.
    """
    found = {}

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for value in node:
                walk(value, f"{path}[]")
        elif isinstance(node, str) and DATE_SHAPED.match(node):
            found.setdefault(path, []).append(node)

    walk(doc, "")
    return found


class AmbientFieldFixture(unittest.TestCase):
    """One fixture store, carrying no date that could pass for today.

    `HOME` and `ISSUE_STORE_ROOT` are pinned into the temp tree: `admin
    install-skills` writes under HOME and every other verb resolves a store,
    so an unpinned run would reach the operator's real store and real skills.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="im262-ambient-"))
        cls.home = cls.tmp / "home"
        cls.home.mkdir()
        cls.store = cls.tmp / "store"
        cls.project = cls.tmp / "project"
        cls.project.mkdir()
        cls.measured = {}

        cls._run(["admin", "init-store", str(cls.store), "--non-interactive"])
        cls._git(cls.project, "init", "--quiet")
        cls._run(["admin", "init-project", "--store", str(cls.store),
                  "--prefix", "PRB", "--non-interactive"], cwd=cls.project)
        # One issue per mutating recipe: the WRITE phase runs in sorted verb
        # order, so two recipes sharing an id would fight (`complete` reached
        # `start`'s issue first and left it `done`).
        for title in ("probe alpha", "probe beta", "probe gamma",
                      "probe delta", "probe epsilon", "probe zeta"):
            cls._run(["create", title, "chore", "medium"], cwd=cls.project)

        cls._backdate_store()
        cls._make_legacy_project()
        # Issue files only. `INDEX.md` carries its own regeneration stamp
        # ("Regenerated by reindex-issues on <date>"), which is a store
        # artifact rather than verb output -- `issue index` computes the
        # board live from frontmatter and never reads it back, so it cannot
        # reach any `--json` document.
        cls.pristine = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(cls.store.rglob("*.md"))
            if path.name != "INDEX.md"}
        cls._measure()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- fixture construction ---------------------------------------------

    @classmethod
    def _backdate_store(cls):
        """Rewrite every date in the store, then re-commit it at a 2020 date.

        Three sources of a date have to move or the discriminator is unsound:
        issue frontmatter, the body's own note lines, and the git commit dates
        the history verbs read. The frontmatter rewrite is a direct file write
        rather than `issue edit` because `created` is a guarded field -- which
        is correct, and is why the fixture does it out of band.
        """
        for path in sorted(cls.store.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"(?m)^created: .*$", f"created: {FIXTURE_DATE}",
                          text)
            text = re.sub(r"(?m)^updated: .*$", f"updated: {FIXTURE_DATE}",
                          text)
            text = re.sub(r"\d{4}-\d{2}-\d{2}", FIXTURE_DATE, text)
            path.write_text(text, encoding="utf-8")
        cls._run(["admin", "reindex"], cwd=cls.project)
        # Squash the whole fixture history into one 2020-dated commit, so no
        # commit the history verbs can reach carries a present-day date.
        cls._git(cls.store, "add", "-A")
        cls._git(cls.store, "commit", "--quiet", "--allow-empty",
                 "--date", FIXTURE_COMMIT_DATE, "-m", "fixture store")
        head = subprocess.run(["git", "-C", str(cls.store), "rev-parse",
                               "HEAD"], capture_output=True, text=True,
                              env=cls._env()).stdout.strip()
        cls._git(cls.store, "checkout", "--quiet", "--orphan", "fixture")
        cls._git(cls.store, "add", "-A")
        cls._git(cls.store, "commit", "--quiet",
                 "--date", FIXTURE_COMMIT_DATE, "-m", "fixture store")
        del head

    @classmethod
    def _make_legacy_project(cls):
        """A pre-store project, so `admin migrate --dry-run` has an input."""
        legacy = cls.tmp / "legacy"
        (legacy / ".issues").mkdir(parents=True)
        (legacy / ".issues" / ".prefix").write_text("LEG\n", encoding="utf-8")
        (legacy / ".issues" / "LEG-001.md").write_text(
            "---\n"
            "id: LEG-001\n"
            'title: "legacy probe"\n'
            "type: chore\n"
            "status: open\n"
            "priority: medium\n"
            f"created: {FIXTURE_DATE}\n"
            f"updated: {FIXTURE_DATE}\n"
            'branch: ""\n'
            "tags: []\n"
            "changelog:\n"
            "---\n\n"
            "## Summary\n\nLegacy fixture.\n",
            encoding="utf-8")
        cls._git(legacy, "init", "--quiet")
        cls._git(legacy, "add", "-A")
        cls._git(legacy, "commit", "--quiet", "--date", FIXTURE_COMMIT_DATE,
                 "-m", "legacy fixture")

    # --- measurement -------------------------------------------------------

    @classmethod
    def _measure(cls):
        """Drive every recipe, read modes first, and record what each emits.

        The run date is captured either side of the sweep so a run straddling
        midnight compares against both dates rather than failing spuriously.
        """
        cls.run_dates = {datetime.date.today().isoformat()}
        for phase in (READ, WRITE):
            for path, modes in sorted(RECIPES.items()):
                for mode, argv in modes:
                    if mode != phase:
                        continue
                    cls.measured.setdefault(path, []).append(
                        cls._invoke(path, argv))
        cls.run_dates.add(datetime.date.today().isoformat())

    @classmethod
    def _invoke(cls, path, argv):
        resolved, cwd = [], cls.project
        for token in argv:
            if token == "@store":
                resolved.append(str(cls.store))
            elif token == "@store2":
                resolved.append(str(cls.tmp / "store2"))
            elif token == "@legacy":
                resolved.append(str(cls.tmp / "legacy"))
            elif token == "@PRB-001":
                resolved.append(str(cls.store / "PRB" / "PRB-001.md"))
            else:
                resolved.append(token)
        if path == "issue admin init-project":
            # Registration keys on the calling repo's git identity, so it
            # needs a repo of its own rather than the already-registered one.
            cwd = cls.tmp / "project2"
            if not cwd.exists():
                cwd.mkdir()
                cls._git(cwd, "init", "--quiet")
                cls._git(cwd, "commit", "--quiet", "--allow-empty",
                         "--date", FIXTURE_COMMIT_DATE, "-m", "init")
        result = cls._run(resolved, cwd=cwd)
        return {"argv": resolved, "exit": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}

    # --- plumbing ----------------------------------------------------------

    @classmethod
    def _env(cls):
        env = dict(os.environ)
        env["HOME"] = str(cls.home)
        env["ISSUE_STORE_ROOT"] = str(cls.store)
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = FIXTURE_NAME
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = FIXTURE_EMAIL
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = FIXTURE_COMMIT_DATE
        return env

    @classmethod
    def _git(cls, cwd, *argv):
        return subprocess.run(["git", "-C", str(cwd)] + list(argv),
                              capture_output=True, text=True, env=cls._env())

    @classmethod
    def _run(cls, argv, cwd=None):
        return subprocess.run(
            [sys.executable, str(ISSUE_CLI)] + list(argv),
            capture_output=True, text=True, env=cls._env(),
            cwd=str(cwd if cwd else cls.tmp))

    # --- derived views the checks read ------------------------------------

    def ambient_paths(self, path):
        """Dotted paths this verb emitted carrying THIS run's clock."""
        found = set()
        for run in self.measured.get(path, []):
            try:
                doc = json.loads(run["stdout"])
            except ValueError:
                continue
            for field, values in date_shaped_paths(doc).items():
                if any(v.startswith(d) for v in values
                       for d in self.run_dates):
                    found.add(field)
        return found

    def emitted_paths(self, path):
        """Every dotted path this verb emitted, ambient or not."""
        found = set()
        for run in self.measured.get(path, []):
            try:
                doc = json.loads(run["stdout"])
            except ValueError:
                continue
            found |= set(date_shaped_paths(doc))
        return found


class AmbientFieldDeclarationTests(AmbientFieldFixture):
    """Measured ambient fields and declared ones are the same set."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        manifest = json.loads(cli_surface.render_current())
        cls.by_path = {v["path"]: v for v in manifest["verbs"]}
        cls.json_verbs = {
            v["path"] for v in manifest["verbs"]
            if any(f["name"] == "--json" for f in v["flags"])}

    def declared(self, path, verb=None):
        verb = verb if verb is not None else self.by_path[path]
        return set(verb["generation_fields"]) | set(verb["record_fields"])

    def test_every_json_verb_is_exercised(self):
        """The recipe table's completeness is derived, not trusted.

        Without this, a new verb is simply absent from the table and every
        check below passes over it -- the silent-coverage-gap shape the
        IM-261 audit found in three separate guards.
        """
        self.assertEqual(
            self.json_verbs - set(RECIPES), set(),
            "a verb registering --json has no recipe in RECIPES, so no "
            "ambient field of its own is measured; add one")
        self.assertEqual(
            set(RECIPES) - self.json_verbs, set(),
            "RECIPES names a verb that does not register --json")

    def test_every_recipe_produced_parseable_json(self):
        """An unparseable run would silently measure nothing."""
        for path, runs in sorted(self.measured.items()):
            for run in runs:
                with self.subTest(verb=path, argv=" ".join(run["argv"][:4])):
                    try:
                        json.loads(run["stdout"])
                    except ValueError:
                        self.fail(
                            f"{path} emitted no JSON document "
                            f"(exit {run['exit']}): "
                            f"stdout={run['stdout'][:120]!r} "
                            f"stderr={run['stderr'].strip()[:200]!r}")

    def test_measured_ambient_fields_are_declared(self):
        """The IM-262 bug: an ambient value reaching --json undeclared."""
        for path in sorted(self.json_verbs):
            with self.subTest(verb=path):
                undeclared = self.ambient_paths(path) - self.declared(path)
                self.assertEqual(
                    undeclared, set(),
                    f"{path} --json emits ambient-derived field(s) "
                    f"{sorted(undeclared)} that no declaration records. "
                    f"Declare each in SURFACE_GENERATION_FIELDS (a value "
                    f"describing this render) or SURFACE_RECORD_FIELDS (a "
                    f"value persisted into the issue) and regenerate the "
                    f"manifest -- see surface.explicit-output-mode.")

    def test_declared_fields_are_observed(self):
        """The other direction: a declaration guarding nothing.

        A misspelled or dead path reads as coverage. `history activity.since`
        is not date-shaped, so this check requires only that the declared
        path is PRESENT in some mode's output -- which still fails a typo and
        still fails a field the recipes never reach.
        """
        for path in sorted(self.json_verbs):
            declared = self.declared(path)
            if not declared:
                continue
            with self.subTest(verb=path):
                present = set()
                for run in self.measured.get(path, []):
                    try:
                        doc = json.loads(run["stdout"])
                    except ValueError:
                        continue
                    present |= _present_paths(doc)
                missing = declared - present
                self.assertEqual(
                    missing, set(),
                    f"{path} declares ambient field(s) {sorted(missing)} "
                    f"that no exercised mode emits: either the declaration "
                    f"is dead (remove it) or RECIPES does not reach the "
                    f"mode that emits it (add one).")

    # --- gate-bites: each guard above is proven to fail on a violation ----

    def test_guard_bites_an_undeclared_ambient_field(self):
        """Drop a real declaration; the measurement must notice."""
        verb = dict(self.by_path["issue create"])
        verb["record_fields"] = []
        undeclared = self.ambient_paths("issue create") - self.declared(
            "issue create", verb=verb)
        self.assertEqual(
            undeclared, {"created"},
            "dropping create's record-field declaration left the ambient "
            "`created` field unflagged -- the guard cannot bite")

    def test_guard_bites_a_dead_declaration(self):
        """Declare a field no verb emits; the presence check must notice."""
        present = set()
        for run in self.measured.get("issue create", []):
            present |= _present_paths(json.loads(run["stdout"]))
        self.assertNotIn(
            "claim.lease_until", present,
            "`create` does not emit a lease stamp, so declaring one must "
            "read as a dead declaration")

    def test_guard_bites_a_missing_recipe(self):
        """Remove a verb from the table; completeness must notice."""
        self.assertEqual(
            self.json_verbs - (set(RECIPES) - {"issue create"}),
            {"issue create"},
            "the completeness check cannot see a verb missing from RECIPES")

    def test_fixture_carries_no_present_day_date(self):
        """The discriminator's own precondition, asserted rather than assumed.

        Snapshotted before the write phase: afterwards the store legitimately
        carries today's dates, because the mutating recipes just wrote them.

        If a fixture file or commit carried today's date, every read verb
        echoing it would read as ambient and the suite would fail confusingly
        -- or worse, a genuinely ambient field would be masked.
        """
        self.assertTrue(self.pristine, "the fixture store has no issue files")
        for name, text in sorted(self.pristine.items()):
            with self.subTest(file=name):
                for date in self.run_dates:
                    self.assertNotIn(
                        date, text,
                        f"{name} carries today's date, so a disk-read "
                        f"value is indistinguishable from an ambient one")


def _present_paths(doc):
    """Every dotted path in `doc`, whatever the value's shape."""
    found = set()

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                found.add(child)
                walk(value, child)
        elif isinstance(node, list):
            for value in node:
                walk(value, f"{path}[]")

    walk(doc, "")
    return found


if __name__ == "__main__":
    unittest.main()
