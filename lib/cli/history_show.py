"""`issue history show` -- git log --follow for one issue file.

History sub-namespace verb. Ported from the flat
`issue-history` binary; the git data path is unchanged -- only the
arg-surface glue moved onto the dispatcher. Store mode only:
pre-migration history lives in the originating project repo and is out
of scope per D9, so this verb refuses in legacy mode and prints
the exact `git log --follow` fallback on stderr.

Exit codes, TSV/`--json` shapes, and the legacy-refusal message are
byte-identical to the flat binary so skills and the future MCP server
need no retest.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import errors  # noqa: E402
import history  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import jsonfields  # noqa: E402
import idresolve  # noqa: E402

from . import common

VERB = "show"
TIER = "history"
SUMMARY = "git commit history for one issue file (git log --follow)"
ORDER = 10

# Top-level keys of each per-commit object under `commits`; the
# field-selection set for `--json <fields>`. Projection applies to
# the per-commit objects; the wrapper (id, store, file) is left intact.
VALID_JSON_FIELDS = ("sha", "date", "author", "subject")

_DESCRIPTION = """\
Show the git history of a single issue file, scoped to
<store>/<PREFIX>/<id>.md. Wraps `git log --follow` against the issue
store. Read-only: no locks acquired. Ported from the flat `issue-history`
binary.

Store mode (required) -- selected when ISSUE_STORE_ROOT is set or the
nearest .issues/.migrated-to-store sentinel exists.

Legacy mode -- this subcommand operates only on the issue store; it
refuses (exit 1) when no store is active and prints the exact fallback
`git log --follow -- .issues/<id>.md` to run in the originating project
repo. Pre-migration history is out of scope for this surface.

Default output is TSV (<sha>\\t<date>\\t<author>\\t<subject>); --json emits
a single object with a `commits` array (empty on no history). --json accepts
an optional comma-separated field list (e.g. --json sha,subject) that
projects each per-commit object to those top-level fields; bare --json emits
the full shape and an unknown field name is a usage error (exit 2)."""

_EPILOG = """\
Examples:
  issue history show ABC-123
  issue history show ABC-123 --limit 5 --json
  issue history show ABC-123 --json sha,subject
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("id", nargs="?", default=None, metavar="ID")
    p.add_argument(
        "--limit",
        default=None,
        metavar="N",
        help="Show at most N commits (positive integer).",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def legacy_mode_error(issue_id: str) -> str:
    return (
        "issue-history: store mode not active "
        "(ISSUE_STORE_ROOT unset and no .issues/.migrated-to-store sentinel). "
        "This subcommand operates only on the issue store. "
        f"For pre-migration history, run the following inside the "
        f"originating project repo:\n"
        f"    git log --follow -- .issues/{issue_id}.md"
    )


def emit_tsv(commits, out):
    for c in commits:
        out.write(f"{c.sha}\t{c.date}\t{c.author}\t{c.subject}\n")


def emit_json(payload, out):
    json.dump(payload, out, indent=2, sort_keys=False)
    out.write("\n")


def run(args) -> int:
    if args.id is None:
        print(
            "issue-history: missing ID (use --help for usage)",
            file=sys.stderr,
        )
        return 2

    if idresolve.split_id(args.id) is None:
        print(
            f"issue-history: invalid ID '{args.id}' "
            f"(expected <PREFIX>-NNN, e.g. ABC-123 or XYZ-9)",
            file=sys.stderr,
        )
        return 2

    limit = None
    if args.limit is not None:
        try:
            limit = int(args.limit)
        except ValueError:
            print(
                f"issue-history: --limit must be a positive integer, "
                f"got '{args.limit}'",
                file=sys.stderr,
            )
            return 2
        if limit <= 0:
            print(
                f"issue-history: --limit must be a positive integer, "
                f"got {limit}",
                file=sys.stderr,
            )
            return 2

    json_fields = None
    if jsonfields.json_enabled(args.json_mode):
        try:
            json_fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog="issue-history"
            )
        except jsonfields.FieldSelectionError as e:
            print(e.message, file=sys.stderr)
            return 2

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)
    if store_root is None:
        print(legacy_mode_error(args.id), file=sys.stderr)
        return 1

    store_root = store_root.expanduser().resolve()
    if not store_root.is_dir():
        print(
            f"issue-history: store root does not exist: {store_root}",
            file=sys.stderr,
        )
        return 1

    def _note(before, after):
        print(f"issue-history: resolved {before} -> {after}", file=sys.stderr)

    try:
        _rr = idresolve.resolve_issue_paths(
            args.id, store_root, anchor, emit_note=_note
        )
    except errors.StoreIntegrityError as e:
        print(e.format_stderr("issue-history", json_mode=args.json_mode),
              file=sys.stderr)
        return e.exit_code
    canonical_id = _rr.canonical_id
    rel_path = history.rel_path_for(canonical_id)
    prefix_dir = store_root / rel_path.split("/", 1)[0]
    if not prefix_dir.is_dir():
        print(
            f"issue-history: prefix directory does not exist: {prefix_dir}",
            file=sys.stderr,
        )
        return 1

    issue_file = store_root / rel_path
    if not issue_file.is_file():
        print(
            f"issue-history: issue file not found: {issue_file}",
            file=sys.stderr,
        )
        return 1

    try:
        commits = history.git_log(store_root, rel_path, limit=limit)
    except history.HistoryError as e:
        print(f"issue-history: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip().splitlines()[-1:] or [""]
        print(
            f"issue-history: git log failed ({e.returncode}): {stderr_tail[0]}",
            file=sys.stderr,
        )
        return 1

    if jsonfields.json_enabled(args.json_mode):
        payload = {
            "id": canonical_id,
            "store": str(store_root),
            "file": rel_path,
            "commits": jsonfields.project_list(
                (c.to_dict() for c in commits), json_fields
            ),
        }
        emit_json(payload, sys.stdout)
    else:
        if not commits:
            print(
                f"issue-history: no history for {args.id} in {store_root}",
                file=sys.stderr,
            )
        else:
            emit_tsv(commits, sys.stdout)

    return 0
