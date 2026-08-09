"""history -- read-only git-log helpers for the issue store.

This module is consumed by `scripts/issue-history`, `scripts/search-history`,
and (eventually) the other history subcommands in phase-doug035-p1c. It is
store-only: callers resolve `ISSUE_STORE_ROOT` (or the `.migrated-to-store`
sentinel) themselves and pass the resolved root in. Legacy mode is out of
scope per D9 -- pre-migration history lives in the originating
project repo and is reached with a plain `git log` call.

Public surface:

    Commit                    dataclass: sha, date, author, subject
    IssueActivity             dataclass: issue_id, prefix, file,
                              commit_count, latest (Commit)
    HistoryError              raised on malformed ID or missing path
    parse_id                  validates and splits "<PREFIX>-NNN" ->
                              (prefix, id)
    rel_path_for              returns "<prefix>/<id>.md" (store-relative)
    validate_prefix           raises HistoryError on a non-conforming
                              prefix shape
    parse_git_log             parses the unit-separator-delimited git log
                              output
    parse_git_log_with_names  parses `git log --name-only` with our
                              custom format -- returns [(Commit, [path])]
    git_log                   single-file `git log --follow` against the
                              store
    git_pickaxe               cross-store `git log -S <term>` (pickaxe),
                              with optional --prefix scoping and --since
                              cutoff
    git_activity              cross-store `git log --name-only` grouped
                              by issue file (`<PREFIX>/<ID>.md`)
    resolve_ref               resolve a SHA / branch / tag / date to a
                              full SHA
    git_show                  contents of a path at a given commit

The module is read-only and acquires no locks.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import idresolve  # noqa: E402  (lib/ is on sys.path via the entrypoint)

# Aliased to the single shared grammar in lib/idresolve.py.
_ID_RE = idresolve.ID_RE
_PREFIX_RE = idresolve.PREFIX_RE
# Path of an issue file under the store: `<PREFIX>/<ID>.md` where the
# basename's stem equals the id. Anchored. INDEX.md, .repos, and other
# non-issue artifacts fail this match by design.
_ISSUE_PATH_RE = re.compile(
    r"^(?P<prefix>[A-Z][A-Z0-9]{1,7})/(?P<id>(?P=prefix)-\d+)\.md$"
)

# Heuristic guard for `resolve_ref` date fallback. Git's approxidate
# silently maps unparseable inputs to "now" -- so without this guard,
# something like `--at IM-030` would resolve to HEAD instead of
# erroring. We only attempt the date fallback when the input starts
# with a digit (ISO dates, `2.weeks.ago`, `1/15/2026`) or a recognized
# date keyword. Anything else fails the rev-parse path and exits with
# "could not resolve" -- which is the right diagnosis for "you typed
# a name git doesn't know."
_DATE_LIKE_RE = re.compile(
    r"^(\d|(yesterday|today|tomorrow|now|noon|midnight|last|next|this)\b)",
    re.IGNORECASE,
)

GIT_LOG_FORMAT = "%H%x1f%aI%x1f%an%x1f%s"
_FIELD_SEP = "\x1f"


class HistoryError(Exception):
    """Raised for malformed IDs or missing on-disk paths."""


@dataclass(frozen=True)
class Commit:
    sha: str
    date: str
    author: str
    subject: str

    def to_dict(self) -> dict:
        return {
            "sha": self.sha,
            "date": self.date,
            "author": self.author,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class IssueActivity:
    """One issue's activity summary inside a windowed git_activity scan."""

    issue_id: str
    prefix: str
    file: str
    commit_count: int
    latest: Commit

    def to_dict(self) -> dict:
        return {
            "id": self.issue_id,
            "prefix": self.prefix,
            "file": self.file,
            "commit_count": self.commit_count,
            "latest": self.latest.to_dict(),
        }


def parse_id(issue_id: str) -> Tuple[str, str]:
    """Validate `<PREFIX>-NNN` and return `(prefix, issue_id)`.

    Raises HistoryError on a bad shape.
    """
    if not isinstance(issue_id, str) or not issue_id:
        raise HistoryError("invalid ID: empty")
    m = _ID_RE.match(issue_id)
    if not m:
        raise HistoryError(
            f"invalid ID '{issue_id}' (expected <PREFIX>-NNN, "
            f"e.g. IM-030 or DOUG-035)"
        )
    return m.group(1), issue_id


