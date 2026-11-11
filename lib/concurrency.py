#!/usr/bin/env python3
"""Concurrency primitives for issue-mgmt mutating operations.

Three layers from docs/spec.md "Concurrency model":
  1. Atomic writes via temp file in same dir, fsync, os.replace.
  2. Per-record advisory locks via fcntl.flock with timeout and retry.
  3. Optimistic mtime+size+sha256 verification at rename time.

Public API for new Python subcommands (store-mode rewrites,
edit-issue, transition-issue, validate-issue, migrate-to-store,
history subcommands):

    from concurrency import (
        atomic_write,
        capture_state,
        verify_unchanged,
        issue_lock,
        index_lock,
        registry_lock,
        flock_acquire,
        StateSnapshot,
    )

The `ConcurrentEdit` exception and the CLI exit-code constants moved to
lib/errors.py -- import them from there:

    from errors import ConcurrentEdit, EXIT_CONCURRENT_EDIT

CLI surface for the bash wrapper (lib/concurrency.sh) and ad-hoc use:

    python3 lib/concurrency.py flock-run <lockpath> [--timeout S] -- <cmd> [args...]
    python3 lib/concurrency.py atomic-write <target>          # stdin -> target
    python3 lib/concurrency.py snapshot <path>                # prints mtime_ns:size:sha256
    python3 lib/concurrency.py verify <path> <snapshot>       # exit 0 / 3

The stdlib-only constraint is a project design rule; if it is lifted,
the lock layer should migrate to py-filelock.
"""

import argparse
import errno
import fcntl
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from errors import (  # noqa: F401  (canonical home is lib/errors.py)
    EXIT_CONCURRENT_EDIT,
    EXIT_PRECONDITION,
    EXIT_STORE_COMMIT_FAILED,
    ConcurrentEdit,
)

DEFAULT_LOCK_TIMEOUT = 5.0
DEFAULT_LOCK_POLL = 0.05
_ABSENT_SHA = ""

# Single source of truth for "what counts as a tracked store path"
# for the auto-commit and catchup-sweep machinery. User-dropped files in
# the store (anything not matching these rules) stay untracked.
STORE_TRACKED_TOPLEVEL_FILES = (
    "INDEX.md",
    ".repos",
    ".migrations",
    "README.md",
    "LAYOUT.md",
    "ISSUE-TEMPLATE.md",
)
# Prefix-directory regex matches "<PFX>/..." for PFX in [A-Z][A-Z0-9]{1,7}.
# This is the same shape <PFX> must satisfy at init-issue-project time
# (init_issue_project enforces ^[A-Z][A-Z0-9]{1,7}$).
STORE_PREFIX_DIR_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}/")
CATCHUP_COMMIT_MESSAGE = "catchup: reconcile pending store changes"


def is_store_tracked_path(rel_path):
    """True if rel_path (relative to store root, posix-separated) is a
    path that the store's auto-commit machinery should manage.

    Single source of truth -- consumers should not duplicate this logic.

    Excludes anything inside a `.locks/` directory at any depth (advisory
    lock files are runtime state, not durable store content). Excludes
    hidden files at the toplevel that are not on the explicit allowlist.
    """
    s = str(rel_path).replace(os.sep, "/")
    parts = s.split("/")
    if ".locks" in parts:
        return False
    if s in STORE_TRACKED_TOPLEVEL_FILES:
        return True
    if STORE_PREFIX_DIR_RE.match(s):
        return True
    return False


class StoreCommitError(Exception):
    """Store commit failed for a non-hook reason (disk full, permissions,
    corrupt repo).

    Two sub-paths share this exception:

    `write_attempted=True` (default) -- the failure came from
    `commit_after_write`, so the caller's atomic write has already
    landed on disk. Recovery is to fix the underlying problem and
    re-run any mutation; the next successful mutation's catchup sweep
    picks up the orphan write.

    `write_attempted=False` -- the failure came from `catchup_sweep`
    (or one of its helpers) before the caller's atomic write ran. No
    issue file mutation was attempted, so there is no orphan on disk
    for a future sweep to reconcile.
    """

    def __init__(self, message, *, git_error=None, write_attempted=True):
        super().__init__(message)
        self.message = message
        self.git_error = git_error
        self.write_attempted = write_attempted

    def format_stderr(self, tool_name="issue-mgmt"):
        detail = self.git_error or self.message
        if self.write_attempted:
            return (
                f"{tool_name}: store commit failed: {detail}; "
                "file is on disk; fix and rerun any mutation to recover"
            )
        return (
            f"{tool_name}: store commit failed: {detail}; "
            "no changes were written; fix the store and retry"
        )


