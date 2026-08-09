"""`issue transition` -- the state-machine primitive + TTL lease ops.

Port of the flat `transition-issue` binary onto the
consolidated dispatcher: the D11 legal-transition matrix (strict), the
`--reason` requirement for `blocked`, the auto-stamp of `updated` + the
timestamped audit line, and the lease primitive
(`--claim`/`--renew`/`--release`/`--lease-ttl`/`--claim-generation`).

This is a dispatcher port, NOT a rewrite (design section 6): the data
path (`lib/concurrency.py`, `lib/frontmatter.py`, `lib/lease.py`,
`lib/errors.py`) is untouched; only the argument surface moved. The
concurrency + one-commit-per-mutation contract, the 3-7 loser-side
exit codes, and the `--json` shapes are byte-identical to the flat
binary. The one deliberate mechanism change is auto-reindex: mutating
verbs regenerate INDEX in-process via ``reindex.reindex_after_mutation``
(the WP0 seam) instead of resolving and subprocessing a sibling binary --
which structurally removes the failure class.

The five MCP-aligned convenience verbs (`start`, `complete`, `cancel`,
`block`, `unblock`) live in their own modules and reuse the shared
``_execute_transition`` / ``_execute_lease`` cores exported here, so the
state-machine logic has exactly one home.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import concurrency  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import errors  # noqa: E402
import frontmatter  # noqa: E402
import idresolve  # noqa: E402
import lease  # noqa: E402
import reindex  # noqa: E402

from . import common

VERB = "transition"
TIER = "tier1"
SUMMARY = "change an issue's status through the legal state machine (primitive)"
ORDER = 30

CANONICAL_STATES = ("open", "in-progress", "blocked", "done", "cancelled")
TERMINAL_STATES = frozenset({"done", "cancelled"})
ACTIVE_STATES = frozenset({"open", "in-progress", "blocked"})

# Single source of truth for legal transitions. Keys are the current
# state, values are the set of legal next states (excluding the
# same-state no-op, which the caller handles separately).
LEGAL_TRANSITIONS = {
    "open":        frozenset({"in-progress", "blocked", "cancelled"}),
    "in-progress": frozenset({"open", "blocked", "done", "cancelled"}),
    "blocked":     frozenset({"open", "in-progress", "cancelled"}),
    "done":        frozenset({"open", "in-progress", "blocked"}),
    "cancelled":   frozenset({"open", "in-progress", "blocked"}),
}

NOTES_SECTION = "Implementation Notes"

_DESCRIPTION = """\
Change an issue's status through the legal state machine (the primitive
the start/complete/cancel/block/unblock convenience verbs wrap). Sole
owner of the `status:` field; `issue edit --set status=...` refuses.

Canonical states (docs/spec.md D11):
  open          created, not yet started
  in-progress   claimed by a branch + actor
  blocked       waiting on an external condition (must record a reason)
  done          complete
  cancelled     intentionally not pursued; file remains

Legal transition matrix (single source of truth):

  From / To      open  in-progress  blocked  done  cancelled
  open           -     yes          yes      -     yes
  in-progress    yes   -            yes      yes   yes
  blocked        yes   yes          -        -     yes
  done           yes   yes          yes      -     -
  cancelled      yes   yes          yes      -     -

  - From any non-terminal state you can reach any other state.
  - From a terminal state (done, cancelled) you can reopen into any
    active state but not flip directly between terminals.
  - Same-state transitions are no-ops and exit 0.

TTL lease primitive: --claim CLAIMANT acquires a lease and flips
status to in-progress; --renew extends it; --release clears it preserving
the fencing counter. --lease-ttl overrides the 3600s default;
--claim-generation is the opt-in caller-side fencing guard."""

_EPILOG = """\
Exit codes (contiguous contract; 3 was 17 through v2.4.1):
  0 success   1 not found (state paths)   2 usage / illegal transition
  3 concurrent-edit   4 lost-race   5 stale-local-view   7 not-claimable

