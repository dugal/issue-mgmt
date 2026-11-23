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

# The one place this verb names itself. Every diagnostic, usage line,
# and `prog=` keyword formats through it, so the surface manifest sees
# exactly one name per verb and `surface.self-naming` is checkable
# rather than a ~90-site grep (IM-239).
PROG = "issue history show"
SUMMARY = "git commit history for one issue file (git log --follow)"
ORDER = 10

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "point-read"
SURFACE_STORE = "flag"
# IM-260 C1: every failure path carries the branchable payload (argparse
# refusals via `common.structured_parser_errors`, the id/limit checks,
# the legacy-mode refusal, the store gate, the not-found conditions, and
# the git failures). Ratchets: may never revert.
SURFACE_STRUCTURED_ERRORS = "converted"

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
        help="Show at most N commits; 0 = unlimited (the default).",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
    p.set_defaults(_run=run)
    return p


def legacy_mode_error(issue_id: str):
    """The structured legacy-mode refusal (history.legacy-refuse).

    The actionable git fallback stays in the message (flattened to one
    line so the key=value payload stays a single stderr line).
    """
    return errors.PreconditionError(
        "store mode not active "
        "(ISSUE_STORE_ROOT unset and no .issues/.migrated-to-store sentinel). "
        "This subcommand operates only on the issue store. "
        "For pre-migration history, run inside the originating project "
        f"repo: git log --follow -- .issues/{issue_id}.md",
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

    if args.id is None:
        common.emit_error(
            errors.UsageError("missing ID (use --help for usage)",
                              reason="missing-id"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_USAGE

    if idresolve.split_id(args.id) is None:
        common.emit_error(
            errors.UsageError(
                f"invalid ID '{args.id}' "
                f"(expected <PREFIX>-NNN, e.g. ABC-123 or XYZ-9)",
                reason="invalid-id", id=str(args.id)),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_USAGE

    limit = None
    if args.limit is not None:
        try:
            limit = int(args.limit)
        except ValueError:
            limit = -1
        if limit < 0:
            common.emit_error(
                errors.UsageError(
                    f"--limit must be 0 (unlimited) or a positive "
                    f"integer, got '{args.limit}'",
                    flag="--limit", reason="bad-arguments"),
                prog=PROG, json_mode=json_mode)
            return errors.EXIT_USAGE
        if limit == 0:
            # IM-241: `--limit 0` means unlimited on every verb that takes
            # --limit; here that is the same as not passing the flag.
            limit = None

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
        common.emit_error(legacy_mode_error(args.id),
                          prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    store_root = store_root.expanduser().resolve()
    if not store_root.is_dir():
        common.emit_error(common.store_precondition_error(store_root),
                          prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    def _note(before, after):
        print(f"{PROG}: resolved {before} -> {after}", file=sys.stderr)

    try:
        _rr = idresolve.resolve_issue_paths(
            args.id, store_root, anchor, emit_note=_note
        )
    except errors.StoreIntegrityError as e:
        common.emit_error(e, prog=PROG, json_mode=json_mode)
        return e.exit_code
    canonical_id = _rr.canonical_id
    rel_path = history.rel_path_for(canonical_id)
    prefix_dir = store_root / rel_path.split("/", 1)[0]
    if not prefix_dir.is_dir():
        common.emit_error(
            errors.NotFoundError(
                f"prefix directory does not exist: {prefix_dir}",
                id=canonical_id, store=str(store_root)),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    issue_file = store_root / rel_path
    if not issue_file.is_file():
        common.emit_error(
            errors.NotFoundError(
                f"issue file not found: {issue_file}",
                id=canonical_id, file=rel_path, store=str(store_root)),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    try:
        commits = history.git_log(store_root, rel_path, limit=limit)
    except history.HistoryError as e:
        common.emit_error(
            errors.PreconditionError(str(e), reason="git-error",
                                     id=canonical_id),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip().splitlines()[-1:] or [""]
        common.emit_error(
            errors.PreconditionError(
                f"git log failed ({e.returncode}): {stderr_tail[0]}",
                reason="git-error", id=canonical_id),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

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
                f"{PROG}: no history for {args.id} in {store_root}",
                file=sys.stderr,
            )
        else:
            emit_tsv(commits, sys.stdout)

    return 0