def rel_path_for(issue_id: str) -> str:
    """Return the store-relative path for `issue_id`.

    The store layout is `<store>/<PREFIX>/<ID>.md`; for git log we
    pass the path relative to the store root. Raises HistoryError on
    a bad ID.
    """
    prefix, full = parse_id(issue_id)
    return f"{prefix}/{full}.md"


def parse_git_log(text: str) -> List[Commit]:
    """Parse the unit-separator-delimited output of git log.

    Each line is `<sha>\\x1f<iso-date>\\x1f<author>\\x1f<subject>`.
    Empty input returns []. Lines with the wrong field count are
    silently skipped -- the format is under our control, so this is
    only defensive.
    """
    commits: List[Commit] = []
    for raw in text.splitlines():
        if not raw:
            continue
        parts = raw.split(_FIELD_SEP)
        if len(parts) != 4:
            continue
        sha, date, author, subject = parts
        commits.append(Commit(sha=sha, date=date, author=author, subject=subject))
    return commits


def git_log(
    store_root: Path,
    rel_path: str,
    limit: Optional[int] = None,
) -> List[Commit]:
    """Invoke `git log` against `rel_path` inside `store_root`.

    Returns the parsed commit list. An empty history (uncommitted
    file, or path matches no commits) returns []; the caller decides
    how to render that.

    Raises HistoryError if `store_root` is not a git repository or
    if `git` is unavailable. Raises subprocess.CalledProcessError on
    any other git failure (the caller renders).
    """
    if not (store_root / ".git").exists():
        raise HistoryError(
            f"store root '{store_root}' is not a git repository "
            f"(no .git directory)"
        )
    try:
        # Empty repo (no HEAD yet) -- git log would exit 128. Treat as
        # "no history" rather than an error: a freshly-init'd store is
        # a legitimate state and the caller's question is still answered.
        head_probe = subprocess.run(
            ["git", "-C", str(store_root), "rev-parse", "--verify", "--quiet", "HEAD"],
            capture_output=True,
            text=True,
        )
        if head_probe.returncode != 0:
            return []
        cmd = ["git", "-C", str(store_root), "log", f"--format={GIT_LOG_FORMAT}"]
        if limit is not None:
            cmd.extend(["-n", str(limit)])
        cmd.extend(["--follow", "--", rel_path])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise HistoryError(f"git not found on PATH: {e}") from e
    return parse_git_log(result.stdout)


def resolve_ref(store_root: Path, at: str) -> str:
    """Resolve a ref or date specifier to a full 40-char commit SHA.

    Tries `git rev-parse --verify <at>^{commit}` first (matches full
    or short SHAs, tags, branches, and other ref forms). On failure,
    falls back to `git log -1 --format=%H --before=<at> HEAD` to
    handle date inputs via git's approxidate parser (relative forms
    like `2.weeks.ago` or absolute dates like `2026-04-01`).

    Raises HistoryError if `store_root` is not a git repo, if `git`
    is unavailable, or if neither path resolves. A non-resolving
    `<at>` and an empty repo (no HEAD reachable, no commit before
    the date) both surface the same "could not resolve" message --
    the caller's diagnosis is "the ref / date did not pick out a
    commit," and the calling script renders.

    Note on approxidate: git silently falls back to "now" for some
    unparseable date inputs. We pre-flight HEAD so an empty repo
    is reported precisely; for non-empty repos, garbage that
    approxidate maps to "now" will return HEAD. The CLI documents
    this in --help.
    """
    if not (store_root / ".git").exists():
        raise HistoryError(
            f"store root '{store_root}' is not a git repository "
            f"(no .git directory)"
        )
    if not isinstance(at, str) or not at:
        raise HistoryError("empty --at value")
    try:
        head_probe = subprocess.run(
            ["git", "-C", str(store_root), "rev-parse", "--verify", "--quiet", "HEAD"],
            capture_output=True,
            text=True,
        )
        if head_probe.returncode != 0:
            raise HistoryError(
                f"could not resolve '{at}': repository has no commits"
            )
        rp = subprocess.run(
            [
                "git",
                "-C",
                str(store_root),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{at}^{{commit}}",
            ],
            capture_output=True,
            text=True,
        )
        if rp.returncode == 0:
            return rp.stdout.strip()
        # Fallback: treat as a date via approxidate, but only for inputs
        # that look date-like. Without this guard, garbage like "IM-030"
        # would resolve to HEAD silently (approxidate's documented "fall
        # back to now" behavior).
        if _DATE_LIKE_RE.match(at):
            log = subprocess.run(
                [
                    "git",
                    "-C",
                    str(store_root),
                    "log",
                    "-1",
                    "--format=%H",
                    f"--before={at}",
                    "HEAD",
                ],
                capture_output=True,
                text=True,
            )
            if log.returncode == 0 and log.stdout.strip():
                return log.stdout.strip()
            raise HistoryError(
                f"no commit found at or before '{at}'"
            )
    except FileNotFoundError as e:
        raise HistoryError(f"git not found on PATH: {e}") from e
    raise HistoryError(
        f"could not resolve '{at}' as a git ref or date"
    )


