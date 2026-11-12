"""issue admin migrate -- copy a project's .issues/ corpus into the issue store.

Synopsis:
  issue admin migrate --project PATH [--store ROOT] [--prefix PREFIX]
                      [--repo NAME] [--dry-run] [--json]
                      [--lock-timeout SECONDS] [--message MSG]
  issue admin migrate [-h | --help]
  issue admin migrate [--version]

What it does:
  Copies every `<PREFIX>-NNN.md` issue file from `<project>/.issues/`
  into `<store>/<PREFIX>/`, plus optional `.prefix` and
  `ISSUE-TEMPLATE.md` companions if present. Validates every issue
  file before writing anything.

  On a successful apply, the tool drives two store-side git commits:
    1. The migration commit -- the migrated issue files + companions.
    2. A `chore: record .migrations entry` commit -- appends one TSV
       line to `<store>/.migrations` capturing the source repo's
       canonical path, its pre-migration HEAD sha, the migration
       commit's sha, and an ISO 8601 timestamp.
  Both commits are path-restricted; any other dirty state in the
  store working tree is preserved untouched.

  Last, the tool writes `<project>/.issues/.migrated-to-store`
  (containing the store-root path) so any failure before that point
  leaves the project unmigrated and safe to re-run.

Flags:
  --project PATH        Project root (the directory that contains
                        `.issues/`). Required.
  --store ROOT          Override store-root resolution. Otherwise
                        resolves from $ISSUE_STORE_ROOT, then
                        ~/.config/issue-mgmt/config (key store_root).
  --prefix PREFIX       Override the prefix. Otherwise resolves from
                        `<project>/.issues/.prefix`, then from
                        `<store>/.repos` keyed by the project's
                        basename (or --repo). Hard-fails on mismatch
                        when more than one source disagrees.
  --repo NAME           Override the basename used for registry
                        lookup. Defaults to `basename(<project>)`.
  --dry-run             Print the plan; write nothing, commit nothing.
  --json                Emit a single JSON object on stdout (plan +
                        result). Default output is a human-readable
                        plan summary.
  --lock-timeout SECONDS
                        Per-issue lock acquisition timeout. Default 5s.
  --message MSG         Override the auto-generated migration commit
                        message. Default is
                        `feat: migrate <repo> to store (<PFX>-MIN..<PFX>-MAX)`.

Idempotency:
  If `<project>/.issues/.migrated-to-store` already exists, exit 0
  with `already migrated; sentinel at <path>`. The sentinel is the
  single source of truth for "migrated" -- absent means not migrated.

  If the sentinel is absent but issue files for the prefix already
  exist in the store (signature of a prior crash between the
  migration commit and the sentinel write), the tool refuses with
  `partial prior migration detected` so a re-run cannot double-commit.

Cross-reference sweep:
  After copying, scan every migrated file for `<PFX>-NNN` tokens
  and check that each resolves to a file in the store. Unresolved
  tokens are warnings, not errors (cross-project refs to
  not-yet-migrated projects are expected during the migration
  window).

Concurrency:
  Each issue copy acquires the per-issue advisory lock at
  `<store>/<PREFIX>/.locks/<ID>.lock`. Lock-acquisition timeout
  exits 3 (`concurrent-edit`; was 17 through v2.4.1). Companion-file copies (.prefix,
  ISSUE-TEMPLATE.md) are unlocked -- they are not concurrent-write
  targets.

Exit codes:
  0   success, including the already-migrated no-op
  1   precondition failed (no .issues/ dir, no prefix resolvable,
      prefix mismatch, store root missing, validation errors,
      sha256 mismatch after copy, partial prior migration, source
      repo not a git repo or no HEAD, git commit failure)
  2   argument / usage error
  3   concurrent-edit (per-issue lock acquisition timeout)

Examples:
  issue admin migrate --project ~/projects/myrepo --dry-run
  ISSUE_STORE_ROOT=~/issues-store \\
      issue admin migrate --project ~/projects/myrepo
  issue admin migrate --project /path/to/repo --prefix MP --json
  issue admin migrate --project /path/to/repo \\
      --message "feat: migrate foo to store (FOO-001..FOO-042)"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import concurrency  # noqa: E402
import errors  # noqa: E402
import migrate as migrate_lib  # noqa: E402

VERSION = "v3.4.1"

# The one place this verb names itself. Every diagnostic, usage line,
# and `prog=` keyword formats through it, so the surface manifest sees
# exactly one name per verb and `surface.self-naming` is checkable
# rather than a ~90-site grep (IM-239).
PROG = "issue admin migrate"

CONFIG_PATH = Path.home() / ".config" / "issue-mgmt" / "config"


def resolve_canonical_template() -> Path | None:
    """Locate the Homebrew/dev generic ISSUE-TEMPLATE.md.

    Order, matching new-issue's fallback chain (Homebrew first, dev second):
      1. <prefix>/share/issue-mgmt/ISSUE-TEMPLATE.md  -- Homebrew install
      2. <prefix>/ISSUE-TEMPLATE.md                   -- dev source
    where PREFIX = dirname(dirname(realpath($0))).

    Returns the first existing path, or None when neither is present.
    The store-root override (<store>/ISSUE-TEMPLATE.md) is resolved
    inside `migrate.plan_migration` and takes precedence over this.
    """
    prefix = Path(os.path.realpath(sys.argv[0])).parent.parent
    for candidate in (
        prefix / "share" / "issue-mgmt" / "ISSUE-TEMPLATE.md",
        prefix / "ISSUE-TEMPLATE.md",
    ):
        if candidate.is_file():
            return candidate
    return None


def build_parser():
    """The verb's real argparse parser.

    Split out of `parse_args` for the surface manifest (IM-245): the
    dispatcher subparser for this verb is a `prefix_chars="\\x00"`
    passthrough, so an argparse walk over it sees none of these flags.
    `SURFACE_PARSER` below points the extractor here instead of letting it
    emit a false-CLEAN empty flag set.
    """
    p = argparse.ArgumentParser(prog=PROG, add_help=False)
    p.add_argument("--project", metavar="PATH", default=None)
    p.add_argument("--store", metavar="ROOT", default=None)
    p.add_argument("--prefix", metavar="PREFIX", default=None)
    p.add_argument("--repo", metavar="NAME", default=None)
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--json", action="store_true", dest="json_mode")
    # Hand-rolled parser (add_help=False + a hand-written usage string), so
    # common.add_lock_timeout_flag's help= would never render and importing
    # `common` here buys nothing. Registered locally with the same name and
    # meaning: the per-record advisory lock (IM-242). The hand-rolled-parser
    # question itself is IM-239 / IM-245.
    p.add_argument(
        "--lock-timeout",
        type=float,
        default=concurrency.DEFAULT_LOCK_TIMEOUT,
        metavar="SECONDS",
    )
    p.add_argument("--message", metavar="MSG", default=None,
                   help="Override the migration commit message")
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    return p


def parse_args(argv):
    return build_parser().parse_args(argv)


def show_help():
    sys.stdout.write(__doc__.lstrip())
    sys.exit(0)


def read_config_store_root():
    if not CONFIG_PATH.is_file():
        return None
    try:
        for raw in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "store_root":
                return value.strip() or None
    except OSError:
        return None
    return None


def resolve_store_root(cli_store):
    if cli_store:
        return Path(cli_store).expanduser()
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return Path(env).expanduser()
    cfg = read_config_store_root()
    if cfg:
        return Path(cfg).expanduser()
    return None


def emit_human_plan(plan: migrate_lib.MigrationPlan, *, dry_run: bool):
    header = "Plan (dry-run):" if dry_run else "Plan:"
    print(header)
    print(f"  project:       {plan.project_path}")
    print(f"  anchor:        {plan.anchor}")
    print(f"  store:         {plan.store_root}")
    if plan.sentinel_store_path != plan.store_root:
        # Surface the symlink-preserved payload when it differs from the
        # resolved store path (e.g. a symlinked ~/store -> /mnt/store).
        print(f"  sentinel payload: {plan.sentinel_store_path}")
    print(f"  prefix:        {plan.prefix}")
    print(f"  target dir:    {plan.target_dir}")
    print(f"  sentinel:      {plan.sentinel_path}")
    if plan.registry_present:
        print(f"  registry:      present ({plan.registry_prefix})")
    else:
        print("  registry:      ABSENT "
              "(run `issue admin init-project` after migration)")
    if plan.template_action == migrate_lib.TEMPLATE_ACTION_ABSENT:
        print("  template:      absent in source (none to copy)")
    elif plan.template_action == migrate_lib.TEMPLATE_ACTION_SKIP_CANONICAL:
        local_sha = (plan.template_local_sha or "")[:8]
        print(
            f"  template:      skipped -- matches canonical at "
            f"{plan.template_canonical_path} (sha={local_sha})"
        )
    else:  # TEMPLATE_ACTION_COPY
        local_sha = (plan.template_local_sha or "")[:8]
        if plan.template_canonical_path is not None:
            canonical_sha = (plan.template_canonical_sha or "")[:8]
            print(
                f"  template:      copied as override (local sha={local_sha} "
                f"differs from canonical sha={canonical_sha} at "
                f"{plan.template_canonical_path})"
            )
        else:
            print(
                f"  template:      copied as override (sha={local_sha}; "
                f"no canonical found for comparison)"
            )
    print(f"  issue copies:  {len(plan.issue_copies)}")
    for op in plan.issue_copies:
        print(f"    {op.source.name}  ->  {op.dest}")
    if plan.companion_copies:
        print(f"  companions:    {len(plan.companion_copies)}")
        for op in plan.companion_copies:
            print(f"    {op.source.name}  ->  {op.dest}")
    if plan.extraneous_files:
        print(f"  extraneous (skipped): {len(plan.extraneous_files)}")
        for p in plan.extraneous_files:
            print(f"    {p}")


def emit_human_result(result: migrate_lib.MigrationResult):
    if result.dry_run:
        print(
            f"  cross-ref scan (sources): "
            f"{result.cross_ref_resolved_count} resolved, "
            f"{result.cross_ref_unresolved_count} unresolved"
        )
        for w in result.cross_ref_warnings:
            print(f"    warning: {w}")
        print("  no files written (dry-run).")
        return
    print(f"  copies executed:   {len(result.copies_succeeded)}")
    if result.migration_commit_sha:
        print(
            f"  migration commit:  {result.migration_commit_sha[:12]} "
            f"{result.migration_commit_message or ''}"
        )
    if result.migrations_commit_sha:
        print(
            f"  .migrations line:  {result.migrations_line}"
        )
        print(
            f"  .migrations commit: {result.migrations_commit_sha[:12]}"
        )
    print(
        f"  cross-ref scan:    "
        f"{result.cross_ref_resolved_count} resolved, "
        f"{result.cross_ref_unresolved_count} unresolved"
    )
    for w in result.cross_ref_warnings:
        print(f"    warning: {w}")
    if result.sentinel_written:
        print(f"  sentinel written:  {result.plan.sentinel_path}")
    else:
        print("  sentinel NOT written.")


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.help:
        show_help()
    if args.version:
        print(VERSION)
        return 0

    if not args.project:
        print(f"{PROG}: --project PATH is required", file=sys.stderr)
        return 2

    project_path = Path(args.project).expanduser().resolve()
    if not project_path.is_dir():
        print(
            f"{PROG}: project path is not a directory: {project_path}",
            file=sys.stderr,
        )
        return 1

    store_root = resolve_store_root(args.store)
    if store_root is None:
        print(
            f"{PROG}: no store root resolved; set ISSUE_STORE_ROOT, "
            "pass --store, or configure ~/.config/issue-mgmt/config",
            file=sys.stderr,
        )
        return 1

    try:
        plan = migrate_lib.plan_migration(
            project_path,
            store_root,
            prefix_override=args.prefix,
            repo_name_override=args.repo,
            canonical_template_path=resolve_canonical_template(),
        )
    except migrate_lib.AlreadyMigrated as e:
        if args.json_mode:
            json.dump(
                {
                    "already_migrated": True,
                    "sentinel_path": str(e.sentinel_path),
                    "sentinel_content": e.content,
                },
                sys.stdout,
                indent=2,
            )
            sys.stdout.write("\n")
        else:
            print(f"already migrated; sentinel at {e.sentinel_path}")
        return 0
    except migrate_lib.MigrationError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 1

    if plan.has_validation_errors:
        print(
            f"{PROG}: validation errors -- no files written.",
            file=sys.stderr,
        )
        for r in plan.validation_results:
            if r.ok:
                continue
            for v in r.violations:
                print(f"  {v.format(r.path)}", file=sys.stderr)
        if args.json_mode:
            json.dump(
                {"plan": plan.to_dict(), "validation_failed": True},
                sys.stdout,
                indent=2,
            )
            sys.stdout.write("\n")
        return 1

    try:
        result = migrate_lib.execute_migration(
            plan,
            dry_run=args.dry_run,
            lock_timeout=args.lock_timeout,
            commit_message=args.message,
        )
    except errors.ContractError as e:
        print(e.format_stderr(PROG), file=sys.stderr)
        return e.exit_code
    except migrate_lib.MigrationError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 1

    if args.json_mode:
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        emit_human_plan(plan, dry_run=args.dry_run)
        emit_human_result(result)

    return 0



# --- consolidated `issue admin migrate` dispatcher glue ------
#
# Dispatcher port, not a rewrite (design section 6): the entire flat-binary
# body above is unchanged except for the sibling-path resolution fixes noted
# inline; only the arg-surface glue is new. The subparser uses a null
# prefix_chars + nargs="*" catch-all so the flat binary's own argparse (its
# flags, exit codes, and __doc__ help) stays the single source of truth and
# every byte of --help / --json / exit behavior is preserved.

VERB = "migrate"
TIER = "admin"
SUMMARY = "copy a project's .issues/ corpus into the issue store"
ORDER = 30

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"
# IM-245: this verb's dispatcher subparser is a prefix_chars="\x00"
# passthrough, so point the extractor at the real parser.
SURFACE_PARSER = build_parser


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
