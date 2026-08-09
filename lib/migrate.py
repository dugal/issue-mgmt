"""migrate -- copy a project's .issues/ corpus into the issue store.

Read-modify-write copy from `<project>/.issues/` into
`<store>/<PREFIX>/`. Steps:

  1. Resolve project anchor, store root, and prefix. Cross-validate
     `<anchor>/.prefix` against the registry when both exist.
  2. Enumerate source files: every `<PREFIX>-NNN.md` issue file
     (skips `INDEX.md`, `.migrated-to-store`, `.locks/`, hidden files).
     Optional companions (`.prefix`, `ISSUE-TEMPLATE.md`) ride along if
     present in the source.
  3. Validate every issue file via `lib/validate.validate_file`. Any
     violation aborts the migration -- nothing is written to the store.
  4. Build a `MigrationPlan` (paths, copies, lock paths, validation
     results, extraneous-file warnings).
  5. `execute_migration(plan)` copies each file via the
     concurrency primitives: per-issue advisory lock + atomic write,
     then sha256 comparison between source and destination as the
     "byte-identical" acceptance gate. The sentinel
     `<anchor>/.migrated-to-store` is written last so the migration is
     atomic at the project level (any failure leaves the sentinel
     absent and a re-run is safe).
  6. Cross-reference sweep: scan every migrated file for `<PFX>-NNN`
     tokens; resolved tokens are counted, unresolved tokens are
     recorded as warnings (not aborts) because cross-project refs to
     not-yet-migrated projects are expected during the migration
     window.

Public surface:

    from migrate import (
        AlreadyMigrated,
        CopyOp,
        MigrationError,
        MigrationPlan,
        MigrationResult,
        execute_migration,
        is_migrated,
        plan_migration,
        scan_cross_references,
    )

The CLI lives in `scripts/migrate-to-store`. Stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Late import: lib/concurrency.py and lib/validate.py are sibling
# modules under `lib/`. Callers (the CLI script and the test suite)
# both put `lib/` on sys.path before importing this module.
import concurrency
import errors  # type: ignore  # noqa: E402
import idresolve  # type: ignore  # noqa: E402
import validate  # type: ignore  # noqa: E402


# Aliased to the single shared grammar in lib/idresolve.py.
PREFIX_RE = idresolve.PREFIX_RE
ID_RE = idresolve.ID_RE
ID_TOKEN_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,7})-(\d+)\b")
SENTINEL_NAME = ".migrated-to-store"
REGISTRY_NAME = ".repos"
TEMPLATE_NAME = "ISSUE-TEMPLATE.md"
MIGRATIONS_NAME = ".migrations"

# Optional companion files that ride along with the issue corpus when
# present, but are not validated as issues. ISSUE-TEMPLATE.md is handled
# separately (de-dup) and is NOT in this list.
COMPANION_FILES = (".prefix",)
# Files we recognize but never copy.
SKIP_FILES = ("INDEX.md", ".migrated-to-store")

# Template action enum.
TEMPLATE_ACTION_COPY = "copy"                    # local diverges from canonical -> per-prefix override
TEMPLATE_ACTION_SKIP_CANONICAL = "skip-canonical"  # local matches canonical -> dead duplicate, skip
TEMPLATE_ACTION_ABSENT = "absent"                # no local template -> nothing to copy


def compute_sentinel_store_path(store_root) -> Path:
    """Compute the symlink-preserved store path written into `.migrated-to-store`.

    The sentinel payload uses `expanduser().absolute()` (NOT
    `.resolve()`) so the routing primitive stays self-sufficient when the
    underlying mount disappears or maps differently on another machine.
    Reused by `init-issue-project` so the rule lives in one place.
    """
    return Path(store_root).expanduser().absolute()


class MigrationError(Exception):
    """Pre-flight failure that aborts the migration before any writes."""


class AlreadyMigrated(Exception):
    """Sentinel already present. Carries the sentinel path for messaging."""

    def __init__(self, sentinel_path: Path, content: str):
        super().__init__(f"already migrated; sentinel at {sentinel_path}")
        self.sentinel_path = sentinel_path
        self.content = content


@dataclass(frozen=True)
class CopyOp:
    """One source -> dest copy. Issue copies carry a lock path."""

    source: Path
    dest: Path
    kind: str  # "issue" | "companion"
    issue_id: Optional[str] = None
    lock_path: Optional[Path] = None

    def to_dict(self) -> Dict[str, object]:
        d: Dict[str, object] = {
            "source": str(self.source),
            "dest": str(self.dest),
            "kind": self.kind,
        }
        if self.issue_id is not None:
            d["issue_id"] = self.issue_id
        if self.lock_path is not None:
            d["lock_path"] = str(self.lock_path)
        return d


@dataclass
class MigrationPlan:
    project_path: Path
    anchor: Path  # <project>/.issues
    store_root: Path  # symlink-resolved; used for filesystem and git operations
    sentinel_store_path: Path  # symlink-preserved; payload written into .migrated-to-store
    prefix: str
    sentinel_path: Path
    target_dir: Path  # <store>/<prefix>
    issue_copies: List[CopyOp] = field(default_factory=list)
    companion_copies: List[CopyOp] = field(default_factory=list)
    validation_results: List[validate.ValidationResult] = field(default_factory=list)
    extraneous_files: List[Path] = field(default_factory=list)
    registry_present: bool = False
    registry_prefix: Optional[str] = None
    template_action: str = TEMPLATE_ACTION_ABSENT
    template_local_path: Optional[Path] = None
    template_canonical_path: Optional[Path] = None
    template_local_sha: Optional[str] = None
    template_canonical_sha: Optional[str] = None

    @property
    def copies(self) -> List[CopyOp]:
        return list(self.issue_copies) + list(self.companion_copies)

    @property
    def has_validation_errors(self) -> bool:
        return any(not r.ok for r in self.validation_results)

    def to_dict(self) -> Dict[str, object]:
        return {
            "project_path": str(self.project_path),
            "anchor": str(self.anchor),
            "store_root": str(self.store_root),
            "sentinel_store_path": str(self.sentinel_store_path),
            "prefix": self.prefix,
            "sentinel_path": str(self.sentinel_path),
            "target_dir": str(self.target_dir),
            "issue_copies": [c.to_dict() for c in self.issue_copies],
            "companion_copies": [c.to_dict() for c in self.companion_copies],
            "extraneous_files": [str(p) for p in self.extraneous_files],
            "validation_errors": [
                r.to_dict() for r in self.validation_results if not r.ok
            ],
            "registry_present": self.registry_present,
            "registry_prefix": self.registry_prefix,
            "template": {
                "action": self.template_action,
                "local_path": (
                    str(self.template_local_path)
                    if self.template_local_path is not None
                    else None
                ),
                "canonical_path": (
                    str(self.template_canonical_path)
                    if self.template_canonical_path is not None
                    else None
                ),
                "local_sha": self.template_local_sha,
                "canonical_sha": self.template_canonical_sha,
            },
        }


@dataclass
class MigrationResult:
    plan: MigrationPlan
    dry_run: bool
    executed: bool
    copies_succeeded: List[CopyOp] = field(default_factory=list)
    sentinel_written: bool = False
    cross_ref_warnings: List[str] = field(default_factory=list)
    cross_ref_resolved_count: int = 0
    cross_ref_unresolved_count: int = 0
    source_pre_migration_sha: Optional[str] = None
    migration_commit_sha: Optional[str] = None
    migrations_commit_sha: Optional[str] = None
    migrations_line: Optional[str] = None
    canonical_source_path: Optional[str] = None
    migration_commit_message: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "plan": self.plan.to_dict(),
            "dry_run": self.dry_run,
            "executed": self.executed,
            "copies_succeeded": [c.to_dict() for c in self.copies_succeeded],
            "sentinel_written": self.sentinel_written,
            "cross_ref_warnings": list(self.cross_ref_warnings),
            "cross_ref_resolved_count": self.cross_ref_resolved_count,
            "cross_ref_unresolved_count": self.cross_ref_unresolved_count,
            "source_pre_migration_sha": self.source_pre_migration_sha,
            "migration_commit_sha": self.migration_commit_sha,
            "migrations_commit_sha": self.migrations_commit_sha,
            "migrations_line": self.migrations_line,
            "canonical_source_path": self.canonical_source_path,
            "migration_commit_message": self.migration_commit_message,
        }


def is_migrated(anchor: Path) -> bool:
    return (Path(anchor) / SENTINEL_NAME).is_file()


def read_sentinel(anchor: Path) -> str:
    return (Path(anchor) / SENTINEL_NAME).read_text(encoding="utf-8").strip()


def _read_prefix_file(anchor: Path) -> Optional[str]:
    pf = Path(anchor) / ".prefix"
    if not pf.is_file():
        return None
    val = pf.read_text(encoding="utf-8").strip()
    return val or None


def _read_registry(store_root: Path) -> Dict[str, str]:
    """Return {basename: prefix} from <store>/.repos. Empty dict if absent."""
    reg = Path(store_root) / REGISTRY_NAME
    if not reg.is_file():
        return {}
    out: Dict[str, str] = {}
    for raw in reg.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        name, _, prefix = line.partition(":")
        out[name.strip()] = prefix.strip()
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_canonical_template(
    store_root: Path,
    extra_canonical: Optional[Path],
) -> Optional[Path]:
    """Resolve the canonical template used for de-dup comparison.

    Order: `<store>/ISSUE-TEMPLATE.md` (store-wide default) ->
    `extra_canonical` (the Homebrew/dev generic provided by the CLI).
    Returns the first existing path, or `None` when neither is present.
    """
    candidates: List[Path] = [Path(store_root) / TEMPLATE_NAME]
    if extra_canonical is not None:
        candidates.append(Path(extra_canonical))
    for c in candidates:
        if c.is_file():
            return c
    return None


def plan_migration(
    project_path: Path,
    store_root: Path,
    *,
    prefix_override: Optional[str] = None,
    repo_name_override: Optional[str] = None,
    canonical_template_path: Optional[Path] = None,
) -> MigrationPlan:
    """Resolve prefix, enumerate sources, validate. Return a MigrationPlan.

    Does not touch the store. Raises MigrationError for unrecoverable
    pre-flight failures (no .issues/ dir, no prefix derivable, prefix
    mismatch between .prefix and registry, etc.). Raises AlreadyMigrated
    when the sentinel is present.

    `canonical_template_path` is the Homebrew/dev generic ISSUE-TEMPLATE.md
    used as the de-dup comparand when `<store>/ISSUE-TEMPLATE.md` is absent.
    When neither is available, a local `ISSUE-TEMPLATE.md` is
    treated as an override and copied.
    """
    project_path = Path(project_path).resolve()
    # Sentinel payload preserves symlinks so the routing primitive
    # stays self-sufficient when the underlying mount disappears or maps
    # differently on a different machine. `store_root` itself is resolved
    # below for downstream filesystem and git operations.
    sentinel_store_path = compute_sentinel_store_path(store_root)
    store_root = Path(store_root).resolve()
    anchor = project_path / ".issues"

    if not anchor.is_dir():
        raise MigrationError(
            f"project has no .issues/ directory at {anchor}"
        )

    sentinel_path = anchor / SENTINEL_NAME
    if sentinel_path.is_file():
        raise AlreadyMigrated(sentinel_path, sentinel_path.read_text(encoding="utf-8").strip())

    if not store_root.is_dir():
        raise MigrationError(
            f"store root does not exist or is not a directory: {store_root}"
        )

    local_prefix = _read_prefix_file(anchor)
    repo_name = repo_name_override or project_path.name
    registry = _read_registry(store_root)
    registry_prefix = registry.get(repo_name)

    # Prefix resolution: --prefix override > .prefix > registry.
    candidates = [
        ("override", prefix_override),
        (".prefix", local_prefix),
        ("registry", registry_prefix),
    ]
    chosen_prefix: Optional[str] = None
    chosen_source: Optional[str] = None
    for src, val in candidates:
        if val:
            chosen_prefix = val
            chosen_source = src
            break
    if not chosen_prefix:
        raise MigrationError(
            f"no prefix resolvable for {project_path}: pass --prefix, "
            f"or write {anchor / '.prefix'}, or register the project via "
            f"init-issue-project"
        )

    if not PREFIX_RE.match(chosen_prefix):
        raise MigrationError(
            f"prefix {chosen_prefix!r} (from {chosen_source}) does not match "
            f"{PREFIX_RE.pattern}"
        )

    # Cross-validation among the sources that ARE present.
    seen = {src: val for src, val in candidates if val}
    distinct = set(seen.values())
    if len(distinct) > 1:
        details = ", ".join(f"{k}={v!r}" for k, v in seen.items())
        raise MigrationError(
            f"prefix mismatch for {project_path}: {details}"
        )

    target_dir = store_root / chosen_prefix
    issue_copies: List[CopyOp] = []
    companion_copies: List[CopyOp] = []
    extraneous: List[Path] = []
    validation_results: List[validate.ValidationResult] = []
    local_template: Optional[Path] = None

    expected_id_re = re.compile(
        rf"^{re.escape(chosen_prefix)}-\d+$"
    )

    for entry in sorted(anchor.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name in SKIP_FILES:
            continue
        if name.startswith("."):
            # .prefix is a known companion; other hidden files are skipped.
            if name == ".prefix":
                companion_copies.append(CopyOp(
                    source=entry,
                    dest=target_dir / name,
                    kind="companion",
                ))
            continue
        if name == TEMPLATE_NAME:
            # Defer to the de-dup decision below.
            local_template = entry
            continue
        if name in COMPANION_FILES:
            companion_copies.append(CopyOp(
                source=entry,
                dest=target_dir / name,
                kind="companion",
            ))
            continue

        if name.endswith(".md"):
            stem = name[:-3]
            if expected_id_re.match(stem):
                issue_copies.append(CopyOp(
                    source=entry,
                    dest=target_dir / name,
                    kind="issue",
                    issue_id=stem,
                    lock_path=concurrency.issue_lock_path(
                        store_root, chosen_prefix, stem
                    ),
                ))
                validation_results.append(validate.validate_file(entry))
                continue

        extraneous.append(entry)

    # Template de-dup. Compare the project's local template (if any)
    # to the canonical (<store>/ISSUE-TEMPLATE.md, else the Homebrew/dev
    # generic the CLI passed in). Skip the copy when they match -- the
    # canonical is reachable via new-issue's resolution order, so a copy
    # would be a dead duplicate.
    canonical_template = _resolve_canonical_template(
        store_root, canonical_template_path
    )
    template_action = TEMPLATE_ACTION_ABSENT
    template_local_sha: Optional[str] = None
    template_canonical_sha: Optional[str] = None

    if local_template is not None:
        template_local_sha = _sha256(local_template)
        if canonical_template is not None:
            template_canonical_sha = _sha256(canonical_template)
            if template_local_sha == template_canonical_sha:
                template_action = TEMPLATE_ACTION_SKIP_CANONICAL
            else:
                template_action = TEMPLATE_ACTION_COPY
        else:
            template_action = TEMPLATE_ACTION_COPY

        if template_action == TEMPLATE_ACTION_COPY:
            companion_copies.append(CopyOp(
                source=local_template,
                dest=target_dir / TEMPLATE_NAME,
                kind="companion",
            ))

    return MigrationPlan(
        project_path=project_path,
        anchor=anchor,
        store_root=store_root,
        sentinel_store_path=sentinel_store_path,
        prefix=chosen_prefix,
        sentinel_path=sentinel_path,
        target_dir=target_dir,
        issue_copies=issue_copies,
        companion_copies=companion_copies,
        validation_results=validation_results,
        extraneous_files=extraneous,
        registry_present=bool(registry_prefix),
        registry_prefix=registry_prefix,
        template_action=template_action,
        template_local_path=local_template,
        template_canonical_path=canonical_template,
        template_local_sha=template_local_sha,
        template_canonical_sha=template_canonical_sha,
    )


def _run_git(
    cwd: Path,
    args: List[str],
    *,
    capture: bool = True,
) -> str:
    """Run a git command inside `cwd`. Return stdout stripped on success.

    Raises MigrationError with a useful message on non-zero exit. We do
    not shell out; subprocess takes a list.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=capture,
            text=True,
        )
    except FileNotFoundError:
        raise MigrationError("git not installed or not on PATH")
    except subprocess.CalledProcessError as e:
        cmd = " ".join(["git", "-C", str(cwd), *args])
        err = (e.stderr or "").strip() or (e.stdout or "").strip()
        raise MigrationError(f"git failed ({cmd}): {err}")
    return (r.stdout or "").strip()


