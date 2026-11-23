"""`issue admin init-store` -- scaffold a new issues store / converge one.

Creates a fresh issues store at the ``--store`` root: makes the
directory, initializes a git repo (``-b main``), and seeds the canonical
README.md, LAYOUT.md, .gitignore, INDEX.md, and ISSUE-TEMPLATE.md
scaffolds. Idempotent convergence on an existing store; never
overwrites. The arg surface is a normal argparse subparser since IM-272
(the hand-written ``__doc__`` help and the ``SURFACE_PARSER``
declaration are retired).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import errors  # noqa: E402  (lib/ is on sys.path via the entrypoint)

from . import common

# The one place this verb names itself. Every diagnostic formats through
# it, so the surface manifest sees exactly one name per verb and
# `surface.self-naming` is checkable rather than a ~90-site grep (IM-239).
PROG = "issue admin init-store"

GITIGNORE_BODY = ".locks/\n.DS_Store\n"
INDEX_BODY = ""  # the reindex populates it on first run

SCAFFOLD_FILES = ("README.md", "LAYOUT.md")

_DESCRIPTION = """\
Scaffold a new issues store / converge an existing one.

Creates a fresh issues store at ROOT: makes the directory, initializes
a git repo (`-b main`), and seeds the canonical README.md, LAYOUT.md,
.gitignore, INDEX.md, and ISSUE-TEMPLATE.md scaffolds. On an existing
store -- green-field or live and populated -- it reports each file as
`present` or `seeded` and never overwrites; re-running is a safe no-op
(the idempotent convergence contract).

Refuses to operate on a non-empty directory that is not already a git
repo, to avoid clobbering unrelated content.

The seeded README.md and LAYOUT.md carry the architectural principles
(P1 distribution, P2 concurrency) inline so the store carries its
contract independent of any project workspace. ISSUE-TEMPLATE.md is
copied byte-identically from the Homebrew generic so the seed matches
the default template that ships with this release."""

_EPILOG = """\
JSON mode (--json): exactly one object on stdout, shape

  {
    "store": PATH,
    "dry_run": BOOL,          # true when --dry-run was passed
    "store_created": BOOL,    # the directory was (or would be) created
    "git_initialized": BOOL,  # `git init -b main` ran (or would run)
    "seeded": [NAME, ...],    # files written (or, under --dry-run, planned)
    "present": [NAME, ...],   # files already there, never overwritten
    "committed": BOOL         # a `chore: scaffold issue store` commit was
                              # made (or, under --dry-run, would be)
  }

Every key is present in both the real and the preview run, so output
shape is a function of argv alone and a caller branches on `dry_run`
rather than on key presence. `dry_run` is the same preview marker
`issue admin reap --json` uses.

Resource resolution:
  Scaffold sources (README.md, LAYOUT.md) live under
    <PREFIX>/share/issue-mgmt/store-template/   (Homebrew install)
    <PREFIX>/share/store-template/              (dev fallback)
  where PREFIX = dirname(dirname(realpath($0))).

  ISSUE-TEMPLATE.md is resolved via the same precedence as `issue create`:
    <PREFIX>/share/issue-mgmt/ISSUE-TEMPLATE.md (Homebrew install)
    <PREFIX>/ISSUE-TEMPLATE.md                  (dev fallback)

Exit codes:
  0  success (including idempotent re-run on an existing store)
  1  precondition failed (non-empty non-git path, missing template
     resources)
  2  argument/usage error (missing --store)

