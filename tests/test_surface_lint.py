#!/usr/bin/env python3
"""The CLI vocabulary registry and surface linter (IM-246).

`docs/cli-vocabulary.json` declares what the surface is allowed to MEAN;
`scripts/surface_lint.py` checks the IM-245 manifest against it. These
tests are the gate: the first group asserts the lint is clean on today's
tree, and the rest inject one synthetic violation at a time and assert the
lint goes red -- because a check nobody has watched fail is a check nobody
knows works. IM-261's audit found five releases' worth of declarations that
drifted from the code precisely because nothing proved the guard could
bite.

The registry is itself a hand-maintained declaration -- the largest in the
project -- so every part of it is reconciled against the manifest in both
directions, and the waiver baseline is proven shrink-only here rather than
asserted in prose.

Run:
    python3 -m unittest tests.test_surface_lint -v
"""

import copy
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "lib"))

import surface_lint  # noqa: E402


def verb(manifest, path):
    for entry in manifest["verbs"]:
        if entry["path"] == path:
            return entry
    raise AssertionError(f"no verb {path!r} in the manifest")


def flag_entry(manifest, verb_path, name):
    for entry in verb(manifest, verb_path)["flags"]:
        if name in entry["names"]:
            return entry
    raise AssertionError(f"no {name} on {verb_path!r}")


def keys_of(findings):
    return {f.key for f in findings}


# The waiver fixtures below supply their own owner id. Borrowing one from
# the committed baseline couples a fixture to state it does not control:
# the baseline is driven to zero by design (IM-289), and a real issue id
# goes stale the moment that issue closes.
FIXTURE_ISSUE = "IM-000"
FIXTURE_FINDING = "unregistered-flag:--cap"


def inject_unregistered_flag(manifest):
    """Add a flag name the registry cannot know about; returns the manifest.

    This is the cheapest REAL finding available, so a fixture needing a
    waiver that covers something can make its own rather than borrow one
    from the committed baseline.
    """
    entry = copy.deepcopy(flag_entry(manifest, "issue index", "--limit"))
    entry.update(name="--cap", names=["--cap"], dest="cap")
    verb(manifest, "issue index")["flags"].append(entry)
    return manifest


def fixture_waiver(key=FIXTURE_FINDING, **kw):
    waiver = {"clause": "surface.vocabulary-registry", "key": key,
              "issue": FIXTURE_ISSUE, "note": "fixture"}
    waiver.update(kw)
    return waiver


