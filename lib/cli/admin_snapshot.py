"""`issue admin snapshot` -- reconcile the store's git history with its tree.

Dispatcher port of the flat `issue-snapshot` binary. Runs the
catchup sweep against an issue store: if there is any pending
tracked-path state (orphan writes from killed mutations, hook-rejected
commits since fixed, manual edits), stage and commit it as one
``catchup: reconcile pending store changes`` commit. When the store is
clean, exits 0 without making a commit.

The catchup-sweep semantics, the stdout lines, and the 0/1/2/3 exit codes
are byte-identical to the flat binary (``lib/concurrency.py`` is untouched).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import concurrency  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import errors  # noqa: E402
import storeresolve  # noqa: E402

from . import common

# The one place this verb names itself; every diagnostic formats through
# it, so the surface manifest sees one name per verb and
# `surface.self-naming` is checkable (IM-239).
PROG = "issue admin snapshot"

VERB = "snapshot"
TIER = "admin"
SUMMARY = "run the catchup sweep outside any mutation (recovery / cron)"
ORDER = 30

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
SURFACE_STORE = "flag"
# IM-260 C3: every failure path carries the branchable payload (the
# store-resolution refusals raise `PreconditionError` and `run` renders
# them; argparse refusals ride `common.structured_parser_errors`; the
# locked-sweep conditions were already structured). Ratchets: may never
# revert.
SURFACE_STRUCTURED_ERRORS = "converted"

CONFIG_PATH = storeresolve.CONFIG_PATH

_DESCRIPTION = """\
Reconcile the issue store with its working tree. If there is any pending
tracked-path state, stage and commit it as one
`catchup: reconcile pending store changes` commit; a clean store exits 0
without a commit. Useful as a belt-and-suspenders cron and as the
post-hook-rejection manual recovery surface."""

_EPILOG = """\
Store-root resolution order (same as `issue admin init-project`):
  1. --store ROOT
  2. ISSUE_STORE_ROOT environment variable
  3. <cwd>/.issues/.migrated-to-store sentinel (walking up)
  4. ~/.config/issue-mgmt/config, key store_root

JSON mode (--json): exactly one object on stdout, shape

  {"store": PATH, "committed": BOOL, "commit": SHA|null,
   "paths": [PATH, ...], "dry_run": BOOL}

`committed` is false on a clean store (and on a hook-rejected catchup,
which warns on stderr and leaves the files on disk), in which case
`commit` is null. `paths` lists the pending tracked store paths the sweep
found -- empty on a clean store. Every key is present in both outcomes, so
the shape is a function of argv alone.

--dry-run reads the same pending set and emits the same shape -- `paths`
names exactly what the real run would commit -- but stages nothing,
commits nothing, and takes no lock; `committed` is false and `commit`
null under a preview.

Exit codes:
  0  clean store, successful catchup commit, or hook-rejected catchup (warned)
  1  precondition failed / store commit failed for a non-hook reason
  2  argument / usage error
  3  store commit lock timeout (concurrent-edit; was 17 through v2.4.1)

Examples:
  issue admin snapshot
  issue admin snapshot --dry-run           # preview the pending set
  issue admin snapshot --commit-timeout 30
  issue admin snapshot --store /Users/me/issues-store
  issue admin snapshot --json
  ISSUE_STORE_ROOT=/path/to/store issue admin snapshot
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
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Report what would be committed without writing.",
    )
    common.add_store_flag(p)
    # IM-242: the flag name follows the lock it bounds. This verb takes only
    # the store-wide commit lock, so it carries --commit-timeout; it spelled
    # that bound --timeout through v3.4.x, and IM-270 retired the spelling.
    common.add_commit_timeout_flag(
        p, default=concurrency.DEFAULT_LOCK_TIMEOUT
    )
    common.add_json_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
    p.set_defaults(_run=run)
    return p


def _die(msg, *, reason, **fields):
    """Raise the branchable refusal; ``run`` renders it (IM-260 C3)."""
    raise errors.PreconditionError(msg, reason=reason, **fields)


def _read_config_store_root():
    """Shared config reader (``storeresolve``, IM-280); None when absent."""
    return storeresolve.read_config_store_root()


def _find_sentinel_store_root():
    cur = Path.cwd().resolve()
    for d in [cur, *cur.parents]:
        sentinel = d / ".issues" / ".migrated-to-store"
        if sentinel.is_file():
            # A present-but-empty sentinel fails loud (IM-294) instead of
            # being skipped toward higher directories or the config leg:
            # it is corrupt routing state, and the nearest sentinel is the
            # one that governs.
            return storeresolve.read_sentinel_payload(sentinel)
    return None


def _resolve_store_root(cli_store):
    """Documented deviation from the shared 4-step chain (IM-280).

    Snapshot is designed to run from cron, where no env var is exported and
    no sentinel walk-up from $HOME succeeds, so after the shared legs it
    falls through to the per-machine config file.
    """
    if cli_store:
        return cli_store
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return env
    s = _find_sentinel_store_root()
    if s:
        return s
    cfg = _read_config_store_root()
    if cfg:
        return cfg
    _die(
        "no store root resolved; set ISSUE_STORE_ROOT, pass --store, "
        "run inside a project with a .migrated-to-store sentinel, or "
        f"set store_root in {CONFIG_PATH}",
        reason="store-unresolvable",
    )


def run(args) -> int:
    json_mode = bool(getattr(args, "json_mode", False))
    try:
        store_root = _resolve_store_root(args.store)
        if not Path(store_root).is_dir():
            _die(
                f"store root does not exist or is not a directory: "
                f"{store_root}",
                reason="store-unresolvable", store=str(store_root),
            )
    except errors.ContractError as e:
        common.emit_error(e, prog=PROG, json_mode=json_mode)
        return e.exit_code

    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        # Preview (IM-259): read the same pending set the sweep would
        # stage, write nothing, commit nothing, take no lock -- a preview
        # contends with nobody and may be marginally stale, exactly like
        # the read-only verbs.
        try:
            paths = concurrency.pending_tracked_paths(store_root)
        except concurrency.StoreCommitError as e:
            print(e.format_stderr(PROG), file=sys.stderr)
            return errors.EXIT_PRECONDITION
        sha = None
    else:
        try:
            with concurrency.store_commit_lock(
                    store_root, timeout=args.commit_timeout):
                # Read the pending set before the sweep claims it, so --json
                # can name what was swept; the sweep itself returns only a sha.
                paths = concurrency.pending_tracked_paths(store_root)
                sha = concurrency.catchup_sweep(store_root)
        except errors.ContractError as e:
            print(e.format_stderr(PROG, json_mode=json_mode),
                  file=sys.stderr)
            return e.exit_code
        except concurrency.StoreCommitError as e:
            print(e.format_stderr(PROG), file=sys.stderr)
            return errors.EXIT_PRECONDITION

    if json_mode:
        json.dump(
            {
                "store": str(store_root),
                "committed": bool(sha),
                "commit": sha or None,
                "paths": paths,
                "dry_run": dry_run,
            },
            sys.stdout,
            indent=2,
            sort_keys=False,
        )
        sys.stdout.write("\n")
        return 0

    if dry_run:
        if paths:
            print(f"would make catchup commit: {len(paths)} pending path(s)")
            for p in paths:
                print(f"  {p}")
        else:
            print("store is clean; no commit made")
    elif sha:
        print(f"catchup commit: {sha}")
    else:
        print("store is clean; no commit made")
    return 0