Examples:
  issue admin init-store --store ~/issues-store
  issue admin init-store --store /tmp/new-store --non-interactive
  issue admin init-store --dry-run --store ~/issues-store
  issue admin init-store --json --store ~/issues-store"""


def die(msg, *, reason, **fields):
    """Raise the branchable refusal; ``run`` renders it (IM-260 C3)."""
    raise errors.PreconditionError(msg, reason=reason, **fields)


def script_prefix():
    """`dirname(dirname(realpath($0)))`: project root in dev, brew prefix in install."""
    return Path(os.path.realpath(sys.argv[0])).parent.parent


def script_dir():
    """Directory holding this script (sibling-script resolution base)."""
    return Path(os.path.realpath(sys.argv[0])).parent


def resolve_store_template_dir(prefix):
    candidates = [
        prefix / "share" / "issue-mgmt" / "store-template",
        prefix / "share" / "store-template",
    ]
    for c in candidates:
        if (c / "README.md").is_file() and (c / "LAYOUT.md").is_file():
            return c
    die(
        "could not locate store-template scaffolds; looked in: "
        + "; ".join(str(c) for c in candidates),
        reason="missing-resource",
    )


def resolve_issue_template(prefix):
    candidates = [
        prefix / "share" / "issue-mgmt" / "ISSUE-TEMPLATE.md",
        prefix / "ISSUE-TEMPLATE.md",
    ]
    for c in candidates:
        if c.is_file():
            return c
    die(
        "could not locate ISSUE-TEMPLATE.md; looked in: "
        + "; ".join(str(c) for c in candidates),
        reason="missing-resource",
    )


def is_git_repo(path):
    return (path / ".git").exists()


def dir_is_empty(path):
    return not any(path.iterdir())


def seed_file(dest, body):
    """Write `body` to `dest` if missing. Returns 'seeded' or 'present'."""
    if dest.exists():
        return "present"
    dest.write_text(body, encoding="utf-8")
    return "seeded"


def copy_file(src, dest):
    """Copy src -> dest if missing. Returns 'seeded' or 'present'."""
    if dest.exists():
        return "present"
    shutil.copyfile(src, dest)
    return "seeded"


def git_init(path):
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
    )


def git_has_staged(path):
    r = subprocess.run(
        ["git", "-C", str(path), "diff", "--cached", "--quiet"],
    )
    return r.returncode != 0


def _git_commit_env():
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", env.get("USER", "issue-mgmt"))
    env.setdefault("GIT_AUTHOR_EMAIL", "issue-mgmt@localhost")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    return env


def git_stage_and_commit(path, files, message):
    subprocess.run(
        ["git", "-C", str(path), "add", "--", *files],
        check=True,
    )
    if not git_has_staged(path):
        return False
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", message],
        check=True,
        env=_git_commit_env(),
    )
    return True


def git_stage_all_and_commit(path, pathspecs, message):
    """Stage pathspecs with `git add -A` semantics (additions AND
    deletions) and commit. Nothing-to-commit is a silent False.

    Non-matching pathspecs are tolerated: each pathspec is added in its
    own unchecked `git add -A` (git aborts the whole add when any one
    pathspec matches nothing); the staged-diff probe decides whether a
    commit happens at all.
    """
    for spec in pathspecs:
        subprocess.run(
            ["git", "-C", str(path), "add", "-A", "--", spec],
            capture_output=True,
        )
    if not git_has_staged(path):
        return False
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", message],
        check=True,
        env=_git_commit_env(),
    )
    return True


# --- Scaffold pass ------------------------------------------------------

def scaffold_dry_run_plan(store, template_dir, issue_template):
    """Read-only convergence plan for --dry-run. Changes nothing.

    Returns the same record shape the real pass returns (IM-240), with
    ``dry_run`` true, so `--json` output is one shape across both runs
    and the human emitter below is the only place the wording differs.
    """
    store_created = not store.exists()
    git_initialized = False
    if store_created:
        git_initialized = True
    elif not is_git_repo(store):
        if not dir_is_empty(store):
            die(
                f"{store} is not a git repo and is not empty; refusing to "
                "scaffold over existing content",
                reason="non-empty-target", store=str(store),
            )
        git_initialized = True
    plan = [(name, template_dir / name) for name in SCAFFOLD_FILES]
    plan += [(".gitignore", None), ("INDEX.md", None),
             ("ISSUE-TEMPLATE.md", issue_template)]
    results = [
        (name, "present" if (store / name).exists() else "seeded")
        for name, _src in plan
    ]
    seeded = [n for n, a in results if a == "seeded"]
    return {
        "store": str(store),
        "dry_run": True,
        "store_created": store_created,
        "git_initialized": git_initialized,
        "seeded": seeded,
        "present": [n for n, a in results if a == "present"],
        "committed": bool(seeded),
        "_results": results,
    }


def emit_dry_run_report(record):
    """Print the --dry-run convergence report (human mode)."""
    store = record["store"]
    print(f"store: {store}")
    if record["store_created"]:
        print(f"  would create: {store}")
    if record["git_initialized"]:
        print("  would run: git init -b main")
    for name, action in record["_results"]:
        if action == "present":
            print(f"  present: {name}")
        else:
            print(f"  would seed: {name}")
    if record["committed"]:
        print("  would commit: chore: scaffold issue store")


def emit_scaffold_report(record):
    """Print the real scaffold pass's report (human mode)."""
    print(f"store: {record['store']}")
    for name, action in record["_results"]:
        print(f"  {action}: {name}")
    if record["git_initialized"]:
        print("  initialized: .git (branch main)")
    if record["committed"]:
        print("  committed: chore: scaffold issue store")


