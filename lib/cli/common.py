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

# Single source of the consolidated CLI version. `stamp-version` rewrites
# this line at release time (directory-wide scan; see docs/releases.md).
VERSION = "v3.4.0"

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
# accepted `--repo` for prefix scoping through v3.3.x; it is carried here as
# a hidden synonym on EVERY prefix-scoping verb for one release (uniformly,
# so the deprecation window itself is not a per-verb exception) and retires
# in the next MAJOR.

PREFIX_SCOPE_SYNONYMS = ("--repo",)


def add_prefix_scope_flag(p: argparse.ArgumentParser, *, multi=True,
                          help=None,
                          synonyms=PREFIX_SCOPE_SYNONYMS) -> None:
    metavar = "PFX[,PFX...]" if multi else "PFX"
    p.add_argument(
        "--prefix",
        default=None,
        metavar=metavar,
        dest="prefix",
        help=help or (
            "Limit to these prefixes (OR within the flag)." if multi
            else "Restrict to one prefix (e.g. ABC, XYZ)."
        ),
    )
    for name in synonyms:
        p.add_argument(
            name,
            default=argparse.SUPPRESS,
            metavar=metavar,
            dest="prefix",
            help=argparse.SUPPRESS,
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
    reintroducing the divergence; `--type` and `--prefix` carry the same
    axis and should adopt this when their own issues land (IM-236).
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


def add_commit_timeout_flag(p: argparse.ArgumentParser, *, default=None,
                            synonyms=()) -> None:
    # Verbs that take the store-wide commit lock directly. `synonyms` carries
    # a retired spelling through its one-cycle deprecation window; each is
    # registered as a separate SUPPRESS'd action so it keeps working without
    # being presented in --help as a current form (surface.self-naming).
    p.add_argument(
        "--commit-timeout",
        type=float,
        default=default,
        dest="commit_timeout",
        metavar="SECONDS",
        help="Acquisition timeout in seconds for the store-wide commit lock.",
    )
    for name in synonyms:
        p.add_argument(
            name,
            type=float,
            default=argparse.SUPPRESS,
            dest="commit_timeout",
            metavar="SECONDS",
            help=argparse.SUPPRESS,
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
    -> None (legacy .issues/ mode).
    """
    if cli_store:
        return Path(cli_store)
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return Path(env)
    sentinel = anchor / ".migrated-to-store"
    if sentinel.is_file():
        return Path(sentinel.read_text(encoding="utf-8").strip())
    return None


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


def canonicalize_prefix_set(prefixes, store_root, anchor, *, emit_note=None):
    """Case-fold each scoping prefix to its known canonical form.

    ``--prefix``/``--repo`` on the read verbs scope by prefix directory;
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
