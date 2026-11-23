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

import errors  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import jsonfields  # noqa: E402
import search  # noqa: E402

from . import common

VERB = "search"
TIER = "tier1"
SUMMARY = "search issue titles and bodies (text query + filters)"
ORDER = 30

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "enumeration-read"
SURFACE_STORE = "flag"
# IM-260 C1: every failure path carries the branchable payload (argparse
# refusals via `common.structured_parser_errors`, the missing/invalid
# query, the store gate, the prefix scope, the field selection). A
# zero-result exit 1 is a RESULT under the grep convention, not a
# failure. Ratchets: may never revert.
SURFACE_STRUCTURED_ERRORS = "converted"

# Top-level keys of each per-issue --json object, derived from the row
# builder itself (search.IssueMatch.to_dict) rather than restated, so a
# field added there is selectable here by construction (IM-263).
VALID_JSON_FIELDS = search.IssueMatch.json_fields()

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
Exit codes: 0 at least one row emitted; 1 zero rows (grep convention,
uniform across every enumeration read); 2 argument/usage error, including
a --prefix the store does not have (error=unknown-prefix).

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
    common.add_prefix_scope_flag(
        p,
        help="Limit scan to these prefix directories (OR within the flag).",
    )
    common.add_value_set_flag(
        p, "--status", metavar="STATUS[,STATUS...]",
        help="Repeatable/comma-separated. Match only these frontmatter "
             "status values.",
    )
    common.add_value_set_flag(
        p, "--type", metavar="TYPE[,TYPE...]",
        help="Repeatable/comma-separated. Match only these frontmatter "
             "type values.",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
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
    json_mode = jsonfields.json_enabled(args.json_mode)

    if args.query is None:
        common.emit_error(
            errors.UsageError(
                'missing QUERY (pass "" for frontmatter-only '
                "search, or use `issue list`; -h for usage)",
                reason="missing-query"),
            prog="issue search", json_mode=json_mode)
        return errors.EXIT_USAGE

    try:
        matcher = search.build_matcher(
            args.syntax,
            args.query,
            case_sensitive=args.case_sensitive,
        )
    except ValueError as e:
        common.emit_error(
            errors.UsageError(str(e), reason="bad-arguments"),
            prog="issue search", json_mode=json_mode)
        return errors.EXIT_USAGE

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)

    rc = common.require_store(
        store_root, prog="issue search",
        json_mode=jsonfields.json_enabled(args.json_mode),
        legacy_anchor=anchor,
    )
    if rc is not None:
        return rc

    prefixes, rc = common.resolve_prefix_scope(
        args.prefix, store_root, anchor, prog="issue search",
        json_mode=jsonfields.json_enabled(args.json_mode),
    )
    if rc is not None:
        return rc

    ff = search.FrontmatterFilter(
        prefixes=prefixes,
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
            common.emit_error(
                errors.UsageError(e.message, flag="--json",
                                  reason="bad-json-field"),
                prog="issue search", json_mode=True)
            return errors.EXIT_USAGE
        emit_json(matches, sys.stdout, fields)
    else:
        emit_tsv(matches, sys.stdout)

    return 0 if matches else 1
