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

import history  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import jsonfields  # noqa: E402
import idresolve  # noqa: E402

from . import common

VERB = "activity"
TIER = "history"
SUMMARY = "recent store commits grouped by issue (git log --name-only)"
ORDER = 40

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
    p.add_argument(
        "--prefix",
        default=None,
        metavar="PFX[,PFX...]",
        help="Comma-separated prefix dirs to scope the scan to (e.g. ABC,XYZ).",
    )
    p.add_argument(
        "--limit",
        default=None,
        metavar="N",
        help="Show at most N issues (positive integer).",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def legacy_mode_error(since: str) -> str:
    fallback_since = since if since else "7.days.ago"
    return (
        "activity-issues: store mode not active "
        "(ISSUE_STORE_ROOT unset and no .issues/.migrated-to-store sentinel). "
        "This subcommand operates only on the issue store. "
        f"For pre-migration activity, run the following inside the "
        f"originating project repo:\n"
        f"    git log --since={fallback_since} --name-only -- .issues/"
    )


def parse_prefix_list(raw: str):
    return [item.strip() for item in raw.split(",") if item.strip()]


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

    limit = None
    if args.limit is not None:
        try:
            limit = int(args.limit)
        except ValueError:
            print(
                f"activity-issues: --limit must be a positive integer, "
                f"got '{args.limit}'",
                file=sys.stderr,
            )
            return 2
        if limit <= 0:
            print(
                f"activity-issues: --limit must be a positive integer, "
                f"got {limit}",
                file=sys.stderr,
            )
            return 2

    prefixes = None
    if args.prefix is not None:
        prefixes = parse_prefix_list(args.prefix)
        if not prefixes:
            print(
                f"activity-issues: --prefix is empty after parsing '{args.prefix}'",
                file=sys.stderr,
            )
            return 2
        for pfx in prefixes:
            try:
                history.validate_prefix(pfx.upper())
            except history.HistoryError as e:
                print(f"activity-issues: {e}", file=sys.stderr)
                return 2

    json_fields = None
    if jsonfields.json_enabled(args.json_mode):
        try:
            json_fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog="activity-issues"
            )
        except jsonfields.FieldSelectionError as e:
            print(e.message, file=sys.stderr)
            return 2

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)
    if store_root is None:
        print(legacy_mode_error(since or ""), file=sys.stderr)
        return 1

    store_root = store_root.expanduser().resolve()
    if not store_root.is_dir():
        print(
            f"activity-issues: store root does not exist: {store_root}",
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
                print(f"activity-issues: resolved {pfx} -> {resolved}",
                      file=sys.stderr)
            canon.append(resolved)
        prefixes = canon
        for pfx in prefixes:
            if not (store_root / pfx).is_dir():
                print(
                    f"activity-issues: prefix directory does not exist: "
                    f"{store_root / pfx}",
                    file=sys.stderr,
                )
                return 1

    try:
        activities = history.git_activity(
            store_root,
            since=since,
            prefixes=prefixes,
            limit=limit,
        )
    except history.HistoryError as e:
        print(f"activity-issues: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip().splitlines()[-1:] or [""]
        print(
            f"activity-issues: git log failed ({e.returncode}): {stderr_tail[0]}",
            file=sys.stderr,
        )
        return 1

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
                f"activity-issues: no activity in window ({window}) in {store_root}",
                file=sys.stderr,
            )
        else:
            emit_tsv(activities, sys.stdout)

    return 0
