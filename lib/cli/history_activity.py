"""`issue history activity` -- windowed activity log grouped by issue.

History sub-namespace verb. Ported from the flat
`activity-issues` binary; the git data path is unchanged -- only the
arg-surface glue moved onto the dispatcher. Wraps
`git log --since=... --name-only` against the issue store and groups
every matched commit by the issue file it touched. Default window is the
last 7 days. Read-only: no locks acquired.

Store mode only; refuses (exit 1) in legacy mode and prints the exact
`git log` fallback per D9. Exit codes and TSV/`--json` shapes
are byte-identical to the flat binary.
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

VERB = "activity"
TIER = "history"

# The one place this verb names itself. Every diagnostic, usage line,
# and `prog=` keyword formats through it, so the surface manifest sees
# exactly one name per verb and `surface.self-naming` is checkable
# rather than a ~90-site grep (IM-239).
PROG = "issue history activity"
SUMMARY = "recent store commits grouped by issue (git log --name-only)"
ORDER = 40

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "enumeration-read"
SURFACE_STORE = "flag"
# IM-260 C1: every failure path carries the branchable payload (argparse
# refusals via `common.structured_parser_errors`, the limit value check,
# the legacy-mode refusal, the store gate, the prefix scope, the field
# selection, and the git failures). A zero-result exit 1 is a RESULT
# under the grep convention, not a failure. Ratchets: may never revert.
SURFACE_STRUCTURED_ERRORS = "converted"
# IM-250: the default `--since 7.days.ago` selects rows by wall clock, and
# the resolved window is echoed back as `since` -- declared for the same
# reason `index` declares `generated_at`.
SURFACE_GENERATION_FIELDS = ("since",)

# Top-level keys of each per-issue object under `issues`; the field-selection
# set for `--json <fields>`. Projection applies to the per-issue
# objects; the wrapper (since, store, prefixes, limit) is left intact.
VALID_JSON_FIELDS = ("id", "prefix", "file", "commit_count", "latest")

DEFAULT_SINCE = "7.days.ago"

_DESCRIPTION = """\
Summarize recent issue-store activity by grouping commits by the issue
file each one touched, via `git log --since=... --name-only`. The default
window is the last 7.days.ago; pass --since "" to scan all history. Only
files matching <PREFIX>/<ID>.md are counted (INDEX.md, .repos, and
non-issue commits are dropped). Read-only: no locks acquired. Ported from
the flat `activity-issues` binary.

Store mode (required) -- selected when ISSUE_STORE_ROOT is set or the
nearest .issues/.migrated-to-store sentinel exists.

Legacy mode -- this subcommand operates only on the issue store; it
refuses (exit 1) when no store is active and prints the exact fallback
`git log --since=... --name-only -- .issues/` to run in the originating
project repo. Pre-migration history is out of scope for this surface.

Default output is TSV (one issue per line, most-recent activity first);
--json emits a single object with an `issues` array (empty on no
activity). --json accepts an optional comma-separated field list (e.g.
--json id,commit_count) that projects each per-issue object to those
top-level fields; bare --json emits the full shape and an unknown field
name is a usage error (exit 2)."""

_EPILOG = """\
Exit codes: 0 at least one row emitted; 1 zero rows (grep convention,
uniform across every enumeration read); 2 argument/usage error, including
a --prefix the store does not have (error=unknown-prefix).

Examples:
  issue history activity
  issue history activity --since 2.weeks.ago --json
  issue history activity --prefix ABC --since 1.month.ago --limit 10
  issue history activity --since 2026-04-01 --prefix ABC,XYZ
  issue history activity --since ""
  issue history activity --json id,commit_count
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--since",
        default=None,
        metavar="DATE",
        help="Pass-through to `git log --since` (default 7.days.ago; "
        "\"\" scans all history).",
    )
    common.add_prefix_scope_flag(
        p,
        help="Comma-separated prefix dirs to scope the scan to (e.g. ABC,XYZ).",
    )
    p.add_argument(
        "--limit",
        default=None,
        metavar="N",
        help="Show at most N issues; 0 = unlimited (the default).",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
    p.set_defaults(_run=run)
    return p


def legacy_mode_error(since: str):
    """The structured legacy-mode refusal (history.legacy-refuse).

    The actionable git fallback stays in the message (flattened to one
    line so the key=value payload stays a single stderr line).
    """
    fallback_since = since if since else "7.days.ago"
    return errors.PreconditionError(
        "store mode not active "
        "(ISSUE_STORE_ROOT unset and no .issues/.migrated-to-store sentinel). "
        "This subcommand operates only on the issue store. "
        "For pre-migration activity, run inside the originating project "
        f"repo: git log --since={fallback_since} --name-only -- .issues/",
        reason="legacy-mode",
    )


def emit_tsv(activities, out):
    for a in activities:
        out.write(
            f"{a.issue_id}\t{a.latest.date}\t{a.commit_count}\t"
            f"{a.latest.author}\t{a.latest.sha}\t{a.latest.subject}\n"
        )


def emit_json(payload, out):
    json.dump(payload, out, indent=2, sort_keys=False)
    out.write("\n")


def run(args) -> int:
    # --since: default to 7.days.ago. An explicit empty string disables
    # the window (scan all history).
    if args.since is None:
        since = DEFAULT_SINCE
    elif args.since == "":
        since = None
    else:
        since = args.since

    json_mode = jsonfields.json_enabled(args.json_mode)

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
        common.emit_error(legacy_mode_error(since or ""),
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
        activities = history.git_activity(
            store_root,
            since=since,
            prefixes=prefixes,
            limit=limit,
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
            "since": since,
            "store": str(store_root),
            "prefixes": prefixes,
            "limit": limit,
            "issues": jsonfields.project_list(
                (a.to_dict() for a in activities), json_fields
            ),
        }
        emit_json(payload, sys.stdout)
    else:
        if not activities:
            window = since if since else "all history"
            print(
                f"{PROG}: no activity in window ({window}) in {store_root}",
                file=sys.stderr,
            )
        else:
            emit_tsv(activities, sys.stdout)

    # IM-238: grep convention, uniform across every enumeration read.
    return 0 if activities else 1
