"""Shared helpers for `issue` verb modules.

Every verb module under ``lib/cli/`` imports from here for the standard
global flags, the version string, and store-root resolution so the flag
names and resolution precedence stay identical across the whole
consolidated surface. Keeping them in one module is what lets the global
contract in ``docs/design/cli-consolidation.md`` section 4.Q4 be honored
without a fan-out WP re-deriving it.

The heavy data-path modules (``lib/frontmatter.py``, ``lib/concurrency.py``,
...) are untouched; this module carries only argument-surface glue.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import idresolve  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import storeresolve  # noqa: E402

# Single source of the consolidated CLI version. `stamp-version` rewrites
# this line at release time (directory-wide scan; see docs/releases.md).
VERSION = "v3.6.2"

# <PREFIX>-NNN, prefix ^[A-Z][A-Z0-9]{1,7}$. Aliased to the single shared
# grammar in lib/idresolve.py -- do not re-derive it here.
ID_RE = idresolve.ID_RE


# --- Global flags (design 4.Q4) --------------------------------------------
#
# Defined once here and attached by each verb's register() so a verb owns
# exactly the globals that apply to it: read-only verbs take --store/--json;
# mutating verbs additionally take --lock-timeout/--no-reindex. A verb may
# still override a global default locally.


def add_store_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--store",
        default=None,
        metavar="ROOT",
        help="Store root override (precedence: --store -> ISSUE_STORE_ROOT "
        "-> .migrated-to-store sentinel -> legacy .issues/).",
    )


def add_version_flag(p: argparse.ArgumentParser) -> None:
    # Every verb accepts --version so a per-verb invocation
    # (`issue show --version`) prints the version and exits 0, matching the
    # flat binaries the consolidated surface replaces. Top-level
    # `issue --version` is handled by the entrypoint.
    p.add_argument("--version", action="version", version=VERSION)


def add_json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_mode",
        help="Emit a machine-readable JSON document on stdout "
        "(informational notices go to stderr; do not merge the streams).",
    )


def add_json_field_flag(p: argparse.ArgumentParser) -> None:
    # Read-verb variant of --json with gh-style field selection.
    # nargs="?"/const=True makes bare `--json` behave exactly as before
    # (json_mode=True -> full documented shape), while `--json id,title`
    # captures the comma list for lib/jsonfields to project. Absent -> None.
    p.add_argument(
        "--json",
        nargs="?",
        const=True,
        default=None,
        dest="json_mode",
        metavar="FIELDS",
        help="Emit a machine-readable JSON document on stdout (informational "
        "notices go to stderr; do not merge the streams). Optionally "
        "pass a comma-separated field list (e.g. --json id,title,status) to "
        "project each object to those top-level fields; bare --json emits the "
        "full documented shape. Unknown field name -> exit 2.",
    )


# Timeout vocabulary (IM-242): the flag name follows the lock it bounds, so
# a verb taking both locks carries both flags and an agent never has to ask
# which lock one budget covers. `--lock-timeout` bounds the per-record
# advisory lock (P2's per-issue write lock); `--commit-timeout` bounds the
# store-wide commit lock (the catchup-sweep serialization).


# Prefix scoping (IM-237): `--prefix` is the CLI's sole spelling for "scope
# this to prefix(es)". `--repo` is the sole spelling for the repo basename
# that keys `<store>/.repos` (create / admin init-project / admin migrate) --
# a disjoint value space, so the two names may not be traded. Read verbs
# spelled prefix scoping `--repo` through v3.4.x; IM-270 retired that
# spelling, so `--prefix` is the only one this helper registers.
#
# IM-236 converged the value space: every scoping verb takes
# `PFX[,PFX...]`, repeatable, through this one helper. `index` / `ready` /
# `admin reap` took a single `PFX` through v3.4.x while `list` / `search` /
# the history verbs took a list, so one flag name carried two value spaces
# across seven verbs -- the surface.one-meaning-per-flag violation IM-246
# measured and waived. The two scaffolding verbs that spell `--prefix` as a
# write operand (`admin init-project` / `admin migrate`) register it
# locally and stay single-valued: a project is registered under exactly one
# prefix (registry.basename-prefix-unique), which is the documented
# capability licensing that deviation in docs/cli-vocabulary.json.


def add_prefix_scope_flag(p: argparse.ArgumentParser, *, help=None) -> None:
    add_value_set_flag(
        p, "--prefix", dest="prefix", metavar="PFX[,PFX...]",
        help=help or "Repeatable/comma-separated. Limit to these prefixes "
                     "(OR within the flag).",
    )


def add_value_set_flag(p: argparse.ArgumentParser, flag: str, *,
                       metavar: str, help: str, dest=None) -> None:
    """Register a filter flag that accepts repeated AND comma-separated values.

    One spelling of the multi-value axis, so the same query shape works on
    every verb carrying the flag. `action="append"` makes the repeated form
    accumulate instead of last-wins; the comma form is flattened downstream
    by `search.parse_csv_set`, which takes either shape.

    Added for IM-269, where `--status` accumulated on `issue index` but was
    last-wins on `issue list` / `issue search` -- so the same two-status
    query silently returned one status on two of the three verbs, with
    exit 0. Registering through one helper is what keeps a fourth verb from
    reintroducing the divergence. IM-236 then brought `--prefix` (via
    `add_prefix_scope_flag`) and `--type` onto it, so every multi-value
    filter on the surface accepts both forms and none of them silently
    drops a repeated occurrence.
    """
    p.add_argument(
        flag, action="append", default=None,
        metavar=metavar, help=help,
        **({"dest": dest} if dest else {}),
    )


def add_lock_timeout_flag(p: argparse.ArgumentParser, *, default=None) -> None:
    # Mutating verbs only. Kept here so every mutating verb spells the flag
    # identically; the value is consumed by the shared concurrency path.
    p.add_argument(
        "--lock-timeout",
        type=float,
        default=default,
        metavar="SECONDS",
        help="Acquisition timeout in seconds for the per-record advisory "
        "lock (the per-issue write lock).",
    )


def add_commit_timeout_flag(p: argparse.ArgumentParser, *, default=None) -> None:
    # Verbs that take the store-wide commit lock directly. Kept here so every
    # such verb spells the flag identically. `admin snapshot` spelled it
    # `--timeout` through v3.4.x; IM-270 retired that spelling.
    p.add_argument(
        "--commit-timeout",
        type=float,
        default=default,
        dest="commit_timeout",
        metavar="SECONDS",
        help="Acquisition timeout in seconds for the store-wide commit lock.",
    )


def add_no_reindex_flag(p: argparse.ArgumentParser) -> None:
    # Mutating verbs only. Suppresses the post-mutation INDEX regeneration.
    p.add_argument(
        "--no-reindex",
        action="store_true",
        help="Skip the automatic INDEX.md regeneration after the mutation.",
    )


# --- Store-root resolution (parity with the flat binaries) -----------------


def find_project_anchor() -> Path:
    """Nearest ``.issues/`` walking up from cwd, else an env/cwd fallback.

    Byte-identical to the resolution the flat binaries used; carried here
    so every verb resolves the anchor the same way.
    """
    cwd = Path.cwd().resolve()
    for d in [cwd, *cwd.parents]:
        if (d / ".issues").is_dir():
            return d / ".issues"
    workspace = os.environ.get("ISSUE_MGMT_ROOT")
    if workspace:
        return Path(workspace) / ".issues"
    return cwd / ".issues"


def resolve_store_root(anchor: Path, cli_store):
    """Resolve the store root, or None for legacy mode.

    Precedence: --store -> ISSUE_STORE_ROOT -> .migrated-to-store sentinel
    -> None (legacy .issues/ mode). Thin alias over the one shared chain in
    ``storeresolve`` (IM-280).
    """
    return storeresolve.resolve_store_root(anchor, cli_store)


def emit_error(err, *, prog, json_mode=False):
    """Render a ContractError to stderr in the shared branchable shape.

    One emitter so every verb's failure payload -- the `key=value` line by
    default, the one-line JSON object under `--json` -- is produced by the
    same code rather than re-derived per verb.
    """
    print(err.format_stderr(prog, json_mode=json_mode), file=sys.stderr)


def store_precondition_error(store_root):
    """The shared PreconditionError for a store that cannot be used.

    One `reason=store-unresolvable` covers both shapes, because the
    caller's branch is the same either way -- the store is unusable and no
    read ran. The recovery differs, so it is named in `detail`, and the
    resolved-but-absent case additionally carries `store=` so a caller can
    report the path it was pointed at.
    """
    import errors  # noqa: E402  (lazy: keeps common.py import-cheap)

    if store_root is None:
        return errors.PreconditionError(
            "issue store not resolvable (use --store, set "
            "ISSUE_STORE_ROOT, or run inside a migrated project)",
            reason="store-unresolvable",
        )
    return errors.PreconditionError(
        "issue store root does not exist or is not a directory "
        "(create it with `issue admin init-store`)",
        reason="store-unresolvable",
        store=str(store_root),
    )


def require_store(store_root, *, prog, json_mode=False, legacy_anchor=None):
    """Refuse an unusable store; return an exit code, or None when OK.

    IM-253: an enumeration read exits 1 both for "matched nothing" (the
    grep convention `output.exit-codes` fixes) and for a precondition
    failure, so a store that never resolved MUST say so -- otherwise an
    unconfigured store is indistinguishable from an empty one, the exact
    confusion IM-238 ruled out one level up for an absent `--prefix`.
    Silence on both streams is the degenerate case of the non-branchable
    error `surface.structured-errors` forbids.

    `legacy_anchor` is passed by the verbs that genuinely support legacy
    `.issues/` mode (`list`, `search`): an existing anchor directory is a
    usable corpus, so store_root None is not a failure there. Verbs that
    are store-mode only pass no anchor. A nonexistent anchor is not a
    corpus -- `find_project_anchor` falls back to `cwd/.issues` whether or
    not it exists, which is what made the failure silent.
    """
    import errors  # noqa: E402  (lazy: keeps common.py import-cheap)

    if store_root is not None and Path(store_root).is_dir():
        return None
    if (store_root is None and legacy_anchor is not None
            and Path(legacy_anchor).is_dir()):
        return None
    emit_error(store_precondition_error(store_root),
               prog=prog, json_mode=json_mode)
    return errors.EXIT_PRECONDITION


def unknown_prefixes(prefixes, store_root, anchor):
    """Prefixes among `prefixes` that the store does not have.

    IM-238: scoping to a prefix that names nothing in the store is an
    argument error (exit 2), not a zero-result -- otherwise a typo is
    indistinguishable from a genuinely empty scope. Returns the sorted
    list of absent prefixes (empty when all resolve), plus the sorted
    known set for the diagnostic. Store mode only: in legacy mode there
    is no registry to validate against, so the caller skips the check.
    """
    known = idresolve._known_prefixes(store_root, anchor)
    absent = sorted(
        p for p in (prefixes or ())
        if idresolve.resolve_prefix(p, known) is None
    )
    return absent, sorted(known)


def prefix_scope_error(absent, known, *, prog):
    """The shared UnknownPrefixError for an absent `--prefix` scope."""
    import errors  # noqa: E402  (lazy: keeps common.py import-cheap)

    return errors.UnknownPrefixError(
        "no such prefix in this store",
        flag="--prefix",
        unknown=",".join(absent),
        known=",".join(known) or "(none)",
    )


def enforce_prefix_scope(prefixes, store_root, anchor, *, prog,
                         json_mode=False):
    """Refuse an absent `--prefix` scope; return exit 2, or None when OK.

    IM-238: one rule across every verb that takes `--prefix`. Legacy mode
    is skipped -- there is no prefix registry to validate a scope against.
    """
    import errors  # noqa: E402  (lazy: keeps common.py import-cheap)

    if store_root is None or not prefixes:
        return None
    absent, known = unknown_prefixes(prefixes, store_root, anchor)
    if not absent:
        return None
    err = prefix_scope_error(absent, known, prog=prog)
    print(err.format_stderr(prog, json_mode=json_mode), file=sys.stderr)
    return errors.EXIT_USAGE


def resolve_prefix_scope(raw, store_root, anchor, *, prog, json_mode=False):
    """Parse, validate, and canonicalize a `--prefix` scope. One rule.

    Returns `(prefixes, rc)`: `prefixes` is the canonicalized set, or None
    when the flag was absent (accept-all); `rc` is an exit code when the
    scope is unusable and None otherwise. Callers return `rc` unchanged.

    IM-236: every scoping verb goes through here. `index`, `ready`, and the
    two history verbs each carried their own copy of this sequence, and the
    copies diverged in two ways an agent could see -- a shape gate rejecting
    an ill-formed prefix with prose text on some verbs and nothing on
    others, and an unresolvable prefix falling back to `value.upper()`
    rather than passing through. The fallback was already unreachable in
    store mode (an unresolvable prefix exits 2 at `enforce_prefix_scope`
    first), and the shape gate is redundant with it: a token that cannot be
    a prefix is not a prefix this store has, so it surfaces through the one
    structured `unknown-prefix` payload that names the known set, rather
    than through a per-verb sentence.

    Order matters and is the `list` / `search` order: refuse an absent
    scope BEFORE canonicalizing, so the diagnostic names what the caller
    typed rather than a case-folded guess at it.
    """
    import errors as _errors  # noqa: E402  (lazy: keeps import cheap)
    import search as _search  # noqa: E402

    if raw is None:
        return None, None
    prefixes = _search.parse_csv_set(raw)
    if not prefixes:
        # Present but empty after parsing (`--prefix ""`, `--prefix ,`).
        # A usage error on every scoping verb, not accept-all. IM-238's rule
        # is that a scope naming nothing is a bad argument rather than a
        # zero-result, because a typo is otherwise indistinguishable from an
        # unscoped read -- and an unset variable expanding to "" is exactly
        # that typo. The two history verbs already refused it; `list` and
        # `search` widened to the whole store, which on `admin reap` would
        # have meant reaping every prefix instead of the one asked for.
        shown = raw if isinstance(raw, str) else ",".join(raw)
        emit_error(
            _errors.UsageError(
                "--prefix was given but names no prefix after parsing "
                f"{shown!r}; drop the flag to scope to the whole store",
                flag="--prefix",
                reason="empty-prefix-scope",
            ),
            prog=prog,
            json_mode=json_mode,
        )
        return None, _errors.EXIT_USAGE
    rc = enforce_prefix_scope(
        prefixes, store_root, anchor, prog=prog, json_mode=json_mode)
    if rc is not None:
        return None, rc

    def _note(before, after):
        print(f"{prog}: resolved {before} -> {after}", file=sys.stderr)

    return canonicalize_prefix_set(
        prefixes, store_root, anchor, emit_note=_note), None


def canonicalize_prefix_set(prefixes, store_root, anchor, *, emit_note=None):
    """Case-fold each scoping prefix to its known canonical form.

    ``--prefix`` on the read verbs scopes by prefix directory;
    previously ``--prefix im`` scoped to a non-existent ``im`` dir and
    returned nothing. This maps each raw prefix through
    ``idresolve.resolve_prefix`` against the store's real prefixes. An
    unknown or ambiguous prefix passes through unchanged (so scoping to a
    genuinely absent prefix keeps its existing empty-result behavior).
    ``prefixes`` is the parsed set (or None); returns the mapped set.
    """
    if not prefixes:
        return prefixes
    known = idresolve._known_prefixes(store_root, anchor)
    out = set()
    for p in prefixes:
        resolved = idresolve.resolve_prefix(p, known)
        if resolved is not None and resolved != p:
            if emit_note is not None:
                emit_note(p, resolved)
            out.add(resolved)
        else:
            out.add(p)
    return out
