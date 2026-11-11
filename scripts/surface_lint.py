#!/usr/bin/env python3
"""Surface vocabulary linter -- the validator half of IM-246.

`docs/cli-surface.json` (IM-245) records WHAT the `issue` CLI's argument
surface is, extracted from the argparse registrations. This module records
what that surface is allowed to MEAN -- `docs/cli-vocabulary.json`, the flag
vocabulary registry -- and checks the manifest against it.

The division of labour matters. The manifest is derived from code, so it is
ground truth about spellings, value spaces, and synonym links. The registry
is a declaration of intent: one concept per flag name, one canonical
spelling per concept, and the deviations a documented capability licenses.
Every registry entry is therefore checked against the manifest in BOTH
directions, per IM-261's rule: an unregistered flag name fails, and a
registered name, declared deviation, collision, or exemption that the
surface no longer carries fails as obsolete. A declaration that cannot go
stale is a declaration doing no work.

Checks, each keyed to the `surface.*` requirement it enforces:

  registry-completeness   surface.vocabulary-registry
  meaning-uniformity      surface.one-meaning-per-flag
  value-space             surface.one-meaning-per-flag
  synonym-uniformity      surface.vocabulary-registry
  class-contract          surface.verb-class-contract
  long-flags-only         surface.long-flags-only

Two clauses are deliberately NOT implemented here, each because another
authority already owns it and a second implementation would be a second
declaration of one rule:

  surface.self-naming          IM-239 built it as `SelfNamingTests` in
                               tests/test_cli_surface.py, keyed on each
                               verb's PROG declaration.
  surface.verb-class-contract  the `--store` column only -- IM-245 checks
                               the four-valued `store` declaration against
                               the registrations in the same test module.

The clauses no manifest can answer (structured error payloads, tracebacks,
non-interactive refusal, dry-run coverage, exit-code space) are recorded in
the registry's `unenforced` table with the issue that owns each, and
`tests/test_surface_lint.py` asserts that every `surface.*` requirement is
either enforced by a check here or listed there -- so the gap between the
twelve clauses and what is mechanically checked is itself visible and
shrink-only.

Waivers live in the registry's `waivers` list, each naming the open issue
that owns the finding. A waiver is itself checked: one that matches no
finding is obsolete (the baseline shrinks, never grows quietly), and one
whose issue has been closed is a failure rather than a silent pass.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "docs" / "cli-vocabulary.json"
MANIFEST_PATH = REPO_ROOT / "docs" / "cli-surface.json"
REQUIREMENTS_PATH = REPO_ROOT / "docs" / "requirements.md"

SCHEMA = 1

# A flag's value space, normalized away from the argparse implementation
# detail the manifest records. `--help` is registered three different ways
# across the surface (`help` with nargs 0, `help` with nargs None, and
# `store_true` on the hand-rolled argv verbs) and means the same thing in
# all three, so the raw action name cannot be the unit of comparison.
#
# Three axes, each agent-visible:
#   arity        none | optional | one     -- what one occurrence takes
#   accumulation single | repeatable       -- whether occurrences compose
#   shape        scalar | list | pair      -- the shape of one value
#
# Accumulation is in deliberately: `--status open --status done` returns
# both statuses on `index` and silently drops the first on `list`, which is
# the agent-visible failure the clause exists to catch (IM-269).
#
# `type` is deliberately OUT. It reads as a divergence on `--limit` (int on
# `index`/`ready`, unset on the history verbs) and is not one: `issue
# history show X --limit abc` exits 2 exactly as `issue index --limit abc`
# does, because the history verbs validate the coercion outside argparse.
# Measured 2026-11-09, before this axis set was fixed.
VALUELESS_ACTIONS = frozenset(
    {"store_true", "store_false", "help", "version", "count"})
REPEATING_ACTIONS = frozenset(
    {"append", "append_const", "OrderedFlagAction"})

CLOSED_STATUSES = frozenset({"done", "cancelled"})


class LintError(Exception):
    """The lint could not run: unreadable or incoherent inputs."""


class Finding:
    """One lint finding. `key` is the identity a waiver matches on."""

    __slots__ = ("clause", "check", "key", "detail")

    def __init__(self, clause, check, key, detail):
        self.clause = clause
        self.check = check
        self.key = key
        self.detail = detail

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<Finding {self.check} {self.key}>"

    def as_dict(self):
        return {"clause": self.clause, "check": self.check, "key": self.key,
                "detail": self.detail}


def value_space(flag) -> str:
    """Normalize one manifest flag entry to its value space."""
    if flag["action"] in VALUELESS_ACTIONS or flag["nargs"] == 0:
        return "none"
    arity = "optional" if flag["nargs"] == "?" else "one"
    accumulation = ("repeatable" if flag["action"] in REPEATING_ACTIONS
                    else "single")
    metavar = flag["metavar"] or ""
    if "[," in metavar:
        shape = "list"
    elif "=" in metavar:
        shape = "pair"
    else:
        shape = "scalar"
    return f"{arity}/{accumulation}/{shape}"


# --------------------------------------------------------------------------
# registry


def load_registry(path=None):
    path = Path(path) if path else REGISTRY_PATH
    if not path.is_file():
        raise LintError(f"no vocabulary registry at {path}")
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise LintError(f"{path}: {e}")
    return validate_registry(registry)


def validate_registry(registry):
    """Fail loud on a registry that cannot be reasoned about.

    These are authoring errors, not surface findings: a flag pointing at an
    unknown concept would make "one meaning per flag" unanswerable, so it
    cannot be waived into the baseline.
    """
    if registry.get("schema") != SCHEMA:
        raise LintError(f"registry schema {registry.get('schema')!r} is not "
                        f"the expected {SCHEMA}")
    concepts = registry.get("concepts")
    flags = registry.get("flags")
    if not isinstance(concepts, dict) or not concepts:
        raise LintError("registry declares no concepts")
    if not isinstance(flags, dict) or not flags:
        raise LintError("registry declares no flags")

    for cid, concept in sorted(concepts.items()):
        for field in ("meaning", "canonical", "value_space"):
            if not concept.get(field):
                raise LintError(f"concept {cid!r} declares no {field}")
        if concept["canonical"] not in flags:
            raise LintError(f"concept {cid!r} names canonical spelling "
                            f"{concept['canonical']} which is not in `flags`")

    for name, decl in sorted(flags.items()):
        if not name.startswith("-"):
            raise LintError(f"registry key {name!r} is not a flag")
        if decl.get("concept") not in concepts:
            raise LintError(f"flag {name} points at unknown concept "
                            f"{decl.get('concept')!r}")
        for override in decl.get("overrides", []):
            if override.get("concept") not in concepts:
                raise LintError(f"flag {name} overrides to unknown concept "
                                f"{override.get('concept')!r}")
            if not override.get("verbs"):
                raise LintError(f"flag {name} declares an override naming no "
                                f"verbs")
            if not override.get("issue"):
                raise LintError(
                    f"flag {name} overrides to {override['concept']!r} with "
                    f"no owning issue; a second meaning for one spelling is "
                    f"a collision, and a collision needs an owner")
    return registry


def concept_for(decl, verb_path) -> str:
    """The concept a flag carries ON THIS VERB (overrides win)."""
    for override in decl.get("overrides", []):
        if verb_path in override["verbs"]:
            return override["concept"]
    return decl["concept"]


def spellings_of(registry, cid) -> list:
    """Every spelling whose DEFAULT concept is `cid`, canonical first.

    Overrides are collisions, not synonyms, so a spelling that merely
    collides into this concept on some verbs is excluded -- otherwise
    `--repo` would read as a synonym of the repo-identity concept it
    collides with.
    """
    canonical = registry["concepts"][cid]["canonical"]
    rest = sorted(name for name, decl in registry["flags"].items()
                  if decl["concept"] == cid and name != canonical)
    owned = [name for name, decl in registry["flags"].items()
             if decl["concept"] == cid]
    return ([canonical] + rest) if canonical in owned else rest


def requirement_keys(path=None) -> set:
    """Every `[REQ: key]` tag in requirements.md.

    A licensed value-space deviation cites one of these, so the licence
    terminates in the requirements document rather than in a string the
    registry made up.
    """
    path = Path(path) if path else REQUIREMENTS_PATH
    if not path.is_file():
        raise LintError(f"no requirements document at {path}")
    text = path.read_text(encoding="utf-8")
    keys, marker, start = set(), "[REQ:", 0
    while True:
        at = text.find(marker, start)
        if at < 0:
            return keys
        end = text.find("]", at)
        if end < 0:
            return keys
        keys.add(text[at + len(marker):end].strip())
        start = end


def manifest_from_disk(path=None):
    path = Path(path) if path else MANIFEST_PATH
    if not path.is_file():
        raise LintError(f"no surface manifest at {path}; run "
                        f"`make cli-surface`")
    return json.loads(path.read_text(encoding="utf-8"))


def flag_uses(manifest) -> dict:
    """flag name -> list of (verb path, verb class, flag entry)."""
    uses = {}
    for verb in manifest["verbs"]:
        for flag in verb["flags"]:
            for name in flag["names"]:
                uses.setdefault(name, []).append(
                    (verb["path"], verb["verb_class"], flag))
    return uses


# --------------------------------------------------------------------------
# checks


def check_registry_completeness(manifest, registry, findings, **_):
    """surface.vocabulary-registry, in both directions."""
    uses = flag_uses(manifest)
    declared = registry["flags"]

    for name in sorted(uses):
        if name not in declared:
            verbs = sorted({path for path, _, _ in uses[name]})
            shown = ", ".join(verbs[:4]) + (", ..." if len(verbs) > 4 else "")
            findings.append(Finding(
                "surface.vocabulary-registry", "registry-completeness",
                f"unregistered-flag:{name}",
                f"{name} appears on {len(verbs)} verb(s) ({shown}) but is "
                f"not in the vocabulary registry -- register the vocabulary "
                f"first, so adding a flag name stays a deliberate act"))

    for name, decl in sorted(declared.items()):
        if name not in uses:
            findings.append(Finding(
                "surface.vocabulary-registry", "registry-completeness",
                f"obsolete-spelling:{name}",
                f"the registry declares {name} (concept "
                f"{decl['concept']!r}), which no verb offers; an entry for a "
                f"spelling that does not exist cannot go stale against the "
                f"code -- remove it"))


def check_meaning_uniformity(manifest, registry, findings, **_):
    """surface.one-meaning-per-flag.

    Two mechanisms, because meaning is not machine-derivable:

    1. Every declared COLLISION -- one spelling overridden to a second
       concept on named verbs -- surfaces as a finding that must be waived
       to the issue that owns it, and is checked for obsolescence. This is
       how `--repo` carries both "scope to this prefix" and "the repo
       basename", and how `--type` still answers to the dependency kind on
       `dep`.
    2. Every DERIVABLE signal of an undeclared second meaning fails. A
       spelling bound to two argparse destinations under one concept is
       that signal. The proxy is sound but incomplete -- `--prefix` is a
       write operand on `admin init-project` under the same dest as its
       scoping use, which no derivation can see -- so the incompleteness is
       recorded in the registry's `unenforced` table rather than implied.
    """
    uses = flag_uses(manifest)

    for name, decl in sorted(registry["flags"].items()):
        for override in decl.get("overrides", []):
            live = {path for path, _, _ in uses.get(name, [])}
            covered = sorted(set(override["verbs"]) & live)
            if not covered:
                findings.append(Finding(
                    "surface.one-meaning-per-flag", "meaning-uniformity",
                    f"obsolete-collision:{name}:{override['concept']}",
                    f"{name} is declared to carry concept "
                    f"{override['concept']!r} on "
                    f"{', '.join(sorted(override['verbs']))}, where it is no "
                    f"longer registered -- the collision is gone, so remove "
                    f"the override"))
                continue
            findings.append(Finding(
                "surface.one-meaning-per-flag", "meaning-uniformity",
                f"meaning-collision:{name}:{override['concept']}",
                f"{name} means {decl['concept']!r} on the rest of the "
                f"surface and {override['concept']!r} on "
                f"{', '.join(covered)}; one spelling, two concepts "
                f"({override['issue']})"))

    by_concept = {}
    for name, decl in registry["flags"].items():
        for path, _, flag in uses.get(name, []):
            cid = concept_for(decl, path)
            by_concept.setdefault(cid, {}).setdefault(
                flag["dest"] or "None", set()).add(f"{name}@{path}")

    for cid, concept in sorted(registry["concepts"].items()):
        actual = by_concept.get(cid, {})
        declared_dests = set(concept.get("dests", []))
        for dest in sorted(set(actual) - declared_dests):
            where = ", ".join(sorted(actual[dest]))
            findings.append(Finding(
                "surface.one-meaning-per-flag", "meaning-uniformity",
                f"dest-divergence:{cid}:{dest}",
                f"concept {cid!r} binds the undeclared destination {dest!r} "
                f"({where}). Either the spelling carries a second meaning "
                f"there -- declare the collision and give it an owning "
                f"issue -- or the destinations differ internally only, in "
                f"which case declare {dest!r} in the concept's `dests`"))
        for dest in sorted(declared_dests - set(actual)):
            findings.append(Finding(
                "surface.one-meaning-per-flag", "meaning-uniformity",
                f"obsolete-dest:{cid}:{dest}",
                f"concept {cid!r} declares the destination {dest!r}, which "
                f"no registration binds any more"))


def check_value_space(manifest, registry, findings, *, req_keys=None, **_):
    """surface.one-meaning-per-flag's value-space half.

    A concept declares one default value space. Every registration carrying
    that concept must match it, unless a declared deviation covers that
    verb -- and a deviation is licensed only by a documented capability,
    cited as a requirement key that exists in requirements.md.
    """
    if req_keys is None:
        req_keys = requirement_keys()
    uses = flag_uses(manifest)

    registrations = {}
    for name, decl in registry["flags"].items():
        for path, _, flag in uses.get(name, []):
            registrations.setdefault(concept_for(decl, path), []).append(
                (name, path, value_space(flag)))

    for cid, concept in sorted(registry["concepts"].items()):
        mine = sorted(registrations.get(cid, []))
        actual_by_verb = {}
        for _, path, space in mine:
            actual_by_verb.setdefault(path, set()).add(space)

        licensed = {}
        for deviation in concept.get("deviations", []):
            key = deviation.get("licensed_by")
            if key not in req_keys:
                findings.append(Finding(
                    "surface.one-meaning-per-flag", "value-space",
                    f"unknown-license:{cid}:{key}",
                    f"concept {cid!r} licenses a value-space deviation with "
                    f"{key!r}, which is not a [REQ:] key in "
                    f"docs/requirements.md -- a licence cites a documented "
                    f"capability, or it is not a licence"))
            space = deviation["value_space"]
            verbs = list(deviation["verbs"])
            if not any(space in actual_by_verb.get(verb, set())
                       for verb in verbs):
                findings.append(Finding(
                    "surface.one-meaning-per-flag", "value-space",
                    f"obsolete-deviation:{cid}:{space}",
                    f"concept {cid!r} declares a {space!r} deviation on "
                    f"{', '.join(sorted(verbs))}, which no registration has "
                    f"any more -- the surface converged, so drop the licence"))
            for verb in verbs:
                licensed[verb] = space

        for name, path, actual in mine:
            expected = licensed.get(path, concept["value_space"])
            if actual != expected:
                findings.append(Finding(
                    "surface.one-meaning-per-flag", "value-space",
                    f"value-space:{name}@{path}",
                    f"{name} on `{path}` takes {actual!r}; concept {cid!r} "
                    f"declares {expected!r}. Value-space variation is "
                    f"licensed only by a documented capability, never by "
                    f"local convenience"))


def check_synonym_uniformity(manifest, registry, findings, **_):
    """surface.vocabulary-registry's synonym half.

    "A spelling accepted for a concept on any verb is accepted for that
    concept on every verb where the concept appears." The concept appears
    on a verb when the verb accepts any of its spellings, which is the only
    reading the surface can answer. Help text may still present one
    canonical spelling per verb -- acceptance is the obligation, not
    advertisement.
    """
    for cid in sorted(registry["concepts"]):
        declared = set(spellings_of(registry, cid))
        if len(declared) < 2:
            continue
        for verb in manifest["verbs"]:
            registered = {name for flag in verb["flags"]
                          for name in flag["names"]}
            accepted = registered & declared
            # A spelling that collides into another concept here is not
            # this concept's appearance on the verb.
            accepted = {name for name in accepted
                        if concept_for(registry["flags"][name],
                                       verb["path"]) == cid}
            if not accepted:
                continue
            for name in sorted(declared - accepted):
                if concept_for(registry["flags"][name],
                               verb["path"]) != cid:
                    continue
                findings.append(Finding(
                    "surface.vocabulary-registry", "synonym-uniformity",
                    f"synonym-gap:{cid}@{verb['path']}:{name}",
                    f"`{verb['path']}` accepts {', '.join(sorted(accepted))} "
                    f"for concept {cid!r} but not {name}; a synonym set is "
                    f"uniform wherever the concept appears, so an agent need "
                    f"not re-learn the spelling per verb"))


def check_class_contract(manifest, registry, findings, **_):
    """surface.verb-class-contract, minus the `--store` column.

    Exemptions are declared per (verb, flag) with a reason and are
    shrink-only: an exemption for a verb that has since gained the flag
    fails, so the list cannot outlive the gap it describes.
    """
    contract = registry.get("class_contract", {})
    exemptions = {(e["verb"], e["flag"]): e
                  for e in contract.get("exemptions", [])}
    claimed = set()

    for verb in manifest["verbs"]:
        names = {name for flag in verb["flags"] for name in flag["names"]}
        obliged = list(contract.get("all", []))
        obliged += list(contract.get("by_class", {}).get(
            verb["verb_class"], []))
        for flag_name in obliged:
            exempt = (verb["path"], flag_name) in exemptions
            if flag_name in names:
                if exempt:
                    claimed.add((verb["path"], flag_name))
                    findings.append(Finding(
                        "surface.verb-class-contract", "class-contract",
                        f"obsolete-exemption:{verb['path']}:{flag_name}",
                        f"`{verb['path']}` is exempted from {flag_name} and "
                        f"now registers it -- remove the exemption rather "
                        f"than leaving one that covers nothing"))
                continue
            if exempt:
                claimed.add((verb["path"], flag_name))
                continue
            findings.append(Finding(
                "surface.verb-class-contract", "class-contract",
                f"class-contract:{verb['path']}:{flag_name}",
                f"`{verb['path']}` declares class {verb['verb_class']!r}, "
                f"whose contract obliges {flag_name}, and does not offer it"))

    for path, flag_name in sorted(set(exemptions) - claimed):
        findings.append(Finding(
            "surface.verb-class-contract", "class-contract",
            f"unreachable-exemption:{path}:{flag_name}",
            f"the exemption for {flag_name} on `{path}` names no obligation "
            f"the contract makes -- a stale verb path, or a flag that verb's "
            f"class never required"))


def check_long_flags_only(manifest, registry, findings, **_):
    """surface.long-flags-only: `-h` is the only registered short flag."""
    allowed = set(registry.get("short_flags", ["-h"]))
    for verb in manifest["verbs"]:
        for flag in verb["flags"]:
            for name in flag["names"]:
                if len(name) == 2 and name.startswith("-") and (
                        not name.startswith("--")) and name not in allowed:
                    findings.append(Finding(
                        "surface.long-flags-only", "long-flags-only",
                        f"short-flag:{name}@{verb['path']}",
                        f"`{verb['path']}` registers the short flag {name}; "
                        f"admitting one requires a registry entry recorded "
                        f"through a decision issue"))


# --------------------------------------------------------------------------
# waiver reconciliation

CHECKS = (
    check_registry_completeness,
    check_meaning_uniformity,
    check_value_space,
    check_synonym_uniformity,
    check_class_contract,
    check_long_flags_only,
)

# A waiver can never suppress a finding ABOUT the declarations themselves.
# Otherwise the baseline could be made to stop shrinking by waiving the
# check that enforces the shrinking.
UNWAIVABLE_PREFIXES = (
    "dest-divergence:",
    "obsolete-waiver:", "closed-waiver-issue:", "obsolete-spelling:",
    "obsolete-deviation:", "obsolete-dest:", "obsolete-exemption:",
    "obsolete-collision:", "unreachable-exemption:", "unknown-license:",
)


def lint(manifest, registry, *, issue_status=None, req_keys=None):
    """Run every check and reconcile findings against the waiver baseline.

    Returns (unwaived, waived) where `waived` pairs each suppressed finding
    with the waiver that suppressed it. `issue_status` is an optional
    callable mapping an issue id to its status; when given, a waiver naming
    a closed issue is itself a finding. It is injected rather than resolved
    here so the checks stay hermetic: tests point it at a fixture, the CLI
    points it at the real store.
    """
    findings = []
    for check in CHECKS:
        check(manifest, registry, findings, req_keys=req_keys)

    waivers = {w["key"]: w for w in registry.get("waivers", [])}
    found = {f.key for f in findings}

    for key in sorted(waivers):
        waiver = waivers[key]
        clause = waiver.get("clause", "surface.vocabulary-registry")
        if key not in found:
            findings.append(Finding(
                clause, "waiver-baseline", f"obsolete-waiver:{key}",
                f"the {waiver['issue']} waiver for {key!r} matches no "
                f"finding -- the violation is gone and the waiver is all "
                f"that is left. The baseline is shrink-only: remove it"))
        if issue_status is not None:
            status = issue_status(waiver["issue"])
            if status in CLOSED_STATUSES:
                findings.append(Finding(
                    clause, "waiver-baseline", f"closed-waiver-issue:{key}",
                    f"{waiver['issue']} is {status}, so its waiver for "
                    f"{key!r} has no owner; either the fix landed (remove "
                    f"the waiver) or it did not (reopen the issue)"))
            elif status is None:
                findings.append(Finding(
                    clause, "waiver-baseline", f"closed-waiver-issue:{key}",
                    f"the waiver for {key!r} names {waiver['issue']}, which "
                    f"the store does not have"))

    unwaived, waived = [], []
    for finding in findings:
        waivable = not finding.key.startswith(UNWAIVABLE_PREFIXES)
        if waivable and finding.key in waivers:
            waived.append((finding, waivers[finding.key]))
        else:
            unwaived.append(finding)
    return unwaived, waived


def transitional_spellings(registry) -> list:
    """Registered spellings carried only until their retirement release.

    `surface.stability` permits a rename only as a registered synonym held
    for at least one release, so these are temporary by construction. The
    registry records the release that introduced each, which is what lets
    the lint report them as retirable instead of modelling them as a
    permanent second spelling (docs/plan.md, Release shape).
    """
    out = []
    for name, decl in sorted(registry["flags"].items()):
        transitional = decl.get("transitional")
        if transitional:
            out.append((name, decl["concept"], transitional))
        # A spelling can be permanent in its own concept and transitional
        # only where it collides into a second one: `--note` is a closing
        # note for good, and the dependency note only until v3.5.0.
        for override in decl.get("overrides", []):
            transitional = override.get("transitional")
            if transitional:
                record = dict(transitional)
                record.setdefault("issue", override["issue"])
                record["verbs"] = list(override["verbs"])
                out.append((name, override["concept"], record))
    return sorted(out)