def capture_source_pre_migration_sha(project_path: Path) -> str:
    """Read source repo HEAD before any in-source side effects.

    Refuses if the source isn't a git repo or has no commits -- both
    cases mean the boundary sha would not be meaningful.
    """
    project_path = Path(project_path)
    if not (project_path / ".git").exists():
        raise MigrationError(
            f"source project is not a git repository: {project_path}"
        )
    return _run_git(project_path, ["rev-parse", "HEAD"])


def _partial_prior_migration_check(plan: MigrationPlan) -> None:
    """Refuse if any destination issue file already exists.

    The sentinel-absent + dest-files-present state is the crash-window
    signature: a previous run made the migration commit but failed
    before writing the source sentinel. Re-running would double-commit
    the same issues. The user resolves manually.
    """
    existing: List[Path] = []
    for op in plan.issue_copies:
        if op.dest.exists():
            existing.append(op.dest)
    if existing:
        listing = "\n  ".join(str(p) for p in existing)
        raise MigrationError(
            "partial prior migration detected: destination files exist "
            "but source sentinel is absent. Resolve manually before "
            f"re-running.\n  {listing}"
        )


def _default_commit_message(plan: MigrationPlan) -> str:
    """Match the established pattern from prior store migrations."""
    repo_name = plan.project_path.name
    ids = sorted(op.issue_id for op in plan.issue_copies if op.issue_id)
    if not ids:
        return f"feat: migrate {repo_name} to store (no issues)"
    if len(ids) == 1:
        return f"feat: migrate {repo_name} to store ({ids[0]})"
    return f"feat: migrate {repo_name} to store ({ids[0]}..{ids[-1]})"


