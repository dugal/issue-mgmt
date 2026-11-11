"""`issue admin reindex` -- regenerate INDEX.md from issue frontmatter.

Thin CLI wrapper over the in-process reindex core ``lib/reindex_core.py``:
it resolves store-vs-legacy dispatch, the ``--all`` walk, and the summary
stdout line, then delegates the rendering + commit to the core. There is no
sibling-binary subprocess and no ``.sh`` name resolution.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import reindex_core  # noqa: E402  (lib/ is on sys.path via the entrypoint)

from . import common

VERB = "reindex"
TIER = "admin"
SUMMARY = "regenerate INDEX.md from issue-file frontmatter (store or legacy)"
ORDER = 10

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
# IM-251: resolves the store through the precedence chain with no operand
# naming it (its positional is a legacy `.issues/` dir, not a store root),
# so the absent `--store` is a real gap -- IM-251 follow-up work.
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"

_DESCRIPTION = """\
Regenerate INDEX.md from issue-file frontmatter.

No argument: prefer store mode when detectable (ISSUE_STORE_ROOT set or a
.issues/.migrated-to-store sentinel), otherwise reindex the nearest .issues/
directory (legacy mode). A positional issues-dir forces a legacy single-dir
reindex. --all walks the workspace for every .issues/ directory (legacy) and,
when ISSUE_STORE_ROOT is set and exists, additionally runs one full store
reindex."""

_EPILOG = """\
JSON mode (--json): exactly one object on stdout, shape

  {
    "mode": "store" | "legacy" | "all",
    "targets": [                       # one entry per index set regenerated
      {"mode": "store", "store": PATH, "index": PATH,
       "prefixes": [{"prefix": PFX, "issues": N}, ...],
       "indexes_written": N, "issues": N},
      {"mode": "legacy", "issues_dir": PATH, "index": PATH,
       "indexes_written": 1, "issues": N}
    ],
    "indexes_written": N,              # summed over targets
    "issues": N                        # summed over targets
  }

`targets` is always a list, with a single entry outside --all mode; an
--all run that found nothing yields an empty list and zero counts. Every
key is present in every mode, so the shape is a function of argv alone.
The human summary lines are replaced by the JSON document, not duplicated.

Examples:
  issue admin reindex                    # auto: store mode or nearest .issues/
  issue admin reindex ~/src/issues/      # explicit legacy dir
  issue admin reindex --all              # all .issues/ dirs + store (if env-resolved)
  issue admin reindex --json             # machine-readable counts
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
    common.add_json_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


# --all walk root: workspace root for the legacy multi-.issues sweep.
def _workspace_root() -> Path:
    env = os.environ.get("ISSUE_MGMT_ROOT")
    if env:
        return Path(env)
    cwd = Path.cwd()
    if (cwd.parent / ".issues").is_dir():
        return cwd.parent
    return cwd


def _emit_json(mode, targets) -> None:
    """One JSON document on stdout; informational lines never ride along."""
    payload = {
        "mode": mode,
        "targets": [
            {k: v for k, v in t.items() if k != "summary"} for t in targets
        ],
        "indexes_written": sum(t["indexes_written"] for t in targets),
        "issues": sum(t["issues"] for t in targets),
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def run(args) -> int:
    json_mode = bool(getattr(args, "json_mode", False))

    def report(result, collected):
        # Human mode prints the summary line as it always did; JSON mode
        # accumulates the structured record and prints nothing here, so
        # stdout carries exactly one document (IM-144).
        collected.append(result)
        if not json_mode:
            print(result["summary"])

    # --all: every .issues/ dir (legacy) + store when env-resolved.
    if args.do_all:
        base = _workspace_root()
        targets = []
        for dirpath, dirnames, _files in os.walk(base):
            if ".issues" in dirnames:
                issues_dir = Path(dirpath) / ".issues"
                try:
                    report(
                        reindex_core.reindex_legacy_dir_detail(issues_dir),
                        targets,
                    )
                except FileNotFoundError as e:
                    print(f"Error: {e}", file=sys.stderr)
                    return 1
        store_env = os.environ.get("ISSUE_STORE_ROOT")
        if store_env and Path(store_env).is_dir():
            try:
                report(_reindex_store(store_env), targets)
            except FileNotFoundError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
        if json_mode:
            _emit_json("all", targets)
        elif not targets:
            print(f"No .issues/ directories or store found under {base}.")
        return 0

    targets = []

    # Explicit positional path: always legacy single-dir reindex.
    if args.issues_dir is not None:
        try:
            report(
                reindex_core.reindex_legacy_dir_detail(args.issues_dir),
                targets,
            )
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        if json_mode:
            _emit_json("legacy", targets)
        return 0

    # No arg: prefer store mode when detectable.
    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, None)
    if store_root is not None:
        try:
            report(_reindex_store(store_root), targets)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        if json_mode:
            _emit_json("store", targets)
        return 0

    # Legacy fallback: nearest .issues/.
    try:
        report(reindex_core.reindex_legacy_dir_detail(anchor), targets)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if json_mode:
        _emit_json("legacy", targets)
    return 0


def _reindex_store(store_root) -> dict:
    # Respect the outer-mutation kill switch exactly as the bash binary did:
    # ISSUE_MGMT_NO_AUTOCOMMIT -> the caller owns the commit, write bare.
    suppress = bool(os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT"))
    return reindex_core.reindex_store_detail(
        store_root, suppress_commit=suppress
    )
