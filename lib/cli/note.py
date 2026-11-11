"""`issue note` -- append a date-stamped line to Implementation Notes.

Dispatcher port of the flat ``issue-note`` binary. Port, not
rewrite (design section 6): it reuses the ``notecore`` helpers
(``resolve_date``, ``read_text``) and delegates the mutation to the shared
``editcore.apply_edits`` lock+snapshot+atomic-write+commit path (commit
message ``note <id>``), exactly as the flat binary does. Only the argument
surface and the reindex call site (subprocess -> in-process
``reindex.reindex_after_mutation``) move. Exit codes and the ``--json`` shape
are byte-identical: 0 ok; 1 not-found / section-not-found; 2 usage / bad
--date / escaping-header; 3 concurrent-edit.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import concurrency  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import editcore as _EI  # noqa: E402  -- the relocated edit-issue data path
import errors  # noqa: E402
import idresolve  # noqa: E402
import notecore as _note  # noqa: E402  -- issue-note's date/text helpers
import reindex as _reindex  # noqa: E402

from . import common

# The one place this verb names itself; every diagnostic formats through
# it, so the surface manifest sees one name per verb and
# `surface.self-naming` is checkable (IM-239).
PROG = "issue note"

VERB = "note"
TIER = "tier1"
SUMMARY = "append a date-stamped line to an issue's Implementation Notes"
ORDER = 50

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"
# IM-250 / IM-262: both clock-derived and both persisted into the issue --
# `date` is the note's date and `note` is the `<date>: <text>` line appended
# to the body, so the date rides inside it too.
SURFACE_RECORD_FIELDS = ("date", "note")

_DESCRIPTION = """\
Append `<date>: <text>` to a level-2 section (default ## Implementation
Notes). A single positional `-` reads the note text from stdin. Convenience
wrapper over `issue edit --section NAME --append`; inherits its per-issue
lock, atomic write, and commit."""

_EPILOG = """\
Examples:
  issue note IM-072 "started work on the note verb"
  echo "drafted SKILL.md" | issue note IM-072 -
  issue note IM-072 "back-dated" --date 2026-05-29
  issue note IM-079 "design decision recorded" --section Design
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=__import__("argparse").RawDescriptionHelpFormatter,
    )
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("text", nargs="?", metavar="TEXT",
                   help="Note text, or '-' to read from stdin.")
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                   help="Override the date prefix (default: today).")
    p.add_argument("--section", default=_note.DEFAULT_SECTION, metavar="NAME",
                   help="Target level-2 section (default: Implementation Notes).")
    common.add_json_flag(p)
    p.add_argument("--no-reindex", action="store_true")
    common.add_lock_timeout_flag(p, default=concurrency.DEFAULT_LOCK_TIMEOUT)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def _resolve_paths(issue_id, cli_store, *, emit_note=None):
    """ResolveResult, honoring --store first.

    Store-root precedence stays here; canonicalization + path build go
    through ``idresolve``, so the lock path is built from the
    canonical stem (P2).
    """
    anchor = _EI.find_project_anchor()
    if cli_store:
        store_root = Path(cli_store)
    else:
        store_root = _EI.resolve_store_root(anchor)
    return idresolve.resolve_issue_paths(
        issue_id, store_root, anchor, emit_note=emit_note
    )


def run(args) -> int:
    if not args.issue_id:
        print(f"{PROG}: missing issue ID (use -h)", file=sys.stderr)
        return 2
    if args.text is None:
        print(
            f"{PROG}: missing note text (positional arg, or '-' for stdin)",
            file=sys.stderr,
        )
        return 2

    try:
        date = _note.resolve_date(args.date)
    except ValueError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 2

    text = _note.read_text(args.text)
    if not text.strip():
        print(f"{PROG}: note text is empty", file=sys.stderr)
        return 2

    payload = f"{date}: {text}"

    try:
        _EI._check_body_nests(payload, "note", f"section {args.section!r}")
    except ValueError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 2

    def _resolved_note(before, after):
        print(f"{PROG}: resolved {before} -> {after}", file=sys.stderr)

    try:
        _rr = _resolve_paths(args.issue_id, args.store, emit_note=_resolved_note)
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
        print(f"{PROG}: issue file not found: {issue_file}",
              file=sys.stderr)
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
        commit_message = f"note {canonical_id}"

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

    section_actions = [("edit", args.section, "append", payload)]

    try:
        summary = _EI.apply_edits(
            issue_file,
            lock_path,
            [],
            section_actions,
            args.lock_timeout,
            store_root=store_root if autocommit_enabled else None,
            commit_paths=commit_paths,
            commit_message=commit_message,
            reindex_fn=reindex_fn,
        )
    except concurrency.StoreCommitError as e:
        print(e.format_stderr(PROG), file=sys.stderr)
        return errors.EXIT_PRECONDITION
    except errors.ContractError as e:
        print(e.format_stderr(PROG), file=sys.stderr)
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
            {
                "file": str(issue_file),
                "id": canonical_id,
                "date": date,
                "section": args.section,
                "note": payload,
                "changes": summary,
            },
            sys.stdout, indent=2, sort_keys=False,
        )
        sys.stdout.write("\n")

    if not args.no_reindex and not in_mutation_reindex_ran[0]:
        _reindex.reindex_after_mutation(store_root, anchor,
                                        to_stderr=args.json_mode)

    return 0
