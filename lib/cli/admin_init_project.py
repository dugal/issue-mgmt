"""
init-issue-project -- register a project into the issue store

Synopsis:
  init-issue-project [--repo NAME] [--prefix PREFIX]
                     [--store ROOT] [--non-interactive]
                     [-h | --help] [--version]

Registers the current git repository into the issue store's project
registry (`<store>/.repos`) under a short uppercase prefix. After
registration, `new-issue` and `reindex-issues` in store mode resolve
the project's prefix via the registry rather than walking up to a
project-local `.issues/.prefix` file.

Store-root resolution order:
  1. --store ROOT
  2. ISSUE_STORE_ROOT environment variable
  3. <anchor>/.issues/.migrated-to-store sentinel (contents = store root)
  4. Config file ~/.config/issue-mgmt/config, key `store_root`
  5. Interactive prompt (unless --non-interactive). Answer is persisted
     to the config file.

Repo identity:
  Default: basename of the main checkout (the directory containing the
  shared .git), resolved via `git rev-parse --git-common-dir` so
  registration from a linked worktree records the main checkout's name
  rather than the worktree directory.
  Override: --repo NAME.

Prefix derivation when --prefix is not given:
  1. Read <anchor>/.issues/.prefix if present.
  2. Interactive prompt, unless --non-interactive.
  Prefix must match ^[A-Z][A-Z0-9]{1,7}$ and may not be "ISSUE".

Template seeding:
  If `<store>/ISSUE-TEMPLATE.md` is missing, it is copied byte-identically
  from the Homebrew generic (`<brew-prefix>/share/issue-mgmt/ISSUE-TEMPLATE.md`,
  or the dev fallback) before the registry is touched. A one-line notice
  is printed on stderr; subsequent calls are silent.

Sentinel writing:
  After registration, writes `<anchor>/.issues/.migrated-to-store`
  containing the symlink-preserved store root so the new-project
  scaffolding recipe is a single command. Idempotent against a matching
  sentinel; refuses to overwrite a divergent one. Uses the symlink-preserving
  rule (`expanduser().absolute()`, NOT `.resolve()`).

Exit codes:
  0  success (including idempotent "already registered" re-run)
  1  configuration / validation error (see stderr message)
  3  lock acquisition timeout (`concurrent-edit`; was 17 through v2.4.1)

Examples:
  ISSUE_STORE_ROOT=/path/to/store init-issue-project --prefix IM
  init-issue-project --repo my-project --prefix MP --non-interactive
"""

import argparse
import errno
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import errors  # noqa: E402
import migrate as migrate_lib  # noqa: E402
import repo as repo_lib  # noqa: E402

VERSION = "v3.3.0"

PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
RESERVED_PREFIXES = {"ISSUE"}

CONCURRENT_EDIT_EXIT = errors.EXIT_CONCURRENT_EDIT
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_INTERVAL = 0.05

CONFIG_PATH = Path.home() / ".config" / "issue-mgmt" / "config"


def die(msg, code=1):
    print(f"init-issue-project: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="init-issue-project",
        description="Register a project into the issue store registry.",
        add_help=False,
    )
    p.add_argument("--repo", metavar="NAME",
                   help="Override the cwd-derived repo basename.")
    p.add_argument("--prefix", metavar="PREFIX",
                   help="Issue ID prefix (uppercase, 2-8 chars).")
    p.add_argument("--store", metavar="ROOT",
                   help="Override store-root resolution.")
    p.add_argument("--non-interactive", action="store_true",
                   help="Fail rather than prompt.")
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    return p.parse_args(argv)


def show_help():
    sys.stdout.write(__doc__.lstrip())
    sys.exit(0)


def read_config_store_root():
    if not CONFIG_PATH.is_file():
        return None
    try:
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "store_root":
                return value.strip() or None
    except OSError as e:
        die(f"failed to read {CONFIG_PATH}: {e}")
    return None


def write_config_store_root(value):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    found = False
    if CONFIG_PATH.is_file():
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("store_root") and "=" in stripped:
                existing.append(f"store_root = {value}")
                found = True
            else:
                existing.append(line)
    if not found:
        existing.append(f"store_root = {value}")
    CONFIG_PATH.write_text("\n".join(existing) + "\n", encoding="utf-8")


def git_toplevel():
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        die("git is not installed or not on PATH")
    except subprocess.CalledProcessError:
        die(
            "not inside a git repository. issue-mgmt keys the project "
            "registry on git identity, so a project must be a git repo -- "
            "run 'git init' first (or cd into an existing repository), then "
            "re-run this command."
        )
    return r.stdout.strip()


def resolve_anchor(toplevel):
    """Locate the project's `.issues/` directory. Walks up from cwd to
    toplevel; if none found, returns toplevel (will create on write)."""
    cur = Path.cwd().resolve()
    top = Path(toplevel).resolve()
    while True:
        if (cur / ".issues").is_dir():
            return cur
        if cur == top:
            return top
        if cur.parent == cur:
            return top
        cur = cur.parent