Examples:
  issue transition ABC-123 in-progress --branch phase-x
  issue transition ABC-123 blocked --reason "waiting on XYZ-9"
  issue transition ABC-123 done --json
  issue transition ABC-123 --claim alice --lease-ttl 7200 --json
  issue transition ABC-123 --renew --claim-generation 8
  issue transition ABC-123 --release
"""


class IllegalTransition(Exception):
    def __init__(self, current, target, legal):
        self.current = current
        self.target = target
        self.legal = sorted(legal)
        super().__init__(
            f"illegal transition: {current!r} -> {target!r}; "
            f"legal next states from {current!r}: "
            f"{{{', '.join(self.legal) if self.legal else '(none)'}}}"
        )


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("issue_id", nargs="?", metavar="ID")
    p.add_argument("new_state", nargs="?", metavar="STATE")
    p.add_argument("--branch", default=None, metavar="NAME")
    p.add_argument("--reason", "--summary", "--note", dest="reason",
                   default=None, metavar="TEXT")
    p.add_argument("--claim", default=None, metavar="CLAIMANT")
    p.add_argument("--renew", action="store_true")
    p.add_argument("--release", action="store_true")
    p.add_argument(
        "--lease-ttl", type=int, default=None, metavar="SECONDS",
        dest="lease_ttl",
    )
    p.add_argument(
        "--claim-generation", type=int, default=None, metavar="N",
        dest="claim_generation",
    )
    common.add_json_flag(p)
    common.add_no_reindex_flag(p)
    common.add_lock_timeout_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


# --- path resolution -------------------------------------------------------


def resolve_paths(issue_id, store, *, emit_note=None):
    """Return (issue_file, lock_path, store_root, anchor, canonical_id).

    Store-root resolution goes through the shared ``common`` helpers so
    every verb resolves identically (design 4.Q4); id/prefix
    canonicalization and the path build go through ``idresolve``,
    so ``IM-7`` / ``im-7`` resolve the on-disk file and the lock path is
    built from the canonical stem (the P2 fix). Raises ValueError on a
    malformed id -- surfaced by the caller as exit 2 -- and
    ``errors.StoreIntegrityError`` (exit 1) on a D2 dup-padding collision.
    """
    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, store)
    result = idresolve.resolve_issue_paths(
        issue_id, store_root, anchor, emit_note=emit_note
    )
    return (
        result.issue_file, result.lock_path, result.store_root,
        result.anchor, result.canonical_id,
    )


def run_reindex(store_root, anchor, *, suppress_commit=False, to_stderr=False):
    """Regenerate INDEX.md in-process via the WP0 seam.

    Honors ISSUE_MGMT_NO_AUTOCOMMIT the way the flat subprocess path did
    through env inheritance: when set, the reindex writes but opens no
    commit of its own (an outer mutation owns the commit).
    """
    if os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT"):
        suppress_commit = True
    reindex.reindex_after_mutation(
        store_root, anchor, suppress_commit=suppress_commit,
        to_stderr=to_stderr,
    )


# --- note helpers (ported verbatim) ----------------------------------------


def append_note_line(body_text, note):
    """Append one line to ## Implementation Notes (create if absent)."""
    if frontmatter.find_section(body_text, NOTES_SECTION) is None:
        if body_text and not body_text.endswith("\n"):
            body_text += "\n"
        if body_text and not body_text.endswith("\n\n"):
            body_text += "\n"
        body_text += f"## {NOTES_SECTION}\n"
    return frontmatter.edit_section(body_text, NOTES_SECTION, "append", note)


def append_transition_note(body_text, today, old_state, new_state, reason):
    note = f"{today}: status: {old_state} -> {new_state}"
    if reason:
        note += f" -- {reason}"
    return append_note_line(body_text, note)


def check_transition(current, target):
    """Raise IllegalTransition if target is not reachable from current.

    Same-state transitions are allowed (caller treats them as no-ops).
    """
    if target == current:
        return
    legal = LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in legal:
        raise IllegalTransition(current, target, legal)


