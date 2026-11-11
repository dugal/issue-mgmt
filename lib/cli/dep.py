"""`issue dep` -- author and read an issue's `depends_on` edges.

Dispatcher port of the flat ``issue-dep`` binary. Port, not
rewrite (design section 6): add/remove delegate to the shared
``editcore.apply_edits`` dep-edge engine (commit message ``dep <id>``,
authoring-time blocks-cycle guard, authoritative-reduced-list retract,
cross-prefix write-now/resolve-later); ``list`` is ``depcore``'s
lock-free reader reused verbatim. Only the argument surface and the reindex
call site (subprocess -> in-process ``reindex.reindex_after_mutation``) move.
Exit codes (0/1/2 + 3-7 loser-side contract) and the ``--json`` shape are
byte-identical to the flat binary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import concurrency  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import depcore  # noqa: E402  -- issue-dep's lock-free list reader
import deps  # noqa: E402
import editcore as _EI  # noqa: E402  -- the relocated edit-issue data path
import errors  # noqa: E402
import idresolve  # noqa: E402
import reindex as _reindex  # noqa: E402

from . import common

# The one place this verb names itself; every diagnostic formats through
# it, so the surface manifest sees one name per verb and
# `surface.self-naming` is checkable (IM-239).
PROG = "issue dep"

VERB = "dep"
TIER = "tier1"
SUMMARY = "add, remove, or list an issue's depends_on edges"
ORDER = 45

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"
# IM-245: locally declared retired spellings (IM-241 synonym window). The
# shared `common.add_*` helpers register their own synonyms and the extractor
# attributes those automatically; these two are declared inline, so they are
# declared here too. No same-dest heuristic could find them reliably -- see
# `edit`, where the pair has two different dests.
SURFACE_SYNONYMS = {"--kind": ("--type",), "--dep-note": ("--note",)}

_DESCRIPTION = """\
Author and read `depends_on` dependency edges.

  issue dep add    ID KIND:TARGET [--dep-note TEXT]
  issue dep remove ID TARGET [--kind KIND]
  issue dep list   ID [--json]

KIND is one of blocks, parent-child, related, discovered-from. TARGET is a
PREFIX-NNN id (cross-prefix accepted, write-now/resolve-later). A
`blocks:TARGET` add that would close a cycle is refused (exit 1) before any
write. Removing the last edge writes `depends_on: []` (never deletes the
key). `list` acquires no lock."""

_EPILOG = """\
Examples:
  issue dep add ABC-123 blocks:XYZ-9
  issue dep add ABC-124 parent-child:ABC-100 --dep-note "spun out of parent"
  issue dep remove ABC-123 XYZ-9 --kind blocks
  issue dep list ABC-123 --json
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", nargs="?", metavar="COMMAND",
                   help="add | remove | list")
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("operand", nargs="?", metavar="OPERAND",
                   help="KIND:TARGET (add) or TARGET (remove).")
    # IM-241: the dependency kind and the dependency note are one concept
    # each, spelled the same way on `dep` and on `edit` -- `--kind` and
    # `--dep-note`. The retired `--type` / `--note` spellings are accepted
    # for one release and hidden from help (surface.self-naming); they
    # collided with `--type` = issue type (search/list/create) and
    # `--note` = closing note (transition/complete).
    p.add_argument("--dep-note", default=None, dest="note", metavar="TEXT",
                   help="Note recorded on the new edge.")
    p.add_argument("--note", default=argparse.SUPPRESS, dest="note",
                   metavar="TEXT", help=argparse.SUPPRESS)
    p.add_argument("--kind", default=None, dest="dep_type", metavar="KIND",
                   help="Restrict the removal to edges of this kind.")
    p.add_argument("--type", default=argparse.SUPPRESS, dest="dep_type",
                   metavar="KIND", help=argparse.SUPPRESS)
    common.add_json_flag(p)
    p.add_argument("--no-reindex", action="store_true")
    common.add_lock_timeout_flag(p, default=concurrency.DEFAULT_LOCK_TIMEOUT)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def _resolve_paths(issue_id, cli_store, *, emit_note=None):
    """ResolveResult, honoring --store first (id canonicalization)."""
    anchor = _EI.find_project_anchor()
    if cli_store:
        store_root = Path(cli_store)
    else:
        store_root = _EI.resolve_store_root(anchor)
    return idresolve.resolve_issue_paths(
        issue_id, store_root, anchor, emit_note=emit_note
    )


