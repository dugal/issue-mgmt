"""`issue index` -- grouped-by-status board view of the store.

New Tier-1 verb. Renders the issue store grouped by status,
scoped by `--prefix`/`--status`, with a bounded default. Deferred from the
flat v2.x CLI to v3 per the design's section 11: under consolidation the grouped
board is an explicit scoped verb rather than the accidental zero-arg dump
(design Q9 -- bare `issue` prints help, not the whole 1000+-issue board).

Deviations from the earlier section-5 sketch, made deliberate by the v3
design:
  - The board is computed live from store frontmatter (reusing the
    read-only `lib/search` enumeration + `lib/frontmatter`), not by
    rendering `<store>/INDEX.md`. Live computation is always fresh, so the
    earlier `--refresh` flag and its dependency both fall away, and
    scoping/bounding become natural. No `lib/*.py` data path is modified.
  - Scoping flag is `--prefix` (IM-237; `--repo` scoping is a retired
    synonym), not the earlier
    `--prefix`; the --json prefix field is emitted as `repo`.
  - Default output is bounded: each status group is capped at --limit
    entries (default 20; 0 = unlimited), never silently -- a suppression
    line is printed under the group.

Store mode only (matches the history-tools D9 boundary): legacy mode exits
1 with an actionable stderr hint. Read-only: no locks acquired. Exit 0 on a
resolvable store (even an empty board); 1 on legacy/unresolvable store;
2 on a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

import errors  # noqa: E402
import frontmatter  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import jsonfields  # noqa: E402
import idresolve  # noqa: E402
import reindex_core  # noqa: E402  (STATUS_ORDER / STATUS_LABELS)
import search  # noqa: E402

from . import common

VERB = "index"
TIER = "tier1"
SUMMARY = "grouped-by-status board (scoped by --prefix/--status, bounded)"
ORDER = 50

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "enumeration-read"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"
# IM-250: `generated_at` is wall-clock-derived, declared so output SHAPE
# stays a function of argv while the carve-out cannot quietly widen.
SURFACE_GENERATION_FIELDS = ("generated_at",)

# Top-level keys of each per-issue object nested under groups[].issues; the
# field-selection set for `--json <fields>`. Field selection projects
# these per-issue objects and leaves the wrapper metadata (store, repo,
# generated_at) and the per-group metadata (status, count) intact.
VALID_JSON_FIELDS = ("id", "title", "priority", "file")

_PFX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")

# Same deterministic ordering within a group as issue-ready:
# priority (high > medium > low > other), then created, then id.
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

_DEFAULT_LIMIT = 20

_DESCRIPTION = """\
Render the issue store as a board grouped by status. Read-only: no locks
acquired; computed live from store frontmatter, so the view is always fresh.

Groups appear in workflow order (In Progress, Open, Blocked, Done,
Cancelled). --prefix restricts to one prefix; --status restricts to the named
statuses. Each group is capped at --limit entries (default 20; pass
--limit 0 for the full group); suppressed rows are announced, never hidden
silently.

Store mode only -- run inside a migrated project or set ISSUE_STORE_ROOT.

--json emits {store, prefix, repo, generated_at,
              groups[{status, count, issues}]}. `repo` duplicates `prefix`
              and is retained only for compatibility.
It accepts an optional comma-separated field list (e.g. --json id,title) that
projects each per-issue object under groups[].issues to those top-level
fields, in the order given, leaving the wrapper and per-group metadata
intact; bare --json emits the full shape and an unknown field name is a usage
error (exit 2)."""

_EPILOG = """\
Exit codes: 0 at least one row emitted; 1 zero rows (grep convention,
uniform across every enumeration read); 2 argument/usage error, including
a --prefix the store does not have (error=unknown-prefix).