def check_generation_guard(fm_lines, caller_generation, *, require_active):
    """Opt-in caller-side fencing guard (W0a F3). No-op when the caller
    did not pass --claim-generation."""
    if caller_generation is None:
        return
    current = lease.get_claim_generation(fm_lines)
    if current != caller_generation:
        raise errors.StaleViewError(
            "claim generation drifted since caller's read",
            current_generation=current,
            caller_generation=caller_generation,
        )
    if require_active and current > 0 and not lease.lease_is_active(fm_lines):
        claim = lease.get_claim(fm_lines) or {}
        raise errors.LeaseExpiredError(
            "caller's generation matches but the lease is expired "
            "or released",
            actor=claim.get("assignee"),
            lease_until=lease.get_lease_until(fm_lines),
        )


def _do_write(issue_file, snapshot, new_text, *, do_autocommit,
              store_root, commit_paths, commit_message, reindex_fn):
    """Shared verify + atomic-write + (optional) commit + reindex block.

    Byte-identical to the two copies in the flat binary: when autocommit
    is on the write and the in-mutation reindex land in one commit named
    by commit_message; otherwise both run bare.
    """
    if do_autocommit:
        with concurrency.store_mutation(store_root, commit_paths, commit_message):
            concurrency.verify_unchanged(
                issue_file, snapshot, resource=str(issue_file)
            )
            concurrency.atomic_write(issue_file, new_text)
            if reindex_fn is not None:
                reindex_fn(suppress_commit=True)
    else:
        concurrency.verify_unchanged(
            issue_file, snapshot, resource=str(issue_file)
        )
        concurrency.atomic_write(issue_file, new_text)
        if reindex_fn is not None:
            reindex_fn(suppress_commit=False)


# --- plain state transition ------------------------------------------------


def apply_transition(issue_file, lock_path, target_state, branch, reason,
                     lock_timeout, *, caller_generation=None, store_root=None,
                     commit_paths=None, commit_message=None, reindex_fn=None):
    """Lock + read + transition + verify + write. Returns (summary, text)."""
    today = datetime.date.today().isoformat()
    with concurrency.flock_acquire(
        lock_path, timeout=lock_timeout, resource=str(issue_file)
    ):
        snapshot = concurrency.capture_state(issue_file)
        if snapshot.is_absent():
            raise FileNotFoundError(str(issue_file))

        fm_lines, body_text = frontmatter.read_issue(issue_file)
        current = frontmatter.get_field(fm_lines, "status")
        if current is None:
            raise ValueError(
                f"{issue_file}: missing 'status' field in frontmatter"
            )

        check_generation_guard(fm_lines, caller_generation, require_active=True)
        check_transition(current, target_state)

        no_op = (current == target_state)
        summary = {
            "id_state_change": {
                "old": current, "new": target_state, "no_op": no_op,
            },
            "branch_change": None,
            "reason": reason,
            "note_appended": False,
        }

        if no_op and branch is None and reason is None:
            return summary, None  # nothing to write

        if not no_op:
            frontmatter.set_field(fm_lines, "status", target_state)

        if branch is not None:
            old_branch = frontmatter.get_field(fm_lines, "branch")
            frontmatter.set_field(fm_lines, "branch", branch)
            summary["branch_change"] = {"old": old_branch, "new": branch}
        elif target_state in TERMINAL_STATES:
            # A done/cancelled issue has no active branch: clear a
            # stale branch: field so the lifecycle convention (terminal
            # transitions clear branch) holds. An explicit --branch wins above.
            old_branch = frontmatter.get_field(fm_lines, "branch")
            if old_branch:
                frontmatter.set_field(fm_lines, "branch", "")
                summary["branch_change"] = {"old": old_branch, "new": ""}

        if not no_op:
            body_text = append_transition_note(
                body_text, today, current, target_state, reason
            )
            summary["note_appended"] = True
        elif reason:
            body_text = append_transition_note(
                body_text, today, current, target_state, reason
            )
            summary["note_appended"] = True

        frontmatter.set_field(fm_lines, "updated", today)
        new_text = frontmatter.serialize_issue(fm_lines, body_text)

        do_autocommit = (
            store_root is not None
            and commit_paths is not None
            and commit_message is not None
        )
        _do_write(
            issue_file, snapshot, new_text,
            do_autocommit=do_autocommit, store_root=store_root,
            commit_paths=commit_paths, commit_message=commit_message,
            reindex_fn=reindex_fn,
        )

    return summary, new_text