def resolve_store_root(cli_store, anchor, non_interactive):
    if cli_store:
        return cli_store
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return env
    sentinel = anchor / ".issues" / ".migrated-to-store"
    if sentinel.is_file():
        content = sentinel.read_text(encoding="utf-8").strip()
        if content:
            return content
    cfg = read_config_store_root()
    if cfg:
        return cfg
    if non_interactive:
        die("no store root resolved; set ISSUE_STORE_ROOT, pass --store, "
            "or run interactively to configure ~/.config/issue-mgmt/config")
    suggestion = str(Path.home() / "issues-store")
    sys.stderr.write(
        "No issue store configured. Enter the path to use as the store root.\n"
        f"  (suggestion: {suggestion})\n"
        "Store root: "
    )
    sys.stderr.flush()
    answer = sys.stdin.readline().strip()
    if not answer:
        answer = suggestion
    write_config_store_root(answer)
    print(f"Saved store_root = {answer} to {CONFIG_PATH}", file=sys.stderr)
    return answer


def validate_prefix(value):
    if not PREFIX_RE.match(value):
        die(f"invalid prefix {value!r}: must match {PREFIX_RE.pattern}")
    if value in RESERVED_PREFIXES:
        die(f"prefix {value!r} is reserved")


def derive_prefix(cli_prefix, anchor, non_interactive):
    local_prefix_file = anchor / ".issues" / ".prefix"
    local_prefix = None
    if local_prefix_file.is_file():
        local_prefix = local_prefix_file.read_text(encoding="utf-8").strip() or None

    if cli_prefix and local_prefix and cli_prefix != local_prefix:
        die(
            f"--prefix {cli_prefix!r} disagrees with "
            f"{local_prefix_file} (contains {local_prefix!r})"
        )

    chosen = cli_prefix or local_prefix
    if not chosen:
        if non_interactive:
            die("no prefix derivable; pass --prefix or run interactively")
        while True:
            sys.stderr.write(
                "Enter the issue prefix for this project "
                "(uppercase, 2-8 chars, e.g. IM): "
            )
            sys.stderr.flush()
            answer = sys.stdin.readline().strip()
            if not answer:
                continue
            if not PREFIX_RE.match(answer) or answer in RESERVED_PREFIXES:
                print(
                    f"  invalid; must match {PREFIX_RE.pattern} "
                    f"and not be in {sorted(RESERVED_PREFIXES)}",
                    file=sys.stderr,
                )
                continue
            chosen = answer
            break

    validate_prefix(chosen)
    return chosen


def parse_registry(path):
    """Return list of (basename, prefix) tuples preserving order, plus
    a set of seen comment/blank line indices for round-tripping."""
    entries = []
    if not path.is_file():
        return entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            die(f"malformed registry line in {path}: {raw!r}")
        name, _, prefix = line.partition(":")
        entries.append((name.strip(), prefix.strip()))
    return entries


def write_registry_atomic(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(f"{n}:{p}\n" for n, p in sorted(entries))
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# TODO: replace with shared lock helper
class RegistryLock:
    def __init__(self, store_root):
        self.lock_dir = Path(store_root) / ".locks"
        self.lock_path = self.lock_dir / ".repos.lock"
        self.fh = None

    def __enter__(self):
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.lock_path, "a+")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fh.seek(0)
                self.fh.truncate()
                self.fh.write(str(os.getpid()))
                self.fh.flush()
                return self
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    holder = ""
                    try:
                        self.fh.seek(0)
                        holder = self.fh.read().strip()
                    except OSError:
                        pass
                    self.fh.close()
                    self.fh = None
                    msg = "concurrent-edit: registry lock timeout"
                    if holder:
                        msg += f" (held by pid {holder})"
                    print(f"init-issue-project: {msg}", file=sys.stderr)
                    sys.exit(CONCURRENT_EDIT_EXIT)
                time.sleep(LOCK_POLL_INTERVAL)

    def __exit__(self, exc_type, exc, tb):
        if self.fh is not None:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None


