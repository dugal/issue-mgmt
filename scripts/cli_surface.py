"""CLI surface manifest extractor (IM-245).

Dev-tree tooling, not shipped: `SCRIPTS` in the Makefile lists only
`scripts/issue`, so this module and its `scripts/cli-surface` entrypoint stay
out of the tarball alongside `scripts/gen-formula`.

What it produces
----------------
A deterministic JSON record of the `issue` CLI's argument surface -- every
verb, its declared class, its flag and positional registrations, the `prog`
strings it puts in user-facing text, and the three per-verb declarations the
Leg 3 decisions handed down (generation-metadata fields from IM-250, the
four-valued `store` field from IM-251, the `structured_errors` conversion
flag from IM-254). The committed copy at `docs/cli-surface.json` IS the
change detector: `tests/test_cli_surface.py` regenerates in-process and
byte-compares, so any surface change fails `make test` until the manifest is
deliberately regenerated (`make cli-surface`) together with a lint pass.

Why it reads actions and not call sites
---------------------------------------
`dep` and `edit` register `--lock-timeout` / `--no-reindex` inline instead of
through `common.add_*_flag`, and `admin reap` declares `--lock-timeout`
locally too. A walk over the `common.add_*` call sites therefore under-reports
those verbs (IM-244 spike finding; the same copy-then-diverge pattern as the
IM-236 index/ready split). This extractor walks the RESULTING argparse
actions, and attributes each one to its shared helper by instrumenting the
helpers during extraction -- so `source` is observed, never inferred.

The five passthrough verbs
--------------------------
Five admin verbs register a dispatcher subparser with `prefix_chars="\\x00"`
plus a single `nargs="*"` catch-all, so an argparse walk over the SUBPARSER
sees zero flags. IM-247 names three such parsers; there are five, and they
split into two different problems:

* `admin init-store`, `admin init-project`, `admin migrate` build a real
  argparse parser inside the module and parse the catch-all with it. They
  declare `SURFACE_PARSER` (a zero-arg builder) and extract faithfully from
  it.
* `admin selftest`, `admin install-skills` have no parser at all -- they walk
  argv by hand in `main()`. They declare `SURFACE_MANUAL_FLAGS`.

A passthrough verb declaring neither is a hard error. Emitting an empty flag
set for one would make the manifest false-CLEAN rather than visibly
incomplete, and that exact blindness has already produced a wrong issue:
IM-240 was filed from a source-level flag sweep and concluded five verbs lack
`--json`, listing `admin selftest` among the compliant ones. The true count is
six and `selftest` is a gap.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "docs" / "cli-surface.json"

# Bumped when the manifest's own shape changes (not when the surface does),
# so a regeneration diff reads as "the surface moved" rather than "the
# recorder moved" without the reviewer having to work out which.
SCHEMA = 2

# `surface.verb-class-contract`: every verb declares exactly one.
VERB_CLASSES = ("enumeration-read", "point-read", "mutating", "scaffolding")

# IM-251: `--store` is obligatory only where a verb resolves a store through
# the resolution precedence and offers no operand naming it. The four values
# make the distinction checkable instead of inferred from flag presence.
STORE_KINDS = ("flag", "positional", "pinned", "none")

# IM-254: the `key=value` payload obligation ratchets per verb rather than
# being waived surface-wide.
STRUCTURED_ERROR_STATES = ("converted", "unconverted")

TIERS = ("tier1", "history", "admin")

# argparse's private action classes -> the `action=` spelling a reader knows.
# An unmapped class keeps its own name (e.g. `OrderedFlagAction`), which is
# how `edit`'s ordered section-flag grouping shows up in the manifest.
_ACTION_NAMES = {
    "_StoreAction": "store",
    "_StoreConstAction": "store_const",
    "_StoreTrueAction": "store_true",
    "_StoreFalseAction": "store_false",
    "_AppendAction": "append",
    "_AppendConstAction": "append_const",
    "_CountAction": "count",
    "_HelpAction": "help",
    "_VersionAction": "version",
    "_ExtendAction": "extend",
    "_SubParsersAction": "subparsers",
}

# The shared flag helpers in lib/cli/common.py. Instrumented during
# extraction so each registration is attributed to the helper that made it.
_SHARED_HELPERS = (
    "add_store_flag",
    "add_version_flag",
    "add_json_flag",
    "add_json_field_flag",
    "add_prefix_scope_flag",
    "add_lock_timeout_flag",
    "add_commit_timeout_flag",
    "add_no_reindex_flag",
)


class SurfaceError(Exception):
    """A module cannot be represented; refuse rather than under-report."""


# --- value normalization ---------------------------------------------------


def _jsonable(value):
    """Coerce an argparse attribute to something json.dumps accepts."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return repr(value)