@dataclass(frozen=True)
class StateSnapshot:
    mtime_ns: int
    size: int
    sha256: str

    @classmethod
    def absent(cls):
        return cls(mtime_ns=0, size=0, sha256=_ABSENT_SHA)

    def is_absent(self):
        return self.sha256 == _ABSENT_SHA and self.mtime_ns == 0 and self.size == 0

    def encode(self):
        if self.is_absent():
            return "absent"
        return f"{self.mtime_ns}:{self.size}:{self.sha256}"

    @classmethod
    def decode(cls, s):
        if s == "absent":
            return cls.absent()
        mtime, size, sha = s.split(":", 2)
        return cls(mtime_ns=int(mtime), size=int(size), sha256=sha)


def capture_state(path):
    """Snapshot file's mtime_ns, size, and sha256 for optimistic verification.

    Returns the absent sentinel for a non-existent file, so creation
    (absent -> present) can be verified the same way as update.
    """
    path = Path(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        return StateSnapshot.absent()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return StateSnapshot(mtime_ns=st.st_mtime_ns, size=st.st_size, sha256=h.hexdigest())


def verify_unchanged(path, snapshot, *, resource=None):
    """Re-snapshot and compare. Raise ConcurrentEdit on any divergence."""
    current = capture_state(path)
    if current != snapshot:
        raise ConcurrentEdit(
            "file changed since read",
            resource=resource or str(path),
        )


def atomic_write(path, content):
    """Write content to path: temp file in same dir -> fsync -> os.replace.

    content may be str or bytes. Cleans up the temp file on exception.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_bytes = isinstance(content, (bytes, bytearray))
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        if is_bytes:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


@contextmanager
def flock_acquire(lock_path, *, timeout=DEFAULT_LOCK_TIMEOUT,
                  poll=DEFAULT_LOCK_POLL, resource=None):
    """Acquire fcntl.LOCK_EX on lock_path with timeout. Writes own PID.

    Kernel auto-releases on process death; no manual stale-lock cleanup.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    # PEP 446: fds are non-inheritable by default. flock-run execs the child
    # while still holding the lock; the inherited fd is what keeps the lock
    # alive across the exec boundary.
    os.set_inheritable(fh.fileno(), True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fh.seek(0)
                fh.truncate()
                fh.write(str(os.getpid()))
                fh.flush()
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    contender = _read_contender_pid(fh)
                    raise ConcurrentEdit(
                        f"lock timeout after {timeout}s",
                        resource=resource or str(lock_path),
                        contender_pid=contender,
                    )
                time.sleep(poll)
        try:
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fh.close()


def _read_contender_pid(fh):
    try:
        fh.seek(0)
        raw = fh.read().strip()
    except OSError:
        return None
    if raw.isdigit():
        return int(raw)
    return None


def issue_lock_path(store_root, prefix, issue_id):
    return Path(store_root) / prefix / ".locks" / f"{issue_id}.lock"


def index_lock_path(store_root):
    return Path(store_root) / ".locks" / "INDEX.lock"


def registry_lock_path(store_root):
    return Path(store_root) / ".locks" / ".repos.lock"


@contextmanager
def issue_lock(store_root, prefix, issue_id, **kw):
    path = issue_lock_path(store_root, prefix, issue_id)
    with flock_acquire(path, resource=f"{prefix}-{issue_id}", **kw):
        yield


@contextmanager
def index_lock(store_root, **kw):
    with flock_acquire(index_lock_path(store_root), resource="INDEX.md", **kw):
        yield


@contextmanager
def registry_lock(store_root, **kw):
    with flock_acquire(registry_lock_path(store_root), resource=".repos", **kw):
        yield


def store_commit_lock_path(store_root):
    return Path(store_root) / ".locks" / "store-commit.lock"


@contextmanager
def store_commit_lock(store_root, *, timeout=DEFAULT_LOCK_TIMEOUT, **kw):
    """Acquire the store-wide commit lock with one internal retry on
    lock-acquisition timeout. Exceptions
    raised inside the body propagate without retry.
    """
    lock_path = store_commit_lock_path(store_root)
    last_err = None
    for _ in range(2):
        cm = flock_acquire(
            lock_path, timeout=timeout, resource="store-commit", **kw
        )
        try:
            cm.__enter__()
        except ConcurrentEdit as e:
            last_err = e
            continue
        try:
            yield
        finally:
            cm.__exit__(None, None, None)
        return
    raise last_err


def _run_git_raw(cwd, args):
    """Run git inside cwd. Returns (returncode, stdout, stderr) without raising.

    Distinct from migrate.py:_run_git which raises MigrationError; callers
    here need to classify failures (hook reject vs system error) themselves.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return (127, "", "git not installed or not on PATH")
    return (
        r.returncode,
        (r.stdout or "").rstrip("\n"),
        (r.stderr or "").rstrip("\n"),
    )


def git_show(repo_root, ref, rel_path):
    """Return file bytes at <ref>:<rel_path> in the git repo at repo_root.

    Used by edit-issue's --if-unchanged-since lost-update guard:
    given a SHA the caller read from, fetch the same path at that SHA so
    the caller-side staleness check can run inside the per-issue lock.

    Errors are classified for the CLI exit-code convention:
      - ValueError: ref cannot be resolved (typo, unreachable from this
        repo, etc.). Maps to exit 2 at the CLI.
      - FileNotFoundError: ref resolves but rel_path did not exist at
        that ref. Maps to exit 1 at the CLI.

    Returns bytes (binary stdout). Callers that need text decode with
    utf-8.
    """
    repo_root = str(repo_root)
    rel = str(rel_path).replace(os.sep, "/")
    rc_resolve, resolved, err_resolve = _run_git_raw(
        repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"]
    )
    if rc_resolve != 0:
        raise ValueError(
            f"cannot resolve {ref!r} in store repo at {repo_root}: "
            f"{err_resolve or 'unknown ref'}"
        )
    try:
        r = subprocess.run(
            ["git", "-C", repo_root, "show", f"{resolved}:{rel}"],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        raise ValueError("git not installed or not on PATH")
    if r.returncode != 0:
        stderr = (r.stderr or b"").decode("utf-8", "replace").rstrip("\n")
        # git surfaces missing-path as "exists on disk, but not in <sha>"
        # or "path '<rel>' does not exist". Treat any non-zero from show
        # (after a successful rev-parse) as path-missing-at-ref.
        raise FileNotFoundError(
            f"{rel} does not exist at {ref} ({resolved[:12]}): {stderr}"
        )
    return r.stdout


def _hook_likely_present(store_root):
    """Best-effort: True if an executable pre-commit hook is configured.

    Used to classify commit failure as hook-reject (exit 0 + warning) vs
    system error (exit 1). Resolves `core.hooksPath` so non-default hook
    locations are detected too.
    """
    rc, hooks_path, _ = _run_git_raw(
        store_root, ["config", "--get", "core.hooksPath"]
    )
    if rc == 0 and hooks_path.strip():
        hp = Path(hooks_path.strip())
        if not hp.is_absolute():
            hp = Path(store_root) / hp
        hook = hp / "pre-commit"
    else:
        hook = Path(store_root) / ".git" / "hooks" / "pre-commit"
    try:
        return hook.is_file() and os.access(str(hook), os.X_OK)
    except OSError:
        return False


def _git_status_pending_tracked_paths(store_root):
    """Return a list of relative store paths that have pending changes
    AND match `is_store_tracked_path`. Anything else (user-dropped files,
    `.locks/`, hidden things) is ignored.

    Failures here are pre-write (catchup_sweep's first step, before any
    caller atomic_write runs), so StoreCommitError is raised with
    `write_attempted=False`.
    """
    rc, out, err = _run_git_raw(
        store_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if rc != 0:
        raise StoreCommitError(
            f"git status failed: {err or out}",
            git_error=err or out,
            write_attempted=False,
        )
    paths = []
    parts = out.split("\x00")
    i = 0
    n = len(parts)
    while i < n:
        part = parts[i]
        i += 1
        if not part or len(part) < 4:
            continue
        status = part[:2]
        path = part[3:]
        if status[0] in ("R", "C") and i < n:
            # Rename/copy: the next null-terminated field is the original
            # path; advance past it. The new path is what we want.
            i += 1
        if is_store_tracked_path(path):
            paths.append(path)
    return paths


def pending_tracked_paths(store_root):
    """Public read of the pending tracked-store-path set.

    The same list `catchup_sweep` is about to claim. Exposed for
    `issue admin snapshot --json`, which reports WHICH paths the sweep
    swept (IM-240) -- the sweep itself returns only the commit sha. Call
    it inside the store commit lock so the read and the sweep see one
    state.
    """
    return _git_status_pending_tracked_paths(store_root)


def catchup_sweep(store_root, *, message=CATCHUP_COMMIT_MESSAGE,
                  exclude=None):
    """If the store has pending tracked-path changes, stage and commit
    them as one catchup commit. Returns the new HEAD sha or None if
    there was nothing to sweep.

    `exclude`, when provided, is an iterable of relative store paths
    that the sweep must leave alone (use case: the caller is about to
    commit those paths with their own message and would lose it if the
    sweep claimed them first).

    Hook-rejected catchups emit a warning and return None (the pending
    paths stay on disk; the next mutation retries after the hook is fixed).
    System errors raise StoreCommitError with `write_attempted=False`
    because the caller's atomic_write has not yet run when
    `catchup_sweep` executes inside `store_mutation`.
    """
    paths = _git_status_pending_tracked_paths(store_root)
    if exclude:
        exclude_set = {str(Path(p)) for p in exclude}
        paths = [p for p in paths if p not in exclude_set]
    if not paths:
        return None
    rc, out, err = _run_git_raw(
        store_root, ["add", "--all", "--", *paths]
    )
    if rc != 0:
        raise StoreCommitError(
            f"git add failed: {err or out}",
            git_error=err or out,
            write_attempted=False,
        )
    rc, out, err = _run_git_raw(
        store_root, ["commit", "-m", message, "--", *paths]
    )
    if rc != 0:
        if _hook_likely_present(store_root):
            print(
                f"issue-mgmt: catchup commit blocked by hook; "
                f"{len(paths)} pending file(s) remain on disk; "
                "next mutation will retry",
                file=sys.stderr,
            )
            return None
        raise StoreCommitError(
            f"git commit failed: {err or out}",
            git_error=err or out,
            write_attempted=False,
        )
    rc, sha, _ = _run_git_raw(store_root, ["rev-parse", "HEAD"])
    return sha if rc == 0 and sha else None


def commit_after_write(store_root, paths, message):
    """Stage and commit just-written paths. Returns the new HEAD sha or
    None (idempotent write or hook-rejected commit). Raises
    StoreCommitError on system failures.

    `paths` may be absolute or relative to `store_root`; absolute paths
    are normalized via `Path.resolve().relative_to(store_root)`.

    Paths that do not exist on disk under `store_root` are silently
    dropped before staging. Callers wrap the issue file plus
    optional INDEX.md paths; under `--no-reindex` against a prefix
    whose INDEX.md has not yet been generated, those INDEX paths are
    legitimately absent and must not abort the commit. The issue file
    is guaranteed to exist after the preceding atomic write, so this
    filter is a no-op for the must-have path.
    """
    store_root_abs = Path(store_root).resolve()
    rel_paths = []
    for p in paths:
        p_path = Path(p)
        if p_path.is_absolute():
            try:
                rel = p_path.resolve().relative_to(store_root_abs)
            except ValueError:
                raise StoreCommitError(
                    f"path {p} is not inside store {store_root_abs}",
                )
            rel_str = str(rel)
        else:
            rel_str = str(p_path)
        if not (store_root_abs / rel_str).exists():
            continue
        rel_paths.append(rel_str)
    if not rel_paths:
        return None
    rc, out, err = _run_git_raw(
        store_root, ["add", "--all", "--", *rel_paths]
    )
    if rc != 0:
        raise StoreCommitError(
            f"git add failed: {err or out}", git_error=err or out
        )
    # If nothing was actually staged for these paths (idempotent write
    # that matched the indexed content), skip the commit.
    rc, _, err = _run_git_raw(
        store_root, ["diff", "--cached", "--quiet", "--", *rel_paths]
    )
    if rc == 0:
        return None
    if rc not in (0, 1):
        raise StoreCommitError(
            f"git diff --cached failed: {err}", git_error=err
        )
    rc, out, err = _run_git_raw(
        store_root, ["commit", "-m", message, "--", *rel_paths]
    )
    if rc != 0:
        if _hook_likely_present(store_root):
            print(
                "issue-mgmt: commit blocked by hook; file written; "
                "next mutation will retry",
                file=sys.stderr,
            )
            return None
        raise StoreCommitError(
            f"git commit failed: {err or out}", git_error=err or out
        )
    rc, sha, _ = _run_git_raw(store_root, ["rev-parse", "HEAD"])
    return sha if rc == 0 and sha else None


@contextmanager
def store_mutation(store_root, paths, message, *,
                   lock_timeout=DEFAULT_LOCK_TIMEOUT):
    """Compose the mutation lifecycle: acquire the store commit
    lock, run the catchup sweep, yield to the caller's atomic-write,
    then commit the named paths.

    Typical caller (Python):

        with concurrency.issue_lock(store, prefix, id):
            with concurrency.store_mutation(store, [issue_path],
                                            f"create {issue_id}"):
                concurrency.atomic_write(issue_path, content)

    `paths` may be absolute or relative to `store_root`.
    """
    with store_commit_lock(store_root, timeout=lock_timeout):
        catchup_sweep(store_root)
        yield
        commit_after_write(store_root, paths, message)



def _stderr_json_mode():
    """True when ISSUE_MGMT_STDERR_JSON is set in the environment.

    Bash front-ends (new-issue.sh) export it when their own --json flag
    is active so ContractError stderr from the concurrency CLI layer
    comes out as the one-line JSON shape.
    """
    return bool(os.environ.get("ISSUE_MGMT_STDERR_JSON"))


def _cli_flock_run(args):
    lock_path = args.lockpath
    cmd = args.cmd
    if not cmd:
        print("flock-run: missing command", file=sys.stderr)
        return 2
    try:
        with flock_acquire(lock_path, timeout=args.timeout):
            os.execvp(cmd[0], cmd)
    except ConcurrentEdit as e:
        print(e.format_stderr("flock-run", json_mode=_stderr_json_mode()), file=sys.stderr)
        return EXIT_CONCURRENT_EDIT
    return 0


def _cli_atomic_write(args):
    data = sys.stdin.buffer.read()
    atomic_write(args.target, data)
    return 0


def _cli_snapshot(args):
    snap = capture_state(args.path)
    print(snap.encode())
    return 0


def _cli_verify(args):
    expected = StateSnapshot.decode(args.snapshot)
    try:
        verify_unchanged(args.path, expected)
    except ConcurrentEdit as e:
        print(e.format_stderr("verify", json_mode=_stderr_json_mode()), file=sys.stderr)
        return EXIT_CONCURRENT_EDIT
    return 0


def _cli_catchup_sweep(args):
    try:
        with store_commit_lock(args.store_root, timeout=args.timeout):
            sha = catchup_sweep(args.store_root)
    except ConcurrentEdit as e:
        print(e.format_stderr("catchup-sweep", json_mode=_stderr_json_mode()), file=sys.stderr)
        return EXIT_CONCURRENT_EDIT
    except StoreCommitError as e:
        print(e.format_stderr("catchup-sweep"), file=sys.stderr)
        return EXIT_STORE_COMMIT_FAILED
    if sha:
        print(sha)
    return 0


def _cli_commit_after_write(args):
    try:
        with store_commit_lock(args.store_root, timeout=args.timeout):
            # Exclude caller's paths so the sweep does not steal their
            # commit message. The caller's paths get their own commit
            # below; everything else dirty gets the catchup commit.
            catchup_sweep(args.store_root, exclude=args.path)
            sha = commit_after_write(
                args.store_root, args.path, args.message
            )
    except ConcurrentEdit as e:
        print(e.format_stderr("commit-after-write", json_mode=_stderr_json_mode()), file=sys.stderr)
        return EXIT_CONCURRENT_EDIT
    except StoreCommitError as e:
        print(e.format_stderr("commit-after-write"), file=sys.stderr)
        return EXIT_STORE_COMMIT_FAILED
    if sha:
        print(sha)
    return 0


def _main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    # flock-run and store-mutation get manual splitting at "--" so argparse
    # REMAINDER does not eat known flags into the command vector.
    if argv and argv[0] == "flock-run":
        return _main_flock_run(argv[1:])
    if argv and argv[0] == "store-mutation":
        return _main_store_mutation(argv[1:])

    parser = argparse.ArgumentParser(prog="concurrency")
    sub = parser.add_subparsers(dest="op", required=True)

    p_write = sub.add_parser("atomic-write", help="stdin -> target atomically")
    p_write.add_argument("target")
    p_write.set_defaults(func=_cli_atomic_write)

    p_snap = sub.add_parser("snapshot", help="print mtime_ns:size:sha256")
    p_snap.add_argument("path")
    p_snap.set_defaults(func=_cli_snapshot)

    p_verify = sub.add_parser("verify", help="exit 3 if path differs from snapshot")
    p_verify.add_argument("path")
    p_verify.add_argument("snapshot")
    p_verify.set_defaults(func=_cli_verify)

    p_sweep = sub.add_parser(
        "catchup-sweep",
        help="acquire store commit lock and run the catchup sweep",
    )
    p_sweep.add_argument("store_root")
    p_sweep.add_argument(
        "--timeout", type=float, default=DEFAULT_LOCK_TIMEOUT
    )
    p_sweep.set_defaults(func=_cli_catchup_sweep)

    p_commit = sub.add_parser(
        "commit-after-write",
        help="lock + catchup sweep + commit specific store paths",
    )
    p_commit.add_argument("store_root")
    p_commit.add_argument("--message", required=True)
    p_commit.add_argument("--path", action="append", default=[])
    p_commit.add_argument(
        "--timeout", type=float, default=DEFAULT_LOCK_TIMEOUT
    )
    p_commit.set_defaults(func=_cli_commit_after_write)

    args = parser.parse_args(argv)
    return args.func(args)


def _main_flock_run(sub_argv):
    if "--" not in sub_argv:
        print("flock-run: missing '--' separator before command", file=sys.stderr)
        return 2
    sep = sub_argv.index("--")
    head, cmd = sub_argv[:sep], sub_argv[sep + 1:]
    parser = argparse.ArgumentParser(prog="concurrency flock-run")
    parser.add_argument("lockpath")
    parser.add_argument("--timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)
    args = parser.parse_args(head)
    args.cmd = cmd
    return _cli_flock_run(args)


def _main_store_mutation(sub_argv):
    """concurrency store-mutation <store> --message MSG --path P1 [...] -- <cmd> [args...]

    Holds the store commit lock for the duration of <cmd>. Runs the
    catchup sweep before <cmd> starts; commits the named paths after
    <cmd> exits 0. If <cmd> exits non-zero, no commit is made and the
    same exit code is propagated.
    """
    if "--" not in sub_argv:
        print(
            "store-mutation: missing '--' separator before command",
            file=sys.stderr,
        )
        return 2
    sep = sub_argv.index("--")
    head, cmd = sub_argv[:sep], sub_argv[sep + 1:]
    parser = argparse.ArgumentParser(prog="concurrency store-mutation")
    parser.add_argument("store_root")
    parser.add_argument("--message", required=True)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_LOCK_TIMEOUT
    )
    args = parser.parse_args(head)
    if not cmd:
        print("store-mutation: missing command", file=sys.stderr)
        return 2
    try:
        with store_commit_lock(args.store_root, timeout=args.timeout):
            try:
                catchup_sweep(args.store_root)
            except StoreCommitError as e:
                print(e.format_stderr("store-mutation"), file=sys.stderr)
                return EXIT_STORE_COMMIT_FAILED
            try:
                r = subprocess.run(cmd, check=False)
            except FileNotFoundError as e:
                print(f"store-mutation: {e}", file=sys.stderr)
                return 2
            if r.returncode != 0:
                return r.returncode
            try:
                commit_after_write(args.store_root, args.path, args.message)
            except StoreCommitError as e:
                print(e.format_stderr("store-mutation"), file=sys.stderr)
                return EXIT_STORE_COMMIT_FAILED
    except ConcurrentEdit as e:
        print(e.format_stderr("store-mutation", json_mode=_stderr_json_mode()), file=sys.stderr)
        return EXIT_CONCURRENT_EDIT
    return 0


if __name__ == "__main__":
    sys.exit(_main())
