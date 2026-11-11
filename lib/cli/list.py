"""`issue list` -- filter-only enumeration of issues (no text query).

New Tier-1 verb (design 5.2). `list` and `search` are two
verbs over one engine (`lib/search.py`): `issue search` takes a body text
query plus filters; `issue list` takes only the frontmatter filters
(--prefix/--status/--type) and never a text query. The split reflects
observed usage -- real `search-issues` calls almost always carry a query,
while filter-only demand routed through a hallucinated `issue-list`
(design section 10.1).

The engine, filters, output shapes (TSV + --json), and grep exit
convention (0 = at least one, 1 = zero) are identical to `issue search` run
with an empty query, so the two verbs stay byte-compatible with one code
path. `--prefix` is the sole scoping spelling (IM-237); `--repo` scoping
survives one release as a hidden synonym. (Design 2a named the filter
`--repo`; 5.2 says list takes "the same filters" as search, which
spells it `--prefix`) -- both map to the same prefix scoping.
"""

from __future__ import annotations

import argparse
import json
import sys

import jsonfields  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import search  # noqa: E402

from . import common

VERB = "list"
TIER = "tier1"
SUMMARY = "list issues by prefix/status/type (filters only, no text query)"
ORDER = 40

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "enumeration-read"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"

# Top-level keys of each per-issue --json object, derived from the row
# builder itself (search.IssueMatch.to_dict) rather than restated, so a
# field added there is selectable here by construction (IM-263).
VALID_JSON_FIELDS = search.IssueMatch.json_fields()

_DESCRIPTION = """\
Enumerate issues by frontmatter filters -- no body text query. Read-only:
no locks acquired. This is `issue search` with the text query fixed empty;
use `issue search QUERY` when you have body text to match.

Filters (--prefix, --status, --type) accept comma-separated lists
(OR within a flag, AND across flags). With no filters, every issue is
listed. Default output is TSV (id<TAB>status<TAB>priority<TAB>title) in scan
order; --json emits a top-level array of per-issue objects (body_matches is
always an empty list here). --json accepts an optional comma-separated field
list (e.g. --json id,title,status) that projects each object to those
top-level fields, in the order given; bare --json emits the full shape and
an unknown field name is a usage error (exit 2)."""

_EPILOG = """\
Exit codes: 0 at least one row emitted; 1 zero rows (grep convention,
uniform across every enumeration read); 2 argument/usage error, including
a --prefix the store does not have (error=unknown-prefix).

Examples:
  issue list
  issue list --status open,blocked
  issue list --prefix ABC --type bug
  issue list --prefix ABC,XYZ --status open --json
  issue list --prefix ABC --json id,title,status
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
        p, help="Limit to these prefixes (OR within the flag).",
    )
    common.add_value_set_flag(
        p, "--status", metavar="STATUS[,STATUS...]",
        help="Repeatable/comma-separated. Match only these frontmatter "
             "status values.",
    )
    p.add_argument(
        "--type", default=None, metavar="TYPE[,TYPE...]",
        help="Match only these frontmatter type values.",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def emit_tsv(matches, out):
    for m in matches:
        out.write(f"{m.id}\t{m.status}\t{m.priority}\t{m.title}\n")


def emit_json(matches, out, fields):
    objs = jsonfields.project_list((m.to_dict() for m in matches), fields)
    json.dump(objs, out, indent=2, sort_keys=False)
    out.write("\n")


def run(args) -> int:
    # Filter-only: the matcher is always the empty-query NoBodyFilter, so a
    # positional query is never accepted (argparse has none registered).
    matcher = search.build_matcher("substring", "")

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)

    def _note(before, after):
        print(f"issue list: resolved {before} -> {after}", file=sys.stderr)

    rc = common.require_store(
        store_root, prog="issue list",
        json_mode=jsonfields.json_enabled(args.json_mode),
        legacy_anchor=anchor,
    )
    if rc is not None:
        return rc

    raw_prefixes = search.parse_csv_set(args.prefix)
    rc = common.enforce_prefix_scope(
        raw_prefixes, store_root, anchor, prog="issue list",
        json_mode=jsonfields.json_enabled(args.json_mode),
    )
    if rc is not None:
        return rc

    ff = search.FrontmatterFilter(
        prefixes=common.canonicalize_prefix_set(
            raw_prefixes, store_root, anchor,
            emit_note=_note,
        ),
        statuses=search.parse_csv_set(args.status),
        types=search.parse_csv_set(args.type),
    )

    matches = search.search(store_root, anchor, matcher, ff)

    if jsonfields.json_enabled(args.json_mode):
        try:
            fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog="issue list"
            )
        except jsonfields.FieldSelectionError as e:
            print(e.message, file=sys.stderr)
            return 2
        emit_json(matches, sys.stdout, fields)
    else:
        emit_tsv(matches, sys.stdout)

    return 0 if matches else 1
