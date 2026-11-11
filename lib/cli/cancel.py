"""`issue cancel` -- move an issue to cancelled.

Convenience verb over the `transition` primitive. Cancellation is the
project's "delete" -- issue files are never removed. Reachable from any
active state and from `done` (reopen semantics are the primitive's);
cancelled -> cancelled is a no-op. Delegates to
``transition._execute_transition`` so the commit pipeline and the 3-7
exit contract are shared code.
"""

from __future__ import annotations

import argparse
import sys

import errors  # noqa: E402

from . import common
from . import transition

VERB = "cancel"
TIER = "tier1"
SUMMARY = "move an issue to cancelled (the never-delete substitute)"
ORDER = 40

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"

_DESCRIPTION = """\
Mark an issue cancelled -- the terminal state for work that will not
happen (issue files are never deleted). A reason is recommended."""

_EPILOG = """\
Examples:
  issue cancel IM-024 --reason "superseded by IM-099"
  issue cancel IM-024 --json
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, description=_DESCRIPTION, epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("--reason", default=None, metavar="TEXT",
                   help="One-line explanation recorded in Implementation Notes.")
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
    prog = "issue cancel"
    if not args.issue_id:
        print(f"{prog}: missing issue ID (use -h for usage)", file=sys.stderr)
        return errors.EXIT_USAGE
    if args.claim_generation is not None and args.claim_generation < 0:
        print(f"{prog}: --claim-generation must be a non-negative integer",
              file=sys.stderr)
        return errors.EXIT_USAGE
    return transition._execute_transition(
        args.issue_id, "cancelled", reason=args.reason,
        claim_generation=args.claim_generation, json_mode=args.json_mode,
        no_reindex=args.no_reindex, lock_timeout=args.lock_timeout,
        store=args.store, prog=prog,
    )
