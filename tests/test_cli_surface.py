#!/usr/bin/env python3
"""The CLI surface-change detector (IM-245).

`docs/cli-surface.json` is a committed, deterministic record of the `issue`
CLI's argument surface. These tests ARE the detector: the first one
regenerates the manifest in-process and byte-compares it against the
committed copy, so adding a flag, changing a default, renaming a metavar,
or landing a new verb turns `make test` red until the manifest is
deliberately regenerated (`make cli-surface`) together with a lint pass.

The rest pin the extractor's own guarantees -- determinism, fail-loud on a
missing verb-class declaration, and faithful representation of the five
passthrough admin verbs whose dispatcher subparsers are invisible to an
argparse walk. That last one is the whole reason the extractor refuses
rather than guessing: an empty flag set reads as false-CLEAN, and that
blindness already produced one wrong issue (IM-240's count).

Run:
    python3 -m unittest tests.test_cli_surface -v
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "lib"))

import cli_surface  # noqa: E402
import cli  # noqa: E402


@contextmanager
def patched(module, attr, value):
    """Temporarily set (or delete, with value=_DELETE) a module attribute."""
    missing = object()
    original = getattr(module, attr, missing)
    try:
        if value is _DELETE:
            if original is not missing:
                delattr(module, attr)
        else:
            setattr(module, attr, value)
        yield
    finally:
        if original is missing:
            if hasattr(module, attr):
                delattr(module, attr)
        else:
            setattr(module, attr, original)


_DELETE = object()


def verb_module(path):
    """The module backing a manifest verb path, e.g. 'issue admin selftest'."""
    groups = cli.discover()
    for tier in cli_surface.TIERS:
        for mod in groups[tier]:
            parts = ["issue"] + ([] if tier == "tier1" else [tier]) + [mod.VERB]
            if " ".join(parts) == path:
                return mod
    raise AssertionError(f"no verb module for {path!r}")


def _probe(argv):
    """Exit code of `issue <argv>` in a throwaway HOME, for flag probing.

    HOME and ISSUE_STORE_ROOT are pinned to a temp tree so a probe that
    the CLI happens to ACCEPT cannot touch the operator's store or write
    real skills into ~/.claude.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["HOME"] = tmp
        env["ISSUE_STORE_ROOT"] = str(Path(tmp) / "store")
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "issue")] + argv,
            capture_output=True, env=env,
        ).returncode