class RealTreeTests(unittest.TestCase):
    """What the lint says about the surface as committed."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = surface_lint.manifest_from_disk()
        cls.registry = surface_lint.load_registry()
        cls.unwaived, cls.waived = surface_lint.lint(
            cls.manifest, cls.registry)

    def test_no_unwaived_findings(self):
        detail = "\n".join(f"{f.key}: {f.detail}" for f in self.unwaived)
        self.assertEqual(self.unwaived, [], f"unwaived findings:\n{detail}")

    def test_every_waived_finding_is_one_the_baseline_declares(self):
        """The baseline is an accounting of real findings, at any size.

        This replaces an assertion that the baseline was NON-EMPTY, which
        was true when the registry was adopted and is false at the
        surface-convergence exit gate, where zero waivers is the success
        condition rather than a defect (IM-289). The waiver machinery is
        proven by `WaiverBaselineTests` against fixtures that supply their
        own finding and waiver, so it never needed a live one.
        """
        declared = {w["key"] for w in self.registry["waivers"]}
        for finding, waiver in self.waived:
            with self.subTest(key=finding.key):
                self.assertIn(finding.key, declared)
                self.assertEqual(waiver["key"], finding.key)
                self.assertRegex(waiver["issue"], r"^[A-Z]+-\d+$")

    def test_every_flag_name_is_registered(self):
        uses = surface_lint.flag_uses(self.manifest)
        unregistered = sorted(set(uses) - set(self.registry["flags"]))
        self.assertEqual(unregistered, [])

    def test_registry_declares_no_spelling_the_cli_lacks(self):
        uses = surface_lint.flag_uses(self.manifest)
        obsolete = sorted(set(self.registry["flags"]) - set(uses))
        self.assertEqual(obsolete, [])

    def test_every_positional_is_declared(self):
        uses = surface_lint.positional_uses(self.manifest)
        declared = {(row["verb"], row["dest"])
                    for row in self.registry["positionals"]}
        self.assertEqual(sorted(set(uses) - declared), [])

    def test_registry_declares_no_positional_the_cli_lacks(self):
        uses = surface_lint.positional_uses(self.manifest)
        declared = {(row["verb"], row["dest"])
                    for row in self.registry["positionals"]}
        self.assertEqual(sorted(declared - set(uses)), [])

    def test_store_backed_issue_checks_pass(self):
        """Waiver owners open, exemption decisions present, per the store.

        Skipped rather than failed without a store: the lint must stay
        usable in a tree with no store attached, and these are the checks
        that cannot be answered from the repo alone.
        """
        sys.path.insert(0, str(REPO_ROOT / "lib"))
        from cli import common

        anchor = common.find_project_anchor()
        store_root = common.resolve_store_root(anchor, None)
        if not store_root or not Path(store_root).is_dir():
            self.skipTest("no issue store resolved from this tree")
        root = Path(store_root)

        def status_of(issue_id):
            path = root / issue_id.split("-")[0] / f"{issue_id}.md"
            if not path.is_file():
                return None
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("status:"):
                    return line.split(":", 1)[1].strip()
            return None

        unwaived, _ = surface_lint.lint(
            self.manifest, self.registry, issue_status=status_of)
        offenders = sorted(f.key for f in unwaived
                           if f.key.startswith("closed-waiver-issue:"))
        self.assertEqual(offenders, [])
        provenance = sorted(
            f.key for f in unwaived
            if f.key.startswith("unknown-exemption-decision:"))
        self.assertEqual(provenance, [],
                         "an exemption records a decision issue the store "
                         "does not have")

    def test_lint_is_deterministic(self):
        first, _ = surface_lint.lint(self.manifest, self.registry)
        second, _ = surface_lint.lint(self.manifest, self.registry)
        self.assertEqual([f.key for f in first], [f.key for f in second])


class GateBitesTests(unittest.TestCase):
    """One synthetic violation per check, each proven to turn the lint red.

    Every fixture mutates a COPY of the real manifest or registry, so each
    test states exactly which surface change the gate is supposed to
    catch, and a check that silently stopped working fails here.
    """

    @classmethod
    def setUpClass(cls):
        cls.base_manifest = surface_lint.manifest_from_disk()
        cls.base_registry = surface_lint.load_registry()
        cls.req_keys = surface_lint.requirement_keys()

    def manifest(self):
        return copy.deepcopy(self.base_manifest)

    def registry(self):
        return copy.deepcopy(self.base_registry)

    def run_lint(self, manifest=None, registry=None, **kw):
        kw.setdefault("req_keys", self.req_keys)
        return surface_lint.lint(manifest or self.manifest(),
                                 registry or self.registry(), **kw)

    def test_unregistered_flag_name_fails(self):
        manifest = self.manifest()
        entry = copy.deepcopy(flag_entry(manifest, "issue index", "--limit"))
        entry.update(name="--cap", names=["--cap"], dest="cap")
        verb(manifest, "issue index")["flags"].append(entry)
        unwaived, _ = self.run_lint(manifest=manifest)
        self.assertIn("unregistered-flag:--cap", keys_of(unwaived))

    def test_status_reverted_to_last_wins_fails(self):
        """IM-269: a repeated --status that silently keeps only one value.

        The bug this pins returned a plausible shorter answer with exit 0 --
        `issue list --status open --status done` gave only `done` while
        `issue index` with the same argv gave both. Reverting any one verb to
        `action="store"` must turn the lint red; there is no waiver left to
        hide it, because fixing IM-269 removed both of its waivers.
        """
        for verb_name in ("issue list", "issue search", "issue index"):
            with self.subTest(verb=verb_name):
                manifest = self.manifest()
                flag_entry(manifest, verb_name, "--status")["action"] = "store"
                unwaived, _ = self.run_lint(manifest=manifest)
                self.assertIn("value-space:--status@%s" % verb_name,
                              keys_of(unwaived))

    def test_obsolete_registry_entry_fails(self):
        registry = self.registry()
        registry["flags"]["--retired"] = {"concept": "result-limit"}
        unwaived, _ = self.run_lint(registry=registry)
        self.assertIn("obsolete-spelling:--retired", keys_of(unwaived))

    def test_second_meaning_for_one_spelling_fails(self):
        """The dest proxy: a name quietly reused for a second purpose."""
        manifest = self.manifest()
        flag_entry(manifest, "issue index", "--limit")["dest"] = "page_size"
        unwaived, _ = self.run_lint(manifest=manifest)
        self.assertIn("dest-divergence:result-limit:page_size",
                      keys_of(unwaived))

    def test_undeclared_collision_cannot_be_waived_into_silence(self):
        """A waiver does not substitute for declaring the second meaning."""
        manifest = self.manifest()
        flag_entry(manifest, "issue index", "--limit")["dest"] = "page_size"
        registry = self.registry()
        registry["waivers"].append({
            "clause": "surface.one-meaning-per-flag",
            "key": "dest-divergence:result-limit:page_size",
            "issue": "IM-246", "note": "not a real finding owner"})
        unwaived, waived = self.run_lint(manifest=manifest, registry=registry)
        self.assertIn("dest-divergence:result-limit:page_size",
                      keys_of(unwaived),
                      "a dest divergence is waivable only once the registry "
                      "declares what the second meaning is")
        self.assertNotIn("dest-divergence:result-limit:page_size",
                         {f.key for f, _ in waived})

    def test_value_space_divergence_fails(self):
        manifest = self.manifest()
        entry = flag_entry(manifest, "issue show", "--frontmatter")
        entry["action"] = "store"
        entry["nargs"] = None
        unwaived, _ = self.run_lint(manifest=manifest)
        self.assertIn("value-space:--frontmatter@issue show",
                      keys_of(unwaived))

    def test_accumulation_counts_as_value_space(self):
        """The axis itself: identical values, different accumulation.

        `--status open --status done` must mean the same thing on every verb
        carrying the flag. Accumulation is the only thing this fixture
        changes -- the accepted values, arity and shape are untouched -- so
        if accumulation were not part of a flag's value space the gate would
        pass the exact divergence it was built to catch.

        Re-pointed when IM-269 landed. This test previously injected the
        divergence on `index` and then asserted that the REAL `list` and
        `search` violations were sitting in the waived set. That second
        assertion pinned the live defect as a fixture, so fixing IM-269
        broke a test of the mechanism -- the failure mode IM-239 hit with
        three IM-262 tests. It now asserts per-verb detection instead: the
        injected verb reports, the untouched verbs stay clean.
        """
        for injected in ("issue index", "issue list", "issue search"):
            with self.subTest(injected=injected):
                manifest = self.manifest()
                flag_entry(manifest, injected, "--status")["action"] = "store"
                unwaived, _ = self.run_lint(manifest=manifest)
                found = keys_of(unwaived)
                self.assertIn("value-space:--status@%s" % injected, found)
                for untouched in ("issue index", "issue list",
                                  "issue search"):
                    if untouched == injected:
                        continue
                    self.assertNotIn(
                        "value-space:--status@%s" % untouched, found,
                        "only the verb whose accumulation changed should "
                        "report; a blanket finding would not prove the "
                        "axis is per-verb")

    def test_unlicensed_deviation_fails(self):
        registry = self.registry()
        registry["concepts"]["json-output"]["deviations"] = []
        unwaived, _ = self.run_lint(registry=registry)
        keys = keys_of(unwaived)
        self.assertIn("value-space:--json@issue list", keys)
        self.assertIn("value-space:--json@issue show", keys)

    def test_licence_must_cite_a_real_requirement(self):
        registry = self.registry()
        registry["concepts"]["json-output"]["deviations"][0][
            "licensed_by"] = "output.made-this-up"
        unwaived, _ = self.run_lint(registry=registry)
        self.assertIn("unknown-license:json-output:output.made-this-up",
                      keys_of(unwaived))

    def test_converged_deviation_must_be_dropped(self):
        """A licence outliving the divergence it licensed."""
        registry = self.registry()
        registry["concepts"]["result-limit"]["deviations"] = [{
            "value_space": "optional/single/scalar",
            "verbs": ["issue index"],
            "licensed_by": "output.json-field-selection"}]
        unwaived, _ = self.run_lint(registry=registry)
        self.assertIn("obsolete-deviation:result-limit:optional/single/scalar",
                      keys_of(unwaived))

    def test_synonym_gap_fails(self):
        manifest = self.manifest()
        entry = flag_entry(manifest, "issue complete", "--summary")
        entry["names"] = ["--summary", "--reason"]
        unwaived, _ = self.run_lint(manifest=manifest)
        self.assertIn("synonym-gap:closing-note@issue complete:--note",
                      keys_of(unwaived))

    def test_class_contract_gap_fails(self):
        manifest = self.manifest()
        flags = verb(manifest, "issue list")["flags"]
        verb(manifest, "issue list")["flags"] = [
            f for f in flags if f["name"] != "--json"]
        unwaived, _ = self.run_lint(manifest=manifest)
        self.assertIn("class-contract:issue list:--json", keys_of(unwaived))

    def test_mutating_class_obligations_are_checked(self):
        manifest = self.manifest()
        flags = verb(manifest, "issue note")["flags"]
        verb(manifest, "issue note")["flags"] = [
            f for f in flags if f["name"] != "--lock-timeout"]
        unwaived, _ = self.run_lint(manifest=manifest)
        self.assertIn("class-contract:issue note:--lock-timeout",
                      keys_of(unwaived))

    def test_obsolete_exemption_fails(self):
        """An exemption for a gap that has since been closed."""
        registry = self.registry()
        registry["class_contract"]["exemptions"].append({
            "verb": "issue note", "flag": "--no-reindex",
            "reason": "fixture: this verb does register it",
            "decided_by": FIXTURE_ISSUE})
        unwaived, _ = self.run_lint(registry=registry)
        self.assertIn("obsolete-exemption:issue note:--no-reindex",
                      keys_of(unwaived))

    def test_exemption_for_an_obligation_that_does_not_exist_fails(self):
        registry = self.registry()
        registry["class_contract"]["exemptions"].append({
            "verb": "issue show", "flag": "--no-reindex",
            "reason": "fixture: show is a point-read, never obliged",
            "decided_by": FIXTURE_ISSUE})
        unwaived, _ = self.run_lint(registry=registry)
        self.assertIn("unreachable-exemption:issue show:--no-reindex",
                      keys_of(unwaived))

    def test_unregistered_positional_fails(self):
        """A new positional must be declared, exactly like a new flag."""
        manifest = self.manifest()
        pos = copy.deepcopy(verb(manifest, "issue note")["positionals"][1])
        pos.update(name="extra", dest="extra", metavar="EXTRA")
        verb(manifest, "issue note")["positionals"].append(pos)
        unwaived, _ = self.run_lint(manifest=manifest)
        self.assertIn("unregistered-positional:issue note:extra",
                      keys_of(unwaived))

    def test_positional_spelling_divergence_fails_per_verb(self):
        """A concept carried as a flag elsewhere and a positional here.

        Injected on `issue search`'s query operand (synthetically re-pointed
        at the status-filter concept, which `--status` spells on three
        verbs) rather than keyed on any live violation -- the three real
        instances are owned by IM-287 and leave with it, and a gate whose
        fixture is the defect dies when the defect is fixed (the IM-273 /
        IM-269 lesson).
        """
        registry = self.registry()
        registry["positionals"].append({
            "verb": "issue search", "dest": "query",
            "concept": "status-filter"})
        unwaived, _ = self.run_lint(registry=registry)
        found = keys_of(unwaived)
        self.assertIn("positional-spelling:status-filter@issue search",
                      found)
        for key in found:
            if key.startswith("positional-spelling:"):
                self.assertEqual(
                    key, "positional-spelling:status-filter@issue search",
                    "only the injected divergence should report unwaived; "
                    "a finding on an untouched verb means the check is not "
                    "per-row")

    def test_obsolete_positional_row_cannot_be_waived(self):
        """A row for an operand the surface lost fails, waiver or not."""
        registry = self.registry()
        registry["positionals"].append({
            "verb": "issue show", "dest": "gone",
            "meaning": "fixture: no such positional"})
        registry["waivers"].append(fixture_waiver(
            key="obsolete-positional:issue show:gone",
            note="fixture: tries to keep a dead row"))
        unwaived, _ = self.run_lint(registry=registry)
        self.assertIn("obsolete-positional:issue show:gone",
                      keys_of(unwaived))

    def test_positional_licence_must_cite_a_real_requirement(self):
        registry = self.registry()
        registry["positionals"].append({
            "verb": "issue search", "dest": "query",
            "concept": "status-filter",
            "licensed_by": "cli.made-this-up"})
        unwaived, _ = self.run_lint(registry=registry)
        self.assertIn(
            "unknown-license:status-filter@issue search:cli.made-this-up",
            keys_of(unwaived))

    def test_licensed_positional_is_silent(self):
        """A licence citing a documented capability ends the finding."""
        registry = self.registry()
        registry["positionals"].append({
            "verb": "issue search", "dest": "query",
            "concept": "status-filter",
            "licensed_by": "cli.search-modes"})
        unwaived, _ = self.run_lint(registry=registry)
        self.assertNotIn("positional-spelling:status-filter@issue search",
                         keys_of(unwaived))

    def test_short_flag_fails(self):
        manifest = self.manifest()
        flag_entry(manifest, "issue index", "--limit")["names"] = [
            "-n", "--limit"]
        unwaived, _ = self.run_lint(manifest=manifest)
        self.assertIn("short-flag:-n@issue index", keys_of(unwaived))

    def test_dash_h_stays_allowed(self):
        """The one registered short flag, so the check is not a blanket ban."""
        unwaived, _ = self.run_lint()
        self.assertEqual(
            [f.key for f in unwaived if f.key.startswith("short-flag:")], [])


class WaiverBaselineTests(unittest.TestCase):
    """The baseline shrinks; it cannot quietly stop covering anything."""

    @classmethod
    def setUpClass(cls):
        cls.base_manifest = surface_lint.manifest_from_disk()
        cls.base_registry = surface_lint.load_registry()
        cls.req_keys = surface_lint.requirement_keys()

    def lint(self, manifest=None, registry=None, **kw):
        kw.setdefault("req_keys", self.req_keys)
        return surface_lint.lint(
            manifest if manifest is not None
            else copy.deepcopy(self.base_manifest),
            registry if registry is not None
            else copy.deepcopy(self.base_registry), **kw)

    def fixture_pair(self):
        """A manifest carrying one real finding, plus the waiver for it.

        The registry's whole baseline becomes that one waiver, so the
        owner-side checks below run against a waiver that was demonstrably
        doing work. Reading `waivers[0]` off the committed registry is how
        they used to find one, which raises IndexError the moment the
        baseline reaches zero -- this phase's success condition (IM-289).
        """
        manifest = inject_unregistered_flag(copy.deepcopy(self.base_manifest))
        registry = copy.deepcopy(self.base_registry)
        registry["waivers"] = [fixture_waiver()]
        return manifest, registry

    def test_the_fixture_waiver_covers_a_real_finding(self):
        """Guards the fixture itself: a waiver covering nothing would make
        every owner-side test below pass for the wrong reason."""
        manifest, registry = self.fixture_pair()
        unwaived, waived = self.lint(manifest=manifest, registry=registry)
        self.assertIn(FIXTURE_FINDING, {f.key for f, _ in waived})
        self.assertNotIn(f"obsolete-waiver:{FIXTURE_FINDING}",
                         keys_of(unwaived))

    def test_removing_any_single_waiver_turns_the_lint_red(self):
        """IM-246 AC2, proven per waiver rather than claimed once.

        This is what makes the baseline an accounting of real findings: if
        any entry could be dropped with the lint staying green, that entry
        was describing nothing. The claim is per waiver, so it is vacuous
        at an empty baseline -- correctly: there is then nothing left to
        account for, and `test_an_empty_baseline_lints_clean` covers that
        state instead.
        """
        for dropped in self.base_registry["waivers"]:
            with self.subTest(waiver=dropped["key"]):
                registry = copy.deepcopy(self.base_registry)
                registry["waivers"] = [w for w in registry["waivers"]
                                       if w["key"] != dropped["key"]]
                unwaived, _ = self.lint(registry=registry)
                self.assertIn(dropped["key"], keys_of(unwaived))

    def test_waiver_matching_no_finding_fails_as_obsolete(self):
        registry = copy.deepcopy(self.base_registry)
        registry["waivers"].append(fixture_waiver(
            key="value-space:--limit@issue index",
            clause="surface.one-meaning-per-flag",
            note="fixture: deliberately matches no finding"))
        unwaived, _ = self.lint(registry=registry)
        self.assertIn("obsolete-waiver:value-space:--limit@issue index",
                      keys_of(unwaived))

    def owner_status(self, status):
        """Resolve the fixture's owner to `status`, every other issue open."""
        return lambda issue_id: (
            status if issue_id == FIXTURE_ISSUE else "open")

    def test_waiver_whose_issue_closed_fails(self):
        """IM-246 AC3. The fix landing is exactly when this must fire."""
        manifest, registry = self.fixture_pair()
        unwaived, _ = self.lint(manifest=manifest, registry=registry,
                                issue_status=self.owner_status("done"))
        self.assertIn(f"closed-waiver-issue:{FIXTURE_FINDING}",
                      keys_of(unwaived))

    def test_cancelled_counts_as_closed(self):
        manifest, registry = self.fixture_pair()
        unwaived, _ = self.lint(manifest=manifest, registry=registry,
                                issue_status=self.owner_status("cancelled"))
        self.assertIn(f"closed-waiver-issue:{FIXTURE_FINDING}",
                      keys_of(unwaived))

    def test_waiver_naming_an_issue_the_store_lacks_fails(self):
        manifest, registry = self.fixture_pair()
        unwaived, _ = self.lint(manifest=manifest, registry=registry,
                                issue_status=self.owner_status(None))
        self.assertIn(f"closed-waiver-issue:{FIXTURE_FINDING}",
                      keys_of(unwaived))

    def test_in_progress_issue_still_owns_its_waiver(self):
        manifest, registry = self.fixture_pair()
        unwaived, _ = self.lint(manifest=manifest, registry=registry,
                                issue_status=lambda issue_id: "in-progress")
        self.assertEqual(
            [f.key for f in unwaived
             if f.key.startswith("closed-waiver-issue:")], [])

    def test_an_empty_baseline_lints_clean(self):
        """Zero waivers is a pass -- the state this phase is driving at.

        Each waiver key names the surface element it is about -- a flag, or
        a (concept, verb) positional pair -- so retiring that element from
        BOTH sides, the surface and the declarations describing it, stands
        in for whatever fix the owning issue lands, without this test having
        to predict it. It stays valid once the real baseline is empty: there
        is then nothing to retire and the committed pair is checked as it
        stands.
        """
        manifest = copy.deepcopy(self.base_manifest)
        registry = copy.deepcopy(self.base_registry)
        flags = set()
        for waiver in registry["waivers"]:
            spelled = re.match(r"positional-spelling:([a-z0-9-]+)@(.+)$",
                               waiver["key"])
            if spelled:
                cid, verb_path = spelled.groups()
                dests = {row["dest"] for row in registry["positionals"]
                         if row["verb"] == verb_path
                         and row.get("concept") == cid}
                registry["positionals"] = [
                    row for row in registry["positionals"]
                    if not (row["verb"] == verb_path
                            and row["dest"] in dests)]
                entry = verb(manifest, verb_path)
                entry["positionals"] = [p for p in entry["positionals"]
                                        if p["dest"] not in dests]
                continue
            found = re.search(r"--[a-z0-9-]+", waiver["key"])
            if found:
                flags.add(found.group(0))
        for entry in manifest["verbs"]:
            entry["flags"] = [f for f in entry["flags"]
                              if not set(f["names"]) & flags]
        for name in flags:
            registry["flags"].pop(name, None)
        for concept in [k for k, c in registry["concepts"].items()
                        if c["canonical"] in flags]:
            registry["concepts"].pop(concept)
        registry["waivers"] = []
        unwaived, waived = self.lint(manifest=manifest, registry=registry)
        self.assertEqual(keys_of(unwaived), set())
        self.assertEqual(waived, [])

    def test_the_waiver_checks_cannot_themselves_be_waived(self):
        registry = copy.deepcopy(self.base_registry)
        registry["waivers"].append(fixture_waiver(
            key="value-space:--limit@issue index",
            note="fixture: obsolete on purpose"))
        registry["waivers"].append(fixture_waiver(
            key="obsolete-waiver:value-space:--limit@issue index",
            note="fixture: tries to hide the above"))
        unwaived, _ = self.lint(registry=registry)
        self.assertIn("obsolete-waiver:value-space:--limit@issue index",
                      keys_of(unwaived),
                      "waiving the obsolete-waiver finding would let the "
                      "baseline stop shrinking")

    def test_every_waiver_carries_a_clause_and_an_owner(self):
        for waiver in self.base_registry["waivers"]:
            with self.subTest(key=waiver["key"]):
                self.assertRegex(waiver["issue"], r"^[A-Z]+-\d+$")
                self.assertTrue(waiver["clause"].startswith("surface."))


