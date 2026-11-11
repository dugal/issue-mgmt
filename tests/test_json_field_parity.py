#!/usr/bin/env python3
"""Declared `--json` field sets must match the keys each verb emits (IM-263).

Every field-selecting read verb declares a ``VALID_JSON_FIELDS`` tuple that
``jsonfields.parse_fields`` validates ``--json <fields>`` against. Nothing
compared those declarations to the keys the verbs actually emit, so
``issue list`` shipped for two releases declaring seven fields while emitting
eight: ``--json title_match`` exited 2 on a key every row carried. The cause
was two hand-maintained copies of one row builder's key list, of which IM-232
updated only one.

This module closes that gap by EXECUTION rather than by restatement: it drives
each verb through the real CLI against a temp store, unions the top-level keys
of the per-object list the verb projects, and requires exact equality with the
declared tuple. Both directions are defects:

  * emitted but not declared -- the IM-263 bug; a field the caller can see but
    cannot select.
  * declared but never emitted -- a dead or misspelled field name that
    ``--json`` accepts and then silently drops from every row.

Each verb is therefore exercised across every mode that contributes keys --
``show`` alone needs three (plain, ``--section``, and a missing id) to reach
its full shape -- so no verb needs a conditional-field waiver list.

Run:
    python3 -m unittest tests.test_json_field_parity -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ISSUE_CLI = REPO_ROOT / "scripts" / "issue"
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fixture_identity import FIXTURE_EMAIL  # noqa: E402

import search  # noqa: E402


def _load_verb_module(module_name: str):
    """Import a lib/cli verb module for its VALID_JSON_FIELDS declaration."""
    import importlib

    return importlib.import_module("cli.%s" % module_name)


# Per verb: the module holding VALID_JSON_FIELDS, the argv modes that
# together produce every declared key, and how to reach the per-object
# list(s) the verb projects.
#
# `extract` returns the list of per-object dicts that field selection
# applies to. For a wrapper-object verb that is the list the wrapper
# carries, not the wrapper itself (output.json-field-selection); for
# `ready` that is the `ready` list only -- `filtered_out` is explanatory
# output with a fixed shape and no field selection of its own.
def _top_level(payload):
    return payload if isinstance(payload, list) else [payload]


def _wrapped(key):
    def extract(payload):
        return payload[key]

    return extract


def _index_issues(payload):
    rows = []
    for group in payload["groups"]:
        rows.extend(group["issues"])
    return rows


VERBS = (
    {
        "name": "list",
        "module": "list",
        "modes": (["list", "--prefix", "PRB", "--json"],),
        "extract": _top_level,
    },
    {
        "name": "search",
        "module": "search",
        "modes": (["search", "probe", "--prefix", "PRB", "--json"],),
        "extract": _top_level,
    },
    {
        "name": "show",
        "module": "show",
        # Three modes: plain (full_text), --section (sections,
        # sections_requested), and a missing id (error).
        "modes": (
            ["show", "PRB-001", "--json"],
            ["show", "PRB-001", "--section", "Summary", "--json"],
            ["show", "PRB-999", "--json"],
        ),
        "extract": _top_level,
    },
    {
        "name": "index",
        "module": "index",
        "modes": (["index", "--prefix", "PRB", "--json"],),
        "extract": _index_issues,
    },
    {
        "name": "ready",
        "module": "ready",
        "modes": (["ready", "--prefix", "PRB", "--json"],),
        "extract": _wrapped("ready"),
    },
    {
        "name": "history show",
        "module": "history_show",
        "modes": (["history", "show", "PRB-001", "--json"],),
        "extract": _wrapped("commits"),
    },
    {
        "name": "history search",
        "module": "history_search",
        "modes": (["history", "search", "probe", "--json"],),
        "extract": _wrapped("commits"),
    },
    {
        "name": "history activity",
        "module": "history_activity",
        "modes": (["history", "activity", "--since", "", "--json"],),
        "extract": _wrapped("issues"),
    },
    {
        "name": "history at",
        "module": "history_at",
        # A point read: the payload object itself is the projected object.
        "modes": (["history", "at", "PRB-001", "--at", "HEAD", "--json"],),
        "extract": _top_level,
    },
)


class JsonFieldParityBase(unittest.TestCase):
    """One temp store, populated so every verb above emits at least one row.

    The store is committed so the history verbs have something to read, and
    `ISSUE_STORE_ROOT` is pinned to the temp tree so the operator's real
    store is never touched.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="im263-json-parity-"))
        cls.store = cls.tmp / "store"
        cls.project = cls.tmp / "project"
        cls.project.mkdir(parents=True)

        cls._run_raw(["admin", "init-store", str(cls.store), "--non-interactive"])
        cls._git(cls.project, "init", "--quiet")
        cls._git(cls.project, "config", "user.email", FIXTURE_EMAIL)
        cls._git(cls.project, "config", "user.name", "fixture")
        cls._run_raw(
            ["admin", "init-project", "--store", str(cls.store),
             "--prefix", "PRB", "--non-interactive"],
            cwd=cls.project,
        )
        # Two issues: one open (so `ready` has a row), one with a body the
        # text query matches (so `search` has a row).
        cls._run_raw(["create", "probe issue alpha", "feature", "medium"],
                     cwd=cls.project)
        cls._run_raw(["create", "probe issue beta", "bug", "high"],
                     cwd=cls.project)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _env(cls):
        env = dict(os.environ)
        env["ISSUE_STORE_ROOT"] = str(cls.store)
        return env

    @classmethod
    def _git(cls, cwd, *argv):
        subprocess.run(["git"] + list(argv), cwd=str(cwd),
                       capture_output=True, text=True, check=False)

    @classmethod
    def _run_raw(cls, argv, cwd=None):
        return subprocess.run(
            [sys.executable, str(ISSUE_CLI)] + list(argv),
            capture_output=True, text=True, env=cls._env(),
            cwd=str(cwd if cwd else cls.tmp),
        )

    def emitted_keys(self, spec):
        """Union of top-level per-object keys across all of a verb's modes."""
        keys = set()
        for argv in spec["modes"]:
            result = self._run_raw(argv, cwd=self.project)
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                self.fail(
                    "%s: `issue %s` did not emit parseable JSON on stdout "
                    "(rc=%d): %s\nstdout=%r\nstderr=%r"
                    % (spec["name"], " ".join(argv), result.returncode, e,
                       result.stdout, result.stderr)
                )
            rows = spec["extract"](payload)
            self.assertTrue(
                rows,
                "%s: `issue %s` emitted no rows, so this mode proves nothing "
                "about the declared field set -- fix the fixture, do not "
                "weaken the assertion" % (spec["name"], " ".join(argv)),
            )
            for row in rows:
                keys.update(row.keys())
        return keys


