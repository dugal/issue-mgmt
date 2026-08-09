"""
init-issue-store -- scaffold a new issues store / converge an existing one

Synopsis:
  init-issue-store <path> [--non-interactive]
                          [--dry-run]
                          [-h | --help] [--version]

Creates a fresh issues store at <path>: makes the directory, initializes
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
the default template that ships with this release.

Options:
  --non-interactive  Never prompt.
  --dry-run          Report every scaffold action that would run and
                     change nothing.

Resource resolution:
  Scaffold sources (README.md, LAYOUT.md) live under
    <PREFIX>/share/issue-mgmt/store-template/   (Homebrew install)
    <PREFIX>/share/store-template/              (dev fallback)
  where PREFIX = dirname(dirname(realpath($0))).

  ISSUE-TEMPLATE.md is resolved via the same precedence as `new-issue`:
    <PREFIX>/share/issue-mgmt/ISSUE-TEMPLATE.md (Homebrew install)
    <PREFIX>/ISSUE-TEMPLATE.md                  (dev fallback)

Exit codes:
  0  success (including idempotent re-run on an existing store)
  1  precondition failed (non-empty non-git path, missing template
     resources)
  2  argument/usage error (missing <path>)

Examples:
  init-issue-store ~/issues-store
  init-issue-store /tmp/new-store --non-interactive
  init-issue-store --dry-run ~/issues-store
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "v3.3.0"

GITIGNORE_BODY = ".locks/\n.DS_Store\n"
INDEX_BODY = ""  # reindex-issues populates on first run

SCAFFOLD_FILES = ("README.md", "LAYOUT.md")


def die(msg, code=1):
    print(f"init-issue-store: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="init-issue-store",
        description="Scaffold a new issues store / converge an existing one.",
        add_help=False,
    )
    p.add_argument("path", nargs="?", help="Destination store root.")
    p.add_argument("--non-interactive", action="store_true",
                   help="Never prompt.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report planned actions; change nothing.")
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    return p.parse_args(argv)


def show_help():
    sys.stdout.write(__doc__.lstrip())
    sys.exit(0)


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
        "could not locate store-template scaffolds; looked in:\n  "
        + "\n  ".join(str(c) for c in candidates)
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
        "could not locate ISSUE-TEMPLATE.md; looked in:\n  "
        + "\n  ".join(str(c) for c in candidates)
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

def scaffold_dry_run_report(store, template_dir, issue_template):
    """Read-only convergence report for --dry-run: what would be
    seeded vs what is already present. Changes nothing.
    """
    print(f"store: {store}")
    if not store.exists():
        print(f"  would create: {store}")
        print("  would run: git init -b main")
    elif not is_git_repo(store):
        if not dir_is_empty(store):
            die(
                f"{store} is not a git repo and is not empty; refusing to "
                "scaffold over existing content"
            )
        print("  would run: git init -b main")
    plan = [(name, template_dir / name) for name in SCAFFOLD_FILES]
    plan += [(".gitignore", None), ("INDEX.md", None),
             ("ISSUE-TEMPLATE.md", issue_template)]
    would_seed = False
    for name, _src in plan:
        if (store / name).exists():
            print(f"  present: {name}")
        else:
            print(f"  would seed: {name}")
            would_seed = True
    if would_seed:
        print("  would commit: chore: scaffold issue store")


def scaffold(store, template_dir, issue_template):
    """The scaffold/convergence pass. Never overwrites."""
    # --- Step 1: ensure target directory ---
    if store.exists():
        if not store.is_dir():
            die(f"{store} exists and is not a directory")
        if not is_git_repo(store) and not dir_is_empty(store):
            die(
                f"{store} is not a git repo and is not empty; refusing to "
                "scaffold over existing content"
            )
    else:
        store.mkdir(parents=True)

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

    # --- Step 5: report ---
    print(f"store: {store}")
    for name, action in results:
        print(f"  {action}: {name}")
    if git_initialized:
        print("  initialized: .git (branch main)")
    if committed:
        print("  committed: chore: scaffold issue store")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    if args.help:
        show_help()
    if args.version:
        print(VERSION)
        sys.exit(0)

    if not args.path:
        die("missing required <path> argument; see --help", code=2)

    store = Path(args.path).expanduser().resolve()

    prefix = script_prefix()
    template_dir = resolve_store_template_dir(prefix)
    issue_template = resolve_issue_template(prefix)

    if args.dry_run:
        scaffold_dry_run_report(store, template_dir, issue_template)
    else:
        scaffold(store, template_dir, issue_template)



# --- consolidated `issue admin init-store` dispatcher glue ------
#
# Dispatcher port, not a rewrite (design section 6): the entire flat-binary
# body above is unchanged except for the sibling-path resolution fixes noted
# inline; only the arg-surface glue is new. The subparser uses a null
# prefix_chars + nargs="*" catch-all so the flat binary's own argparse (its
# flags, exit codes, and __doc__ help) stays the single source of truth and
# every byte of --help / --json / exit behavior is preserved.

VERB = "init-store"
TIER = "admin"
SUMMARY = "scaffold a new issue store / converge an existing one"
ORDER = 10


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, add_help=False, prefix_chars="\x00"
    )
    p.add_argument("rest", nargs="*")
    p.set_defaults(_run=run)
    return p


def run(args):
    try:
        rc = main(args.rest)
    except SystemExit as e:  # flat main()/die()/show_help() exit in place
        rc = e.code
    if rc is None:
        return 0
    return rc if isinstance(rc, int) else 1