def emit_json(record):
    """One JSON document on stdout; no human lines ride along (IM-144)."""
    json.dump(
        {k: v for k, v in record.items() if not k.startswith("_")},
        sys.stdout,
        indent=2,
        sort_keys=False,
    )
    sys.stdout.write("\n")


def scaffold(store, template_dir, issue_template):
    """The scaffold/convergence pass. Never overwrites."""
    # --- Step 1: ensure target directory ---
    store_created = False
    if store.exists():
        if not store.is_dir():
            die(f"{store} exists and is not a directory",
                reason="not-a-directory", store=str(store))
        if not is_git_repo(store) and not dir_is_empty(store):
            die(
                f"{store} is not a git repo and is not empty; refusing to "
                "scaffold over existing content",
                reason="non-empty-target", store=str(store),
            )
    else:
        store.mkdir(parents=True)
        store_created = True

    # --- Step 2: git init if needed ---
    git_initialized = False
    if not is_git_repo(store):
        git_init(store)
        git_initialized = True

    # --- Step 3: seed scaffold files (idempotent, never overwrite) ---
    results = []
    for name in SCAFFOLD_FILES:
        action = copy_file(template_dir / name, store / name)
        results.append((name, action))
    results.append((".gitignore", seed_file(store / ".gitignore", GITIGNORE_BODY)))
    results.append(("INDEX.md", seed_file(store / "INDEX.md", INDEX_BODY)))
    results.append(("ISSUE-TEMPLATE.md",
                    copy_file(issue_template, store / "ISSUE-TEMPLATE.md")))

    # --- Step 4: commit any newly seeded files ---
    seeded_names = [n for n, a in results if a == "seeded"]
    committed = False
    if seeded_names:
        committed = git_stage_and_commit(
            store,
            seeded_names,
            "chore: scaffold issue store",
        )

    # --- Step 5: structured record (rendered by the caller) ---
    return {
        "store": str(store),
        "dry_run": False,
        "store_created": store_created,
        "git_initialized": git_initialized,
        "seeded": seeded_names,
        "present": [n for n, a in results if a == "present"],
        "committed": committed,
        "_results": results,
    }


# --- consolidated `issue admin init-store` dispatcher glue ------
#
# The scaffold/convergence body above is the v3.0.0 dispatcher port of the
# flat binary, unchanged; IM-272 moved the arg surface onto a normal
# argparse subparser (the passthrough catch-all and the __doc__ help it
# preserved are retired, so --help is generated from the registrations).

VERB = "init-store"
TIER = "admin"
SUMMARY = "scaffold a new issue store / converge an existing one"
ORDER = 10

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "scaffolding"
# IM-287: the destination is named --store, the spelling every verb uses
# for a store root. "flag" rather than resolution-chain semantics: the
# scaffolder requires the flag and never falls through the chain.
SURFACE_STORE = "flag"
# IM-260 C3: every failure path carries the branchable payload (the
# `die` refusals raise `PreconditionError` and `run` renders them;
# argparse refusals, including the required --store, ride
# `common.structured_parser_errors`). Ratchets: may never revert.
SURFACE_STRUCTURED_ERRORS = "converted"


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--store", required=True, metavar="ROOT",
                   help="Destination store root to scaffold or converge "
                        "(required; the scaffolder never falls through the "
                        "store-resolution chain).")
    p.add_argument("--non-interactive", action="store_true",
                   help="Never prompt.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report planned actions; change nothing.")
    common.add_json_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
    p.set_defaults(_run=run)
    return p


def run(args):
    try:
        store = Path(args.store).expanduser().resolve()

        prefix = script_prefix()
        template_dir = resolve_store_template_dir(prefix)
        issue_template = resolve_issue_template(prefix)

        if args.dry_run:
            record = scaffold_dry_run_plan(store, template_dir, issue_template)
            emitter = emit_dry_run_report
        else:
            record = scaffold(store, template_dir, issue_template)
            emitter = emit_scaffold_report

        if getattr(args, "json_mode", False):
            emit_json(record)
        else:
            emitter(record)
    except errors.ContractError as e:
        common.emit_error(e, prog=PROG,
                          json_mode=bool(getattr(args, "json_mode", False)))
        return e.exit_code
    except SystemExit as e:  # legacy in-place exits
        rc = e.code
        if rc is None:
            return 0
        return rc if isinstance(rc, int) else 1
    return 0