class TestDeclaredMatchesEmitted(JsonFieldParityBase):
    """Every field-selecting verb: declared set == emitted keys, exactly."""

    def test_every_verb_declares_what_it_emits(self):
        failures = []
        for spec in VERBS:
            module = _load_verb_module(spec["module"])
            declared = set(module.VALID_JSON_FIELDS)
            emitted = self.emitted_keys(spec)
            if declared == emitted:
                continue
            failures.append(
                "%s: emitted-but-not-declared=%s declared-but-not-emitted=%s"
                % (spec["name"],
                   sorted(emitted - declared) or "none",
                   sorted(declared - emitted) or "none")
            )
        self.assertEqual(
            failures, [],
            "VALID_JSON_FIELDS diverges from the keys these verbs emit. A key "
            "emitted but not declared is rejected by `--json <field>` though "
            "every row carries it (the IM-263 bug); a key declared but not "
            "emitted is accepted and then silently dropped.\n  "
            + "\n  ".join(failures),
        )

    def test_selecting_every_declared_field_succeeds(self):
        """The declared set is not just equal -- it is wholly selectable.

        Equality above compares names; this proves `--json <all fields>`
        actually parses and projects for each verb, which is the operation a
        caller performs and the one that failed on `list --json title_match`.
        """
        for spec in VERBS:
            module = _load_verb_module(spec["module"])
            fields = ",".join(module.VALID_JSON_FIELDS)
            argv = list(spec["modes"][0])
            # Replace the bare --json with the full field list.
            argv[argv.index("--json")] = "--json"
            argv.insert(argv.index("--json") + 1, fields)
            result = self._run_raw(argv, cwd=self.project)
            self.assertNotEqual(
                result.returncode, 2,
                "%s: `issue %s` rejected its own declared field set: %s"
                % (spec["name"], " ".join(argv), result.stderr.strip()),
            )