def git_show(store_root: Path, sha: str, rel_path: str) -> str:
    """Return the contents of `rel_path` at commit `sha`.

    Wraps `git show <sha>:<rel_path>`. Returns the file body as a
    string. Raises `HistoryError` with a precise "file did not exist
    at <sha>" message when the path is not in that commit's tree;
    other git failures raise HistoryError with the git stderr tail.
    """
    if not (store_root / ".git").exists():
        raise HistoryError(
            f"store root '{store_root}' is not a git repository "
            f"(no .git directory)"
        )
    try:
        result = subprocess.run(
            ["git", "-C", str(store_root), "show", f"{sha}:{rel_path}"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise HistoryError(f"git not found on PATH: {e}") from e
    if result.returncode == 0:
        return result.stdout
    stderr = (result.stderr or "").strip()
    # `git show <sha>:<path>` emits one of:
    #   fatal: path '<path>' exists on disk, but not in '<sha>'
    #   fatal: path '<path>' does not exist in '<sha>'
    # Both mean "the path is not in that commit's tree."
    if (
        "exists on disk, but not in" in stderr
        or "does not exist in" in stderr
    ):
        raise HistoryError(
            f"file '{rel_path}' did not exist at commit {sha}"
        )
    raise HistoryError(
        f"git show failed: {stderr.splitlines()[-1] if stderr else 'unknown error'}"
    )


def validate_prefix(prefix: str) -> None:
    """Validate a `<PREFIX>` shape. Raises HistoryError on bad input."""
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        raise HistoryError(
            f"invalid prefix '{prefix}' "
            f"(expected <PREFIX> matching ^[A-Z][A-Z0-9]{{1,7}}$, "
            f"e.g. IM or DOUG)"
        )


def git_pickaxe(
    store_root: Path,
    term: str,
    prefixes: Optional[List[str]] = None,
    since: Optional[str] = None,
) -> List[Commit]:
    """Run `git log -S <term>` (pickaxe) against the store repo.

    Returns commits whose diff changes the count of `term` in any file
    under `store_root`. When `prefixes` is given, the scan is scoped to
    those `<PREFIX>/` pathspecs (an OR across prefixes). When `since`
    is given, it is passed verbatim to `git log --since` so both
    relative (`2.weeks.ago`) and absolute (`2026-04-01`) forms work.

    Empty repo (no HEAD) returns []. Raises HistoryError if
    `store_root` is not a git repo, if `git` is missing, or if any
    entry in `prefixes` has an invalid shape. Raises
    subprocess.CalledProcessError on any other git failure (the
    caller renders).
    """
    if not (store_root / ".git").exists():
        raise HistoryError(
            f"store root '{store_root}' is not a git repository "
            f"(no .git directory)"
        )
    if prefixes:
        for pfx in prefixes:
            validate_prefix(pfx)
    try:
        head_probe = subprocess.run(
            ["git", "-C", str(store_root), "rev-parse", "--verify", "--quiet", "HEAD"],
            capture_output=True,
            text=True,
        )
        if head_probe.returncode != 0:
            return []
        cmd = ["git", "-C", str(store_root), "log", f"--format={GIT_LOG_FORMAT}"]
        if since:
            cmd.extend(["--since", since])
        cmd.extend(["-S", term, "--"])
        if prefixes:
            cmd.extend(f"{pfx}/" for pfx in prefixes)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise HistoryError(f"git not found on PATH: {e}") from e
    return parse_git_log(result.stdout)


def parse_git_log_with_names(text: str) -> List[Tuple[Commit, List[str]]]:
    """Parse `git log --name-only` output with our custom GIT_LOG_FORMAT.

    Git emits a blank line both between the format header and the
    file list and between consecutive commits, so the on-the-wire
    layout is:

        <sha>\\x1f<iso-date>\\x1f<author>\\x1f<subject>
        <blank>
        path/to/file1
        path/to/file2
        <blank>
        <sha>...

    Strategy: blank lines are pure separators; ignore them. A line
    that splits cleanly into four unit-separator-delimited fields
    starts a new commit; any other non-blank line is a file path
    belonging to the current commit. Empty input returns [].
    Defensive against malformed input -- header-shaped lines with
    the wrong field count are not treated as headers, and stray file
    lines that appear before any header are silently dropped.
    """
    commits: List[Tuple[Commit, List[str]]] = []
    current: Optional[Commit] = None
    current_files: List[str] = []
    for raw in text.splitlines():
        if not raw:
            continue
        parts = raw.split(_FIELD_SEP)
        if len(parts) == 4:
            if current is not None:
                commits.append((current, current_files))
            current = Commit(
                sha=parts[0], date=parts[1], author=parts[2], subject=parts[3]
            )
            current_files = []
        elif current is not None:
            current_files.append(raw)
        # else: file line before any header -- drop silently.
    if current is not None:
        commits.append((current, current_files))
    return commits


def git_activity(
    store_root: Path,
    since: Optional[str] = None,
    prefixes: Optional[List[str]] = None,
    limit: Optional[int] = None,
) -> List[IssueActivity]:
    """Group recent commits by issue file inside `store_root`.

    Runs `git log --name-only [--since DATE] -- [<PREFIX>/...]` and
    bins commits by the issue file each one touches. Only paths
    matching `<PREFIX>/<ID>.md` (with `<PREFIX>` matching the id's
    prefix) are counted; INDEX.md, `.repos`, and any commits that
    touch only non-issue files are dropped. This is the "rebased
    migration commit" graceful case.

    `since` is passed verbatim to `git log --since` (caller's choice
    of relative or absolute form). `prefixes` scopes the scan to one
    or more `<PREFIX>/` pathspecs (OR within the flag). `limit` caps
    the number of *issues* returned (after sorting), not the number
    of underlying commits.

    Result ordering: by `latest.date` descending, ties broken by
    `issue_id` ascending (deterministic).

    Empty repo (no HEAD) returns []. Raises HistoryError if
    `store_root` is not a git repo, if `git` is missing, or if any
    entry in `prefixes` has an invalid shape. Raises
    `subprocess.CalledProcessError` on any other git failure (the
    caller renders).
    """
    if not (store_root / ".git").exists():
        raise HistoryError(
            f"store root '{store_root}' is not a git repository "
            f"(no .git directory)"
        )
    if prefixes:
        for pfx in prefixes:
            validate_prefix(pfx)
    try:
        head_probe = subprocess.run(
            ["git", "-C", str(store_root), "rev-parse", "--verify", "--quiet", "HEAD"],
            capture_output=True,
            text=True,
        )
        if head_probe.returncode != 0:
            return []
        cmd = [
            "git",
            "-C",
            str(store_root),
            "log",
            f"--format={GIT_LOG_FORMAT}",
            "--name-only",
        ]
        if since:
            cmd.extend(["--since", since])
        cmd.append("--")
        if prefixes:
            cmd.extend(f"{pfx}/" for pfx in prefixes)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise HistoryError(f"git not found on PATH: {e}") from e

    # Bin commits by issue id. git log returns commits newest-first,
    # so the first commit we see for a given id is its `latest`.
    by_id: dict = {}
    for commit, files in parse_git_log_with_names(result.stdout):
        for f in files:
            m = _ISSUE_PATH_RE.match(f)
            if not m:
                continue
            issue_id = m.group("id")
            prefix = m.group("prefix")
            entry = by_id.get(issue_id)
            if entry is None:
                by_id[issue_id] = {
                    "prefix": prefix,
                    "file": f,
                    "count": 1,
                    "latest": commit,
                }
            else:
                entry["count"] += 1

    activities = [
        IssueActivity(
            issue_id=iid,
            prefix=d["prefix"],
            file=d["file"],
            commit_count=d["count"],
            latest=d["latest"],
        )
        for iid, d in by_id.items()
    ]
    # Stable two-step sort: ascending by id first, then descending by
    # date. Python's sort is stable, so equal-date entries end up in
    # id-ascending order.
    activities.sort(key=lambda a: a.issue_id)
    activities.sort(key=lambda a: a.latest.date, reverse=True)

    if limit is not None:
        activities = activities[:limit]
    return activities