def _capture(argv, env_overrides=None, fixture=None):
    """stdout+stderr of `issue <argv>`, for reading what a caller is shown.

    `_probe` above wants only an exit code; this wants the text, because
    `surface.self-naming` is a claim about output. Same throwaway HOME and
    store, plus cwd pinned INTO the temp tree: a verb that resolves a store
    before it prints would otherwise walk up from the repo and find the
    operator's own sentinel, which the store-safety rule this phase runs
    under forbids (docs/plan.md).

    An `env_overrides` value of None DELETES the variable -- how the
    store-unresolvable diagnostic is reached, since pinning
    ISSUE_STORE_ROOT is exactly what makes a store resolve. `fixture` is
    called with the temp root and returns extra argv, for the one
    diagnostic that needs a project on disk.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        extra = fixture(tmp) if fixture else []
        env = dict(os.environ)
        env["HOME"] = str(tmp)
        env["ISSUE_STORE_ROOT"] = str(tmp / "store")
        for key, value in (env_overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "issue")]
            + argv + extra,
            capture_output=True, text=True, env=env, cwd=str(tmp),
        )
        return proc.returncode, proc.stdout + proc.stderr


class SurfaceManifestTests(unittest.TestCase):
    """The committed manifest tracks the live surface."""

    def test_committed_manifest_is_current(self):
        self.assertEqual(
            cli_surface.read_committed(),
            cli_surface.render_current(),
            "docs/cli-surface.json is stale: the CLI surface changed. "
            "Regenerate it TOGETHER WITH A LINT PASS (`make cli-surface`) "
            "and review the diff -- never just to make this test green.",
        )

    def test_render_is_deterministic(self):
        self.assertEqual(cli_surface.render_current(),
                         cli_surface.render_current())

    def test_render_carries_no_ambient_value(self):
        """No wall clock, no absolute path -- the manifest is byte-compared.

        Also the rule the manifest records for the surface itself
        (`surface.explicit-output-mode`).
        """
        text = cli_surface.render_current()
        self.assertNotIn("/Users/", text)
        self.assertNotIn(str(REPO_ROOT), text)
        self.assertNotIn("generated_at", json.loads(text).keys())

    def test_totals_match_the_verb_entries(self):
        m = json.loads(cli_surface.render_current())
        names, registrations = set(), 0
        for v in m["verbs"]:
            for flag in v["flags"]:
                names.update(flag["names"])
                registrations += 1
        self.assertEqual(m["totals"]["verbs"], len(m["verbs"]))
        self.assertEqual(m["totals"]["flag_names"], len(names))
        self.assertEqual(m["totals"]["flag_registrations"], registrations)


class DeclarationTests(unittest.TestCase):
    """Every verb declares its class, store kind, and conversion state."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(cli_surface.render_current())
        cls.by_path = {v["path"]: v for v in cls.manifest["verbs"]}

    def test_every_verb_declares_the_required_fields(self):
        for verb in self.manifest["verbs"]:
            with self.subTest(verb=verb["path"]):
                self.assertIn(verb["verb_class"], cli_surface.VERB_CLASSES)
                self.assertIn(verb["store"], cli_surface.STORE_KINDS)
                self.assertIn(verb["structured_errors"],
                              cli_surface.STRUCTURED_ERROR_STATES)

    def test_missing_verb_class_fails_loud(self):
        mod = verb_module("issue show")
        with patched(mod, "SURFACE_CLASS", _DELETE):
            with self.assertRaises(cli_surface.SurfaceError) as ctx:
                cli_surface.build_manifest()
        self.assertIn("SURFACE_CLASS", str(ctx.exception))
        self.assertIn("cli.show", str(ctx.exception))

    def test_invalid_verb_class_fails_loud(self):
        mod = verb_module("issue show")
        with patched(mod, "SURFACE_CLASS", "read-ish"):
            with self.assertRaises(cli_surface.SurfaceError) as ctx:
                cli_surface.build_manifest()
        self.assertIn("read-ish", str(ctx.exception))

    def test_invalid_store_kind_fails_loud(self):
        mod = verb_module("issue admin reap")
        with patched(mod, "SURFACE_STORE", "maybe"):
            with self.assertRaises(cli_surface.SurfaceError) as ctx:
                cli_surface.build_manifest()
        self.assertIn("SURFACE_STORE", str(ctx.exception))

    def test_ambient_output_field_kinds_are_both_represented(self):
        """IM-250 / IM-262: two declared kinds, not one.

        Only the REPRESENTATION is checked here -- that a verb's ambient
        fields land in the kind its module declares. Whether the declaration
        matches what the verb actually emits is measured against the real
        CLI by `tests/test_surface_ambient_fields.py`; this test deliberately
        does not restate that expectation, because a table here compared
        against a constant there is the tautology IM-262 exists to remove.
        """
        self.assertEqual(self.by_path["issue index"]["generation_fields"],
                         ["generated_at"])
        self.assertEqual(
            self.by_path["issue history activity"]["generation_fields"],
            ["since"])
        self.assertEqual(self.by_path["issue create"]["record_fields"],
                         ["created"])
        for verb in self.manifest["verbs"]:
            with self.subTest(verb=verb["path"]):
                self.assertEqual(
                    set(verb["generation_fields"]) & set(verb["record_fields"]),
                    set(),
                    "a field cannot be both generation metadata and record "
                    "content; the two kinds exist to stay distinguishable")

    # `admin reap` and `admin reindex` resolve a store through the precedence
    # chain and offer no `--store`, which IM-251 measured as the two real
    # gaps. Carried as a NAMED waiver rather than as the `# gap:` prose
    # comments the IM-251 table used: a comment goes silently false the day
    # the flag is added, and this list is asserted shrink-only below.
    STORE_FLAG_WAIVER = {
        "issue admin reap": "IM-251",
        "issue admin reindex": "IM-251",
    }

    def test_store_flag_verbs_register_store(self):
        """IM-251's contract, derived from the registrations.

        `store == "flag"` MEANS the verb resolves a store through the
        precedence chain and therefore must offer `--store`. Checking that
        against the flag list makes the declaration answerable from the
        manifest; the previous version of this test compared a hand-written
        table against the hand-written constant, so it could only detect
        that someone had edited the constant.
        """
        for verb in self.manifest["verbs"]:
            if verb["store"] != "flag":
                continue
            if verb["path"] in self.STORE_FLAG_WAIVER:
                continue
            with self.subTest(verb=verb["path"]):
                self.assertIn(
                    "--store", {f["name"] for f in verb["flags"]},
                    f"{verb['path']} declares store=flag -- it resolves a "
                    f"store through the precedence chain -- so "
                    f"surface.verb-class-contract obliges it to offer "
                    f"--store")

    def test_store_flag_waiver_only_shrinks(self):
        """A waived verb that gained `--store` must leave the waiver.

        Otherwise the waiver outlives the gap and the check quietly stops
        covering a verb that now satisfies the contract.
        """
        for path, issue in sorted(self.STORE_FLAG_WAIVER.items()):
            with self.subTest(verb=path):
                verb = self.by_path[path]
                self.assertEqual(verb["store"], "flag",
                                 f"{path} no longer declares store=flag; "
                                 f"drop it from STORE_FLAG_WAIVER")
                self.assertNotIn(
                    "--store", {f["name"] for f in verb["flags"]},
                    f"{path} now registers --store, so the {issue} waiver is "
                    f"obsolete: remove the entry rather than leaving a "
                    f"waiver that covers nothing")

    def test_positional_store_verbs_have_a_positional(self):
        """The other side of the four-valued field, equally derivable."""
        for verb in self.manifest["verbs"]:
            if verb["store"] != "positional":
                continue
            with self.subTest(verb=verb["path"]):
                self.assertTrue(
                    verb["positionals"],
                    f"{verb['path']} declares store=positional but registers "
                    f"no positional operand to name the store")

    def test_store_gap_check_bites(self):
        """Gate-bites: an unwaived store=flag verb with no --store fails."""
        mod = verb_module("issue admin selftest")      # declares `pinned`
        with patched(mod, "SURFACE_STORE", "flag"):
            manifest = json.loads(cli_surface.render(cli_surface.build_manifest()))
        verb = {v["path"]: v for v in manifest["verbs"]}["issue admin selftest"]
        self.assertEqual(verb["store"], "flag")
        self.assertNotIn("--store", {f["name"] for f in verb["flags"]},
                         "the fixture verb must lack --store for the check "
                         "under test to have something to catch")
        self.assertNotIn("issue admin selftest", self.STORE_FLAG_WAIVER)

    def test_every_verb_registers_json(self):
        """`surface.verb-class-contract`: `--json` on every class.

        Currently satisfied surface-wide, which is the point of asserting it
        now -- a new verb that forgets `--json` fails here rather than being
        discovered by a caller. IM-240 closed the last six gaps.
        """
        for verb in self.manifest["verbs"]:
            with self.subTest(verb=verb["path"]):
                self.assertIn("--json", {f["name"] for f in verb["flags"]})