def apply_complete(issue_file, lock_path, reason, lock_timeout, *,
                   caller_generation=None, store_root=None, commit_paths=None,
                   commit_message=None, reindex_fn=None):
    """`issue complete` open -> done pass-through (design 5.5).

    On an `open` issue, performs open -> in-progress -> done as ONE atomic
    operation under a single lock and a single commit, appending BOTH
    audit lines to ## Implementation Notes. On an `in-progress` issue it
    is a plain -> done. From `blocked`/`cancelled` it defers to the strict
    D11 matrix (IllegalTransition, exit 2). The `transition` primitive is
    never taught open -> done; only this convenience verb papers over the
    two-step (the #1 usage error in the session-log corpus).
    """
    today = datetime.date.today().isoformat()
    with concurrency.flock_acquire(
        lock_path, timeout=lock_timeout, resource=str(issue_file)
    ):
        snapshot = concurrency.capture_state(issue_file)
        if snapshot.is_absent():
            raise FileNotFoundError(str(issue_file))

        fm_lines, body_text = frontmatter.read_issue(issue_file)
        current = frontmatter.get_field(fm_lines, "status")
        if current is None:
            raise ValueError(
                f"{issue_file}: missing 'status' field in frontmatter"
            )

        check_generation_guard(fm_lines, caller_generation, require_active=True)

        passthrough = (current == "open")
        summary = {
            "id_state_change": {
                "old": current, "new": "done", "no_op": current == "done",
            },
            "branch_change": None,
            "reason": reason,
            "note_appended": False,
            "passthrough": passthrough,
        }

        if current == "done":
            if reason is None:
                return summary, None  # same-state no-op, nothing to write
            body_text = append_transition_note(
                body_text, today, current, "done", reason
            )
            summary["note_appended"] = True
        elif passthrough:
            # open -> in-progress -> done in one write; two audit lines.
            check_transition("open", "in-progress")
            check_transition("in-progress", "done")
            frontmatter.set_field(fm_lines, "status", "done")
            body_text = append_transition_note(
                body_text, today, "open", "in-progress", None
            )
            body_text = append_transition_note(
                body_text, today, "in-progress", "done", reason
            )
            summary["note_appended"] = True
        else:
            check_transition(current, "done")  # blocked/cancelled -> exit 2
            frontmatter.set_field(fm_lines, "status", "done")
            body_text = append_transition_note(
                body_text, today, current, "done", reason
            )
            summary["note_appended"] = True

        # A completed issue has no active branch: clear a stale
        # branch: field so the lifecycle convention (done clears branch) holds.
        old_branch = frontmatter.get_field(fm_lines, "branch")
        if old_branch:
            frontmatter.set_field(fm_lines, "branch", "")
            summary["branch_change"] = {"old": old_branch, "new": ""}

        frontmatter.set_field(fm_lines, "updated", today)
        new_text = frontmatter.serialize_issue(fm_lines, body_text)

        do_autocommit = (
            store_root is not None
            and commit_paths is not None
            and commit_message is not None
        )
        _do_write(
            issue_file, snapshot, new_text,
            do_autocommit=do_autocommit, store_root=store_root,
            commit_paths=commit_paths, commit_message=commit_message,
            reindex_fn=reindex_fn,
        )

    return summary, new_text


# --- lease primitive -------------------------------------------------------


def lease_result(issue_id, fm_lines, *, no_op=False):
    claim = lease.get_claim(fm_lines)
    claim_obj = None
    if claim is not None:
        claim_obj = {
            "assignee": claim.get("assignee"),
            "claimed_at": claim.get("claimed_at"),
            "lease_until": lease.get_lease_until(fm_lines),
        }
    return {
        "id": issue_id,
        "status": frontmatter.get_field(fm_lines, "status"),
        "claim": claim_obj,
        "claim_generation": lease.get_claim_generation(fm_lines),
        "no_op": no_op,
    }


