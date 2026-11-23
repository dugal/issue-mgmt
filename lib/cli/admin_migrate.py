"""`issue admin migrate` -- copy a project's .issues/ corpus into the store.

One-shot, idempotent migration of a project's legacy ``.issues/`` corpus
into the central store: validates before writing, verifies byte-identity
on every copy, drives the two store-side commits, and writes the
``.migrated-to-store`` sentinel last. The arg surface is a normal
argparse subparser since IM-272 (the hand-written ``__doc__`` help and
the ``SURFACE_PARSER`` declaration are retired).
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
import storeresolve  # noqa: E402

from . import common

# The one place this verb names itself. Every diagnostic formats through
# it, so the surface manifest sees exactly one name per verb and
# `surface.self-naming` is checkable rather than a ~90-site grep (IM-239).
PROG = "issue admin migrate"

CONFIG_PATH = storeresolve.CONFIG_PATH

_DESCRIPTION = """\
Copy a project's .issues/ corpus into the issue store.

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
leaves the project unmigrated and safe to re-run."""

_EPILOG = """\
Store-root resolution (documented deviation from the shared chain):
  Migration targets a project that is not yet migrated, so there is no
  sentinel leg by construction; after --store and ISSUE_STORE_ROOT it
  falls through to ~/.config/issue-mgmt/config (key store_root).

Prefix resolution when --prefix is not given:
  `<project>/.issues/.prefix`, then `<store>/.repos` keyed by the
  project's basename (or --repo). Hard-fails on mismatch when more than
  one source disagrees.

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
  exits 3 (`concurrent-edit`; was 17 through v2.4.1). Companion-file
  copies (.prefix, ISSUE-TEMPLATE.md) are unlocked -- they are not
  concurrent-write targets.

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


def read_config_store_root():
    """Shared config reader (``storeresolve``, IM-280); None when absent."""
    return storeresolve.read_config_store_root()


def resolve_store_root(cli_store):
    """Documented deviation from the shared 4-step chain (IM-280).

    Migration targets a project that is not yet migrated, so there is no
    sentinel leg by construction; after --store and ISSUE_STORE_ROOT it
    falls through to the per-machine config file instead.
    """
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


def run(args):
    json_mode = bool(getattr(args, "json_mode", False))
    project_path = Path(args.project).expanduser().resolve()
    if not project_path.is_dir():
        common.emit_error(
            errors.PreconditionError(
                f"project path is not a directory: {project_path}",
                reason="not-a-directory", path=str(project_path)),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    store_root = resolve_store_root(args.store)
    if store_root is None:
        common.emit_error(
            errors.PreconditionError(
                "no store root resolved; set ISSUE_STORE_ROOT, pass "
                "--store, or configure ~/.config/issue-mgmt/config",
                reason="store-unresolvable"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

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
        common.emit_error(
            errors.PreconditionError(str(e), reason="migration-failed"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    if plan.has_validation_errors:
        common.emit_error(
            errors.PreconditionError(
                "validation errors -- no files written.",
                reason="validation-failed"),
            prog=PROG, json_mode=json_mode)
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
        common.emit_error(e, prog=PROG, json_mode=json_mode)
        return e.exit_code
    except migrate_lib.MigrationError as e:
        common.emit_error(
            errors.PreconditionError(str(e), reason="migration-failed"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    if args.json_mode:
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        emit_human_plan(plan, dry_run=args.dry_run)
        emit_human_result(result)

    return 0



# --- consolidated `issue admin migrate` dispatcher glue ------
#
# The migration body above is the v3.0.0 dispatcher port of the flat
# binary, unchanged; IM-272 moved the arg surface onto a normal argparse
# subparser (the passthrough catch-all and the __doc__ help it preserved
# are retired, so --help is generated from the registrations) and
# re-attached the shared flags to their `common.add_*` helpers.

VERB = "migrate"
TIER = "admin"
SUMMARY = "copy a project's .issues/ corpus into the issue store"
ORDER = 30

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
SURFACE_STORE = "flag"
# IM-260 C3: every failure path carries the branchable payload (bad
# project path, unresolvable store, every MigrationError, the
# validation-errors abort; argparse refusals ride
# `common.structured_parser_errors`; the locked-copy conditions were
# already structured). Ratchets: may never revert.
SURFACE_STRUCTURED_ERRORS = "converted"


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--project", required=True, metavar="PATH",
                   help="Project root (the directory that contains "
                        ".issues/). Required.")
    common.add_store_flag(p)
    # Registered locally, single-valued: a project migrates under exactly
    # one prefix (the registry.basename-prefix-unique licence in
    # docs/cli-vocabulary.json), so this deliberately does NOT use the
    # repeatable add_prefix_scope_flag the read verbs share.
    p.add_argument("--prefix", metavar="PREFIX", default=None,
                   help="Override the prefix resolved from .issues/.prefix "
                        "or the store registry.")
    p.add_argument("--repo", metavar="NAME", default=None,
                   help="Override the basename used for registry lookup.")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Print the plan; write nothing, commit nothing.")
    common.add_json_flag(p)
    common.add_lock_timeout_flag(p, default=concurrency.DEFAULT_LOCK_TIMEOUT)
    p.add_argument("--message", metavar="MSG", default=None,
                   help="Override the migration commit message.")
    common.add_version_flag(p)
    common.structured_parser_errors(p)
    p.set_defaults(_run=run)
    return p