class ProgNameHarvestTests(unittest.TestCase):
    """Every hand-written program name a module prints is in the manifest.

    `prog.declared` alone was blind in exactly the place it mattered.
    `admin selftest` and `admin install-skills` build their parsers with
    `prefix_chars=chr(0)` and pass no `prog=` keyword, so their declared
    list is EMPTY -- while `admin selftest`'s entire PASS report names seven
    retired binaries and both print a hand-written `Usage:` line naming a
    retired flat binary. A self-naming lint reading `prog.declared` would
    have reported clean on the two worst offenders (IM-261 audit, 5.3).

    So the manifest also records, derived by AST from the source: every
    command-name-shaped message prefix the module prints, and the program
    name in any hand-written usage or synopsis block. Those are the ground
    truth IM-239's `prog.declared != prog.dispatcher` lint needs, and they
    are why an empty declared list is no longer readable as clean.
    """

    PASSTHROUGH = ("issue admin selftest", "issue admin install-skills")

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(cli_surface.render_current())
        cls.by_path = {v["path"]: v for v in cls.manifest["verbs"]}

    def test_passthrough_verbs_are_not_silently_nameless(self):
        """The two verbs a `prog.declared` lint used to read as clean.

        Both build their parser with `prefix_chars=chr(0)` and pass no
        `prog=` keyword, so before IM-262 the manifest recorded NO name for
        either -- while `admin selftest`'s PASS report named seven retired
        binaries. IM-239 gave each a `PROG` constant and routed its
        hand-written usage line through it, so the name is now both correct
        and visible.
        """
        for path in self.PASSTHROUGH:
            with self.subTest(verb=path):
                prog = self.by_path[path]["prog"]
                self.assertIn(
                    prog["dispatcher"], prog["declared"],
                    f"{path} records no name, so a manifest-driven "
                    f"self-naming lint reads it as clean")

    def test_interpolated_usage_leaves_no_literal_to_drift(self):
        """A usage line built from PROG contributes no harvested literal.

        This is the shape that makes the collapse worth doing rather than
        merely correcting the text: there is no second copy left to drift.
        """
        for path in self.PASSTHROUGH:
            with self.subTest(verb=path):
                self.assertEqual(
                    self.by_path[path]["prog"]["usage_names"], [],
                    f"{path} still hand-writes a literal program name in "
                    f"its usage block; route it through PROG")

    def test_local_prog_assignment_is_harvested(self):
        """`transition.py` binds its prefix as a lowercase local.

        Harvesting only `PROG = "..."` and `prog=` keywords left the whole
        verb invisible: `transition` formatted every error through
        `prog = "transition-issue"` at function scope, so the manifest
        recorded no name for the verb that had a retired one. The harvest now
        reads that form, and IM-239 repointed the local at the module
        constant -- so the value proves both halves.
        """
        self.assertEqual(self.by_path["issue transition"]["prog"]["declared"],
                         ["issue transition"])

    def test_harvest_reads_the_source_not_a_declaration(self):
        """Gate-bites: inject each shape into a scratch module and see it.

        Patching a module attribute cannot test an AST harvest -- it reads
        the file -- so the fixture is a real file handed to the harvest
        functions directly.
        """
        with tempfile.TemporaryDirectory() as tmp:
            module_path = Path(tmp) / "fixture_verb.py"
            module_path.write_text(
                'PROG = "issue fixture"\n'
                '_USAGE = """\\\n'
                'Usage: fixture-legacy [--json]\n'
                '"""\n'
                'def main(argv):\n'
                '    print("fixture-legacy: something went wrong")\n'
                '    print(f"issue fixture: another thing {argv}")\n',
                encoding="utf-8")
            stub = types.SimpleNamespace(__file__=str(module_path),
                                         __name__="fixture_verb")
            self.assertEqual(cli_surface._prog_literals(stub),
                             ["issue fixture"])
            self.assertEqual(cli_surface._usage_names(stub),
                             ["fixture-legacy"])
            self.assertEqual(cli_surface._message_prefixes(stub),
                             ["fixture-legacy", "issue fixture"])

    def test_harvest_ignores_prefixes_that_are_not_command_shaped(self):
        """Condition keys ride near the same shape and must not be names.

        `f"{prog}: illegal transition ..."` is a condition, not a command.
        Shape-matching keeps `concurrent-edit` (hyphenated, so ambiguous by
        construction) and drops the prose ones; separating the remainder is
        the lint's job against the dispatcher form, not the extractor's.
        """
        with tempfile.TemporaryDirectory() as tmp:
            module_path = Path(tmp) / "fixture_conditions.py"
            module_path.write_text(
                'def main(argv):\n'
                '    print("illegal transition: open -> done")\n'
                '    print("unknown lease op: renew")\n'
                '    print("catchup commit: nothing to do")\n',
                encoding="utf-8")
            stub = types.SimpleNamespace(__file__=str(module_path),
                                         __name__="fixture_conditions")
            self.assertEqual(cli_surface._message_prefixes(stub), [])


class StructuredErrorDeclarationTests(unittest.TestCase):
    """The conversion ratchet is checked against the clause that owns it.

    `SURFACE_STRUCTURED_ERRORS` reads `unconverted` on all 28 verbs, which
    `_require_declaration` validates only as "one of two legal strings". So
    nothing could fail: the ratchet was unfalsifiable, and the manifest and
    the requirement could drift apart freely -- the requirement names the
    read verbs' `unknown-prefix` path as converted at adoption, and that
    sentence has been amended twice (IM-245, IM-254) with no check reading
    it.

    Converting the verbs themselves is IM-260's job. What is in scope here
    is that the two declarations cannot disagree: the converted set in the
    manifest must be exactly the set the requirement enumerates, derived
    from the requirement text rather than restated in this file.
    """

    REQUIREMENTS = REPO_ROOT / "docs" / "requirements.md"
    REQ_KEY = "[REQ: surface.structured-errors]"
    MARKER = "Converted at adoption:"

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(cli_surface.render_current())
        cls.paths = [v["path"] for v in cls.manifest["verbs"]]

    def clause(self):
        """The requirement bullet, located rather than assumed present."""
        for line in self.REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            if self.REQ_KEY in line:
                return line
        self.fail(
            f"no requirement carries {self.REQ_KEY}: the clause the manifest's "
            f"`structured_errors` field answers to has been renamed or "
            f"removed, so this check no longer knows what to compare against")

    def enumerated_converted(self):
        """Verb paths the clause names as converted, longest match first.

        The clause enumerates by naming verbs; today it names none (it
        converts one PATH -- `unknown-prefix` -- on the read verbs, which
        verb-level granularity cannot express, and explicitly declares the
        mutating verbs unconverted). Deriving the set from the sentence means
        the day someone writes a verb into it, the manifest has to follow or
        this fails.
        """
        clause = self.clause()
        start = clause.find(self.MARKER)
        self.assertNotEqual(
            start, -1,
            f"the {self.REQ_KEY} clause no longer carries an "
            f"'{self.MARKER}' sentence, so the converted set is no longer "
            f"enumerated anywhere this check can read")
        sentence = clause[start + len(self.MARKER):].split(".")[0]
        return {path for path in self.paths if path in sentence}

    def declared_converted(self):
        return {v["path"] for v in self.manifest["verbs"]
                if v["structured_errors"] == "converted"}

    def test_converted_set_matches_the_requirement(self):
        self.assertEqual(
            self.declared_converted(), self.enumerated_converted(),
            "the manifest's `structured_errors` converted set and the set "
            "the surface.structured-errors clause enumerates have drifted; "
            "convert the verb and amend the clause together, or neither")

    def test_unconverted_set_is_enumerated_not_waived(self):
        """The clause's own promise: visible, and only shrinking."""
        unconverted = [v["path"] for v in self.manifest["verbs"]
                       if v["structured_errors"] == "unconverted"]
        self.assertEqual(
            sorted(unconverted) + sorted(self.declared_converted()),
            sorted(self.paths),
            "every verb must carry one of the two states, so the unconverted "
            "set stays enumerated in the manifest rather than waived")

    def test_drift_in_either_direction_is_caught(self):
        """Gate-bites: both halves of the comparison are proven live."""
        # A verb declared converted that the clause does not name.
        mod = verb_module("issue show")
        with patched(mod, "SURFACE_STRUCTURED_ERRORS", "converted"):
            drifted = json.loads(
                cli_surface.render(cli_surface.build_manifest()))
        declared = {v["path"] for v in drifted["verbs"]
                    if v["structured_errors"] == "converted"}
        self.assertEqual(declared, {"issue show"})
        self.assertNotIn("issue show", self.enumerated_converted(),
                         "fixture assumes the clause does not name `show`")
        # A clause naming a verb the manifest leaves unconverted.
        sentence = " the issue transition verb, and nothing else"
        named = {path for path in self.paths if path in sentence}
        self.assertEqual(named, {"issue transition"})
        self.assertNotIn("issue transition", self.declared_converted())