def apply_lease_op(issue_file, lock_path, issue_id, op, claimant, lease_ttl,
                   caller_generation, lock_timeout, *, branch=None,
                   store_root=None, commit_paths=None, commit_message=None,
                   reindex_fn=None):
    """Lock + read + lease mutation + verify + write.

    `op` is "claim", "renew", or "release". `branch` (start convenience
    only) sets the `branch:` field in the same claim write; the primitive
    never passes it (flat rejects --branch with a lease op).
    """
    today = datetime.date.today().isoformat()
    with concurrency.flock_acquire(
        lock_path, timeout=lock_timeout, resource=str(issue_file)
    ):
        snapshot = concurrency.capture_state(issue_file)
        if snapshot.is_absent():
            raise errors.IssueNotFoundError(
                f"issue file not found: {issue_file}", issue_id=issue_id
            )

        fm_lines, body_text = frontmatter.read_issue(issue_file)
        current_status = frontmatter.get_field(fm_lines, "status")
        if current_status is None:
            raise ValueError(
                f"{issue_file}: missing 'status' field in frontmatter"
            )

        check_generation_guard(fm_lines, caller_generation, require_active=False)

        generation = lease.get_claim_generation(fm_lines)
        claim = lease.get_claim(fm_lines)
        lease_until = lease.get_lease_until(fm_lines)
        active = lease.lease_is_active(fm_lines)
        now = lease.now_utc()
        now_ts = lease.format_ts(now)

        wrote_branch = False

        if op == "claim":
            if active:
                if claim.get("assignee") == claimant:
                    # Same-actor re-claim within an unexpired lease: pure
                    # no-op. When start supplies a branch we still record
                    # it (the claim itself is a no-op, but the branch may
                    # be new); otherwise nothing to write.
                    if branch is None:
                        return lease_result(issue_id, fm_lines, no_op=True)
                else:
                    raise errors.LostRaceError(
                        "claim held by another actor",
                        contender=claim.get("assignee"),
                        claimed_at=claim.get("claimed_at"),
                        lease_until=lease_until,
                    )
            if not active:
                if current_status not in ("open", "in-progress"):
                    raise errors.NotClaimableError(
                        f"status {current_status!r} is not claimable",
                        status=current_status,
                    )
                new_generation = generation + 1
                until_ts = lease.format_ts(
                    now + datetime.timedelta(seconds=lease_ttl)
                )
                lease.set_claim(
                    fm_lines, claimant, now_ts, until_ts, new_generation
                )
                if current_status == "open":
                    frontmatter.set_field(fm_lines, "status", "in-progress")
                    body_text = append_transition_note(
                        body_text, today, "open", "in-progress",
                        f"claimed by {claimant} (generation {new_generation}, "
                        f"lease until {until_ts})",
                    )
                else:
                    body_text = append_note_line(
                        body_text,
                        f"{today}: claim: acquired by {claimant} "
                        f"(generation {new_generation}, lease until {until_ts})",
                    )
        elif op == "renew":
            if claim is None:
                raise errors.NotClaimableError(
                    "no claim to renew", status=current_status,
                    reason="no-claim",
                )
            if not active:
                raise errors.LeaseExpiredError(
                    "lease already expired; re-claim to start a new era",
                    status=current_status, actor=claim.get("assignee"),
                    lease_until=lease_until,
                )
            until_ts = lease.format_ts(
                now + datetime.timedelta(seconds=lease_ttl)
            )
            lease.set_lease_until(fm_lines, until_ts)
        elif op == "release":
            if claim is None:
                return lease_result(issue_id, fm_lines, no_op=True)
            body_text = append_note_line(
                body_text,
                f"{today}: claim: released (was {claim.get('assignee')}, "
                f"generation {generation} preserved)",
            )
            lease.clear_claim(fm_lines)
        else:  # pragma: no cover -- dispatcher guards op values
            raise ValueError(f"unknown lease op: {op!r}")

        if branch is not None:
            frontmatter.set_field(fm_lines, "branch", branch)
            wrote_branch = True

        frontmatter.set_field(fm_lines, "updated", today)
        new_text = frontmatter.serialize_issue(fm_lines, body_text)

        do_autocommit = (
            store_root is not None
            and commit_paths is not None
            and commit_message is not None
        )
        _do_write(
            issue_file, snapshot, new_text,
            do_autocommit=do_autocommit, store_root=store_root,
            commit_paths=commit_paths, commit_message=commit_message,
            reindex_fn=reindex_fn,
        )

        return lease_result(issue_id, fm_lines, no_op=False)


