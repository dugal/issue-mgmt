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

VERB = "note"
TIER = "tier1"
SUMMARY = "append a date-stamped line to an issue's Implementation Notes"
ORDER = 50

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
    p.add_argument("--lock-timeout", type=float,
                   default=concurrency.DEFAULT_LOCK_TIMEOUT, metavar="SECONDS")
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
        print("issue note: missing issue ID (use -h)", file=sys.stderr)
        return 2
    if args.text is None:
        print(
            "issue note: missing note text (positional arg, or '-' for stdin)",
            file=sys.stderr,
        )
        return 2

    try:
        date = _note.resolve_date(args.date)
    except ValueError as e:
        print(f"issue note: {e}", file=sys.stderr)
        return 2

    text = _note.read_text(args.text)
    if not text.strip():
        print("issue note: note text is empty", file=sys.stderr)
        return 2

    payload = f"{date}: {text}"

    try:
        _EI._check_body_nests(payload, "note", f"section {args.section!r}")
    except ValueError as e:
        print(f"issue note: {e}", file=sys.stderr)
        return 2

    def _resolved_note(before, after):
        print(f"issue note: resolved {before} -> {after}", file=sys.stderr)

    try:
        _rr = _resolve_paths(args.issue_id, args.store, emit_note=_resolved_note)
    except ValueError as e:
        print(f"issue note: {e}", file=sys.stderr)
        return 2
    except errors.StoreIntegrityError as e:
        print(e.format_stderr("issue note", json_mode=args.json_mode),
              file=sys.stderr)
        return e.exit_code
    issue_file = _rr.issue_file
    lock_path = _rr.lock_path
    store_root = _rr.store_root
    anchor = _rr.anchor
    canonical_id = _rr.canonical_id

    if not issue_file.exists():
        print(f"issue note: issue file not found: {issue_file}",
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
        print(e.format_stderr("issue note"), file=sys.stderr)
        return errors.EXIT_PRECONDITION
    except errors.ContractError as e:
        print(e.format_stderr("issue note"), file=sys.stderr)
        return e.exit_code
    except FileNotFoundError as e:
        print(f"issue note: file disappeared between resolve and read: {e}",
              file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"issue note: {e}", file=sys.stderr)
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