class PassthroughVerbTests(unittest.TestCase):
    """The five prefix_chars="\\x00" verbs are represented, never flagless."""

    @classmethod
    def setUpClass(cls):
        cls.by_path = {v["path"]: v
                       for v in json.loads(cli_surface.render_current())["verbs"]}

    def flags(self, path):
        return {f["name"] for f in self.by_path[path]["flags"]}

    def test_manual_argv_verbs_carry_help_and_version(self):
        for path in ("issue admin selftest", "issue admin install-skills"):
            with self.subTest(verb=path):
                self.assertEqual(self.by_path[path]["parser"]["kind"], "manual")
                self.assertTrue(self.by_path[path]["parser"]["passthrough"])
                self.assertIn("--help", self.flags(path))
                self.assertIn("--version", self.flags(path))

    def test_manual_argv_verbs_match_the_real_cli(self):
        """The hand-maintained declaration must track the hand-walked argv.

        SURFACE_MANUAL_FLAGS is the extractor's escape hatch for the two
        verbs an argparse walk cannot see, and nothing derives it from the
        code -- so it can drift the moment someone adds a branch to the argv
        loop without touching the declaration. That is not hypothetical: it
        happened on the IM-245/IM-240 integration merge, where IM-240 taught
        both verbs --json and the declaration still said --help/--version.
        The manifest went false-CLEAN again, through the escape hatch rather
        than through the blindness it was built to cover.

        So probe the real CLI: every flag name on the surface that this verb
        does NOT declare must be rejected by it. An undeclared flag the verb
        actually accepts fails here, naming the drift.
        """
        manifest = json.loads(cli_surface.render_current())
        every_name = {f["name"] for v in manifest["verbs"] for f in v["flags"]}
        for path in ("issue admin selftest", "issue admin install-skills"):
            declared = self.flags(path)
            argv = path.split()[1:]
            for name in sorted(every_name - declared):
                with self.subTest(verb=path, flag=name):
                    code = _probe(argv + [name])
                    self.assertEqual(
                        code, 2,
                        f"{path} accepted undeclared flag {name} "
                        f"(exit {code}); add it to SURFACE_MANUAL_FLAGS "
                        f"and regenerate the manifest",
                    )

    def test_manual_argv_verbs_accept_what_they_declare(self):
        """The cheap half of the same contract, in the other direction.

        IM-262: this used to iterate the literal `("--help", "--version")`,
        so a flag removed from the argv loop but left declared was the one
        flag never probed -- the declaration could claim `--json` while the
        verb rejected it and this test stayed green. Iterate the declaration
        instead, so every declared flag is proven reachable.
        """
        for path in ("issue admin selftest", "issue admin install-skills"):
            argv = path.split()[1:]
            for name in sorted(self.flags(path)):
                with self.subTest(verb=path, flag=name):
                    self.assertEqual(
                        _probe(argv + [name]), 0,
                        f"{path} declares {name} but the CLI rejects it; "
                        f"the argv loop in main() and "
                        f"SURFACE_MANUAL_FLAGS have drifted",
                    )

    def test_internal_parser_verbs_extract_their_real_flags(self):
        for path, expected in (
            ("issue admin init-store", {"--non-interactive", "--dry-run"}),
            ("issue admin init-project", {"--prefix", "--repo", "--store"}),
            ("issue admin migrate", {"--project", "--json", "--lock-timeout"}),
        ):
            with self.subTest(verb=path):
                self.assertEqual(self.by_path[path]["parser"]["kind"],
                                 "argparse-internal")
                self.assertTrue(expected <= self.flags(path))

    def test_declaration_is_derived_from_the_argv_loop(self):
        """IM-262: the declaration is checked against the code, not the manifest.

        `test_manual_argv_verbs_match_the_real_cli` probes the real CLI, which
        is the right instinct, but it draws its probe set from the manifest --
        the artifact built from the declaration under test. So it can only
        catch drift that reuses a flag name already present on some other
        verb. `--json` was such a name, which is the only reason instance 5
        was caught at all.
        """
        for path in ("issue admin selftest", "issue admin install-skills"):
            with self.subTest(verb=path):
                mod = verb_module(path)
                declared = {name
                            for spec in mod.SURFACE_MANUAL_FLAGS
                            for name in spec["names"]}
                self.assertEqual(cli_surface._argv_flag_literals(mod),
                                 declared)

    def test_undeclared_argv_flag_fails_loud(self):
        """Direction A: a flag the loop accepts and the declaration omits.

        The injected name is deliberately absent from every other verb, so
        the manifest-derived probe set could never contain it.
        """
        mod = verb_module("issue admin selftest")
        surfaced = {f["name"]
                    for v in json.loads(cli_surface.render_current())["verbs"]
                    for f in v["flags"]}
        self.assertNotIn("--quiet", surfaced,
                         "pick a name novel to the surface for this fixture")
        with patched(cli_surface, "_argv_flag_literals",
                     lambda m, *a, **k: {"-h", "--help", "--version",
                                         "--json", "--quiet"}):
            with self.assertRaises(cli_surface.SurfaceError) as ctx:
                cli_surface.build_manifest()
        message = str(ctx.exception)
        self.assertIn("--quiet", message)
        self.assertIn("not declared", message)

    def test_unreachable_declared_flag_fails_loud(self):
        """Direction B: a flag declared but no longer accepted by the loop."""
        with patched(cli_surface, "_argv_flag_literals",
                     lambda m, *a, **k: {"-h", "--help", "--version"}):
            with self.assertRaises(cli_surface.SurfaceError) as ctx:
                cli_surface.build_manifest()
        message = str(ctx.exception)
        self.assertIn("--json", message)
        self.assertIn("not accepted by main()", message)

    def test_passthrough_without_a_declaration_fails_loud(self):
        """An empty flag set must never be emitted for a passthrough verb."""
        mod = verb_module("issue admin selftest")
        with patched(mod, "SURFACE_MANUAL_FLAGS", _DELETE):
            with self.assertRaises(cli_surface.SurfaceError) as ctx:
                cli_surface.build_manifest()
        message = str(ctx.exception)
        self.assertIn("prefix_chars", message)
        self.assertIn("SURFACE_MANUAL_FLAGS", message)


