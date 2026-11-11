"""`issue complete` -- move an issue to done.

Convenience verb over the `transition` primitive. The evidence review
found the refused `open -> done` transition to be the single most repeated
usage error in the session-log corpus (work finished on an issue that was
never flipped to in-progress). So on an `open` issue this verb performs
open -> in-progress -> done as ONE atomic operation -- one lock, one
commit, both audit lines in ## Implementation Notes (design 5.5). On an
`in-progress` issue it is a plain -> done. From `blocked`/`cancelled` the
strict D11 matrix still applies (exit 2 -- unblock or reopen first).

The `transition` primitive is NOT taught open -> done; only this
convenience verb papers over the two-step. Delegates to
``transition._execute_transition(complete_passthrough=True)`` so the
commit pipeline and the 3-7 exit contract are shared code.
"""

from __future__ import annotations

import argparse
import sys

import errors  # noqa: E402

from . import common
from . import transition

VERB = "complete"
TIER = "tier1"
SUMMARY = "move an issue to done (open issues pass through in-progress)"
ORDER = 20

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"

_DESCRIPTION = """\
Mark an issue done. On an `open` issue this performs
open -> in-progress -> done atomically (one lock, one commit, both audit
lines) rather than refusing the two-step. On an `in-progress` issue it is
a plain -> done. From `blocked` or `cancelled` the strict transition
matrix applies (exit 2)."""

_EPILOG = """\
Examples:
  issue complete IM-024
  issue complete IM-024 --summary "all acceptance checks passed"
  issue complete IM-024 --claim-generation 8 --json
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, description=_DESCRIPTION, epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("--summary", "--reason", "--note", dest="reason",
                   default=None, metavar="TEXT",
                   help="One-line closing summary recorded in Implementation "
                        "Notes. (--reason / --note are accepted synonyms.)")
    p.add_argument("--claim-generation", type=int, default=None, metavar="N",
                   dest="claim_generation")
    common.add_json_flag(p)
    common.add_no_reindex_flag(p)
    common.add_lock_timeout_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def run(args):
    prog = "issue complete"
    if not args.issue_id:
        print(f"{prog}: missing issue ID (use -h for usage)", file=sys.stderr)
        return errors.EXIT_USAGE
    if args.claim_generation is not None and args.claim_generation < 0:
        print(f"{prog}: --claim-generation must be a non-negative integer",
              file=sys.stderr)
        return errors.EXIT_USAGE
    return transition._execute_transition(
        args.issue_id, "done", reason=args.reason,
        claim_generation=args.claim_generation, json_mode=args.json_mode,
        no_reindex=args.no_reindex, lock_timeout=args.lock_timeout,
        store=args.store, prog=prog, complete_passthrough=True,
    )
