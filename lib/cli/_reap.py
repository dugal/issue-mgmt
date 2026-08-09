"""In-process expired-lease reaper for the `start` / `ready` verbs (WP3).

The evidence review (design 2a "Reap amendment") found the reaper is
session-start *flow*, not admin: organic use stopped the day the
`/issue-start` skill stopped prompting it. So `issue start` and
`issue ready` run the reap automatically before they act, while the
manual `issue admin reap` override (WP5) keeps the flat
`issue-reap-expired-claims` surface (`--dry-run`, exit codes, JSON).

This helper is the auto-reap path only: best-effort and in-process (no
subprocess -- the flat `issue-reap-expired-claims` binary becomes a
tombstone at WP7, so shelling out to it would break post-consolidation).
It reuses the same ``lib`` primitives (`lease`, `frontmatter`,
`concurrency`) the flat reaper does, so a reaped issue is committed with
the identical `reap <id>` message. Any per-issue failure (lock timeout,
non-git store, commit rejection) is swallowed -- an auto-reap must never
break the start/ready operation it precedes.

Named with a leading underscore so the verb-discovery walk in
``lib/cli/__init__`` skips it (it declares no ``VERB``).
"""

from __future__ import annotations

import datetime
import os
import re

import concurrency
import frontmatter
import lease

NOTES_SECTION = "Implementation Notes"
_PREFIX_DIR_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")


def _append_note_line(body_text, note):
    if frontmatter.find_section(body_text, NOTES_SECTION) is None:
        if body_text and not body_text.endswith("\n"):
            body_text += "\n"
        if body_text and not body_text.endswith("\n\n"):
            body_text += "\n"
        body_text += f"## {NOTES_SECTION}\n"
    return frontmatter.edit_section(body_text, NOTES_SECTION, "append", note)


def _iter_issue_files(store_root, repo_filter):
    for child in sorted(store_root.iterdir()):
        if not child.is_dir():
            continue
        prefix = child.name
        if not _PREFIX_DIR_RE.match(prefix):
            continue
        if repo_filter is not None and prefix != repo_filter:
            continue
        for f in sorted(child.glob(f"{prefix}-*.md")):
            yield prefix, f


def _reap_one(store_root, prefix, issue_file, issue_id, lock_timeout, now):
    """Lock + re-verify expiry + clear claim + note + commit one issue.

    Returns the reaped record, or None when the claim was renewed /
    released / re-claimed between the unlocked scan and the lock (the
    reaper never touches a valid lease).
    """
    today = datetime.date.today().isoformat()
    lock_path = concurrency.issue_lock_path(store_root, prefix, issue_id)
    autocommit_enabled = not os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT")
    with concurrency.flock_acquire(
        lock_path, timeout=lock_timeout, resource=str(issue_file)
    ):
        snapshot = concurrency.capture_state(issue_file)
        if snapshot.is_absent():
            return None
        fm_lines, body_text = frontmatter.read_issue(issue_file)
        claim = lease.get_claim(fm_lines)
        if claim is None or lease.lease_is_active(fm_lines, now=now):
            return None
        lease_until = lease.get_lease_until(fm_lines)
        try:
            generation = lease.get_claim_generation(fm_lines)
        except ValueError:
            generation = None
        body_text = _append_note_line(
            body_text,
            f"{today}: claim: expired lease for {claim.get('assignee')} "
            f"reaped (lease_until {lease_until}, generation "
            f"{generation} preserved)",
        )
        lease.clear_claim(fm_lines)
        frontmatter.set_field(fm_lines, "updated", today)
        new_text = frontmatter.serialize_issue(fm_lines, body_text)

        if autocommit_enabled:
            rel = issue_file.resolve().relative_to(store_root.resolve())
            with concurrency.store_mutation(
                store_root, [rel], f"reap {issue_id}"
            ):
                concurrency.verify_unchanged(
                    issue_file, snapshot, resource=str(issue_file)
                )
                concurrency.atomic_write(issue_file, new_text)
        else:
            concurrency.verify_unchanged(
                issue_file, snapshot, resource=str(issue_file)
            )
            concurrency.atomic_write(issue_file, new_text)

    return {
        "id": issue_id,
        "assignee": claim.get("assignee"),
        "lease_until": lease_until,
        "claim_generation": generation,
    }


def auto_reap(store_root, *, repo=None, lock_timeout=None):
    """Best-effort reap of every expired lease under the store.

    `repo` restricts to one prefix directory. Returns the list of reaped
    records. Never raises: any per-issue failure is swallowed so the
    caller (start/ready) proceeds regardless. A None or non-directory
    store, or a legacy (unmigrated) tree, is a silent no-op.
    """
    if store_root is None:
        return []
    try:
        if not store_root.is_dir():
            return []
    except OSError:
        return []
    if lock_timeout is None:
        lock_timeout = concurrency.DEFAULT_LOCK_TIMEOUT
    now = lease.now_utc()
    reaped = []
    try:
        candidates = list(_iter_issue_files(store_root, repo))
    except OSError:
        return reaped
    for prefix, path in candidates:
        try:
            fm_lines, _body = frontmatter.read_issue(path)
        except (OSError, ValueError):
            continue
        try:
            claim = lease.get_claim(fm_lines)
            if claim is None or lease.lease_is_active(fm_lines, now=now):
                continue
        except ValueError:
            continue
        try:
            record = _reap_one(
                store_root, prefix, path, path.stem, lock_timeout, now
            )
        except Exception:
            # Best-effort: a lock timeout, non-git store, or commit
            # rejection must not break the start/ready op that follows.
            continue
        if record is not None:
            reaped.append(record)
    return reaped