class ExemptionProvenanceTests(unittest.TestCase):
    """An exemption's `decided_by` is provenance: existence, not openness.

    An exemption is a permanent licensed declaration that a flag's concept
    does not apply to a verb. The decision issue that vetted it closes when
    the decision is recorded, so a closed `decided_by` is the normal state
    and only one the store does not have is a finding -- the inverse of the
    waiver channel, whose owner must stay OPEN because a waiver is a
    tracked gap (IM-292; the two were confused once, on IM-271).
    """

    @classmethod
    def setUpClass(cls):
        cls.base_manifest = surface_lint.manifest_from_disk()
        cls.base_registry = surface_lint.load_registry()
        cls.req_keys = surface_lint.requirement_keys()

    def lint(self, registry=None, **kw):
        kw.setdefault("req_keys", self.req_keys)
        return surface_lint.lint(
            copy.deepcopy(self.base_manifest),
            registry if registry is not None
            else copy.deepcopy(self.base_registry), **kw)

    def provenance_keys(self, findings):
        return {f.key for f in findings
                if f.key.startswith("unknown-exemption-decision:")}

    def test_decision_issue_the_store_lacks_fails(self):
        registry = copy.deepcopy(self.base_registry)
        ex = registry["class_contract"]["exemptions"][0]
        ex["decided_by"] = FIXTURE_ISSUE
        unwaived, _ = self.lint(
            registry=registry,
            issue_status=lambda i: None if i == FIXTURE_ISSUE else "open")
        self.assertIn(
            f"unknown-exemption-decision:{ex['verb']}:{ex['flag']}",
            self.provenance_keys(unwaived))

    def test_closed_decision_issue_is_not_a_finding(self):
        """Every decision issue reads as done; nothing may fire."""
        unwaived, _ = self.lint(issue_status=lambda i: "done")
        self.assertEqual(self.provenance_keys(unwaived), set())

    def test_without_issue_status_provenance_is_not_checked(self):
        """No store resolved means the check downgrades, like the waiver
        owner check -- the store-backed run is the authoritative one."""
        unwaived, _ = self.lint(issue_status=None)
        self.assertEqual(self.provenance_keys(unwaived), set())

    def test_provenance_finding_cannot_be_waived(self):
        registry = copy.deepcopy(self.base_registry)
        ex = registry["class_contract"]["exemptions"][0]
        ex["decided_by"] = FIXTURE_ISSUE
        key = f"unknown-exemption-decision:{ex['verb']}:{ex['flag']}"
        registry["waivers"].append(fixture_waiver(
            key=key, clause="surface.verb-class-contract",
            note="fixture: tries to hide missing provenance"))
        unwaived, _ = self.lint(
            registry=registry,
            issue_status=lambda i: None if i == FIXTURE_ISSUE else "open")
        self.assertIn(key, self.provenance_keys(unwaived),
                      "waiving a provenance finding would let an exemption "
                      "outlive the record of who granted it")