def format_lease_human(op, result):
    issue_id = result["id"]
    claim = result.get("claim") or {}
    generation = result["claim_generation"]
    if op == "claim":
        if result.get("no_op"):
            return (
                f"Claim already held on {issue_id} by "
                f"{claim.get('assignee')} (generation {generation}, "
                f"lease until {claim.get('lease_until')})."
            )
        return (
            f"Claim acquired on {issue_id} for {claim.get('assignee')} "
            f"(generation {generation}, lease until "
            f"{claim.get('lease_until')})."
        )
    if op == "renew":
        return (
            f"Lease renewed on {issue_id} for {claim.get('assignee')} "
            f"(generation {generation}, lease until "
            f"{claim.get('lease_until')})."
        )
    if result.get("no_op"):
        return f"No claim to release on {issue_id} (generation {generation})."
    return (
        f"Claim released on {issue_id} "
        f"(generation {generation} preserved)."
    )


# --- shared execution cores (used by the convenience verbs too) ------------


def _commit_plan(issue_id, store_root, issue_file, autocommit_enabled,
                 *, lease_op, new_state, reason):
    """Compute (commit_paths, commit_message) for one mutation, byte-for-
    byte as the flat binary did. Returns (None, None) when autocommit is
    off (legacy mode or ISSUE_MGMT_NO_AUTOCOMMIT)."""
    if not autocommit_enabled:
        return None, None
    m = common.ID_RE.match(issue_id)
    prefix = m.group(1)
    store_resolved = store_root.resolve()
    issue_rel = issue_file.resolve().relative_to(store_resolved)
    index_paths = [issue_rel, Path(prefix) / "INDEX.md", Path("INDEX.md")]
    if lease_op in ("renew", "release"):
        return [issue_rel], f"{lease_op} {issue_id}"
    if lease_op == "claim":
        return index_paths, f"claim {issue_id}"
    msg = f"transition {issue_id} {new_state}"
    if reason:
        msg += f" -- {reason}"
    return index_paths, msg