def _action_name(action) -> str:
    cls = type(action).__name__
    return _ACTION_NAMES.get(cls, cls.lstrip("_"))


def _type_name(action):
    t = getattr(action, "type", None)
    if t is None:
        return None
    return getattr(t, "__name__", None) or repr(t)


def _describe_common(action) -> dict:
    """The attributes shared by flag and positional entries."""
    default = getattr(action, "default", None)
    suppressed = default is argparse.SUPPRESS
    return {
        "action": _action_name(action),
        "dest": action.dest,
        "nargs": _jsonable(action.nargs),
        "const": _jsonable(getattr(action, "const", None)),
        "default": None if suppressed else _jsonable(default),
        "default_suppressed": suppressed,
        "choices": _jsonable(action.choices),
        "metavar": _jsonable(action.metavar),
        "type": _type_name(action),
    }


def _primary_name(option_strings):
    """The canonical spelling: the first long form, else the first form.

    Short forms are kept in `names` so `surface.long-flags-only` can see
    them -- `-h` is the sole registered short flag and the linter must be
    able to prove that from the manifest rather than from a source grep.
    """
    for name in option_strings:
        if name.startswith("--"):
            return name
    return option_strings[0]


def _describe_flag(action, *, source, mutex_group, synonym_of) -> dict:
    entry = _describe_common(action)
    entry.update({
        "name": _primary_name(list(action.option_strings)),
        "names": list(action.option_strings),
        "hidden": action.help is argparse.SUPPRESS,
        "source": source,
        "mutex_group": mutex_group,
        "synonym_of": synonym_of,
    })
    return entry


def _describe_positional(action) -> dict:
    entry = _describe_common(action)
    entry["name"] = action.dest
    return entry


# --- helper attribution ----------------------------------------------------


class _HelperProbe:
    """Record which option strings each shared `common.add_*` call added.

    Wrapping the helpers is what makes `source` an observation. The
    alternative -- grepping for `common.add_*(` -- is precisely the call-site
    walk that under-reports `dep`, `edit`, and `admin reap`.
    """

    def __init__(self, common_module):
        self._common = common_module
        self._originals = {}
        self.attribution = {}   # (id(parser), option_string) -> helper name

    def __enter__(self):
        for name in _SHARED_HELPERS:
            original = getattr(self._common, name)
            self._originals[name] = original
            setattr(self._common, name, self._wrap(name, original))
        return self

    def __exit__(self, *exc):
        for name, original in self._originals.items():
            setattr(self._common, name, original)
        return False

    def _wrap(self, name, original):
        label = f"common.{name}"

        def wrapper(parser, *args, **kwargs):
            before = len(parser._actions)
            result = original(parser, *args, **kwargs)
            for action in parser._actions[before:]:
                for option in action.option_strings:
                    self.attribution[(id(parser), option)] = label
            return result

        return wrapper


# --- prog strings in user-facing text --------------------------------------