class RegistryIntegrityTests(unittest.TestCase):
    """Authoring errors fail loud instead of becoming findings."""

    def setUp(self):
        self.registry = copy.deepcopy(surface_lint.load_registry())

    def assertRefused(self, registry, needle):
        with self.assertRaises(surface_lint.LintError) as ctx:
            surface_lint.validate_registry(registry)
        self.assertIn(needle, str(ctx.exception))

    def test_schema_mismatch_refused(self):
        self.registry["schema"] = 99
        self.assertRefused(self.registry, "schema")

    def test_unknown_concept_refused(self):
        self.registry["flags"]["--limit"]["concept"] = "page-size"
        self.assertRefused(self.registry, "unknown concept")

    def test_canonical_spelling_must_be_registered(self):
        self.registry["concepts"]["result-limit"]["canonical"] = "--cap"
        self.assertRefused(self.registry, "canonical")

    def _inject_collision(self, **fields):
        """Declare a synthetic second meaning for `--limit` and return it.

        INJECTED rather than borrowed from whatever the live registry
        happens to collide on today. Both tests below keyed on `--prefix`'s
        write-operand override, which IM-236 deleted when it settled that
        the read and write uses are one concept -- so a test OF THE
        MECHANISM died with the violation it used as a fixture, the same
        failure mode IM-269 and IM-239 each hit. A mechanism test that
        depends on a live collision existing is measuring the collision.
        """
        override = {"concept": "page-size", "issue": "IM-000",
                    "verbs": ["issue list"]}
        override.update(fields)
        self.registry["concepts"]["page-size"] = {
            "canonical": "--page-size",
            "dests": ["limit"],
            "meaning": "Synthetic second meaning, injected by this test.",
            "value_space": "one/single/scalar",
        }
        self.registry["flags"]["--page-size"] = {"concept": "page-size"}
        self.registry["flags"]["--limit"]["overrides"] = [override]
        return override

    def test_collision_without_an_owning_issue_refused(self):
        """A second meaning for one spelling always has an owner."""
        self._inject_collision()
        self.registry["flags"]["--limit"]["overrides"][0].pop("issue")
        self.assertRefused(self.registry, "no owning issue")

    def test_collision_naming_no_verbs_refused(self):
        self._inject_collision(verbs=[])
        self.assertRefused(self.registry, "naming no verbs")

    def test_concept_without_a_meaning_refused(self):
        self.registry["concepts"]["result-limit"].pop("meaning")
        self.assertRefused(self.registry, "meaning")

    def test_positional_row_without_verb_refused(self):
        self.registry["positionals"].append({"dest": "x", "meaning": "x"})
        self.assertRefused(self.registry, "names no verb/dest")

    def test_positional_row_with_unknown_concept_refused(self):
        self.registry["positionals"].append({
            "verb": "issue search", "dest": "query", "concept": "page-size"})
        self.assertRefused(self.registry, "unknown concept")

    def test_positional_row_saying_nothing_refused(self):
        self.registry["positionals"].append({
            "verb": "issue search", "dest": "query"})
        self.assertRefused(self.registry, "neither a concept nor a meaning")

    def test_positional_licence_without_a_concept_refused(self):
        self.registry["positionals"].append({
            "verb": "issue search", "dest": "query",
            "meaning": "fixture", "licensed_by": "cli.search-modes"})
        self.assertRefused(self.registry, "names no concept")

    def test_positional_meaning_alongside_a_concept_refused(self):
        self.registry["positionals"].append({
            "verb": "issue search", "dest": "query",
            "concept": "status-filter", "meaning": "fixture"})
        self.assertRefused(self.registry, "the concept owns the meaning")

    def test_duplicate_positional_row_refused(self):
        self.registry["positionals"].append(
            dict(self.registry["positionals"][0]))
        self.assertRefused(self.registry, "duplicate declaration")

    def test_exemption_without_a_reason_refused(self):
        self.registry["class_contract"]["exemptions"][0].pop("reason")
        self.assertRefused(self.registry, "declares no reason")

    def test_exemption_without_a_deciding_issue_refused(self):
        """A licence records the decision that granted it (IM-292).

        This is what keeps a sixth exemption from absorbing a future real
        gap behind a plausible free-text sentence: the entry cannot exist
        without naming the decision issue that vetted it.
        """
        self.registry["class_contract"]["exemptions"][0].pop("decided_by")
        self.assertRefused(self.registry, "no deciding issue")