def _execute_transition(issue_id, new_state, *, branch=None, reason=None,
                        claim_generation=None, json_mode=False,
                        no_reindex=False, lock_timeout=None, store=None,
                        prog="issue transition", complete_passthrough=False):
    """Resolve + validate + apply a plain state transition (or the
    `complete` open -> done pass-through) with the full commit pipeline
    and the 3-7 loser-side error contract. Returns an exit code."""
    if lock_timeout is None:
        lock_timeout = concurrency.DEFAULT_LOCK_TIMEOUT

    if new_state == "blocked" and not reason:
        print(f"{prog}: --reason is required when transitioning to 'blocked'",
              file=sys.stderr)
        return errors.EXIT_USAGE

    def _note(before, after):
        print(f"{prog}: resolved {before} -> {after}", file=sys.stderr)

    try:
        issue_file, lock_path, store_root, anchor, issue_id = resolve_paths(
            issue_id, store, emit_note=_note
        )
    except ValueError as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return errors.EXIT_USAGE
    except errors.StoreIntegrityError as e:
        print(e.format_stderr(prog, json_mode=json_mode), file=sys.stderr)
        return e.exit_code

    if not issue_file.exists():
        print(f"{prog}: issue file not found: {issue_file}", file=sys.stderr)
        return errors.EXIT_PRECONDITION

    autocommit_enabled = (
        store_root is not None
        and not os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT")
    )
    commit_paths, commit_message = _commit_plan(
        issue_id, store_root, issue_file, autocommit_enabled,
        lease_op=None, new_state=new_state, reason=reason,
    )

    reindex_wanted = not no_reindex
    reindex_fn = None
    in_mutation_reindex_ran = [False]
    if reindex_wanted:
        def reindex_fn(*, suppress_commit=False):
            in_mutation_reindex_ran[0] = True
            run_reindex(store_root, anchor, suppress_commit=suppress_commit,
                        to_stderr=json_mode)

    try:
        if complete_passthrough:
            summary, _ = apply_complete(
                issue_file, lock_path, reason, lock_timeout,
                caller_generation=claim_generation,
                store_root=store_root if autocommit_enabled else None,
                commit_paths=commit_paths, commit_message=commit_message,
                reindex_fn=reindex_fn,
            )
        else:
            summary, _ = apply_transition(
                issue_file, lock_path, new_state, branch, reason, lock_timeout,
                caller_generation=claim_generation,
                store_root=store_root if autocommit_enabled else None,
                commit_paths=commit_paths, commit_message=commit_message,
                reindex_fn=reindex_fn,
            )
    except concurrency.StoreCommitError as e:
        print(e.format_stderr(prog), file=sys.stderr)
        return errors.EXIT_PRECONDITION
    except errors.ContractError as e:
        print(e.format_stderr(prog, json_mode=json_mode), file=sys.stderr)
        return e.exit_code
    except IllegalTransition as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return errors.EXIT_USAGE
    except FileNotFoundError as e:
        print(f"{prog}: file disappeared between resolve and read: {e}",
              file=sys.stderr)
        return errors.EXIT_PRECONDITION
    except ValueError as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return errors.EXIT_PRECONDITION

    if json_mode:
        json.dump(
            {"file": str(issue_file), "id": issue_id, "changes": summary},
            sys.stdout, indent=2, sort_keys=False,
        )
        sys.stdout.write("\n")

    if reindex_wanted and not in_mutation_reindex_ran[0]:
        run_reindex(store_root, anchor, to_stderr=json_mode)

    return errors.EXIT_SUCCESS


def _execute_lease(issue_id, op, *, claimant=None, lease_ttl=None,
                   claim_generation=None, branch=None, json_mode=False,
                   no_reindex=False, lock_timeout=None, store=None,
                   prog="issue transition"):
    """Resolve + apply a lease op (claim/renew/release) with the full
    commit pipeline and the 3-7 contract. Returns an exit code."""
    if lock_timeout is None:
        lock_timeout = concurrency.DEFAULT_LOCK_TIMEOUT

    def _note(before, after):
        print(f"{prog}: resolved {before} -> {after}", file=sys.stderr)

    try:
        issue_file, lock_path, store_root, anchor, issue_id = resolve_paths(
            issue_id, store, emit_note=_note
        )
    except ValueError as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return errors.EXIT_USAGE
    except errors.StoreIntegrityError as e:
        print(e.format_stderr(prog, json_mode=json_mode), file=sys.stderr)
        return e.exit_code

    if not issue_file.exists():
        e = errors.IssueNotFoundError(
            f"issue file not found: {issue_file}", issue_id=issue_id,
        )
        print(e.format_stderr(prog, json_mode=json_mode), file=sys.stderr)
        return e.exit_code

    autocommit_enabled = (
        store_root is not None
        and not os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT")
    )
    commit_paths, commit_message = _commit_plan(
        issue_id, store_root, issue_file, autocommit_enabled,
        lease_op=op, new_state=None, reason=None,
    )

    # Renew/release never change status, so INDEX cannot go stale.
    reindex_wanted = not no_reindex and op not in ("renew", "release")
    reindex_fn = None
    if reindex_wanted:
        def reindex_fn(*, suppress_commit=False):
            run_reindex(store_root, anchor, suppress_commit=suppress_commit,
                        to_stderr=json_mode)

    try:
        result = apply_lease_op(
            issue_file, lock_path, issue_id, op, claimant,
            lease_ttl or lease.DEFAULT_LEASE_TTL, claim_generation, lock_timeout,
            branch=branch,
            store_root=store_root if autocommit_enabled else None,
            commit_paths=commit_paths, commit_message=commit_message,
            reindex_fn=reindex_fn,
        )
    except concurrency.StoreCommitError as e:
        print(e.format_stderr(prog), file=sys.stderr)
        return errors.EXIT_PRECONDITION
    except errors.ContractError as e:
        print(e.format_stderr(prog, json_mode=json_mode), file=sys.stderr)
        return e.exit_code
    except ValueError as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return errors.EXIT_PRECONDITION

    if json_mode:
        json.dump(result, sys.stdout, sort_keys=False)
        sys.stdout.write("\n")
    else:
        print(format_lease_human(op, result))

    return errors.EXIT_SUCCESS


