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

import history  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import jsonfields  # noqa: E402
import idresolve  # noqa: E402

from . import common

VERB = "search"
TIER = "history"
SUMMARY = "pickaxe search over store history (git log -S)"
ORDER = 20

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
    p.add_argument(
        "--prefix",
        default=None,
        metavar="PFX[,PFX...]",
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
    p.set_defaults(_run=run)
    return p


def legacy_mode_error(term: str) -> str:
    return (
        "search-history: store mode not active "
        "(ISSUE_STORE_ROOT unset and no .issues/.migrated-to-store sentinel). "
        "This subcommand operates only on the issue store. "
        f"For pre-migration history, run the following inside the "
        f"originating project repo:\n"
        f'    git log -S "{term}" -- .issues/'
    )


def parse_prefix_list(raw: str):
    return [item.strip() for item in raw.split(",") if item.strip()]


def emit_tsv(commits, out):
    for c in commits:
        out.write(f"{c.sha}\t{c.date}\t{c.author}\t{c.subject}\n")


def emit_json(payload, out):
    json.dump(payload, out, indent=2, sort_keys=False)
    out.write("\n")


def run(args) -> int:
    if args.term is None:
        print(
            "search-history: missing TERM (use --help for usage)",
            file=sys.stderr,
        )
        return 2

    prefixes = None
    if args.prefix is not None:
        prefixes = parse_prefix_list(args.prefix)
        if not prefixes:
            print(
                f"search-history: --prefix is empty after parsing '{args.prefix}'",
                file=sys.stderr,
            )
            return 2
        for pfx in prefixes:
            try:
                history.validate_prefix(pfx.upper())
            except history.HistoryError as e:
                print(f"search-history: {e}", file=sys.stderr)
                return 2

    json_fields = None
    if jsonfields.json_enabled(args.json_mode):
        try:
            json_fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog="search-history"
            )
        except jsonfields.FieldSelectionError as e:
            print(e.message, file=sys.stderr)
            return 2

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)
    if store_root is None:
        print(legacy_mode_error(args.term), file=sys.stderr)
        return 1

    store_root = store_root.expanduser().resolve()
    if not store_root.is_dir():
        print(
            f"search-history: store root does not exist: {store_root}",
            file=sys.stderr,
        )
        return 1

    if prefixes:
        # Case-fold each scoping prefix to the store's canonical form.
        known = idresolve._known_prefixes(store_root, anchor)
        canon = []
        for pfx in prefixes:
            resolved = idresolve.resolve_prefix(pfx, known) or pfx.upper()
            if resolved != pfx:
                print(f"search-history: resolved {pfx} -> {resolved}",
                      file=sys.stderr)
            canon.append(resolved)
        prefixes = canon
        for pfx in prefixes:
            if not (store_root / pfx).is_dir():
                print(
                    f"search-history: prefix directory does not exist: "
                    f"{store_root / pfx}",
                    file=sys.stderr,
                )
                return 1

    try:
        commits = history.git_pickaxe(
            store_root,
            args.term,
            prefixes=prefixes,
            since=args.since,
        )
    except history.HistoryError as e:
        print(f"search-history: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip().splitlines()[-1:] or [""]
        print(
            f"search-history: git log failed ({e.returncode}): {stderr_tail[0]}",
            file=sys.stderr,
        )
        return 1

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
                f'search-history: no matches for "{args.term}" in {store_root}',
                file=sys.stderr,
            )
        else:
            emit_tsv(commits, sys.stdout)

    return 0