def _do_edit(args, dep_actions) -> int:
    def _note(before, after):
        print(f"{PROG}: resolved {before} -> {after}", file=sys.stderr)

    try:
        _rr = _resolve_paths(args.issue_id, args.store, emit_note=_note)
    except ValueError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 2
    except errors.StoreIntegrityError as e:
        print(e.format_stderr(PROG, json_mode=args.json_mode),
              file=sys.stderr)
        return e.exit_code
    issue_file = _rr.issue_file
    lock_path = _rr.lock_path
    store_root = _rr.store_root
    anchor = _rr.anchor
    canonical_id = _rr.canonical_id

    if not issue_file.exists():
        print(f"{PROG}: issue file not found: {issue_file}", file=sys.stderr)
        return 1

    autocommit_enabled = (
        store_root is not None
        and not os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT")
    )
    commit_paths = None
    commit_message = None
    if autocommit_enabled:
        prefix = _rr.prefix
        store_resolved = store_root.resolve()
        commit_paths = [
            issue_file.resolve().relative_to(store_resolved),
            Path(prefix) / "INDEX.md",
            Path("INDEX.md"),
        ]
        commit_message = f"dep {canonical_id}"

    reindex_fn = None
    in_mutation_reindex_ran = [False]
    if not args.no_reindex:
        def reindex_fn(*, suppress_commit=False):
            in_mutation_reindex_ran[0] = True
            _reindex.reindex_after_mutation(
                store_root, anchor,
                suppress_commit=suppress_commit,
                to_stderr=args.json_mode,
            )

    try:
        summary = _EI.apply_edits(
            issue_file,
            lock_path,
            [],
            [],
            args.lock_timeout,
            store_root=store_root if autocommit_enabled else None,
            commit_paths=commit_paths,
            commit_message=commit_message,
            reindex_fn=reindex_fn,
            dep_actions=dep_actions,
        )
    except concurrency.StoreCommitError as e:
        print(e.format_stderr(PROG), file=sys.stderr)
        return errors.EXIT_PRECONDITION
    except errors.ContractError as e:
        print(e.format_stderr(PROG, json_mode=args.json_mode),
              file=sys.stderr)
        return e.exit_code
    except FileNotFoundError as e:
        print(f"{PROG}: file disappeared between resolve and read: {e}",
              file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 1

    if args.json_mode:
        json.dump(
            {"file": str(issue_file), "id": canonical_id, "changes": summary},
            sys.stdout, indent=2, sort_keys=False,
        )
        sys.stdout.write("\n")

    if not args.no_reindex and not in_mutation_reindex_ran[0]:
        _reindex.reindex_after_mutation(store_root, anchor,
                                        to_stderr=args.json_mode)

    return 0


def _cmd_add(args) -> int:
    if not args.operand:
        print("issue dep add: missing KIND:TARGET (use -h)", file=sys.stderr)
        return 2
    kind, sep, target = args.operand.partition(":")
    if not sep or not kind or not target:
        print("issue dep add: expected KIND:TARGET (e.g. blocks:XYZ-9)",
              file=sys.stderr)
        return 2
    try:
        deps.validate_kind(kind)
        deps.validate_target(target)
    except ValueError as e:
        print(f"issue dep add: {e}", file=sys.stderr)
        return 2
    return _do_edit(args, [("add-dep", target, kind, args.note)])


def _cmd_remove(args) -> int:
    if not args.operand:
        print("issue dep remove: missing TARGET (use -h)", file=sys.stderr)
        return 2
    try:
        deps.validate_target(args.operand)
        if args.dep_type is not None:
            deps.validate_kind(args.dep_type)
    except ValueError as e:
        print(f"issue dep remove: {e}", file=sys.stderr)
        return 2
    return _do_edit(args, [("remove-dep", args.operand, args.dep_type)])


def run(args) -> int:
    if not args.command:
        print(f"{PROG}: missing subcommand (add|remove|list); use -h",
              file=sys.stderr)
        return 2
    if args.command not in ("add", "remove", "list"):
        print(f"{PROG}: unknown subcommand {args.command!r} "
              "(expected add, remove, or list)", file=sys.stderr)
        return 2
    if not args.issue_id:
        print(f"{PROG}: missing issue ID (use -h)", file=sys.stderr)
        return 2

    if args.command == "add":
        return _cmd_add(args)
    if args.command == "remove":
        return _cmd_remove(args)
    # list -- reuse the relocated lock-free reader verbatim.
    return depcore.cmd_list(args)