# --- primitive verb entrypoint (byte-identical to flat transition-issue) ---


def run(args):
    prog = "transition-issue"

    if not args.issue_id:
        print(f"{prog}: missing issue ID (use -h for usage)", file=sys.stderr)
        return errors.EXIT_USAGE

    lease_ops = [
        name for name, active in (
            ("claim", args.claim is not None),
            ("renew", args.renew),
            ("release", args.release),
        ) if active
    ]
    if len(lease_ops) > 1:
        print(f"{prog}: --claim, --renew, and --release are mutually "
              "exclusive", file=sys.stderr)
        return errors.EXIT_USAGE
    lease_op = lease_ops[0] if lease_ops else None

    if lease_op is not None:
        if args.new_state:
            print(f"{prog}: --{lease_op} does not take a target state (the "
                  "lease operation manages status itself)", file=sys.stderr)
            return errors.EXIT_USAGE
        if args.branch is not None or args.reason is not None:
            print(f"{prog}: --branch/--reason are not valid with --{lease_op}",
                  file=sys.stderr)
            return errors.EXIT_USAGE
        if args.lease_ttl is not None and lease_op == "release":
            print(f"{prog}: --lease-ttl is not valid with --release",
                  file=sys.stderr)
            return errors.EXIT_USAGE
        if args.claim is not None and not lease.CLAIMANT_RE.match(args.claim):
            print(f"{prog}: invalid claimant {args.claim!r} (must match "
                  f"{lease.CLAIMANT_RE.pattern})", file=sys.stderr)
            return errors.EXIT_USAGE
    else:
        if args.lease_ttl is not None:
            print(f"{prog}: --lease-ttl requires --claim or --renew",
                  file=sys.stderr)
            return errors.EXIT_USAGE
        if not args.new_state:
            print(f"{prog}: missing target state (use -h for usage)",
                  file=sys.stderr)
            return errors.EXIT_USAGE
        if args.new_state not in CANONICAL_STATES:
            print(f"{prog}: unknown state {args.new_state!r}; legal: "
                  f"{{{', '.join(CANONICAL_STATES)}}}", file=sys.stderr)
            return errors.EXIT_USAGE

    if args.lease_ttl is not None and args.lease_ttl <= 0:
        print(f"{prog}: --lease-ttl must be a positive integer of seconds",
              file=sys.stderr)
        return errors.EXIT_USAGE
    if args.claim_generation is not None and args.claim_generation < 0:
        print(f"{prog}: --claim-generation must be a non-negative integer",
              file=sys.stderr)
        return errors.EXIT_USAGE

    if lease_op is not None:
        return _execute_lease(
            args.issue_id, lease_op, claimant=args.claim,
            lease_ttl=args.lease_ttl, claim_generation=args.claim_generation,
            json_mode=args.json_mode, no_reindex=args.no_reindex,
            lock_timeout=args.lock_timeout, store=args.store, prog=prog,
        )
    return _execute_transition(
        args.issue_id, args.new_state, branch=args.branch, reason=args.reason,
        claim_generation=args.claim_generation, json_mode=args.json_mode,
        no_reindex=args.no_reindex, lock_timeout=args.lock_timeout,
        store=args.store, prog=prog,
    )
