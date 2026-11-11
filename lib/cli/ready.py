"""`issue ready` -- frontmatter-native readiness filter.

Port of the flat `issue-ready` binary (the bd read path was
retired in v2.11.0). Computes the ready set directly from the markdown
store's frontmatter:

    ready = status `open`
            AND every direct `blocks`-type depends_on target is
                `done` or `cancelled`

Same-prefix and cross-prefix `blocks` edges are one uniform path. The
ready list is ordered deterministically (priority high>medium>low, then
created, then id) and never silently truncated (`--limit N` is a
caller-requested, stderr-announced cap on the ready list).

The verb answers "what can I claim". The different question "why can I
not claim X" is answered by the explanatory `filtered_out` section, which
rides under its own argv control (`--filtered all`, default `none`) --
IM-249. `--limit` bounds this verb's primary result rows only; it never
bounds explanatory output.

New in WP3 (design 2a "Reap amendment"): before computing the ready set
this verb reaps expired leases (best-effort, store-wide or `--prefix`
scoped) -- the same opportunistic reap the `/issue-start` skill ran, moved
into the flow where "what can I claim?" actually lives. The reap may make
one or more `reap <id>` store commits; the readiness computation itself
is otherwise read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import deps  # noqa: E402
import errors  # noqa: E402
import frontmatter  # noqa: E402
import jsonfields  # noqa: E402
import idresolve  # noqa: E402
import lease  # noqa: E402

from . import common
from . import _reap

VERB = "ready"
TIER = "tier1"
SUMMARY = "list ready-to-claim issues (auto-reaps expired leases first)"
ORDER = 70

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "enumeration-read"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"

# Top-level keys of each per-issue object in the `ready` list; the
# field-selection set for `--json <fields>`. Projection applies to
# the `ready` list; the `filtered_out` list is left intact.
#
# `filtered_out` carries no field selection of its own: it is explanatory
# output with a fixed two-key shape (id, reason), not a result set.
VALID_JSON_FIELDS = ("id", "title", "prefix")

# Aliased to the single shared grammar in lib/idresolve.py.
_ID_RE = idresolve.ID_RE
_PFX_RE = idresolve.PREFIX_RE

CLOSED_STATUSES = ("done", "cancelled")
BLOCKING_DEP_TYPE = "blocks"
READY_SOURCE_STATUS = "open"

_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

_DESCRIPTION = """\
Compute the full ready set from store frontmatter (never a truncated
page). An issue is ready when it is `open` and every direct `blocks`
dependency is `done`/`cancelled`. Expired leases are reaped first
(best-effort). Read-only otherwise: no locks on the read; results may be
slightly stale if a write commits during the read."""

_EPILOG = """\
Exit codes: 0 ready work exists; 1 empty ready set (error=no-ready-work)
or store unresolvable -- the grep convention every enumeration read holds;
2 argument/usage error, including a --prefix the store does not have
(error=unknown-prefix).

Examples:
  issue ready
  issue ready --json
  issue ready --prefix ABC
  issue ready --prefix ABC --actor claude-code-w5 --json id,title
  issue ready --prefix ABC --filtered all    # and why the rest are not
