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

import errors  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import reindex_core  # noqa: E402

from . import common

VERB = "reindex"
TIER = "admin"
# IM-239 / surface.self-naming: the one place this verb's own name is
# written, so a message prefix cannot drift from the dispatcher spelling.
# Declared because IM-275 gave the verb its first name-prefixed diagnostic;
# before that it printed no command name at all and carried no PROG.
PROG = "issue admin reindex"
SUMMARY = "regenerate INDEX.md from issue-file frontmatter (store or legacy)"
ORDER = 10

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
# Resolves the store through the documented precedence chain and names it
# with `--store`. Its positional is a legacy `.issues/` directory, NOT a
# store root, which is why `positional` would be the wrong declaration and
# why IM-251 measured the absent flag as a real gap (added by IM-275).
SURFACE_STORE = "flag"
# IM-260 C3: every failure path carries the branchable payload (the
# conflicting store/positional refusal, argparse refusals via
# `common.structured_parser_errors`, and each missing-directory error).
# Ratchets: may never revert.
SURFACE_STRUCTURED_ERRORS = "converted"

_DESCRIPTION = """\
Regenerate INDEX.md from issue-file frontmatter.

No argument: prefer store mode when detectable (ISSUE_STORE_ROOT set or a
.issues/.migrated-to-store sentinel), otherwise reindex the nearest .issues/
directory (legacy mode). A positional issues-dir forces a legacy single-dir
reindex. --all walks the workspace for every .issues/ directory (legacy) and,
when a store root is named (--store, else ISSUE_STORE_ROOT) and exists,
additionally runs one full store reindex.

--store names a STORE root; the positional names a legacy .issues/ directory.
They select different modes, so passing both is a usage error."""

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
    "issues": N,                       # summed over targets
    "dry_run": BOOL                    # true when --dry-run was passed
  }

`targets` is always a list, with a single entry outside --all mode; an
--all run that found nothing yields an empty list and zero counts. Every
key is present in every mode, so the shape is a function of argv alone.
The human summary lines are replaced by the JSON document, not duplicated.

--dry-run runs the same scan and emits the same shape -- the counts and
index paths the real run would write -- but writes no index, makes no
commit, and takes no lock; `indexes_written` then counts planned writes.

Examples:
  issue admin reindex                    # auto: store mode or nearest .issues/
  issue admin reindex ~/src/issues/      # explicit legacy dir
  issue admin reindex --store ~/personal/issues-store
  issue admin reindex --all              # all .issues/ dirs + the named store
  issue admin reindex --dry-run          # preview: scan + counts, no writes
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
        help="Walk the workspace for every .issues/ dir (+ the store, when "
             "one is named by --store or ISSUE_STORE_ROOT).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Report what would be reindexed without writing.",
    )
    common.add_store_flag(p)
    common.add_json_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
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


def _emit_json(mode, targets, dry_run) -> None:
    """One JSON document on stdout; informational lines never ride along."""
    payload = {
        "mode": mode,
        "targets": [
            {k: v for k, v in t.items() if k != "summary"} for t in targets
        ],
        "indexes_written": sum(t["indexes_written"] for t in targets),
        "issues": sum(t["issues"] for t in targets),
        "dry_run": dry_run,
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def run(args) -> int:
    json_mode = bool(getattr(args, "json_mode", False))
    dry_run = bool(getattr(args, "dry_run", False))

    # The positional and --store select different MODES (a legacy .issues/
    # directory versus a store root), so one invocation naming both has no
    # single meaning -- refuse it rather than silently preferring one
    # (IM-275).
    if args.issues_dir is not None and args.store:
        common.emit_error(
            errors.UsageError(
                "--store names a store root and the positional issues-dir "
                "names a legacy .issues/ directory -- pass one or the "
                "other, not both",
                reason="conflicting-flags", flags="--store,issues-dir"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_USAGE

    def report(result, collected):
        # Human mode prints the summary line as it always did; JSON mode
        # accumulates the structured record and prints nothing here, so
        # stdout carries exactly one document (IM-144).
        collected.append(result)
        if not json_mode:
            print(result["summary"])

    # --all: every .issues/ dir (legacy) + the named store, if any.
    if args.do_all:
        base = _workspace_root()
        targets = []
        for dirpath, dirnames, _files in os.walk(base):
            if ".issues" in dirnames:
                issues_dir = Path(dirpath) / ".issues"
                try:
                    report(
                        reindex_core.reindex_legacy_dir_detail(
                            issues_dir, dry_run=dry_run
                        ),
                        targets,
                    )
                except FileNotFoundError as e:
                    common.emit_error(
                        errors.NotFoundError(str(e)),
                        prog=PROG, json_mode=json_mode)
                    return errors.EXIT_PRECONDITION
        # --store overrides the env for the store half of the sweep; the
        # sentinel is deliberately not consulted here, as it never was.
        named_store = args.store or os.environ.get("ISSUE_STORE_ROOT")
        if named_store and Path(named_store).expanduser().is_dir():
            try:
                report(
                    _reindex_store(
                        Path(named_store).expanduser(), dry_run=dry_run
                    ),
                    targets,
                )
            except FileNotFoundError as e:
                common.emit_error(
                    errors.NotFoundError(str(e)),
                    prog=PROG, json_mode=json_mode)
                return errors.EXIT_PRECONDITION
        if json_mode:
            _emit_json("all", targets, dry_run)
        elif not targets:
            print(f"No .issues/ directories or store found under {base}.")
        return 0

    targets = []

    # Explicit positional path: always legacy single-dir reindex.
    if args.issues_dir is not None:
        try:
            report(
                reindex_core.reindex_legacy_dir_detail(
                    args.issues_dir, dry_run=dry_run
                ),
                targets,
            )
        except FileNotFoundError as e:
            common.emit_error(
                errors.NotFoundError(str(e)),
                prog=PROG, json_mode=json_mode)
            return errors.EXIT_PRECONDITION
        if json_mode:
            _emit_json("legacy", targets, dry_run)
        return 0

    # No arg: prefer store mode when detectable.
    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)
    if store_root is not None:
        store_root = store_root.expanduser()
        try:
            report(_reindex_store(store_root, dry_run=dry_run), targets)
        except FileNotFoundError as e:
            common.emit_error(
                errors.NotFoundError(str(e)),
                prog=PROG, json_mode=json_mode)
            return errors.EXIT_PRECONDITION
        if json_mode:
            _emit_json("store", targets, dry_run)
        return 0

    # Legacy fallback: nearest .issues/.
    try:
        report(
            reindex_core.reindex_legacy_dir_detail(anchor, dry_run=dry_run),
            targets,
        )
    except FileNotFoundError as e:
        common.emit_error(
            errors.NotFoundError(str(e)),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION
    if json_mode:
        _emit_json("legacy", targets, dry_run)
    return 0


def _reindex_store(store_root, *, dry_run=False) -> dict:
    # Respect the outer-mutation kill switch exactly as the bash binary did:
    # ISSUE_MGMT_NO_AUTOCOMMIT -> the caller owns the commit, write bare.
    suppress = bool(os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT"))
    return reindex_core.reindex_store_detail(
        store_root, suppress_commit=suppress, dry_run=dry_run
    )
