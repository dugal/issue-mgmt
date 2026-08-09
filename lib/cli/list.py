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
path. `--repo` is accepted as an alias of `--prefix` (design 2a names the
filter `--repo`; 5.2 says list takes "the same filters" as search, which
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

# Top-level keys of each per-issue --json object (search.IssueMatch.to_dict);
# the field-selection set for `--json <fields>`.
VALID_JSON_FIELDS = (
    "id", "file", "title", "status", "priority", "type", "body_matches",
)

_DESCRIPTION = """\
Enumerate issues by frontmatter filters -- no body text query. Read-only:
no locks acquired. This is `issue search` with the text query fixed empty;
use `issue search QUERY` when you have body text to match.

Filters (--prefix/--repo, --status, --type) accept comma-separated lists
(OR within a flag, AND across flags). With no filters, every issue is
listed. Default output is TSV (id<TAB>status<TAB>priority<TAB>title) in scan
order; --json emits a top-level array of per-issue objects (body_matches is
always an empty list here). --json accepts an optional comma-separated field
list (e.g. --json id,title,status) that projects each object to those
top-level fields, in the order given; bare --json emits the full shape and
an unknown field name is a usage error (exit 2)."""

_EPILOG = """\
Examples:
  issue list
  issue list --status open,blocked
  issue list --repo ABC --type bug
  issue list --prefix ABC,XYZ --status open --json
  issue list --repo ABC --json id,title,status
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
        "--prefix", "--repo", default=None, metavar="PFX[,PFX...]",
        dest="prefix",
        help="Limit to these prefixes (OR within the flag). Alias: --repo.",
    )
    p.add_argument(
        "--status", default=None, metavar="STATUS[,STATUS...]",
        help="Match only these frontmatter status values.",
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

    ff = search.FrontmatterFilter(
        prefixes=common.canonicalize_prefix_set(
            search.parse_csv_set(args.prefix), store_root, anchor,
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