class FlagRepresentationTests(unittest.TestCase):
    """Flag entries record the resulting registrations, not the call sites."""

    @classmethod
    def setUpClass(cls):
        cls.by_path = {v["path"]: v
                       for v in json.loads(cli_surface.render_current())["verbs"]}

    def flag(self, path, name):
        for f in self.by_path[path]["flags"]:
            if f["name"] == name:
                return f
        raise AssertionError(f"{path} has no {name}")

    def test_inline_declarations_are_attributed_local(self):
        """The copy-then-diverge pattern IM-244 warned about, as measured.

        IM-244's note said `dep` / `edit` / `admin reap` declare
        `--lock-timeout` inline; measurement on 2026-11-07 shows IM-242 (Leg 2)
        already converged all three onto `common.add_lock_timeout_flag`, so
        that specific instance is closed. The pattern itself survives in
        `--no-reindex` on the same three verbs, and across the three
        internal-parser admin verbs, which import no `common` at all.

        Either way the extractor reads the resulting registrations, so it
        reports what IS registered rather than what a call-site grep would
        guess.
        """
        for path in ("issue dep", "issue edit", "issue note"):
            with self.subTest(verb=path, flag="--no-reindex"):
                self.assertEqual(self.flag(path, "--no-reindex")["source"],
                                 "local")
        self.assertEqual(self.flag("issue block", "--no-reindex")["source"],
                         "common.add_no_reindex_flag")
        for path in ("issue dep", "issue edit", "issue admin reap"):
            with self.subTest(verb=path, flag="--lock-timeout"):
                self.assertEqual(self.flag(path, "--lock-timeout")["source"],
                                 "common.add_lock_timeout_flag")
        self.assertEqual(
            self.flag("issue admin migrate", "--lock-timeout")["source"],
            "local")

    def test_argparse_aliases_land_in_names(self):
        """One action, several spellings: the closing-note flag."""
        self.assertEqual(self.flag("issue complete", "--summary")["names"],
                         ["--summary", "--reason", "--note"])
        self.assertEqual(self.flag("issue transition", "--reason")["names"],
                         ["--reason", "--summary", "--note"])

    def test_helper_registered_synonyms_are_attributed(self):
        repo = self.flag("issue list", "--repo")
        self.assertEqual(repo["synonym_of"], "--prefix")
        self.assertTrue(repo["hidden"])
        self.assertEqual(
            self.flag("issue admin snapshot", "--timeout")["synonym_of"],
            "--commit-timeout")

    def test_create_repo_is_not_read_as_a_prefix_synonym(self):
        """`create --repo` is the repo BASENAME -- a disjoint value space, and
        a live `surface.one-meaning-per-flag` question. Folding it into
        `--prefix`'s synonym set would launder that."""
        repo = self.flag("issue create", "--repo")
        self.assertIsNone(repo["synonym_of"])
        self.assertEqual(repo["metavar"], "NAME")

    def test_dead_synonym_declaration_fails_loud(self):
        """IM-262: a declared spelling the verb does not accept.

        The pre-existing check fired only when the retired spelling WAS
        registered, so the one drift it could not see was the one that
        matters: a synonym dropped from argv but left in the declaration,
        leaving the manifest advertising a flag that exits 2.
        """
        mod = verb_module("issue dep")
        declared = dict(getattr(mod, "SURFACE_SYNONYMS", {}))
        canonical = sorted(declared)[0]
        with patched(mod, "SURFACE_SYNONYMS",
                     dict(declared, **{canonical: ("--retired-last-cycle",)})):
            with self.assertRaises(cli_surface.SurfaceError) as ctx:
                cli_surface.build_manifest()
        message = str(ctx.exception)
        self.assertIn("--retired-last-cycle", message)
        self.assertIn("declaration is dead", message)

    def test_locally_declared_synonyms_are_recorded(self):
        self.assertEqual(self.flag("issue dep", "--type")["synonym_of"],
                         "--kind")
        self.assertEqual(self.flag("issue edit", "--dep-type")["synonym_of"],
                         "--kind")

    def test_mutually_exclusive_grouping_is_represented(self):
        self.assertEqual(
            self.by_path["issue ready"]["mutually_exclusive_groups"],
            [["--json", "--text"]])

    def test_ordered_section_action_is_visible(self):
        """`edit`'s section-flag grouping lives in its shared action class."""
        self.assertEqual(self.flag("issue edit", "--section")["action"],
                         "OrderedFlagAction")

    def test_short_flags_are_enumerable(self):
        """`surface.long-flags-only` must be checkable from the manifest."""
        shorts = set()
        for verb in self.by_path.values():
            for f in verb["flags"]:
                shorts.update(n for n in f["names"]
                              if not n.startswith("--"))
        self.assertEqual(shorts, {"-h"})

    def test_prog_literals_are_recorded_per_verb(self):
        """Self-naming data: the names a module puts in user-facing text.

        This test USED to assert that `migrate-to-store` and `search-history`
        were recorded -- the drift itself, pinned as a fixture. IM-239 removed
        the drift, so what is worth pinning now is that the recording still
        happens: each verb records its own current name, which is what
        `SelfNamingTests` below compares against.
        """
        self.assertEqual(self.by_path["issue admin migrate"]["prog"]["declared"],
                         ["issue admin migrate"])
        self.assertEqual(self.by_path["issue history search"]["prog"]["declared"],
                         ["issue history search"])
        self.assertEqual(self.by_path["issue show"]["prog"]["dispatcher"],
                         "issue show")