class ClausePartitionTests(unittest.TestCase):
    """Every `surface.*` clause is either checked here or declared unchecked.

    The point is that the gap between the twelve adopted clauses and what
    is mechanically enforced stays visible. A clause that is neither
    enforced nor listed in the registry's `unenforced` table fails, so a
    thirteenth clause cannot be adopted and then quietly go unchecked --
    and the enforced set is OBSERVED from the findings the checks actually
    emit, never read from a hand-kept list of which checks exist.
    """

    @classmethod
    def setUpClass(cls):
        cls.registry = surface_lint.load_registry()
        cls.manifest = surface_lint.manifest_from_disk()
        cls.req_keys = surface_lint.requirement_keys()

    def observed_clauses(self):
        """Clauses that real findings carry, across the live tree and the
        synthetic fixtures the gate-bites tests use."""
        clauses = set()
        unwaived, waived = surface_lint.lint(
            self.manifest, self.registry, req_keys=self.req_keys)
        for finding in unwaived:
            clauses.add(finding.clause)
        for finding, _ in waived:
            clauses.add(finding.clause)
        # long-flags-only has no live instance by design, so provoke one.
        manifest = copy.deepcopy(self.manifest)
        flag_entry(manifest, "issue index", "--limit")["names"] = [
            "-n", "--limit"]
        more, _ = surface_lint.lint(manifest, self.registry,
                                    req_keys=self.req_keys)
        for finding in more:
            clauses.add(finding.clause)
        # one-meaning-per-flag likewise has no live instance once the
        # positional baseline converges (IM-287), so provoke a
        # positional-spelling divergence the same way.
        registry = copy.deepcopy(self.registry)
        registry["positionals"].append({
            "verb": "issue search", "dest": "query",
            "concept": "status-filter"})
        more, _ = surface_lint.lint(self.manifest, registry,
                                    req_keys=self.req_keys)
        for finding in more:
            clauses.add(finding.clause)
        return clauses

    def test_surface_clauses_are_partitioned(self):
        adopted = {key for key in self.req_keys if key.startswith("surface.")}
        self.assertGreaterEqual(len(adopted), 12)
        enforced = self.observed_clauses()
        declared_unchecked = {entry["clause"]
                              for entry in self.registry["unenforced"]}
        unaccounted = adopted - enforced - declared_unchecked
        self.assertEqual(
            unaccounted, set(),
            "these surface.* clauses are neither enforced by a check nor "
            "listed in the registry's `unenforced` table; adopting a clause "
            "without deciding how it is checked is how the drift in IM-261 "
            "started")

    def test_unenforced_table_names_only_real_clauses(self):
        adopted = {key for key in self.req_keys if key.startswith("surface.")}
        for entry in self.registry["unenforced"]:
            with self.subTest(clause=entry["clause"]):
                self.assertIn(entry["clause"], adopted)

    def test_unenforced_entries_name_an_authority_or_an_owner(self):
        """Not checked here is fine; not checked anywhere, unowned, is not."""
        for entry in self.registry["unenforced"]:
            with self.subTest(clause=entry["clause"]):
                self.assertTrue(
                    entry.get("enforced_by") or entry.get("issues"),
                    "an unenforced clause names either the authority that "
                    "does check it or the issue(s) that will")

    def test_self_naming_authority_exists(self):
        """The `enforced_by` pointer terminates in a real test class.

        IM-239 built the self-naming check, so this module must not build a
        second one -- but a prose pointer that goes stale would leave the
        clause unchecked with the table still claiming coverage.
        """
        entry = next(e for e in self.registry["unenforced"]
                     if e["clause"] == "surface.self-naming")
        self.assertIn("SelfNamingTests", entry["enforced_by"])
        source = (REPO_ROOT / "tests" / "test_cli_surface.py").read_text(
            encoding="utf-8")
        self.assertIn("class SelfNamingTests", source)

    def test_store_column_authority_exists(self):
        """Likewise for the `--store` column IM-245 owns.

        The pointer used to name the authority's waiver dict as well. IM-275
        closed both gaps and deleted that dict, so naming it here would
        re-create the drift this test exists to catch -- the authority is the
        check, not the exemptions it once carried.
        """
        source = (REPO_ROOT / "tests" / "test_cli_surface.py").read_text(
            encoding="utf-8")
        self.assertIn("test_store_flag_verbs_register_store", source)


class ExecutableTests(unittest.TestCase):
    """The `scripts/surface-lint` entry point itself."""

    SCRIPT = REPO_ROOT / "scripts" / "surface-lint"

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True,
                              cwd=str(REPO_ROOT))

    def test_clean_tree_exits_zero(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clean", result.stdout)

    def test_help_and_version(self):
        self.assertEqual(self.run_script("--help").returncode, 0)
        version = self.run_script("--version")
        self.assertEqual(version.returncode, 0)
        self.assertRegex(version.stdout.strip(), r"^v?\d|^dev-")

    def test_json_shape(self):
        result = self.run_script("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["totals"]["unwaived"], 0)
        self.assertEqual(payload["totals"]["waived"],
                         len(payload["waived"]))
        for row in payload["waived"]:
            self.assertRegex(row["issue"], r"^[A-Z]+-\d+$")

    def test_bad_argument_is_a_usage_error(self):
        self.assertEqual(self.run_script("--nope").returncode, 2)
