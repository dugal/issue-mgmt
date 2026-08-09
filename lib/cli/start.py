"""`issue start` -- claim an issue and move it to in-progress.

Convenience verb over the `transition` primitive's lease path: acquires a
TTL lease (design 5.4 -- `--claim` hangs off `start`), which atomically
flips an `open` issue to `in-progress`, and records the working branch in
the SAME claim write. Before claiming it runs the expired-lease reaper
opportunistically (design 2a "Reap amendment" -- this was the
`/issue-start` skill's behavior; auto-running it here is what keeps the
reaper in the flow it actually lives in).

Delegates the claim to ``transition._execute_lease`` so the lease
mechanics, the one-commit pipeline, and the 3-7 loser-side exit
contract are the exact same code the primitive uses.
"""

from __future__ import annotations

import argparse
import os
import sys

import lease  # noqa: E402
import errors  # noqa: E402
import idresolve  # noqa: E402

from . import common
from . import transition
from . import _reap

VERB = "start"
TIER = "tier1"
SUMMARY = "claim an issue and move it to in-progress (auto-reaps expired leases)"
ORDER = 10

_DESCRIPTION = """\
Claim an issue with a TTL lease and flip it to in-progress, recording the
working branch in the same write. Expired leases in the issue's prefix are
reaped first (opportunistic; best-effort).

The claimant defaults to $USER; pass --claim/--actor to override (e.g. a
parallel-agent actor id). Claiming an issue held by another actor is exit
4 (lost-race); a blocked/done/cancelled issue is exit 7 (not-claimable)."""

_EPILOG = """\
Examples:
  issue start IM-024 --branch phase-x
  issue start IM-024 --claim claude-code-w2 --lease-ttl 7200 --json
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, description=_DESCRIPTION, epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("--claim", "--actor", dest="claimant", default=None,
                   metavar="CLAIMANT",
                   help="Claimant recorded in claim.assignee (default $USER).")
    p.add_argument("--branch", default=None, metavar="NAME",
                   help="Working branch recorded on the issue's branch: field.")
    p.add_argument("--lease-ttl", type=int, default=None, metavar="SECONDS",
                   dest="lease_ttl")
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
    prog = "issue start"
    if not args.issue_id:
        print(f"{prog}: missing issue ID (use -h for usage)",
              file=sys.stderr)
        return errors.EXIT_USAGE

    claimant = args.claimant or os.environ.get("USER") or "unknown"
    if not lease.CLAIMANT_RE.match(claimant):
        print(f"{prog}: invalid claimant {claimant!r} (must match "
              f"{lease.CLAIMANT_RE.pattern})", file=sys.stderr)
        return errors.EXIT_USAGE
    if args.lease_ttl is not None and args.lease_ttl <= 0:
        print(f"{prog}: --lease-ttl must be a positive integer of seconds",
              file=sys.stderr)
        return errors.EXIT_USAGE
    if args.claim_generation is not None and args.claim_generation < 0:
        print(f"{prog}: --claim-generation must be a non-negative integer",
              file=sys.stderr)
        return errors.EXIT_USAGE

    # Opportunistic auto-reap of the target's prefix before claiming
    # (best-effort; never blocks the claim). Resolve the prefix
    # case-insensitively so `im-7` still reaps the IM prefix.
    parsed = idresolve.split_id(args.issue_id)
    if parsed is not None:
        anchor = common.find_project_anchor()
        store_root = common.resolve_store_root(anchor, args.store)
        if store_root is not None:
            _reap.auto_reap(store_root.expanduser(), repo=parsed[0],
                            lock_timeout=args.lock_timeout)

    return transition._execute_lease(
        args.issue_id, "claim", claimant=claimant, lease_ttl=args.lease_ttl,
        claim_generation=args.claim_generation, branch=args.branch,
        json_mode=args.json_mode, no_reindex=args.no_reindex,
        lock_timeout=args.lock_timeout, store=args.store, prog=prog,
    )
