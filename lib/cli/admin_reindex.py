"""`issue admin reindex` -- regenerate INDEX.md from issue frontmatter.

Dispatcher port of the flat `reindex-issues.sh` bash binary,
built on the in-process reindex seam ``lib/reindex_core.py`` that WP0
extracted for exactly this handoff. There is no sibling-binary subprocess
and no ``.sh`` name resolution, which is the failure class killed
structurally rather than patched.

The rendered INDEX output, the store-vs-legacy dispatch, the ``--all`` walk,
and the summary stdout line are byte-for-byte identical to the bash binary
(the bash copy still backs the flat `reindex-issues` until WP7 tombstones
it; both must match).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import reindex_core  # noqa: E402  (lib/ is on sys.path via the entrypoint)

from . import common

VERB = "reindex"
TIER = "admin"
SUMMARY = "regenerate INDEX.md from issue-file frontmatter (store or legacy)"
ORDER = 10

_DESCRIPTION = """\
Regenerate INDEX.md from issue-file frontmatter.

No argument: prefer store mode when detectable (ISSUE_STORE_ROOT set or a
.issues/.migrated-to-store sentinel), otherwise reindex the nearest .issues/
directory (legacy mode). A positional issues-dir forces a legacy single-dir
reindex. --all walks the workspace for every .issues/ directory (legacy) and,
when ISSUE_STORE_ROOT is set and exists, additionally runs one full store
reindex."""

_EPILOG = """\
Examples:
  issue admin reindex                    # auto: store mode or nearest .issues/
  issue admin reindex ~/src/issues/      # explicit legacy dir
  issue admin reindex --all              # all .issues/ dirs + store (if env-resolved)
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
        "issues_dir",
        nargs="?",
        metavar="issues-dir",
        help="Explicit legacy .issues/ directory to reindex.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="do_all",
        help="Walk the workspace for every .issues/ dir (+ store if env-resolved).",
    )
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


# also in the flat reindex-issues.sh -- keep in sync
def _workspace_root() -> Path:
    env = os.environ.get("ISSUE_MGMT_ROOT")
    if env:
        return Path(env)
    cwd = Path.cwd()
    if (cwd.parent / ".issues").is_dir():
        return cwd.parent
    return cwd


def run(args) -> int:
    # --all: every .issues/ dir (legacy) + store when env-resolved.
    if args.do_all:
        base = _workspace_root()
        found = False
        for dirpath, dirnames, _files in os.walk(base):
            if ".issues" in dirnames:
                issues_dir = Path(dirpath) / ".issues"
                try:
                    print(reindex_core.reindex_legacy_dir(issues_dir))
                except FileNotFoundError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    return 1
                found = True
        store_env = os.environ.get("ISSUE_STORE_ROOT")
        if store_env and Path(store_env).is_dir():
            try:
                print(_reindex_store(store_env))
            except FileNotFoundError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            found = True
        if not found:
            print(f"No .issues/ directories or store found under {base}.")
        return 0

    # Explicit positional path: always legacy single-dir reindex.
    if args.issues_dir is not None:
        try:
            print(reindex_core.reindex_legacy_dir(args.issues_dir))
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    # No arg: prefer store mode when detectable.
    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, None)
    if store_root is not None:
        try:
            print(_reindex_store(store_root))
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    # Legacy fallback: nearest .issues/.
    try:
        print(reindex_core.reindex_legacy_dir(anchor))
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def _reindex_store(store_root) -> str:
    # Respect the outer-mutation kill switch exactly as the bash binary did:
    # ISSUE_MGMT_NO_AUTOCOMMIT -> the caller owns the commit, write bare.
    suppress = bool(os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT"))
    return reindex_core.reindex_store(store_root, suppress_commit=suppress)
