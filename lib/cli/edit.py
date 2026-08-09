"""`issue edit` -- mutate an issue's frontmatter fields and sections.

Dispatcher port of the flat ``edit-issue`` binary. This is a
port, not a rewrite (design ``docs/design/cli-consolidation.md`` section 6):
the whole data path -- argv interpretation (``interpret_section_actions``),
the lock+snapshot+atomic-write+commit lifecycle (``apply_edits``),
the ``--if-unchanged-since`` guard, the ``--claim-generation``
fencing, the dep-edge engine, the authoring cycle guard, and
the escaping-header refusal -- is reused verbatim from
``lib/editcore.py`` (relocated there from the flat ``scripts/edit-issue``
when that binary became a v3.0.0 tombstone stub). Only two
things move: the argument surface onto the
dispatcher's subparser, and the post-write reindex from a sibling-binary
subprocess to the in-process ``reindex.reindex_after_mutation`` seam. Exit
codes (0/1/2 + the 3-7 loser-side contract) and the ``--json`` shape are
byte-identical to the flat binary.

Help trim (design 5.6): the argparse ``-h`` surface leads with the
observed-dominant paths (``--set``, ``--section`` + body ops, dep ops); the
five zero-use flags (``--prepend``, ``--prepend-file``,
``--if-unchanged-since``, ``--rename-section``, ``--remove-section``) are
retained and fully functional but hidden from the front-page help (listed in
the epilog). No flag is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import concurrency  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import editcore as _ei  # noqa: E402  -- the relocated edit-issue data path
import errors  # noqa: E402
import idresolve  # noqa: E402
import reindex as _reindex  # noqa: E402

from . import common

VERB = "edit"
TIER = "tier1"
SUMMARY = "edit an issue's frontmatter fields or level-2 sections"
ORDER = 40

_DESCRIPTION = """\
Edit an issue non-interactively. Dominant paths:

  issue edit ID --set FIELD=VALUE [--set ...]
  issue edit ID --section NAME (--append-file PATH | --replace-file PATH
                               | --append TEXT | --replace TEXT)
  issue edit ID --add-dep KIND:TARGET | --remove-dep TARGET

--set changes frontmatter (guarded: status/id/created refused; updated is
auto-stamped). Section body ops and dep ops compose in argv order under one
lock + snapshot + atomic write + commit. A body whose markdown headers would
escape the target section is refused (exit 2) before any lock."""

_EPILOG = """\
Structural section ops:
  --add-section NAME [--body TEXT] [--at-end|--after NAME|--before NAME
                                  |--to-position N]
  --move-section NAME (--after NAME|--before NAME|--to-position N)

Extended flags (retained, see the design doc / edit-issue reference):
  --prepend TEXT   --prepend-file PATH   --rename-section OLD NEW
  --remove-section NAME   --if-unchanged-since SHA   --claim-generation N