def _git_commit_paths(
    store_root: Path,
    paths: List[Path],
    message: str,
) -> str:
    """Path-restricted stage + commit via the shared helper.

    Acquires the store commit lock, runs the catchup sweep (excluding
    the paths we are about to commit, so the sweep does not steal our
    commit message), then commits the named paths via
    `concurrency.commit_after_write`. Returns the new HEAD sha.

    Wraps `StoreCommitError` and `ConcurrentEdit` as `MigrationError`
    so existing call-sites continue to surface a single exception type.
    The two-commit invariant (migration commit + `.migrations`
    commit) is preserved by calling this helper twice from
    `execute_migration`.
    """
    if not paths:
        raise MigrationError("no paths to commit")
    store_resolved = Path(store_root).resolve()
    rel_paths: List[str] = []
    for p in paths:
        try:
            rel = Path(p).resolve().relative_to(store_resolved)
        except ValueError:
            raise MigrationError(
                f"path {p} is not inside store {store_root}"
            )
        rel_paths.append(str(rel))
    try:
        with concurrency.store_commit_lock(store_root):
            concurrency.catchup_sweep(store_root, exclude=rel_paths)
            sha = concurrency.commit_after_write(
                store_root, rel_paths, message
            )
    except concurrency.StoreCommitError as e:
        raise MigrationError(
            f"store commit failed: {e.git_error or e.message}"
        )
    except errors.ConcurrentEdit as e:
        raise MigrationError(
            f"concurrent store commit -- {e.message}"
        )
    if sha is None:
        raise MigrationError(
            f"commit produced no sha (no staged changes for {rel_paths})"
        )
    return sha