# ---------------------------------------------------------------------------
# Retired-name ground truth, and the output-keyed half of self-naming
# (IM-247).
#
# The set of pre-v3.0.0 flat binaries is CLOSED: v3.0.0 retired all 21 and
# no new flat binary can ever be created, so unlike the declarations the
# IM-261 audit catalogued, this one cannot drift forward. It can still be
# re-typed wrongly, so it is read from the single place the project already
# enumerates it -- `_consolidated_verb_for` in tests/verification/lib.sh,
# which the whole black-box suite routes through -- rather than restated
# here. `test_retired_name_set_is_derived_from_ground_truth` proves the
# derivation is neither empty nor stale.

RETIRED_NAME_SOURCE = REPO_ROOT / "tests" / "verification" / "lib.sh"

_CASE_ARM = re.compile(r'^\s*([a-z][a-z0-9-]+)\)\s*echo\s+"([a-z][a-z0-9 -]*)"')


def retired_flat_names():
    """{retired flat binary name: the verb path that replaced it}.

    Fails loud rather than returning an empty map: an empty set would make
    every assertion keyed on it vacuously green, which is the false-clean
    shape this whole issue is about.
    """
    text = RETIRED_NAME_SOURCE.read_text(encoding="utf-8")
    body = text.partition("_consolidated_verb_for() {")[2].partition("\n}")[0]
    mapping = {}
    for line in body.splitlines():
        match = _CASE_ARM.match(line)
        if match:
            mapping[match.group(1)] = "issue " + match.group(2)
    if not mapping:
        raise AssertionError(
            f"no retired-name mapping found in {RETIRED_NAME_SOURCE}: "
            f"_consolidated_verb_for moved or changed shape, and the "
            f"self-naming output check has nothing to look for"
        )
    return mapping


# An explicit historical qualifier, which `surface.self-naming` requires
# before a retired spelling may appear at all. Deliberately short -- every
# token is either in live sanctioned use or named by a requirement, so the
# vocabulary cannot quietly become a way to launder a dead spelling:
#
#   flat        -- "the flat `X` binary", the project's standing phrasing
#                  for a pre-v3.0.0 binary, and the token carried by every
#                  wrapped continuation line of the forms docs/plan.md
#                  sanctions.
#   port/ported -- "Ported from the flat `X` binary", the four history
#                  verbs' DESCRIPTION text, sanctioned by name.
#   was         -- `issue search`'s "Was (flat binary, removed in v3)",
#                  and the example qualifier named in requirements.md
#                  (dist.skill-exit-code-parity).
#   removed     -- live in that same line.
#   retired     -- the word surface.self-naming itself uses for the
#                  condition.
#   through v   -- the second qualifier requirements.md names by example.
#
# Matched per LINE, not per help page: a page-wide rule would let one
# qualified sentence excuse an unqualified instruction anywhere else on it.
# The cost is that re-wrapping a sanctioned line can move the qualifier off
# it, which fails loud and is cheap to fix -- the right direction for a
# guard whose other failure mode is reading clean.
_HISTORICAL_QUALIFIER = re.compile(
    r"\b(?:was|flat|ported?|retired|removed)\b|\bthrough\s+v", re.I)


def _migrate_dry_run_fixture(tmp):
    """An unmigrated project plus an empty store: enough for a plan preview.

    No git anywhere -- `admin migrate --dry-run` plans without touching a
    repo -- so this stays a unit-test fixture rather than a shell harness.
    """
    issues = tmp / "proj" / ".issues"
    issues.mkdir(parents=True)
    (tmp / "store").mkdir(exist_ok=True)
    (issues / ".prefix").write_text("XY\n", encoding="utf-8")
    (issues / "XY-001.md").write_text(
        "---\nid: XY-001\ntitle: fixture\nstatus: open\ntype: chore\n"
        "priority: low\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n\n"
        "## Summary\n\nfixture\n", encoding="utf-8")
    return ["--project", str(tmp / "proj"), "--store", str(tmp / "store"),
            "--dry-run"]


# Diagnostics that no `--help` page reaches, each with the condition that
# reaches it and a sentinel substring proving it still does. Without the
# sentinel a probe whose condition stopped firing would scan empty output
# and pass -- a guard that reads clean because it checked nothing.
DIAGNOSTIC_PROBES = (
    # Store resolution must FAIL here, so ISSUE_STORE_ROOT is removed
    # rather than pinned; _capture's temp cwd and HOME are what keep the
    # operator's store out of reach.
    ("admin reap, store unresolvable",
     ["admin", "reap", "--dry-run"], {"ISSUE_STORE_ROOT": None}, None,
     "store mode required"),
    ("admin install-skills, brew absent",
     ["admin", "install-skills"], {"PATH": ""}, None,
     "brew not found on PATH"),
    ("admin migrate, dry-run plan with the registry absent",
     ["admin", "migrate"], None, _migrate_dry_run_fixture,
     "registry:"),
)