def _prog_literals(module) -> list:
    """Every literal prog string the module puts in user-facing text.

    Collected by AST so the scan is deterministic and side-effect free:
    any ``PROG = "..."`` / ``prog = "..."`` assignment plus every
    ``prog="..."`` keyword (the `argparse.ArgumentParser(prog=...)` builders
    and the `common.emit_error(..., prog=...)` call sites). Non-literal
    `prog=prog` forwarding is skipped -- it carries no name of its own.

    The lowercase form is harvested because `transition.py:843` binds its
    message prefix as a local `prog = "transition-issue"` and then formats
    every error through it. Keying on the module-level constant alone made
    the manifest blind to that whole verb (IM-262).

    This is the data `surface.self-naming` needs: several of these are
    retired flat binary names (`init-issue-store`, `migrate-to-store`,
    `search-history`, ...) still being handed to callers as the form to
    retry.
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Name)
                        and target.id in ("PROG", "prog")
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    found.add(node.value.value)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "prog" and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    found.add(kw.value.value)
    return sorted(found)


def _argv_flag_literals(module, func_name="main") -> set:
    """Every flag name a hand-walked argv loop actually compares against.

    Ground truth for `SURFACE_MANUAL_FLAGS`. The two `prefix_chars=chr(0)`
    verbs register no argparse actions, so the declaration is the only thing
    the manifest can report -- and a declaration nothing derives drifts the
    moment someone adds a branch to the loop. That is instance 5 of the
    declaration-drift class (IM-245/IM-240) and it is why IM-262 exists.

    Collected by AST, deterministically and without import side effects:
    every string constant beginning with "-" that appears as a comparator in
    a `Compare` node inside the named function. That covers both shapes the
    loops use -- `arg == "--json"` and `arg in ("-h", "--help")`. A
    `startswith("-")` test is a `Call`, not a `Compare`, so prefix probes are
    correctly ignored rather than mistaken for flag names.
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                target = node
                break
    if target is None:
        raise SurfaceError(
            f"{module.__name__}: no {func_name}() to derive argv flags from; "
            f"SURFACE_MANUAL_FLAGS cannot be checked against the code"
        )
    found = set()
    for node in ast.walk(target):
        if not isinstance(node, ast.Compare):
            continue
        for operand in node.comparators:
            items = (operand.elts
                     if isinstance(operand, (ast.Tuple, ast.List, ast.Set))
                     else [operand])
            for item in items:
                if (isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                        and item.value.startswith("-")
                        and item.value != "-"):
                    found.add(item.value)
    return found


# A command-name-shaped token: the `issue ...` dispatcher form, or a single
# hyphenated word (which is what every retired flat binary looks like --
# `new-issue`, `migrate-to-store`, `issue-mgmt-selftest`). Deliberately
# shape-based rather than keyed to a retired-name list: a list would be one
# more hand-maintained declaration, and the point here is to record what the
# module prints and let the lint decide which names are current (IM-239).
_NAME_SHAPED = re.compile(
    r"^(?:issue(?: [a-z][a-z0-9-]*){0,2}|[a-z][a-z0-9]*(?:-[a-z0-9]+)+)$")

# `<name>: ` at the head of a user-facing message.
_MESSAGE_PREFIX = re.compile(r"^([a-z][a-z0-9.-]*(?: [a-z][a-z0-9-]*){0,2}): ")

# A hand-written synopsis line: the first token after a `usage:` / `Usage:` /
# `Synopsis:` heading, or on the heading line itself.
_USAGE_HEADING = re.compile(r"^\s*(?:usage|synopsis)\s*:\s*(\S*)", re.I)