def _format_migrations_line(
    prefix: str,
    canonical_source_path: str,
    source_pre_sha: str,
    migration_sha: str,
    when_iso: str,
) -> str:
    return (
        f"{prefix}\t{canonical_source_path}\t{source_pre_sha}\t"
        f"{migration_sha}\t{when_iso}\n"
    )


def _append_migrations_line(
    store_root: Path,
    line: str,
) -> Path:
    """Append-only write to <store>/.migrations. Returns the file path."""
    path = Path(store_root) / MIGRATIONS_NAME
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    return path


def execute_migration(
    plan: MigrationPlan,
    *,
    dry_run: bool = False,
    lock_timeout: float = concurrency.DEFAULT_LOCK_TIMEOUT,
    commit_message: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
) -> MigrationResult:
    """Run the plan. With dry_run=True, returns a MigrationResult that
    records what WOULD happen but writes nothing.

    Raises MigrationError if the plan has validation failures (the caller
    is expected to surface specifics from `plan.validation_results`).

    Raises `errors.ConcurrentEdit` on lock-acquisition timeout, which
    the CLI maps to exit 3 (concurrent-edit).

    On apply, the tool drives two store-side commits:
      1. The migration commit -- the migrated issue files + companions.
      2. The .migrations record commit -- one TSV line capturing the
         source repo path, its pre-migration HEAD sha, the migration
         commit sha, and the timestamp.

    Commits are path-restricted (`git commit -- <paths>`), so unrelated
    dirty state in the store working tree is not swept in.
    """
    if plan.has_validation_errors:
        raise MigrationError(
            "validation errors block migration; "
            "no files written to the store"
        )

    result = MigrationResult(plan=plan, dry_run=dry_run, executed=False)

    if dry_run:
        warnings, resolved, unresolved = scan_cross_references(
            plan, use_sources=True
        )
        result.cross_ref_warnings = warnings
        result.cross_ref_resolved_count = resolved
        result.cross_ref_unresolved_count = unresolved
        return result

    # Pre-migration source sha must be captured before any in-source
    # side effect (the sentinel write at the end).
    source_pre_sha = capture_source_pre_migration_sha(plan.project_path)
    result.source_pre_migration_sha = source_pre_sha

    _partial_prior_migration_check(plan)

    plan.target_dir.mkdir(parents=True, exist_ok=True)

    for op in plan.issue_copies:
        _copy_with_lock(op, lock_timeout=lock_timeout)
        result.copies_succeeded.append(op)

    for op in plan.companion_copies:
        _copy_simple(op)
        result.copies_succeeded.append(op)

    # Migration commit -- path-restricted to the files we just wrote.
    migration_paths = [op.dest for op in plan.issue_copies] + [
        op.dest for op in plan.companion_copies
    ]
    message = commit_message or _default_commit_message(plan)
    result.migration_commit_message = message
    migration_sha = _git_commit_paths(plan.store_root, migration_paths, message)
    result.migration_commit_sha = migration_sha

    # .migrations TSV append + second commit.
    canonical_source = os.path.realpath(str(plan.project_path))
    result.canonical_source_path = canonical_source
    when = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat()
    line = _format_migrations_line(
        plan.prefix, canonical_source, source_pre_sha, migration_sha, when,
    )
    migrations_path = _append_migrations_line(plan.store_root, line)
    result.migrations_line = line.rstrip("\n")
    second_sha = _git_commit_paths(
        plan.store_root,
        [migrations_path],
        f"chore: record .migrations entry for {plan.prefix}",
    )
    result.migrations_commit_sha = second_sha

    warnings, resolved, unresolved = scan_cross_references(
        plan, use_sources=False
    )
    result.cross_ref_warnings = warnings
    result.cross_ref_resolved_count = resolved
    result.cross_ref_unresolved_count = unresolved

    # Sentinel writes last so any failure before this point leaves the
    # source repo unmigrated. The store side is already committed; a
    # later re-run will be caught by _partial_prior_migration_check.
    sentinel_payload = str(plan.sentinel_store_path) + "\n"
    concurrency.atomic_write(plan.sentinel_path, sentinel_payload)
    result.sentinel_written = True
    result.executed = True
    return result