"""


class NoReadyWork(errors.ContractError):
    exit_code = errors.EXIT_PRECONDITION
    error_class = "no-ready-work"


# Message prefix for this verb. `issue-ready` is a retired flat name
# (v3.0.0 tombstone), so user-facing text must not hand it back to a
# caller as the form to retry -- surface.self-naming.
PROG = "issue ready"


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, description=_DESCRIPTION, epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common.add_prefix_scope_flag(
        p, multi=False,
        help="Restrict the ready set to one prefix (e.g. ABC, XYZ).",
    )
    # IM-241 / surface.explicit-output-mode: human-readable text is the
    # default on every verb and --json is the opt-in. `ready` defaulted to
    # JSON with a --text opt-out through v3.3.x -- the surface's only
    # inverted verb. `--text` stays accepted (and still mutually exclusive
    # with --json, so a contradictory pair is exit 2) but is hidden from
    # help: it now names the default, not an opt-out.
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--json", nargs="?", const=True, default=None,
        dest="json_mode", metavar="FIELDS",
        help="Emit a machine-readable JSON document on stdout "
        "(informational notices go to stderr; do not merge the streams). "
        "Optionally pass a comma-separated field list (e.g. --json "
        "id,title) to project each issue in the `ready` list to those "
        "top-level fields; bare --json emits the full shape. Unknown "
        "field name -> exit 2.",
    )
    mode.add_argument(
        "--text", action="store_true", dest="text_mode",
        help=argparse.SUPPRESS,
    )
    p.add_argument("--actor", default=None, metavar="ACTOR")
    p.add_argument(
        "--limit", default=0, type=int, metavar="N",
        help="Cap the ready list -- this verb's primary result rows -- at "
        "N; 0 = unlimited (the default). Suppressed rows are always "
        "announced on stderr. Never bounds the explanatory --filtered "
        "output.",
    )
    # IM-249 / surface.one-meaning-per-flag. `filtered_out` answers a
    # different question than the verb does -- "why can I not claim X",
    # not "what can I claim" -- so it is a secondary explanatory section
    # under its own argv control, not a second list riding in the default
    # payload. The surveyed idiom: a cap flag bounds the primary rows
    # (gh pr list --limit; rg --max-count) while a secondary section gets
    # its own switch (git status -u no|normal|all; rg --stats), and the
    # multi-section verb carries no cap at all (gh pr status).
    p.add_argument(
        "--filtered", choices=("none", "all"), default="none",
        help="Emit the explanatory `filtered_out` section: candidates that "
        "are not ready, each with the reason (blocked by an open "
        "dependency, or claimed by another actor). Default none -- the "
        "verb answers what is claimable. Under --json the `filtered_out` "
        "key is always present, and is [] unless --filtered all.",
    )
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


# --- store resolution (ported from the flat issue-ready binary) ------------


def resolve_store_root(cli_store):
    if cli_store:
        return Path(cli_store).expanduser()
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return Path(env).expanduser()
    sentinel = common.find_project_anchor() / ".migrated-to-store"
    if sentinel.is_file():
        return Path(
            sentinel.read_text(encoding="utf-8").strip()
        ).expanduser()
    return None


def id_prefix(issue_id):
    m = _ID_RE.match(str(issue_id))
    return m.group(1) if m else None


def sweep_store(store_root):
    records = {}
    for path in store_root.glob("*/*.md"):
        stem = path.stem
        if not _ID_RE.match(stem):
            continue
        try:
            fm_lines, _body = frontmatter.read_issue(path)
        except (OSError, ValueError):
            continue
        edges = deps.get_deps(fm_lines) or []
        blocks_targets = [
            e["to"] for e in edges
            if isinstance(e, dict)
            and e.get("type") == BLOCKING_DEP_TYPE and e.get("to")
        ]
        records[stem] = {
            "id": stem,
            "status": frontmatter.get_field(fm_lines, "status"),
            "priority": frontmatter.get_field(fm_lines, "priority"),
            "created": frontmatter.get_field(fm_lines, "created") or "",
            "title": frontmatter.get_field(fm_lines, "title") or "",
            "prefix": id_prefix(stem),
            "blocks_targets": blocks_targets,
            "fm_lines": fm_lines,
        }
    return records


def blocking_deps(record, records):
    blockers = []
    for dep_id in record["blocks_targets"]:
        dep_rec = records.get(dep_id)
        status = dep_rec["status"] if dep_rec else None
        if status not in CLOSED_STATUSES:
            blockers.append((dep_id, status))
    return blockers


def claim_blocker(record, actor):
    fm_lines = record["fm_lines"]
    if not lease.lease_is_active(fm_lines):
        return None
    claim = lease.get_claim(fm_lines) or {}
    assignee = claim.get("assignee")
    if assignee == actor:
        return None
    return assignee or "(unknown)"


def _sort_key(record):
    return (
        _PRIORITY_RANK.get(record["priority"], len(_PRIORITY_RANK)),
        record["created"],
        record["id"],
    )


def compute_ready(records, repo, actor):
    candidates = sorted(
        (rec for rec in records.values()
         if rec["status"] == READY_SOURCE_STATUS
         and (repo is None or rec["prefix"] == repo)),
        key=_sort_key,
    )
    ready = []
    filtered = []
    for rec in candidates:
        blockers = blocking_deps(rec, records)
        if blockers:
            reason = "; ".join(
                "blocked by %s (%s)" % (dep_id, status if status else "unknown")
                for dep_id, status in blockers
            )
            filtered.append({
                "id": rec["id"], "reason": reason,
                "_detail": ("blocked", blockers),
            })
            continue
        if actor is not None:
            holder = claim_blocker(rec, actor)
            if holder is not None:
                filtered.append({
                    "id": rec["id"],
                    "reason": "claim held by %s, not %s" % (holder, actor),
                    "_detail": ("claim", holder),
                })
                continue
        ready.append({
            "id": rec["id"], "title": rec["title"], "prefix": rec["prefix"],
        })
    return ready, filtered


def render_json(ready, filtered, fields, show_filtered):
    payload = {
        "ready": jsonfields.project_list(ready, fields),
        # The key is unconditional -- output shape is a function of argv,
        # never of store state (surface.explicit-output-mode) -- and its
        # contents are empty unless `--filtered all` asked for them.
        "filtered_out": [
            {"id": f["id"], "reason": f["reason"]} for f in filtered
        ] if show_filtered else [],
    }
    return json.dumps(payload, indent=2) + "\n"


def render_text(ready, filtered, show_filtered):
    lines = ["Ready:"]
    if ready:
        width = max(len(r["id"]) for r in ready)
        for r in ready:
            lines.append("  %-*s  %s" % (width, r["id"], r["title"]))
    else:
        lines.append("  (none)")
    if show_filtered and filtered:
        lines.append("")
        lines.append("Filtered out (not ready):")
        for f in filtered:
            kind, payload = f["_detail"]
            if kind == "blocked":
                detail = "blocked by " + ", ".join(
                    "%s (%s)" % (dep_id, status if status else "unknown")
                    for dep_id, status in payload
                )
            else:
                detail = "claim held by %s" % payload
            lines.append("  %s  %s" % (f["id"], detail))
    return "\n".join(lines) + "\n"


def emit_error(err, json_stderr):
    common.emit_error(err, prog=PROG, json_mode=json_stderr)


def run(args):
    prog = PROG
    # stdout is human-readable text unless --json was passed (IM-241);
    # stderr errors are JSON-formatted under the same condition, so a bare
    # invocation emits the key=value stderr line.
    json_stderr = jsonfields.json_enabled(args.json_mode)
    text_mode = not json_stderr

    if args.prefix is not None and not _PFX_RE.match(args.prefix.upper()):
        print("%s: invalid --prefix '%s' (expected e.g. ABC, XYZ)"
              % (prog, args.prefix), file=sys.stderr)
        return errors.EXIT_USAGE
    if args.limit < 0:
        print("%s: invalid --limit %d (expected 0 for unlimited, or a "
              "positive cap)" % (prog, args.limit), file=sys.stderr)
        return errors.EXIT_USAGE

    # Field selection is a usage error before any side effect (the reap).
    fields = None
    if json_stderr:
        try:
            fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog=prog
            )
        except jsonfields.FieldSelectionError as e:
            print(e.message, file=sys.stderr)
            return errors.EXIT_USAGE

    store_root = resolve_store_root(args.store)
    rc = common.require_store(store_root, prog=prog, json_mode=json_stderr)
    if rc is not None:
        return rc

    # Case-fold --prefix to the store's canonical prefix so `--prefix im`
    # scopes the IM prefix rather than returning nothing.
    repo = args.prefix
    if repo is not None:
        rc = common.enforce_prefix_scope(
            [repo], store_root, None, prog=prog, json_mode=json_stderr,
        )
        if rc is not None:
            return rc
        resolved = idresolve.resolve_prefix(
            repo, idresolve._known_prefixes(store_root, None)
        ) or repo.upper()
        if resolved != repo:
            print("%s: resolved %s -> %s" % (prog, repo, resolved),
                  file=sys.stderr)
            repo = resolved

    # Auto-reap expired leases before computing the ready set (design 2a).
    # Best-effort; never blocks the readiness read.
    _reap.auto_reap(store_root, repo=repo)

    records = sweep_store(store_root)
    ready, filtered = compute_ready(records, repo, args.actor)

    if args.limit > 0 and len(ready) > args.limit:
        suppressed = len(ready) - args.limit
        ready = ready[:args.limit]
        print("%s: %d more ready issue(s) not shown (--limit %d; drop "
              "--limit or pass --limit 0 to see all)"
              % (prog, suppressed, args.limit), file=sys.stderr)

    show_filtered = args.filtered == "all"
    if text_mode:
        sys.stdout.write(render_text(ready, filtered, show_filtered))
    else:
        sys.stdout.write(render_json(ready, filtered, fields, show_filtered))

    if not ready:
        # The empty ready set is the one case where the explanation is the
        # only useful output. Point at it on stderr, so stdout stays a
        # function of argv alone (surface.explicit-output-mode) while the
        # empty case does not become a dead end.
        if filtered and not show_filtered:
            print("%s: %d candidate issue(s) filtered out; pass --filtered "
                  "all to see why" % (prog, len(filtered)), file=sys.stderr)
        emit_error(NoReadyWork("no ready issues after filtering"), json_stderr)
        return errors.EXIT_PRECONDITION
    return errors.EXIT_SUCCESS