class SelfNamingTests(unittest.TestCase):
    """`surface.self-naming`: user-facing text names only current forms.

    The clause covers usage blocks, `prog` strings, error prefixes and help.
    IM-261 measured roughly ninety such sites across eight modules, each
    module having hand-copied one retired flat-binary name into every
    diagnostic it prints. IM-239 collapsed each module onto a single `PROG`
    constant, so this check is one comparison per verb against the
    dispatcher's own prog rather than a ~90-site grep.

    What ACCEPTS a harvested name, all four rules derived rather than listed:

      * it is the verb's own dispatcher prog, or a word-wise prefix of it
        (`issue` heading a wrapped synopsis), or a word-wise extension
        (`issue dep add` on `issue dep`, which is a real subcommand form);
      * it is the dispatcher prog of some OTHER verb on the surface --
        `admin selftest` names each verb it drives, and those names are
        current by construction;
      * it is an error-condition key from `lib/errors.py`, since
        `f"concurrent-edit: registry lock timeout"` is a condition rather
        than a command. Read from the `error_class` attributes so it moves
        with the contract, the way `skill-exit-codes.sh` reads the `EXIT_*`
        constants.

    A retired flat binary name satisfies none of them: it is a single
    hyphenated token, is no verb's prog, and is no condition key.

    That is the DECLARATION-keyed half, and it is only as wide as the three
    source positions the extractor harvests. The OUTPUT-keyed half below
    (IM-247) drives the real CLI and reads what a caller is shown, because
    a retired name written mid-sentence in a description, an epilog, or a
    one-off diagnostic sits in none of those positions. Both halves live in
    this one class on purpose: docs/cli-vocabulary.json names it the sole
    authority for `surface.self-naming`, and one rule gets one authority.
    """

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(cli_surface.render_current())
        cls.progs = {v["prog"]["dispatcher"] for v in cls.manifest["verbs"]}
        cls.conditions = cls.condition_keys()
        cls.retired_map = retired_flat_names()

    @staticmethod
    def condition_keys():
        """Every `error=` condition the CLI can report, from lib/errors.py."""
        import inspect

        import errors

        keys = {getattr(obj, "error_class")
                for _, obj in inspect.getmembers(errors, inspect.isclass)
                if isinstance(getattr(obj, "error_class", None), str)}
        assert keys, "no error_class attributes found; errors.py moved"
        return keys

    def acceptable(self, name, dispatcher):
        words = name.split()
        for prog in {dispatcher} | self.progs:
            other = prog.split()
            # A prefix of the name it heads (`issue` on a wrapped synopsis),
            # or an extension of it (`issue dep add`, a real subcommand form
            # that `admin selftest` names when it drives `issue dep`).
            if words == other[:len(words)] or words[:len(other)] == other:
                return True
        return name in self.conditions

    def offenders(self, verb):
        prog = verb["prog"]
        harvested = set(prog["declared"]) | set(prog["message_prefixes"]) \
            | set(prog["usage_names"])
        return sorted(n for n in harvested
                      if not self.acceptable(n, prog["dispatcher"]))

    def test_no_verb_names_a_retired_command(self):
        for verb in self.manifest["verbs"]:
            with self.subTest(verb=verb["path"]):
                self.assertEqual(
                    self.offenders(verb), [],
                    f"{verb['path']} puts {self.offenders(verb)} in "
                    f"user-facing text, which is neither its own name, nor "
                    f"another verb on the surface, nor an error-condition "
                    f"key. Route the message through the module's PROG "
                    f"constant (surface.self-naming).")

    def test_a_verb_that_prints_a_name_declares_it(self):
        """Printing a name without declaring one is the false-clean shape.

        The two `prefix_chars=chr(0)` passthrough verbs recorded an EMPTY
        `prog.declared` before IM-262, which is why a lint keyed on that
        field alone read clean on the two with the worst drift (IM-261
        audit, 5.3).

        The obligation is conditional on purpose. `issue admin reindex`
        prints no command name at all -- argparse generates its usage and
        its diagnostics are prefixed `Error:` -- so requiring a `PROG` from
        it would add a declaration with no consumer, which is the shape this
        whole issue exists to remove. The condition is computed from the
        manifest, so a verb that STARTS printing a name is caught.
        """
        for verb in self.manifest["verbs"]:
            prog = verb["prog"]
            if not (prog["message_prefixes"] or prog["usage_names"]):
                continue
            with self.subTest(verb=verb["path"]):
                self.assertIn(
                    prog["dispatcher"], prog["declared"],
                    f"{verb['path']} prints a command name but declares no "
                    f"PROG matching its dispatcher prog, so a self-naming "
                    f"check cannot see what it prints")

    def test_lint_bites_a_retired_name(self):
        """Gate-bites: the shape the ~90 collapsed sites had."""
        verb = {"path": "issue history show", "prog": {
            "dispatcher": "issue history show",
            "declared": ["issue-history"],
            "message_prefixes": ["issue-history"],
            "usage_names": [],
        }}
        self.assertEqual(self.offenders(verb), ["issue-history"])

    def test_lint_bites_a_plausible_but_wrong_current_form(self):
        """A name that looks consolidated but is not a verb must fail too."""
        verb = {"path": "issue show", "prog": {
            "dispatcher": "issue show",
            "declared": ["issue show"],
            "message_prefixes": ["issue display"],
            "usage_names": [],
        }}
        self.assertEqual(self.offenders(verb), ["issue display"])

    def test_lint_accepts_the_four_derived_exemptions(self):
        """Each acceptance rule is exercised, so none is dead code."""
        self.assertTrue(self.acceptable("issue show", "issue show"))
        self.assertTrue(self.acceptable("issue", "issue create"))
        self.assertTrue(self.acceptable("issue dep add", "issue dep"))
        self.assertTrue(self.acceptable("issue create", "issue admin selftest"))
        self.assertTrue(self.acceptable("concurrent-edit", "issue admin init-project"))
        self.assertFalse(self.acceptable("new-issue", "issue create"))

    def test_condition_keys_come_from_the_error_contract(self):
        """Derived, not restated: a new error class extends the set."""
        self.assertIn("concurrent-edit", self.conditions)
        self.assertIn("unknown-prefix", self.conditions)
        self.assertNotIn("new-issue", self.conditions)

    # -- output-keyed half (IM-247) ----------------------------------------

    @property
    def retired(self):
        """Retired spellings that are not, today, some verb's current form.

        Filtered through the same `acceptable` the declaration-keyed half
        uses, so the set corrects itself: were a retired spelling ever
        reinstated as a real verb form, it would drop out here instead of
        becoming a permanent false positive that gets waived.
        `test_retired_name_set_is_derived_from_ground_truth` pins that the
        filter drops nothing today, so the check cannot shrink silently.
        """
        return [name for name in sorted(self.retired_map)
                if not self.acceptable(name, "issue")]

    def unqualified_retired_names(self, text):
        """Retired names in `text` on lines carrying no historical qualifier.

        The whole output-keyed check reduces to this one pure function, so
        the gate-bites tests below can feed it the exact lines IM-247 fixed
        rather than needing a mutated copy of the CLI.
        """
        found = set()
        for line in text.splitlines():
            if _HISTORICAL_QUALIFIER.search(line):
                continue
            for name in self.retired:
                if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", line):
                    found.add(name)
        return sorted(found)

    def test_no_verb_help_names_a_retired_command(self):
        """Every verb's `--help`, read as the caller reads it.

        The declaration-keyed half cannot see these: `prog.declared` is a
        `prog=` keyword, `prog.message_prefixes` is a `<name>: ` head, and
        `prog.usage_names` is the first token under a `usage:` heading, so
        a name written mid-sentence in a DESCRIPTION or _EPILOG is in none
        of the three. Three live instances sat there -- `admin init-store`
        naming `new-issue`, `admin snapshot` naming `init-issue-project`,
        `edit` pointing at an "edit-issue reference" that does not exist
        under that name -- while every declaration-keyed assertion was
        green.

        The verb inventory is the manifest, so a verb added later is
        covered without anyone remembering to add it.
        """
        for verb in self.manifest["verbs"]:
            path = verb["path"]
            with self.subTest(verb=path):
                code, text = _capture(path.split()[1:] + ["--help"])
                self.assertEqual(code, 0, f"{path} --help exited {code}")
                self.assertTrue(text.strip(), f"{path} --help printed nothing")
                offenders = self.unqualified_retired_names(text)
                self.assertEqual(
                    offenders, [],
                    f"{path} --help names the retired {offenders} with no "
                    f"historical qualifier. Name the current verb form "
                    f"instead -- a caller is never handed a dead spelling "
                    f"(surface.self-naming).")

    def test_no_diagnostic_names_a_retired_command(self):
        """The same rule on output no `--help` page reaches.

        A caller reads a diagnostic exactly as attentively as a help page,
        and the worst instance IM-247 found was one: `admin migrate`'s
        dry-run plan told the caller to "run init-issue-project after
        migration" -- the form the clause forbids most specifically, since
        it is what the caller is told to type next.
        """
        for label, argv, env, fixture, sentinel in DIAGNOSTIC_PROBES:
            with self.subTest(probe=label):
                _, text = _capture(argv, env, fixture)
                self.assertIn(
                    sentinel, text,
                    f"probe {label!r} no longer reaches the diagnostic it "
                    f"was built to read (expected {sentinel!r}); fix the "
                    f"probe rather than deleting it, or this check passes "
                    f"by scanning nothing")
                offenders = self.unqualified_retired_names(text)
                self.assertEqual(
                    offenders, [],
                    f"{label} names the retired {offenders} with no "
                    f"historical qualifier (surface.self-naming)")

    def test_retired_name_set_is_derived_from_ground_truth(self):
        """The derivation, not the data: empty or stale must fail loud.

        A retired-name list re-typed into this file would be one more of
        exactly the hand-maintained declarations the IM-261 audit
        catalogued, so the map is read from `_consolidated_verb_for` in
        tests/verification/lib.sh. Checking each mapped target against the
        live prog set makes that borrowed declaration self-validating here
        too: a verb renamed without updating the map fails.
        """
        self.assertEqual(sorted(self.retired), sorted(self.retired_map),
                         "acceptable() dropped a retired spelling from the "
                         "scanned set; the check just got narrower")
        self.assertEqual(self.retired_map["new-issue"], "issue create")
        self.assertEqual(self.retired_map["migrate-to-store"],
                         "issue admin migrate")
        for name, target in sorted(self.retired_map.items()):
            with self.subTest(retired=name):
                self.assertIn(
                    target, self.progs,
                    f"{name} is mapped to {target!r}, which is no verb on "
                    f"today's surface")

    def test_output_check_bites_each_site_it_was_built_for(self):
        """Gate-bites: the six lines IM-247 changed, verbatim as they were.

        Pinned as text rather than as a mutated CLI, so these stay a record
        of what the check is for. A rule that cannot be shown to fail is not
        a guard (the taxonomy rule in docs/design/declaration-drift-audit.md).
        """
        for before, expected in (
            ("  registry:      ABSENT (run init-issue-project after "
             "migration)", "init-issue-project"),
            ("Error: brew not found on PATH; issue-mgmt-install-skills "
             "relies on Homebrew.", "issue-mgmt-install-skills"),
            ("issue admin reap: store mode required -- set ISSUE_STORE_ROOT "
             "or run inside a project with a .issues/.migrated-to-store "
             "sentinel (see migrate-to-store)", "migrate-to-store"),
            ("Store-root resolution order (same as init-issue-project):",
             "init-issue-project"),
            ("  ISSUE-TEMPLATE.md is resolved via the same precedence as "
             "`new-issue`:", "new-issue"),
            ("Extended flags (retained, see the design doc / edit-issue "
             "reference):", "edit-issue"),
        ):
            with self.subTest(line=before.strip()[:48]):
                self.assertEqual(self.unqualified_retired_names(before),
                                 [expected])

    def test_output_check_accepts_the_sanctioned_qualified_forms(self):
        """The permitted historical mentions must not be false positives.

        docs/plan.md sanctions both of these explicitly, and each wrapped
        continuation line has to pass on its own token, which is why `flat`
        is in the qualifier vocabulary at all.
        """
        for line in (
            "store. Read-only: no locks acquired. Ported from the flat "
            "`issue-history`",
            "the flat `activity-issues` binary.",
            "flat `show-issue-at` binary.",
            "Read-only search across the issue corpus. Ported from the flat "
            "search-issues",
            'Was (flat binary, removed in v3): search-issues "concurrent "',
        ):
            with self.subTest(line=line[:48]):
                self.assertEqual(self.unqualified_retired_names(line), [])

    def test_qualifier_vocabulary_does_not_launder_an_instruction(self):
        """A near-miss word must not pass as a qualifier.

        The vocabulary is the one place this check could be quietly widened
        into uselessness, so the words deliberately NOT in it are pinned:
        `retained` describes a flag rather than a spelling, and `see` and
        `run` are what an unqualified instruction reads like.
        """
        for line in (
            "see migrate-to-store for the one-shot copy",
            "run init-issue-project after migration",
            "the retained edit-issue reference",
        ):
            with self.subTest(line=line):
                self.assertNotEqual(self.unqualified_retired_names(line), [])

    def test_word_boundaries_keep_a_longer_token_from_matching(self):
        """`.migrated-to-store` is the sentinel, not the retired binary."""
        self.assertEqual(self.unqualified_retired_names(
            "set the .issues/.migrated-to-store sentinel"), [])
        self.assertEqual(self.unqualified_retired_names(
            "see migrate-to-store"), ["migrate-to-store"])


if __name__ == "__main__":
    unittest.main()