def _copy_with_lock(op: CopyOp, *, lock_timeout: float) -> None:
    """Per-issue lock + atomic write + sha256 verification."""
    assert op.lock_path is not None
    src_bytes = op.source.read_bytes()
    src_sha = hashlib.sha256(src_bytes).hexdigest()
    with concurrency.flock_acquire(
        op.lock_path, timeout=lock_timeout, resource=op.issue_id
    ):
        concurrency.atomic_write(op.dest, src_bytes)
        dest_sha = _sha256(op.dest)
        if dest_sha != src_sha:
            # Should be impossible: atomic_write writes the same bytes.
            # Surface explicitly per the acceptance criterion.
            raise MigrationError(
                f"sha256 mismatch after copy: {op.source} -> {op.dest} "
                f"(src={src_sha}, dest={dest_sha})"
            )


def _copy_simple(op: CopyOp) -> None:
    """Companion-file copy: atomic write, no lock. .prefix and
    ISSUE-TEMPLATE.md are not concurrent-write targets."""
    src_bytes = op.source.read_bytes()
    src_sha = hashlib.sha256(src_bytes).hexdigest()
    concurrency.atomic_write(op.dest, src_bytes)
    dest_sha = _sha256(op.dest)
    if dest_sha != src_sha:
        raise MigrationError(
            f"sha256 mismatch after copy: {op.source} -> {op.dest} "
            f"(src={src_sha}, dest={dest_sha})"
        )


