"""`issue history search` -- pickaxe search over the store's git history.

History sub-namespace verb. Ported from the flat
`search-history` binary; the git data path is unchanged -- only the
arg-surface glue moved onto the dispatcher. Wraps `git log -S "<term>"`
(the literal pickaxe) against the issue store. Read-only: no locks.

Store mode only; refuses (exit 1) in legacy mode and prints the exact
`git log -S` fallback per D9. Exit codes and TSV/`--json`
shapes are byte-identical to the flat binary.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import errors  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import history  # noqa: E402
import jsonfields  # noqa: E402

from . import common

VERB = "search"
TIER = "history"

# The one place this verb names itself. Every diagnostic, usage line,
# and `prog=` keyword formats through it, so the surface manifest sees
# exactly one name per verb and `surface.self-naming` is checkable
# rather than a ~90-site grep (IM-239).
PROG = "issue history search"
SUMMARY = "pickaxe search over store history (git log -S)"
ORDER = 20

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "enumeration-read"
SURFACE_STORE = "flag"
# IM-260 C1: every failure path carries the branchable payload (argparse
# refusals via `common.structured_parser_errors`, the missing term, the
# legacy-mode refusal, the store gate, the prefix scope, the field
# selection, and the git failures). A zero-result exit 1 is a RESULT
# under the grep convention, not a failure. Ratchets: may never revert.
SURFACE_STRUCTURED_ERRORS = "converted"

# Top-level keys of each per-commit object under `commits`; the
# field-selection set for `--json <fields>`. Projection applies to
# the per-commit objects; the wrapper (term, store, prefixes, since) intact.
VALID_JSON_FIELDS = ("sha", "date", "author", "subject")

_DESCRIPTION = """\
Search the issue store's git history for a literal term with the pickaxe
(`git log -S "<term>"`), reporting every commit whose diff changes the
number of occurrences of the term. Optionally scope to one or more
<PREFIX>/ directories and/or a --since cutoff. Read-only: no locks
acquired. Ported from the flat `search-history` binary.

Store mode (required) -- selected when ISSUE_STORE_ROOT is set or the
nearest .issues/.migrated-to-store sentinel exists.

Legacy mode -- this subcommand operates only on the issue store; it
refuses (exit 1) when no store is active and prints the exact fallback
`git log -S "<term>" -- .issues/` to run in the originating project repo.
Pre-migration history is out of scope for this surface.

--since accepts both relative (2.weeks.ago) and absolute (2026-04-01)
forms. Default output is TSV; --json emits a single object with a
`commits` array (empty on zero matches). --json accepts an optional
comma-separated field list (e.g. --json sha,subject) that projects each
per-commit object to those top-level fields; bare --json emits the full
shape and an unknown field name is a usage error (exit 2)."""

_EPILOG = """\
Exit codes: 0 at least one row emitted; 1 zero rows (grep convention,
uniform across every enumeration read); 2 argument/usage error, including
a --prefix the store does not have (error=unknown-prefix).

Examples:
  issue history search "concurrent-edit"
  issue history search "lock" --prefix ABC,XYZ --json
  issue history search "ConcurrentEdit" --since 2.weeks.ago
  issue history search "ISSUE_STORE_ROOT" --since 2026-04-01
  issue history search "concurrent-edit" --json sha,subject
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("term", nargs="?", default=None, metavar="TERM")
    common.add_prefix_scope_flag(
        p,
        help="Comma-separated prefix dirs to scope the scan to (e.g. ABC,XYZ).",
    )
    p.add_argument(
        "--since",
        default=None,
        metavar="DATE",
        help="Pass-through to `git log --since` (relative or absolute).",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
    p.set_defaults(_run=run)
    return p


def legacy_mode_error(term: str):
    """The structured legacy-mode refusal (history.legacy-refuse).

    The actionable git fallback stays in the message (flattened to one
    line so the key=value payload stays a single stderr line).
    """
    return errors.PreconditionError(
        "store mode not active "
        "(ISSUE_STORE_ROOT unset and no .issues/.migrated-to-store sentinel). "
        "This subcommand operates only on the issue store. "
        "For pre-migration history, run inside the originating project "
        f'repo: git log -S "{term}" -- .issues/',
        reason="legacy-mode",
    )


def emit_tsv(commits, out):
    for c in commits:
        out.write(f"{c.sha}\t{c.date}\t{c.author}\t{c.subject}\n")


def emit_json(payload, out):
    json.dump(payload, out, indent=2, sort_keys=False)
    out.write("\n")


def run(args) -> int:
    json_mode = jsonfields.json_enabled(args.json_mode)

    if args.term is None:
        common.emit_error(
            errors.UsageError("missing TERM (use --help for usage)",
                              reason="missing-query"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_USAGE

    json_fields = None
    if json_mode:
        try:
            json_fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog=PROG
            )
        except jsonfields.FieldSelectionError as e:
            common.emit_error(
                errors.UsageError(e.message, flag="--json",
                                  reason="bad-json-field"),
                prog=PROG, json_mode=True)
            return errors.EXIT_USAGE

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)
    if store_root is None:
        common.emit_error(legacy_mode_error(args.term),
                          prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    store_root = store_root.expanduser().resolve()
    if not store_root.is_dir():
        common.emit_error(common.store_precondition_error(store_root),
                          prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    # Parse, refuse an absent scope (IM-238; exit 1 here through v3.3.x),
    # and case-fold -- one call to the shared resolver (IM-236). This verb
    # carried its own copy that canonicalized BEFORE enforcing, so the
    # diagnostic named a case-folded guess at the caller's argument rather
    # than the argument; the shared order names what was typed.
    scope, rc = common.resolve_prefix_scope(
        args.prefix, store_root, anchor, prog=PROG,
        json_mode=jsonfields.json_enabled(args.json_mode),
    )
    if rc is not None:
        return rc
    prefixes = sorted(scope) if scope else None

    try:
        commits = history.git_pickaxe(
            store_root,
            args.term,
            prefixes=prefixes,
            since=args.since,
        )
    except history.HistoryError as e:
        common.emit_error(
            errors.PreconditionError(str(e), reason="git-error"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip().splitlines()[-1:] or [""]
        common.emit_error(
            errors.PreconditionError(
                f"git log failed ({e.returncode}): {stderr_tail[0]}",
                reason="git-error"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    if jsonfields.json_enabled(args.json_mode):
        payload = {
            "term": args.term,
            "store": str(store_root),
            "prefixes": prefixes,
            "since": args.since,
            "commits": jsonfields.project_list(
                (c.to_dict() for c in commits), json_fields
            ),
        }
        emit_json(payload, sys.stdout)
    else:
        if not commits:
            print(
                f'{PROG}: no matches for "{args.term}" in {store_root}',
                file=sys.stderr,
            )
        else:
            emit_tsv(commits, sys.stdout)

    # IM-238: grep convention, uniform across every enumeration read.
    return 0 if commits else 1
