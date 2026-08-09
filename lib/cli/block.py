"""`issue block` -- move an issue to blocked with a required reason.

Convenience verb over the `transition` primitive. A reason is mandatory
(the state machine records why work cannot proceed). `issue unblock` is
the inverse (its own module). Delegates to
``transition._execute_transition`` so the commit pipeline and the 3-7
exit contract are shared code.
"""

from __future__ import annotations

import argparse
import sys

import errors  # noqa: E402

from . import common
from . import transition

VERB = "block"
TIER = "tier1"
SUMMARY = "move an issue to blocked (reason required)"
ORDER = 50

_DESCRIPTION = """\
Mark an issue blocked, recording the required reason in Implementation
Notes. Use `issue unblock` to return it to an active state once the
blocker clears."""

_EPILOG = """\
Examples:
  issue block IM-024 --reason "waiting on RM-038"
  issue block IM-024 --reason "upstream API change" --json
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, description=_DESCRIPTION, epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("--reason", default=None, metavar="TEXT",
                   help="Required. Why work cannot proceed.")
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
    prog = "issue block"
    if not args.issue_id:
        print(f"{prog}: missing issue ID (use -h for usage)", file=sys.stderr)
        return errors.EXIT_USAGE
    if args.claim_generation is not None and args.claim_generation < 0:
        print(f"{prog}: --claim-generation must be a non-negative integer",
              file=sys.stderr)
        return errors.EXIT_USAGE
    # --reason enforcement lives in _execute_transition (single source),
    # matching the primitive's blocked-reason contract.
    return transition._execute_transition(
        args.issue_id, "blocked", reason=args.reason,
        claim_generation=args.claim_generation, json_mode=args.json_mode,
        no_reindex=args.no_reindex, lock_timeout=args.lock_timeout,
        store=args.store, prog=prog,
    )