def scan_cross_references(
    plan: MigrationPlan, *, use_sources: bool
) -> Tuple[List[str], int, int]:
    """Scan every migrated issue file for `<PFX>-NNN` tokens.

    Returns (warnings, resolved_count, unresolved_count). A token
    resolves if `<store>/<token-prefix>/<token>.md` exists. For the
    current project's own prefix, the just-migrated files are
    in-place by the time this runs (post-execute); during dry-run, we
    pass use_sources=True so own-prefix tokens resolve against the
    source corpus as well.

    Unresolved tokens are NOT errors -- cross-project refs to
    not-yet-migrated projects are expected during the migration
    window. They surface as warnings.
    """
    own_prefix = plan.prefix
    own_ids = {op.issue_id for op in plan.issue_copies if op.issue_id}

    warnings: List[str] = []
    resolved = 0
    unresolved = 0

    files_to_scan = (
        [op.source for op in plan.issue_copies]
        if use_sources
        else [op.dest for op in plan.issue_copies]
    )

    for path in files_to_scan:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        seen_in_file: set = set()
        for match in ID_TOKEN_RE.finditer(text):
            token = match.group(0)
            if token in seen_in_file:
                continue
            seen_in_file.add(token)
            pfx = match.group(1)
            if pfx == own_prefix:
                # Own-project token: check both the in-progress source
                # corpus and the destination dir for completeness.
                if token in own_ids:
                    resolved += 1
                    continue
            store_path = plan.store_root / pfx / f"{token}.md"
            if store_path.is_file():
                resolved += 1
            else:
                unresolved += 1
                warnings.append(
                    f"unresolved cross-reference {token} in {path.name}"
                )
    return warnings, resolved, unresolved
