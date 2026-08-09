"""`issue unblock` -- return a blocked issue to an active state.

Convenience verb over the `transition` primitive: the inverse of
`issue block`. Lands in `in-progress` by default, or `open` via
`--to open`. The strict D11 matrix still governs (blocked -> open and
blocked -> in-progress are both legal). Delegates to
``transition._execute_transition`` so the commit pipeline and the 3-7
exit contract are shared code.
"""

from __future__ import annotations

import argparse
import sys

import errors  # noqa: E402

from . import common
from . import transition

VERB = "unblock"
TIER = "tier1"
SUMMARY = "return a blocked issue to an active state (default in-progress)"
ORDER = 55

_DESCRIPTION = """\
Transition an issue out of blocked into an active state -- in-progress by
default, or open via --to open. The transition matrix applies (only
blocked -> open / in-progress are legal targets here)."""

_EPILOG = """\
Examples:
  issue unblock IM-024
  issue unblock IM-024 --to open --reason "RM-038 shipped"
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, description=_DESCRIPTION, epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("--to", default="in-progress", choices=("open", "in-progress"),
                   dest="to_state",
                   help="Active state to land in (default: in-progress).")
    p.add_argument("--reason", default=None, metavar="TEXT",
                   help="Optional note recorded in Implementation Notes.")
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
    prog = "issue unblock"
    if not args.issue_id:
        print(f"{prog}: missing issue ID (use -h for usage)", file=sys.stderr)
        return errors.EXIT_USAGE
    if args.claim_generation is not None and args.claim_generation < 0:
        print(f"{prog}: --claim-generation must be a non-negative integer",
              file=sys.stderr)
        return errors.EXIT_USAGE
    return transition._execute_transition(
        args.issue_id, args.to_state, reason=args.reason,
        claim_generation=args.claim_generation, json_mode=args.json_mode,
        no_reindex=args.no_reindex, lock_timeout=args.lock_timeout,
        store=args.store, prog=prog,
    )