class TestParityCheckBites(JsonFieldParityBase):
    """Gate-bites: the parity check must fail on an injected divergence.

    Per the IM-261 taxonomy, a guard is only worth committing once the
    violation it exists to catch has been shown to trip it.
    """

    def test_bites_on_the_real_im263_divergence(self):
        """The pre-fix `list` declaration must fail the comparison.

        This is the exact tuple `lib/cli/list.py` carried through v3.3.1 --
        the eight-key row minus `title_match`.
        """
        pre_fix_declared = {
            "id", "file", "title", "status", "priority", "type", "body_matches",
        }
        emitted = self.emitted_keys(VERBS[0])  # list
        self.assertNotEqual(
            pre_fix_declared, emitted,
            "the pre-IM-263 declaration must not compare equal to the "
            "emitted keys, or this test proves nothing",
        )
        self.assertEqual(
            sorted(emitted - pre_fix_declared), ["title_match"],
            "the injected divergence should be exactly the field IM-263 "
            "reported as unselectable",
        )

    def test_bites_on_a_declared_but_unemitted_field(self):
        """The other direction: a phantom declared field is also a failure."""
        emitted = self.emitted_keys(VERBS[0])  # list
        phantom_declared = emitted | {"not_a_real_field"}
        self.assertNotEqual(phantom_declared, emitted)
        self.assertEqual(
            sorted(phantom_declared - emitted), ["not_a_real_field"],
        )


class TestJsonFieldsIsDerived(unittest.TestCase):
    """`IssueMatch.json_fields()` must track `to_dict`, not restate it.

    `list` and `search` both source their declaration from this classmethod,
    so a field added to the row builder becomes selectable on both verbs
    without either being edited -- which is what makes correcting the data
    unnecessary. If this ever reverts to a literal, the test fails.
    """

    def test_matches_a_real_rows_keys(self):
        row = search.IssueMatch(
            id="PRB-001", file=Path("PRB/PRB-001.md"), title="t",
            status="open", priority="medium", type="bug",
        )
        self.assertEqual(
            tuple(row.to_dict().keys()), search.IssueMatch.json_fields(),
        )

    def test_tracks_a_new_field_without_editing_the_declaration(self):
        class ExtendedMatch(search.IssueMatch):
            def to_dict(self):
                d = super().to_dict()
                d["new_field"] = True
                return d

        self.assertIn("new_field", ExtendedMatch.json_fields())
        self.assertNotIn("new_field", search.IssueMatch.json_fields())

    def test_list_and_search_share_the_derived_declaration(self):
        list_mod = _load_verb_module("list")
        search_mod = _load_verb_module("search")
        self.assertEqual(
            list_mod.VALID_JSON_FIELDS, search_mod.VALID_JSON_FIELDS,
            "cli.list promises its output shape matches `issue search` run "
            "with an empty query (cli.list); the two field sets must be the "
            "same object-shape declaration, not two copies",
        )
        self.assertEqual(
            list_mod.VALID_JSON_FIELDS, search.IssueMatch.json_fields(),
        )


if __name__ == "__main__":
    unittest.main()