Examples:
  issue index
  issue index --prefix ABC
  issue index --status open,in-progress
  issue index --prefix ABC --status open --limit 0
  issue index --json
  issue index --prefix ABC --json id,title
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common.add_prefix_scope_flag(
        p, multi=False,
        help="Restrict the board to one prefix (e.g. ABC, XYZ).",
    )
    p.add_argument(
        "--status", action="append", default=None,
        metavar="STATUS[,STATUS...]",
        help="Repeatable/comma-separated. Show only these status groups.",
    )
    p.add_argument(
        "--limit", type=int, default=_DEFAULT_LIMIT, metavar="N",
        help="Per-group cap (default %d; 0 = unlimited)." % _DEFAULT_LIMIT,
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def _sort_key(rec):
    return (
        _PRIORITY_RANK.get(rec["priority"], len(_PRIORITY_RANK)),
        rec["created"],
        rec["id"],
    )


def _parse_status_filter(values):
    """Flatten a repeatable + comma-separated --status into an ordered set.

    Returns None when unset (all groups). Unknown statuses raise ValueError.
    """
    if not values:
        return None
    wanted = []
    for chunk in values:
        for part in chunk.split(","):
            s = part.strip()
            if not s:
                continue
            if s not in reindex_core.STATUS_ORDER:
                raise ValueError(
                    "unknown --status '%s' (expected one of: %s)"
                    % (s, ", ".join(reindex_core.STATUS_ORDER))
                )
            if s not in wanted:
                wanted.append(s)
    return wanted or None


def _collect(store_root, repo):
    """Group issue records by status. Records carry id/title/priority/created/file."""
    groups = {s: [] for s in reindex_core.STATUS_ORDER}
    prefixes = {repo} if repo else None
    for path in search.enumerate_issue_files(store_root, None, prefixes):
        try:
            fm_lines, _body = frontmatter.read_issue(path)
        except (OSError, ValueError):
            continue
        issue_id = frontmatter.get_field(fm_lines, "id")
        if not issue_id:
            continue
        status = frontmatter.get_field(fm_lines, "status")
        if status not in groups:
            continue
        groups[status].append({
            "id": issue_id,
            "title": frontmatter.get_field(fm_lines, "title") or "",
            "priority": frontmatter.get_field(fm_lines, "priority") or "",
            "created": frontmatter.get_field(fm_lines, "created") or "",
            "file": str(path),
        })
    for s in groups:
        groups[s].sort(key=_sort_key)
    return groups


def _render_text(store_root, repo, generated_at, groups, statuses, limit):
    out = []
    scope = " (prefix %s)" % repo if repo else ""
    out.append("Issues board -- %s%s" % (store_root, scope))
    out.append("generated %s" % generated_at)
    for status in statuses:
        rows = groups.get(status, [])
        out.append("")
        out.append("## %s (%d)" % (reindex_core.STATUS_LABELS[status], len(rows)))
        if not rows:
            out.append("  (none)")
            continue
        shown = rows if limit == 0 else rows[:limit]
        width = max(len(r["id"]) for r in shown)
        for r in shown:
            out.append(
                "  %-*s  %-6s  %s" % (width, r["id"], r["priority"], r["title"])
            )
        suppressed = len(rows) - len(shown)
        if suppressed > 0:
            out.append(
                "  ... %d more (--limit 0 to show all)" % suppressed
            )
    return "\n".join(out) + "\n"


def _render_json(store_root, repo, generated_at, groups, statuses, limit, fields):
    group_list = []
    for status in statuses:
        rows = groups.get(status, [])
        shown = rows if limit == 0 else rows[:limit]
        issues = [
            {
                "id": r["id"],
                "title": r["title"],
                "priority": r["priority"],
                "file": r["file"],
            }
            for r in shown
        ]
        group_list.append({
            "status": status,
            "count": len(rows),
            "issues": jsonfields.project_list(issues, fields),
        })
    payload = {
        "store": str(store_root),
        # `prefix` is the canonical key (IM-237); `repo` is retained because
        # surface.stability forbids removing a shipped --json field. Both
        # carry the same value.
        "prefix": repo,
        "repo": repo,
        "generated_at": generated_at,
        "groups": group_list,
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def run(args) -> int:
    if args.prefix is not None and not _PFX_RE.match(args.prefix.upper()):
        print(
            "issue index: invalid --prefix '%s' (expected e.g. ABC, XYZ)"
            % args.prefix,
            file=sys.stderr,
        )
        return 2
    if args.limit < 0:
        print(
            "issue index: invalid --limit %d (expected 0 for unlimited, or a "
            "positive cap)" % args.limit,
            file=sys.stderr,
        )
        return 2
    try:
        wanted = _parse_status_filter(args.status)
    except ValueError as e:
        print("issue index: %s" % e, file=sys.stderr)
        return 2

    fields = None
    if jsonfields.json_enabled(args.json_mode):
        try:
            fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog="issue index"
            )
        except jsonfields.FieldSelectionError as e:
            print(e.message, file=sys.stderr)
            return 2

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)
    if store_root is not None:
        store_root = store_root.expanduser()
    rc = common.require_store(
        store_root, prog="issue index",
        json_mode=jsonfields.json_enabled(args.json_mode),
    )
    if rc is not None:
        return rc

    repo = args.prefix
    if repo is not None:
        rc = common.enforce_prefix_scope(
            [repo], store_root, anchor, prog="issue index",
            json_mode=jsonfields.json_enabled(args.json_mode),
        )
        if rc is not None:
            return rc
        resolved = idresolve.resolve_prefix(
            repo, idresolve._known_prefixes(store_root, anchor)
        ) or repo.upper()
        if resolved != repo:
            print(f"issue index: resolved {repo} -> {resolved}",
                  file=sys.stderr)
            repo = resolved

    statuses = wanted if wanted is not None else list(reindex_core.STATUS_ORDER)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    groups = _collect(store_root, repo)

    if jsonfields.json_enabled(args.json_mode):
        sys.stdout.write(
            _render_json(store_root, repo, generated_at, groups, statuses, args.limit, fields)
        )
    else:
        sys.stdout.write(
            _render_text(store_root, repo, generated_at, groups, statuses, args.limit)
        )

    # IM-238: one zero-result rule for every enumeration read -- 0 = at
    # least one row, 1 = zero rows (grep convention). The board still
    # renders (an empty board is meaningful output); only the exit code
    # tells a caller there was nothing in it.
    rows = sum(len(groups.get(st, ())) for st in statuses)
    return errors.EXIT_SUCCESS if rows else errors.EXIT_PRECONDITION