def resolve_issue_template():
    """Resolve the canonical Homebrew/dev ISSUE-TEMPLATE.md path or None."""
    prefix = Path(os.path.realpath(sys.argv[0])).parent.parent
    candidates = [
        prefix / "share" / "issue-mgmt" / "ISSUE-TEMPLATE.md",
        prefix / "ISSUE-TEMPLATE.md",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def ensure_store_template(store_root):
    """Lazy-seed `<store>/ISSUE-TEMPLATE.md` from the Homebrew generic when
    absent. Prints a one-line notice on seed; silent when already present.
    No-op when no source template can be located (no shipped install)."""
    dest = Path(store_root) / "ISSUE-TEMPLATE.md"
    if dest.exists():
        return
    src = resolve_issue_template()
    if src is None:
        return
    shutil.copyfile(src, dest)
    print(
        f"seeded {dest} from {src}",
        file=sys.stderr,
    )


def ensure_local_prefix_file(anchor, prefix):
    issues_dir = anchor / ".issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    prefix_file = issues_dir / ".prefix"
    if prefix_file.is_file():
        existing = prefix_file.read_text(encoding="utf-8").strip()
        if existing == prefix:
            return prefix_file, False
        die(
            f"{prefix_file} contains {existing!r}; registry would write "
            f"{prefix!r} -- refusing to overwrite"
        )
    prefix_file.write_text(prefix + "\n", encoding="utf-8")
    return prefix_file, True


def ensure_local_sentinel_file(anchor, store_root):
    """Write `<anchor>/.issues/.migrated-to-store` so the scaffolding recipe
    is one command. Idempotent against a matching sentinel; refuses
    to overwrite a divergent one. Payload uses the symlink-preserving
    rule via `migrate.compute_sentinel_store_path`."""
    issues_dir = anchor / ".issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    sentinel = issues_dir / ".migrated-to-store"
    expected = str(migrate_lib.compute_sentinel_store_path(store_root))
    if sentinel.is_file():
        existing = sentinel.read_text(encoding="utf-8").strip()
        if existing == expected:
            return sentinel, False
        die(
            f"{sentinel} contains {existing!r}; would write {expected!r} "
            f"-- refusing to overwrite a divergent sentinel"
        )
    sentinel.write_text(expected + "\n", encoding="utf-8")
    return sentinel, True


def _autocommit_registration(store_root, repo_name, prefix, already):
    """Commit any store changes from this registration.

    `commit_after_write`'s diff-cached check is the no-op gate -- if
    ensure_store_template did not seed and the registry was already
    up-to-date (already-registered path), nothing is staged and no
    commit is made. Catchup-sweep still runs, so unrelated dirty state
    in the store is reconciled before the registration commit lands.
    """
    if os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT"):
        return
    import concurrency  # noqa: E402
    paths = ["ISSUE-TEMPLATE.md", ".repos"]
    verb = "re-register" if already else "register"
    message = f"{verb} {repo_name}:{prefix}"
    try:
        with concurrency.store_commit_lock(store_root):
            concurrency.catchup_sweep(store_root, exclude=paths)
            concurrency.commit_after_write(store_root, paths, message)
    except errors.ContractError as e:
        print(e.format_stderr("init-issue-project"), file=sys.stderr)
        sys.exit(e.exit_code)
    except concurrency.StoreCommitError as e:
        print(e.format_stderr("init-issue-project"), file=sys.stderr)
        sys.exit(errors.EXIT_PRECONDITION)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    if args.help:
        show_help()
    if args.version:
        print(VERSION)
        sys.exit(0)

    toplevel = git_toplevel()
    if args.repo:
        repo_name = args.repo
    else:
        # Resolve via --git-common-dir so registration from a linked
        # worktree records the main checkout's basename, not the
        # worktree directory's name.
        repo_name = repo_lib.resolve_main_basename()
    if not repo_name:
        die("could not derive repo basename")

    anchor = resolve_anchor(toplevel)
    store_root = resolve_store_root(args.store, anchor, args.non_interactive)
    prefix = derive_prefix(args.prefix, anchor, args.non_interactive)

    store_path = Path(store_root)
    registry_path = store_path / ".repos"

    ensure_store_template(store_root)

    already_registered = False
    with RegistryLock(store_root):
        entries = parse_registry(registry_path)
        by_name = {n: p for n, p in entries}
        by_prefix = {p: n for n, p in entries}

        if repo_name in by_name and by_name[repo_name] == prefix:
            ensure_local_prefix_file(anchor, prefix)
            already_registered = True
        elif repo_name in by_name:
            die(
                f"repo {repo_name!r} is already registered with prefix "
                f"{by_name[repo_name]!r}; refusing to re-register as "
                f"{prefix!r}"
            )
        elif prefix in by_prefix:
            die(
                f"prefix {prefix!r} is already used by repo "
                f"{by_prefix[prefix]!r}; choose a different prefix"
            )
        else:
            entries.append((repo_name, prefix))
            write_registry_atomic(registry_path, entries)

    prefix_file, _ = ensure_local_prefix_file(anchor, prefix)
    sentinel_path, _ = ensure_local_sentinel_file(anchor, store_root)

    _autocommit_registration(store_root, repo_name, prefix, already_registered)

    if already_registered:
        print(
            f"already registered: {repo_name}:{prefix}\n"
            f"registry: {registry_path}\n"
            f"local prefix: {prefix_file}\n"
            f"sentinel: {sentinel_path}"
        )
    else:
        print(
            f"registered {repo_name}:{prefix}\n"
            f"registry: {registry_path}\n"
            f"local prefix: {prefix_file}\n"
            f"sentinel: {sentinel_path}"
        )



# --- consolidated `issue admin init-project` dispatcher glue ------
#
# Dispatcher port, not a rewrite (design section 6): the entire flat-binary
# body above is unchanged except for the sibling-path resolution fixes noted
# inline; only the arg-surface glue is new. The subparser uses a null
# prefix_chars + nargs="*" catch-all so the flat binary's own argparse (its
# flags, exit codes, and __doc__ help) stays the single source of truth and
# every byte of --help / --json / exit behavior is preserved.

VERB = "init-project"
TIER = "admin"
SUMMARY = "register a project into the issue store registry"
ORDER = 20


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