def _string_constants(module) -> list:
    """Every string literal in the module, including f-string heads.

    An f-string's leading constant is where a message prefix lives
    (`f"issue-history: invalid ID {x}"`), so a walk over `ast.Constant`
    alone would miss most of them.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                out.append(head.value)
    return out


def _message_prefixes(module) -> list:
    """Command-name-shaped `<name>: ` message prefixes the module prints.

    Derived from the source, never declared. This is the ground truth
    `surface.self-naming` needs and the reason an empty `prog.declared` must
    not be read as clean: `admin selftest` and `admin install-skills` build
    their parsers with `prefix_chars=chr(0)` and emit no `prog=` keyword, so
    their declared list is empty while they print roughly thirty hand-written
    names between them (IM-261 audit, section 5.3).

    Shape-matched, so a handful of hyphenated error-condition keys
    (`concurrent-edit`) ride along. Separating a condition key from a command
    name is the lint's job, against the dispatcher form and the retired-name
    map; recording both here keeps that decision out of the extractor.
    """
    found = set()
    for text in _string_constants(module):
        match = _MESSAGE_PREFIX.match(text)
        if match and _NAME_SHAPED.match(match.group(1)):
            found.add(match.group(1))
    return sorted(found)


def _leading_command(text) -> str:
    """The command words at the head of a synopsis line.

    `Usage: issue create [--repo NAME] ...` yields `issue create`, not the
    bare `issue`: a single-token read reported a prefix of the real name,
    which then looked like a different (acceptable) thing to the self-naming
    lint and hid that the line was hand-written. Stops at the first operand,
    flag, or metavar.
    """
    words = []
    for token in text.strip().split():
        if not re.match(r"^[a-z][a-z0-9-]*$", token):
            break
        words.append(token)
    return " ".join(words)


def _usage_names(module) -> list:
    """Program names appearing in hand-written usage / synopsis text.

    argparse generates a correct usage line from `prog`; these are the ones
    it does not generate -- `admin migrate`'s `Synopsis:` block, the two
    passthrough verbs' `_USAGE` strings -- which are exactly the ones that
    drifted (IM-239).
    """
    found = set()
    for text in _string_constants(module):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            heading = _USAGE_HEADING.match(line)
            if not heading:
                continue
            candidates = []
            if heading.group(1):
                candidates.append(_leading_command(line[heading.end(1) - len(heading.group(1)):]))
            candidates += [_leading_command(l) for l in lines[i + 1:i + 6]
                           if l.strip()]
            for candidate in candidates:
                if candidate and _NAME_SHAPED.match(candidate):
                    found.add(candidate)
    return sorted(found)


# --- per-verb declarations -------------------------------------------------


def _require_declaration(module, attr, allowed):
    value = getattr(module, attr, None)
    if value is None:
        raise SurfaceError(
            f"{module.__name__}: missing required declaration {attr}. "
            f"Every verb module must declare it (one of: "
            f"{', '.join(allowed)}); see the verb-module protocol table in "
            f"lib/cli/__init__.py."
        )
    if value not in allowed:
        raise SurfaceError(
            f"{module.__name__}: {attr}={value!r} is not one of "
            f"{', '.join(allowed)}."
        )
    return value


def _mutex_groups(parser):
    """Mutually exclusive groups, as lists of primary flag names.

    Grouping is part of the surface: `ready`'s `--json` / `--text` pair is
    exit 2 when both are given, which a flat flag list cannot express.
    """
    groups = []
    for group in getattr(parser, "_mutually_exclusive_groups", []):
        names = sorted(
            _primary_name(list(a.option_strings))
            for a in group._group_actions if a.option_strings
        )
        if names:
            groups.append(names)
    return sorted(groups)


def _synonym_map(module) -> dict:
    """Locally declared retired spellings: {retired: canonical}.

    Helper-registered synonyms (`--repo` for `--prefix`, `--timeout` for
    `--commit-timeout`) are attributed automatically from the helper's own
    synonym list. Locally declared ones cannot be: `dep`'s `--type` shares
    a dest with `--kind`, but `edit`'s `--dep-type` has a DIFFERENT dest
    (`dep_type` vs `dep_kind`), so no same-dest heuristic finds it. Those
    modules declare `SURFACE_SYNONYMS` and the extractor validates both
    spellings exist.
    """
    declared = getattr(module, "SURFACE_SYNONYMS", {}) or {}
    out = {}
    for canonical, retired in declared.items():
        for name in retired:
            out[name] = canonical
    return out


def _helper_synonyms(parser, probe) -> dict:
    """Retired spellings the shared helpers register, keyed to canonical.

    Attributed from the registrations, not from a constant. The shared
    helpers register a deprecation-window synonym as its own action sharing
    the canonical flag's `dest`, defaulted to `argparse.SUPPRESS` (so an
    unused synonym leaves no attribute) and hidden from help (so it is never
    presented as a current form). That triple is the signature.

    Keying on the helper matters: `create --repo` is the repo BASENAME that
    keys `<store>/.repos` -- a disjoint value space from prefix scoping,
    registered locally -- so a check over the flag NAME alone would misread
    it as `--prefix`'s synonym and launder a real one-meaning-per-flag
    question into a synonym pair.
    """
    canonical = {}
    candidates = []
    for action in parser._actions:
        if not action.option_strings:
            continue
        name = _primary_name(list(action.option_strings))
        if probe.attribution.get((id(parser), name)) is None:
            continue
        if action.default is argparse.SUPPRESS and action.help is argparse.SUPPRESS:
            candidates.append((name, action.dest))
        else:
            canonical.setdefault(action.dest, name)
    return {name: canonical[dest] for name, dest in candidates
            if dest in canonical}


def _manual_flags(module) -> list:
    """Flag entries for a verb that walks argv by hand.

    The declaration is checked against the argv loop itself (IM-262), so it
    cannot go false-CLEAN in either direction: a flag added to the loop and
    not declared, or declared and removed from the loop, fails the build
    rather than the manifest silently misreporting the verb.
    """
    declared = getattr(module, "SURFACE_MANUAL_FLAGS", None)
    declared_names = {name for spec in declared for name in spec["names"]}
    argv_names = _argv_flag_literals(module)
    if declared_names != argv_names:
        undeclared = sorted(argv_names - declared_names)
        unreachable = sorted(declared_names - argv_names)
        detail = []
        if undeclared:
            detail.append(
                f"accepted by main() but not declared: {', '.join(undeclared)}"
            )
        if unreachable:
            detail.append(
                f"declared but not accepted by main(): {', '.join(unreachable)}"
            )
        raise SurfaceError(
            f"{module.__name__}: SURFACE_MANUAL_FLAGS has drifted from the "
            f"argv loop in main() -- " + "; ".join(detail)
            + ". Update the declaration to match the code, then regenerate "
            "the manifest."
        )
    entries = []
    for spec in declared:
        names = list(spec["names"])
        entries.append({
            "action": spec.get("action", "store"),
            "dest": spec.get("dest"),
            "nargs": spec.get("nargs"),
            "const": spec.get("const"),
            "default": spec.get("default"),
            "default_suppressed": False,
            "choices": spec.get("choices"),
            "metavar": spec.get("metavar"),
            "type": spec.get("type"),
            "name": _primary_name(names),
            "names": names,
            "hidden": bool(spec.get("hidden", False)),
            "source": "manual-declaration",
            "mutex_group": None,
            "synonym_of": spec.get("synonym_of"),
        })
    return entries


def _extract_parser(parser, *, module, probe) -> dict:
    """Flag / positional / grouping entries for one real argparse parser."""
    mutex_index = {}
    for i, names in enumerate(_mutex_groups(parser)):
        for name in names:
            mutex_index[name] = i

    synonyms = dict(_helper_synonyms(parser, probe))
    synonyms.update(_synonym_map(module))

    flags, positionals = [], []
    for action in parser._actions:
        if _action_name(action) == "subparsers":
            continue
        if not action.option_strings:
            positionals.append(_describe_positional(action))
            continue
        primary = _primary_name(list(action.option_strings))
        flags.append(_describe_flag(
            action,
            source=probe.attribution.get((id(parser), primary), "local"),
            mutex_group=mutex_index.get(primary),
            synonym_of=synonyms.get(primary),
        ))

    registered = {f["name"] for f in flags}
    declared = set(_synonym_map(module))
    for retired, canonical in sorted(synonyms.items()):
        if retired in registered and canonical not in registered:
            raise SurfaceError(
                f"{module.__name__}: SURFACE_SYNONYMS declares {retired} as a "
                f"synonym of {canonical}, which is not registered on this verb."
            )
        # The other direction (IM-262): a declaration naming a spelling the
        # verb does not accept. The check above fires only when `retired` IS
        # registered, so a synonym retired from argv but left declared slipped
        # through and the manifest kept advertising a flag that exits 2.
        if retired in declared and retired not in registered:
            raise SurfaceError(
                f"{module.__name__}: SURFACE_SYNONYMS declares {retired} as a "
                f"synonym of {canonical}, but this verb registers no such "
                f"flag -- the declaration is dead; remove it."
            )

    flags.sort(key=lambda f: f["name"])
    return {
        "flags": flags,
        "positionals": positionals,
        "mutually_exclusive_groups": _mutex_groups(parser),
    }


def _extract_verb(module, tier, parser, probe) -> dict:
    """One verb entry, or raise SurfaceError if it cannot be represented."""
    path_parts = ["issue"] + ([] if tier == "tier1" else [tier]) + [module.VERB]
    verb = {
        "path": " ".join(path_parts),
        "verb": module.VERB,
        "tier": tier,
        "module": module.__name__,
        "verb_class": _require_declaration(module, "SURFACE_CLASS",
                                           VERB_CLASSES),
        "store": _require_declaration(module, "SURFACE_STORE", STORE_KINDS),
        "structured_errors": _require_declaration(
            module, "SURFACE_STRUCTURED_ERRORS", STRUCTURED_ERROR_STATES),
        "generation_fields": sorted(
            getattr(module, "SURFACE_GENERATION_FIELDS", ()) or ()),
        "record_fields": sorted(
            getattr(module, "SURFACE_RECORD_FIELDS", ()) or ()),
        "prog": {
            "dispatcher": parser.prog,
            "declared": _prog_literals(module),
            "message_prefixes": _message_prefixes(module),
            "usage_names": _usage_names(module),
        },
    }

    passthrough = "-" not in parser.prefix_chars
    surface_parser = getattr(module, "SURFACE_PARSER", None)
    manual = getattr(module, "SURFACE_MANUAL_FLAGS", None)

    if not passthrough:
        if surface_parser is not None or manual is not None:
            raise SurfaceError(
                f"{module.__name__}: declares SURFACE_PARSER / "
                f"SURFACE_MANUAL_FLAGS but its dispatcher subparser is a "
                f"normal argparse parser; the declaration would shadow the "
                f"real registrations."
            )
        verb["parser"] = {
            "kind": "argparse",
            "add_help": bool(parser.add_help),
            "passthrough": False,
        }
        verb.update(_extract_parser(parser, module=module, probe=probe))
        return verb

    # Passthrough: the dispatcher subparser carries prefix_chars="\x00" and a
    # single nargs="*" catch-all, so its action list is NOT this verb's
    # surface. Refuse to guess.
    if surface_parser is not None:
        real = surface_parser()
        verb["parser"] = {
            "kind": "argparse-internal",
            "add_help": bool(real.add_help),
            "passthrough": True,
            "internal_prog": real.prog,
        }
        verb.update(_extract_parser(real, module=module, probe=probe))
        return verb

    if manual is not None:
        flags = _manual_flags(module)
        flags.sort(key=lambda f: f["name"])
        verb["parser"] = {
            "kind": "manual",
            "add_help": False,
            "passthrough": True,
        }
        verb["flags"] = flags
        verb["positionals"] = [
            _jsonable(p) for p in
            (getattr(module, "SURFACE_MANUAL_POSITIONALS", ()) or ())
        ]
        verb["mutually_exclusive_groups"] = []
        return verb

    raise SurfaceError(
        f"{module.__name__}: dispatcher subparser uses prefix_chars="
        f"{parser.prefix_chars!r}, so an argparse walk sees none of this "
        f"verb's flags. Declare SURFACE_PARSER (a zero-arg builder returning "
        f"the real parser) or SURFACE_MANUAL_FLAGS (for a hand-walked argv). "
        f"Emitting an empty flag set would read as false-CLEAN rather than "
        f"visibly incomplete -- see IM-245."
    )


# --- manifest assembly -----------------------------------------------------


def build_manifest() -> dict:
    """Extract the whole surface. Raises SurfaceError on an unrepresentable verb."""
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    import cli                      # noqa: E402
    from cli import common          # noqa: E402

    groups = cli.discover()
    verbs = []
    with _HelperProbe(common) as probe:
        top = argparse.ArgumentParser(prog="issue", add_help=False)
        sub = top.add_subparsers(dest="_verb", metavar="<verb>")
        routers = {"tier1": sub}
        for name in ("history", "admin"):
            router = sub.add_parser(name, add_help=True)
            routers[name] = router.add_subparsers(dest="_subverb",
                                                  metavar="<subverb>")
        for tier in TIERS:
            for module in groups[tier]:
                parser = module.register(routers[tier])
                verbs.append(_extract_verb(module, tier, parser, probe))

    verbs.sort(key=lambda v: v["path"])

    names = set()
    registrations = 0
    for v in verbs:
        for flag in v["flags"]:
            names.update(flag["names"])
            registrations += 1

    return {
        "schema": SCHEMA,
        "generated_by": "scripts/cli-surface",
        "cli": {"prog": "issue", "tiers": list(TIERS)},
        "vocabulary": {
            "verb_classes": list(VERB_CLASSES),
            "store_kinds": list(STORE_KINDS),
            "structured_error_states": list(STRUCTURED_ERROR_STATES),
        },
        "totals": {
            "verbs": len(verbs),
            "flag_names": len(names),
            "flag_registrations": registrations,
        },
        "verbs": verbs,
    }


def render(manifest) -> str:
    """Deterministic serialization: sorted keys, fixed indent, trailing newline.

    No wall-clock stamp and no absolute path anywhere in the document -- the
    manifest is compared byte-for-byte by the diff test, so anything ambient
    would make it a flake generator. (Which is also the rule it records for
    the surface itself: `surface.explicit-output-mode`.)
    """
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def render_current() -> str:
    return render(build_manifest())


def read_committed() -> str:
    return MANIFEST_PATH.read_text(encoding="utf-8")
