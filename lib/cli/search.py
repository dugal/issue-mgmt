"""`issue search` -- read-only title/body + frontmatter search over the store.

Tier-1 verb. The free-text query matches the issue title and body text; an
issue is reported when either matches AND it satisfies every frontmatter
filter (--prefix/--status/--type). Matching the title (a frontmatter field)
is a text hit, not a filter -- it means `issue search dry-run` finds an issue
titled "add --dry-run flag" even with an empty body. Pass an empty query ("")
to filter by frontmatter only -- but `issue list` is the dedicated verb for
filter-only enumeration (design 5.2).

The TSV / --json output shapes and the grep exit convention (0 match /
1 zero matches / 2 usage) are byte-identical to the flat binary so skills
and the future MCP server need no retest.
"""

from __future__ import annotations

import argparse
import json
import sys

import jsonfields  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import search  # noqa: E402

from . import common

VERB = "search"
TIER = "tier1"
SUMMARY = "search issue titles and bodies (text query + filters)"
ORDER = 30

# Top-level keys of each per-issue --json object (search.IssueMatch.to_dict);
# the field-selection set for `--json <fields>`.
VALID_JSON_FIELDS = (
    "id", "file", "title", "status", "priority", "type", "title_match",
    "body_matches",
)

_DESCRIPTION = """\
Read-only search across the issue corpus. Ported from the flat search-issues
binary; no locks acquired.

The positional QUERY matches the title and body text (default:
case-insensitive literal substring, line-scoped; --syntax regex for a Python
regex), so an issue is found by what it is about even when its body is empty.
An empty query ("") disables text matching -- but prefer `issue list` for
filter-only enumeration. Filters (--prefix/--status/--type) accept
comma-separated lists (OR within a flag, AND across flags). Results are issues
whose title or body matches the query AND that satisfy every frontmatter
filter.

Default output is TSV (id<TAB>status<TAB>priority<TAB>title) in scan order;
--json emits a top-level array of per-issue objects (each carries a
`title_match` boolean and a `body_matches` list). --json accepts an optional
comma-separated field list (e.g. --json id,title,status) that projects each
object to those top-level fields, in the order given; bare --json emits the
full shape and an unknown field name is a usage error (exit 2)."""

_EPILOG = """\
Examples:
  issue search "concurrent edit"
  issue search "lock" --prefix ABC,XYZ
  issue search "" --status open,blocked --type bug
  issue search "\\bTODO\\b" --syntax regex --case-sensitive
  issue search "lucene" --json
  issue search "lock" --prefix IM --json id,title,status

Was (flat binary, removed in v3): search-issues "concurrent edit"
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query", nargs="?", default=None, metavar="QUERY")
    p.add_argument(
        "--syntax",
        choices=search.SUPPORTED_SYNTAXES,
        default="substring",
        help="Query syntax: substring (default) or regex.",
    )
    p.add_argument(
        "--case-sensitive",
        action="store_true",
        dest="case_sensitive",
        help="Disable the default case-insensitive match.",
    )
    p.add_argument(
        "--prefix", default=None, metavar="PFX[,PFX...]",
        help="Limit scan to these prefix directories (OR within the flag).",
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
    if args.query is None:
        print(
            'issue search: missing QUERY (pass "" for frontmatter-only '
            "search, or use `issue list`; -h for usage)",
            file=sys.stderr,
        )
        return 2

    try:
        matcher = search.build_matcher(
            args.syntax,
            args.query,
            case_sensitive=args.case_sensitive,
        )
    except ValueError as e:
        print(f"issue search: {e}", file=sys.stderr)
        return 2

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)

    def _note(before, after):
        print(f"issue search: resolved {before} -> {after}", file=sys.stderr)

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
                args.json_mode, VALID_JSON_FIELDS, prog="issue search"
            )
        except jsonfields.FieldSelectionError as e:
            print(e.message, file=sys.stderr)
            return 2
        emit_json(matches, sys.stdout, fields)
    else:
        emit_tsv(matches, sys.stdout)

    return 0 if matches else 1