Whole-body escape hatch:
  --replace-body-file PATH   replace the ENTIRE body below the frontmatter
  fence with the file at PATH ('-' reads stdin) through the locked mutation
  path. The body is opaque (## lines become real sections); frontmatter is
  untouched. Exclusive with section ops; composes with --set, dep ops, and
  --if-unchanged-since SHA (whole-body comparison).

Exit: 0 ok; 1 not-found / section-not-found / blocks-cycle; 2 usage /
guarded field / escaping header; 3-7 loser-side contract.

Examples:
  issue edit IM-023 --set priority=high
  issue edit IM-023 --section "Implementation Notes" --append "2026-08-01: x"
  issue edit RM-070 --section "Archived Plan Content" --append-file block.md
  issue edit IM-102 --add-dep parent-child:IM-101 --dep-note "spun out"
"""

# Flags hidden from the front-page help (design 5.6). Functional, not removed.
_TRIMMED = {
    "--prepend", "--prepend-file", "--if-unchanged-since",
    "--rename-section", "--remove-section",
}


def _ordered(p, *names, **kw):
    """Add an _OrderedFlagAction flag, suppressing help for trimmed flags."""
    if names[0] in _TRIMMED:
        kw["help"] = argparse.SUPPRESS
    p.add_argument(*names, action=_ei._OrderedFlagAction, **kw)


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("--set", action="append", default=[],
                   metavar="FIELD=VALUE", dest="sets",
                   help="Set a frontmatter field. Repeatable.")

    # Section anchors
    _ordered(p, "--section", metavar="NAME")
    _ordered(p, "--add-section", metavar="NAME", dest="add_section")
    _ordered(p, "--rename-section", nargs=2, metavar=("OLD", "NEW"),
             dest="rename_section")
    _ordered(p, "--remove-section", metavar="NAME", dest="remove_section")
    _ordered(p, "--move-section", metavar="NAME", dest="move_section")

    # Qualifiers
    _ordered(p, "--append", metavar="TEXT")
    _ordered(p, "--prepend", metavar="TEXT")
    _ordered(p, "--replace", metavar="TEXT")
    _ordered(p, "--append-file", metavar="PATH", dest="append_file")
    _ordered(p, "--prepend-file", metavar="PATH", dest="prepend_file")
    _ordered(p, "--replace-file", metavar="PATH", dest="replace_file")
    _ordered(p, "--body", metavar="TEXT")
    _ordered(p, "--at-end", nargs=0, dest="at_end")
    _ordered(p, "--after", metavar="NAME")
    _ordered(p, "--before", metavar="NAME")
    _ordered(p, "--to-position", type=int, metavar="N", dest="to_position")

    # Dep-edge ops
    _ordered(p, "--add-dep", metavar="KIND:TARGET", dest="add_dep")
    _ordered(p, "--remove-dep", metavar="TARGET", dest="remove_dep")
    _ordered(p, "--dep-type", metavar="KIND", dest="dep_type")
    _ordered(p, "--dep-note", metavar="TEXT", dest="dep_note")

    p.add_argument("--replace-body-file", default=None, metavar="PATH",
                   dest="replace_body_file", help=argparse.SUPPRESS)
    p.add_argument("--if-unchanged-since", default=None, metavar="SHA",
                   dest="if_unchanged_since", help=argparse.SUPPRESS)
    p.add_argument("--claim-generation", type=int, default=None, metavar="N",
                   dest="claim_generation", help=argparse.SUPPRESS)
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

    Store-root precedence (``--store`` -> ``ISSUE_STORE_ROOT`` -> sentinel)
    stays here; id/prefix canonicalization + the path build go through
    ``idresolve``, so the returned ``lock_path`` is always built
    from the canonical stem (P2). Raises ValueError on a malformed id and
    ``errors.StoreIntegrityError`` (exit 1) on a D2 collision.
    """
    anchor = _ei.find_project_anchor()
    if cli_store:
        store_root = Path(cli_store)
    else:
        store_root = _ei.resolve_store_root(anchor)
    return idresolve.resolve_issue_paths(
        issue_id, store_root, anchor, emit_note=emit_note
    )


def run(args) -> int:
    if not args.issue_id:
        print("issue edit: missing issue ID (use -h)", file=sys.stderr)
        return 2

    try:
        sets = _ei.parse_set_args(args.sets)
        ordered_flags = getattr(args, "ordered_section_flags", None) or []
        section_actions, dep_actions = _ei.interpret_section_actions(
            ordered_flags
        )
    except _ei.OperandInputError as e:
        print(f"issue edit: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"issue edit: {e}", file=sys.stderr)
        return 2

    # Whole-body replace: exclusive with section ops (the two
    # target the same bytes with no defined ordering); composes with
    # --set and dep ops (frontmatter plane, disjoint by construction).
    # Refuse BEFORE reading the operand so a doomed invocation never
    # consumes stdin.
    replace_body = None
    if args.replace_body_file is not None:
        if section_actions:
            print(
                "issue edit: --replace-body-file is exclusive with "
                "section ops (--section/--add-section/--rename-section/"
                "--remove-section/--move-section); --set and dep ops "
                "compose",
                file=sys.stderr,
            )
            return 2
        try:
            replace_body = _ei._read_op_file(args.replace_body_file)
        except _ei.OperandInputError as e:
            print(f"issue edit: {e}", file=sys.stderr)
            return 1

    if (not sets and not section_actions and not dep_actions
            and replace_body is None):
        print(
            "issue edit: nothing to do (specify --set, a section op, "
            "a dep op, or --replace-body-file)",
            file=sys.stderr,
        )
        return 2

    if args.claim_generation is not None and args.claim_generation < 0:
        print(
            "issue edit: --claim-generation must be a non-negative integer",
            file=sys.stderr,
        )
        return 2

    if_unchanged_since_section = None
    if args.if_unchanged_since is not None and replace_body is None:
        if (
            len(section_actions) != 1
            or section_actions[0][0] != "edit"
            or section_actions[0][2] != "replace"
        ):
            print(
                "issue edit: --if-unchanged-since requires --section NAME "
                "--replace TEXT (and no other section ops in the same "
                "invocation) or --replace-body-file PATH",
                file=sys.stderr,
            )
            return 2
        if_unchanged_since_section = section_actions[0][1]

    def _note(before, after):
        print(f"issue edit: resolved {before} -> {after}", file=sys.stderr)

    try:
        _rr = _resolve_paths(args.issue_id, args.store, emit_note=_note)
    except ValueError as e:
        print(f"issue edit: {e}", file=sys.stderr)
        return 2
    except errors.StoreIntegrityError as e:
        print(e.format_stderr("issue edit", json_mode=args.json_mode),
              file=sys.stderr)
        return e.exit_code
    issue_file = _rr.issue_file
    lock_path = _rr.lock_path
    store_root = _rr.store_root
    anchor = _rr.anchor
    canonical_id = _rr.canonical_id

    if args.if_unchanged_since is not None and store_root is None:
        print(
            "issue edit: --if-unchanged-since requires store mode "
            "(no store repo to resolve SHA against)",
            file=sys.stderr,
        )
        return 2

    if not issue_file.exists():
        print(f"issue edit: issue file not found: {issue_file}",
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
        commit_message = f"edit {canonical_id}"

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
        summary = _ei.apply_edits(
            issue_file,
            lock_path,
            sets,
            section_actions,
            args.lock_timeout,
            store_root=store_root,
            commit_paths=commit_paths,
            commit_message=commit_message,
            reindex_fn=reindex_fn,
            if_unchanged_since=args.if_unchanged_since,
            if_unchanged_since_section=if_unchanged_since_section,
            caller_generation=args.claim_generation,
            dep_actions=dep_actions,
            replace_body=replace_body,
        )
    except concurrency.StoreCommitError as e:
        print(e.format_stderr("issue edit"), file=sys.stderr)
        return errors.EXIT_PRECONDITION
    except errors.ContractError as e:
        print(e.format_stderr("issue edit", json_mode=args.json_mode),
              file=sys.stderr)
        return e.exit_code
    except _ei.GuardedFieldError as e:
        print(f"issue edit: cannot set '{e.field}' -- {e.reason}",
              file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"issue edit: file disappeared between resolve and read: {e}",
              file=sys.stderr)
        return 1
    except ValueError as e:
        msg = str(e)
        if msg.startswith("--if-unchanged-since:"):
            print(f"issue edit: {msg}", file=sys.stderr)
            return 2
        print(f"issue edit: {msg}", file=sys.stderr)
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
