#!/usr/bin/env python3
"""Unit tests for scripts/transition-issue.

Covers the in-process surface that does not need real subprocesses:
  - LEGAL_TRANSITIONS table shape, terminal/active sets
  - check_transition: legal, illegal, same-state no-op
  - append_transition_note: appends to existing section, creates section
    when absent, preserves bytes outside the section
  - apply_transition: status update, branch update, updated stamp,
    note appended, no-op-with-no-changes returns without writing,
    illegal transition raises IllegalTransition,
    no-op-with-reason still records the reason
  - Locking: ConcurrentEdit propagates when the lock is held by another
    process (covered by tests/verification/transition-issue.sh, not here)

  - Lease primitive: apply_lease_op claim/renew/release
    semantics, generation bump rules (W0a F2), loser-side typed errors,
    and the --claim-generation guard on plain transitions.

End-to-end CLI behavior (exit codes, --help, --json, lock contention,
auto-reindex) lives in tests/verification/transition-issue.sh and
tests/verification/im-098-lease.sh.

Run:
    python3 -m unittest tests.test_transition -v
"""

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import concurrency  # noqa: E402
import errors  # noqa: E402
import frontmatter  # noqa: E402
import lease  # noqa: E402


# The flat `scripts/transition-issue` is now a tombstone stub.
# Its data path (LEGAL_TRANSITIONS, check_transition, apply_transition,
# apply_lease_op, append_transition_note, ...) lives in the consolidated
# `issue transition` verb module lib/cli/transition.py -- import it directly.
import cli.transition as tx  # noqa: E402  (lib/ on sys.path above)


def _load_dispatcher():
    """Load scripts/issue (the consolidated dispatcher) as a module."""
    from importlib.machinery import SourceFileLoader
    path = REPO_ROOT / "scripts" / "issue"
    loader = SourceFileLoader("issue_dispatcher", str(path))
    spec = importlib.util.spec_from_loader("issue_dispatcher", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_issue_cli = _load_dispatcher()


def tx_main(argv):
    """Drive the flat transition arg surface through `issue transition`.

    The flat `transition-issue <argv>` entry became `issue transition
    <argv>` (the primitive verb re-hosts the full flat arg surface). Returns
    the exit code, normalizing argparse's SystemExit (e.g. --version, a
    parse error) to its int so the exit-code assertions are unchanged.
    """
    try:
        return _issue_cli.main(["transition"] + list(argv))
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


SAMPLE = """---
id: IM-999
title: "Sample"
type: feature
status: open
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: [a]
changelog:
---

## Summary

Body of summary.

## Implementation Notes

## Archived Plan Content
"""


SAMPLE_NO_NOTES = """---
id: IM-999
title: "Sample"
type: feature
status: open
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
---

## Summary

Body of summary.
"""


class StateMachineTable(unittest.TestCase):
    def test_table_keys_match_canonical_states(self):
        self.assertEqual(
            set(tx.LEGAL_TRANSITIONS.keys()),
            set(tx.CANONICAL_STATES),
        )

    def test_terminal_and_active_sets_partition_canonical(self):
        self.assertEqual(
            tx.TERMINAL_STATES | tx.ACTIVE_STATES,
            set(tx.CANONICAL_STATES),
        )
        self.assertEqual(
            tx.TERMINAL_STATES & tx.ACTIVE_STATES,
            set(),
        )

    def test_no_self_loops_in_table(self):
        for src, targets in tx.LEGAL_TRANSITIONS.items():
            self.assertNotIn(
                src, targets,
                msg=f"{src!r} self-loop should be a no-op, not in table",
            )

    def test_terminal_to_terminal_disallowed(self):
        for term in tx.TERMINAL_STATES:
            for other in tx.TERMINAL_STATES:
                if term == other:
                    continue
                self.assertNotIn(
                    other, tx.LEGAL_TRANSITIONS[term],
                    msg=(
                        f"{term!r} -> {other!r} should require reopen first"
                    ),
                )

    def test_terminal_can_reach_all_active_states(self):
        for term in tx.TERMINAL_STATES:
            self.assertEqual(
                tx.LEGAL_TRANSITIONS[term],
                tx.ACTIVE_STATES,
                msg=f"{term!r} should reopen to any active state",
            )

    def test_open_cannot_go_directly_to_done(self):
        self.assertNotIn("done", tx.LEGAL_TRANSITIONS["open"])

    def test_blocked_cannot_go_directly_to_done(self):
        self.assertNotIn("done", tx.LEGAL_TRANSITIONS["blocked"])

    def test_in_progress_can_reach_all_other_states(self):
        self.assertEqual(
            tx.LEGAL_TRANSITIONS["in-progress"],
            {"open", "blocked", "done", "cancelled"},
        )


class CheckTransition(unittest.TestCase):
    def test_legal_transition_returns_none(self):
        self.assertIsNone(tx.check_transition("open", "in-progress"))

    def test_same_state_is_no_op(self):
        self.assertIsNone(tx.check_transition("blocked", "blocked"))

    def test_illegal_transition_raises_with_legal_set(self):
        with self.assertRaises(tx.IllegalTransition) as cm:
            tx.check_transition("open", "done")
        self.assertEqual(cm.exception.current, "open")
        self.assertEqual(cm.exception.target, "done")
        self.assertEqual(
            set(cm.exception.legal),
            {"in-progress", "blocked", "cancelled"},
        )

    def test_terminal_to_terminal_raises(self):
        with self.assertRaises(tx.IllegalTransition):
            tx.check_transition("done", "cancelled")
        with self.assertRaises(tx.IllegalTransition):
            tx.check_transition("cancelled", "done")

    def test_unknown_current_state_treated_as_no_legal_transitions(self):
        with self.assertRaises(tx.IllegalTransition) as cm:
            tx.check_transition("triaged", "open")
        self.assertEqual(cm.exception.legal, [])

    def test_error_message_lists_legal_states(self):
        with self.assertRaises(tx.IllegalTransition) as cm:
            tx.check_transition("done", "cancelled")
        msg = str(cm.exception)
        self.assertIn("'done'", msg)
        self.assertIn("'cancelled'", msg)
        # Legal next states must be named explicitly.
        self.assertIn("open", msg)
        self.assertIn("in-progress", msg)
        self.assertIn("blocked", msg)


class AppendTransitionNote(unittest.TestCase):
    def test_appends_to_existing_implementation_notes(self):
        result = tx.append_transition_note(
            "## Implementation Notes\n",
            "2026-05-13", "open", "in-progress", None,
        )
        self.assertIn("2026-05-13: status: open -> in-progress", result)

    def test_includes_reason_when_provided(self):
        result = tx.append_transition_note(
            "## Implementation Notes\n",
            "2026-05-13", "open", "blocked", "waiting on RM-038",
        )
        self.assertIn(
            "2026-05-13: status: open -> blocked -- waiting on RM-038",
            result,
        )

    def test_creates_section_when_absent(self):
        body = "## Summary\n\nBody.\n"
        result = tx.append_transition_note(
            body, "2026-05-13", "open", "in-progress", None,
        )
        self.assertIn("## Implementation Notes", result)
        self.assertIn("2026-05-13: status: open -> in-progress", result)
        # Original section must survive byte-for-byte at the start.
        self.assertTrue(result.startswith("## Summary\n\nBody.\n"))

    def test_preserves_adjacent_sections(self):
        body = (
            "## Summary\n\nBody.\n\n"
            "## Implementation Notes\n\n"
            "Existing line.\n\n"
            "## Archived Plan Content\n"
        )
        result = tx.append_transition_note(
            body, "2026-05-13", "open", "in-progress", None,
        )
        self.assertIn("Existing line.", result)
        self.assertIn("2026-05-13: status: open -> in-progress", result)
        self.assertIn("## Archived Plan Content", result)
        # Order preserved: Summary < Notes < Archived
        self.assertLess(
            result.index("## Summary"),
            result.index("## Implementation Notes"),
        )
        self.assertLess(
            result.index("## Implementation Notes"),
            result.index("## Archived Plan Content"),
        )


class ApplyTransition(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.locks = self.root / ".locks"
        self.locks.mkdir()
        self.issue = self.root / "IM-999.md"
        self.lock = self.locks / "IM-999.lock"
        self.issue.write_text(SAMPLE, encoding="utf-8")
        self.today = datetime.date.today().isoformat()

    def _read(self):
        return self.issue.read_text(encoding="utf-8")

    def test_legal_transition_updates_status_and_stamps_updated(self):
        summary, _ = tx.apply_transition(
            self.issue, self.lock,
            target_state="in-progress",
            branch=None, reason=None, lock_timeout=2.0,
        )
        text = self._read()
        self.assertIn("status: in-progress\n", text)
        self.assertIn(f"updated: {self.today}\n", text)
        self.assertEqual(
            summary["id_state_change"],
            {"old": "open", "new": "in-progress", "no_op": False},
        )
        self.assertTrue(summary["note_appended"])

    def test_appends_transition_note_to_implementation_notes(self):
        tx.apply_transition(
            self.issue, self.lock,
            target_state="in-progress",
            branch=None, reason=None, lock_timeout=2.0,
        )
        text = self._read()
        self.assertIn(
            f"{self.today}: status: open -> in-progress",
            text,
        )

    def test_branch_set_when_provided(self):
        summary, _ = tx.apply_transition(
            self.issue, self.lock,
            target_state="in-progress",
            branch="phase-doug035-p1b", reason=None, lock_timeout=2.0,
        )
        text = self._read()
        # set_field preserves the existing quoting style (was branch: "")
        self.assertIn('branch: "phase-doug035-p1b"\n', text)
        self.assertEqual(
            summary["branch_change"],
            {"old": "", "new": "phase-doug035-p1b"},
        )

    def test_branch_left_alone_when_not_provided(self):
        tx.apply_transition(
            self.issue, self.lock,
            target_state="in-progress",
            branch=None, reason=None, lock_timeout=2.0,
        )
        text = self._read()
        self.assertIn('branch: ""\n', text)

    def test_branch_explicit_empty_string_clears_field(self):
        # First set it to something non-empty.
        tx.apply_transition(
            self.issue, self.lock,
            target_state="in-progress",
            branch="some-branch", reason=None, lock_timeout=2.0,
        )
        # Now transition done with --branch ""
        tx.apply_transition(
            self.issue, self.lock,
            target_state="done",
            branch="", reason=None, lock_timeout=2.0,
        )
        text = self._read()
        self.assertIn('branch: ""\n', text)
        self.assertIn("status: done\n", text)

    def test_terminal_transition_clears_stale_branch(self):
        # A done transition with no --branch auto-clears a non-empty
        # branch: field, honoring the lifecycle convention (done clears branch).
        tx.apply_transition(
            self.issue, self.lock, target_state="in-progress",
            branch="feat-x", reason=None, lock_timeout=2.0,
        )
        summary, text = tx.apply_transition(
            self.issue, self.lock, target_state="done",
            branch=None, reason=None, lock_timeout=2.0,
        )
        self.assertIn('branch: ""\n', text)
        self.assertEqual(summary["branch_change"], {"old": "feat-x", "new": ""})

    def test_cancel_transition_clears_stale_branch(self):
        # Cancelled is terminal too; a stale branch is cleared.
        tx.apply_transition(
            self.issue, self.lock, target_state="in-progress",
            branch="feat-y", reason=None, lock_timeout=2.0,
        )
        _, text = tx.apply_transition(
            self.issue, self.lock, target_state="cancelled",
            branch=None, reason="not pursued", lock_timeout=2.0,
        )
        self.assertIn('branch: ""\n', text)

    def test_complete_clears_stale_branch(self):
        # `complete` (apply_complete) also clears a non-empty branch.
        tx.apply_transition(
            self.issue, self.lock, target_state="in-progress",
            branch="feat-z", reason=None, lock_timeout=2.0,
        )
        summary, text = tx.apply_complete(
            self.issue, self.lock, "all checks pass", 2.0,
        )
        self.assertIn('branch: ""\n', text)
        self.assertEqual(summary["branch_change"], {"old": "feat-z", "new": ""})

    def test_reason_appended_to_note_line(self):
        tx.apply_transition(
            self.issue, self.lock,
            target_state="blocked",
            branch=None, reason="waiting on RM-038", lock_timeout=2.0,
        )
        text = self._read()
        self.assertIn(
            f"{self.today}: status: open -> blocked -- waiting on RM-038",
            text,
        )

    def test_illegal_transition_raises(self):
        with self.assertRaises(tx.IllegalTransition):
            tx.apply_transition(
                self.issue, self.lock,
                target_state="done",
                branch=None, reason=None, lock_timeout=2.0,
            )
        # File untouched.
        self.assertEqual(self._read(), SAMPLE)

    def test_no_op_with_no_args_does_not_write(self):
        before_mtime = self.issue.stat().st_mtime_ns
        summary, new_text = tx.apply_transition(
            self.issue, self.lock,
            target_state="open",
            branch=None, reason=None, lock_timeout=2.0,
        )
        self.assertIsNone(new_text)
        self.assertTrue(summary["id_state_change"]["no_op"])
        self.assertFalse(summary["note_appended"])
        # mtime unchanged.
        self.assertEqual(self.issue.stat().st_mtime_ns, before_mtime)

    def test_no_op_with_reason_records_note(self):
        # blocked -> blocked with --reason. First get to blocked.
        tx.apply_transition(
            self.issue, self.lock,
            target_state="blocked",
            branch=None, reason="initial block", lock_timeout=2.0,
        )
        tx.apply_transition(
            self.issue, self.lock,
            target_state="blocked",
            branch=None, reason="still waiting", lock_timeout=2.0,
        )
        text = self._read()
        self.assertIn("-- initial block", text)
        self.assertIn("-- still waiting", text)

    def test_creates_implementation_notes_section_when_absent(self):
        self.issue.write_text(SAMPLE_NO_NOTES, encoding="utf-8")
        tx.apply_transition(
            self.issue, self.lock,
            target_state="in-progress",
            branch=None, reason=None, lock_timeout=2.0,
        )
        text = self._read()
        self.assertIn("## Implementation Notes", text)
        self.assertIn(
            f"{self.today}: status: open -> in-progress",
            text,
        )

    def test_missing_status_field_raises_value_error(self):
        broken = SAMPLE.replace("status: open\n", "")
        self.issue.write_text(broken, encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            tx.apply_transition(
                self.issue, self.lock,
                target_state="in-progress",
                branch=None, reason=None, lock_timeout=2.0,
            )
        self.assertIn("status", str(cm.exception))

    def test_concurrent_edit_raises_when_file_changes_under_lock(self):
        # Simulate hand-edit between snapshot and write: patch read_issue
        # so that, after the snapshot is captured, the file's bytes change
        # before verify_unchanged runs.
        original_read = frontmatter.read_issue
        issue_path = self.issue
        original_text = self._read()

        def tampered_read(path):
            result = original_read(path)
            # Touch the file with different bytes to force a divergence
            # against the just-captured StateSnapshot.
            issue_path.write_text(
                original_text + "\n# hand-edit\n", encoding="utf-8"
            )
            return result

        frontmatter.read_issue = tampered_read
        try:
            with self.assertRaises(errors.ConcurrentEdit):
                tx.apply_transition(
                    self.issue, self.lock,
                    target_state="in-progress",
                    branch=None, reason=None, lock_timeout=2.0,
                )
        finally:
            frontmatter.read_issue = original_read




class ApplyLeaseOp(unittest.TestCase):
    """IM-098 lease primitive semantics through apply_lease_op."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.locks = self.root / ".locks"
        self.locks.mkdir()
        self.issue = self.root / "IM-999.md"
        self.lock = self.locks / "IM-999.lock"
        self.issue.write_text(SAMPLE, encoding="utf-8")
        self.today = datetime.date.today().isoformat()

    def _read(self):
        return self.issue.read_text(encoding="utf-8")

    def _fm(self):
        fm_lines, _ = frontmatter.read_issue(self.issue)
        return fm_lines

    def _claim(self, claimant="carol", ttl=3600, generation=None):
        return tx.apply_lease_op(
            self.issue, self.lock, "IM-999", "claim", claimant, ttl,
            generation, 2.0,
        )

    def _renew(self, ttl=3600, generation=None):
        return tx.apply_lease_op(
            self.issue, self.lock, "IM-999", "renew", None, ttl,
            generation, 2.0,
        )

    def _release(self, generation=None):
        return tx.apply_lease_op(
            self.issue, self.lock, "IM-999", "release", None, 3600,
            generation, 2.0,
        )

    def _expire_lease(self):
        """Rewrite lease_until to the past (simulates TTL expiry)."""
        fm_lines, body = frontmatter.read_issue(self.issue)
        lease.set_lease_until(fm_lines, "2020-01-01T00:00:00Z")
        self.issue.write_text(
            frontmatter.serialize_issue(fm_lines, body), encoding="utf-8"
        )

    # -- claim ---------------------------------------------------------

    def test_first_claim_sets_status_and_all_three_fields(self):
        result = self._claim()
        fm = self._fm()
        self.assertEqual(frontmatter.get_field(fm, "status"), "in-progress")
        self.assertEqual(lease.get_claim(fm)["assignee"], "carol")
        self.assertIsNotNone(lease.get_lease_until(fm))
        self.assertEqual(lease.get_claim_generation(fm), 1)
        self.assertEqual(result["claim_generation"], 1)
        self.assertEqual(result["status"], "in-progress")
        self.assertEqual(result["claim"]["assignee"], "carol")
        self.assertFalse(result["no_op"])

    def test_first_claim_default_ttl_is_3600(self):
        result = self._claim()
        claimed_at = lease.parse_ts(result["claim"]["claimed_at"])
        until = lease.parse_ts(result["claim"]["lease_until"])
        self.assertEqual((until - claimed_at).total_seconds(), 3600)

    def test_claim_lease_ttl_override(self):
        result = self._claim(ttl=60)
        claimed_at = lease.parse_ts(result["claim"]["claimed_at"])
        until = lease.parse_ts(result["claim"]["lease_until"])
        self.assertEqual((until - claimed_at).total_seconds(), 60)

    def test_claim_appends_transition_note(self):
        self._claim()
        text = self._read()
        self.assertIn(
            f"{self.today}: status: open -> in-progress -- claimed by carol",
            text,
        )

    def test_same_actor_reclaim_is_noop_and_does_not_bump(self):
        self._claim()
        before = self._read()
        result = self._claim()
        self.assertTrue(result["no_op"])
        self.assertEqual(result["claim_generation"], 1)
        self.assertEqual(self._read(), before)

    def test_other_actor_claim_on_valid_lease_raises_lost_race(self):
        self._claim(claimant="carol")
        with self.assertRaises(errors.LostRaceError) as cm:
            self._claim(claimant="claude")
        self.assertEqual(cm.exception.contender, "carol")
        self.assertIsNotNone(cm.exception.lease_until)

    def test_claim_over_expired_lease_starts_new_era(self):
        self._claim(claimant="carol")
        self._expire_lease()
        result = self._claim(claimant="claude")
        self.assertEqual(result["claim_generation"], 2)
        self.assertEqual(result["claim"]["assignee"], "claude")

    def test_same_actor_reclaim_after_expiry_bumps_generation(self):
        # Expired-then-reclaimed-by-same-actor is a new era (W0a F2).
        self._claim(claimant="carol")
        self._expire_lease()
        result = self._claim(claimant="carol")
        self.assertEqual(result["claim_generation"], 2)
        self.assertFalse(result["no_op"])

    def test_claim_on_in_progress_unclaimed_issue_is_claimable(self):
        tx.apply_transition(
            self.issue, self.lock, target_state="in-progress",
            branch=None, reason=None, lock_timeout=2.0,
        )
        result = self._claim()
        self.assertEqual(result["claim_generation"], 1)
        note = self._read()
        self.assertIn("claim: acquired by carol", note)

    def test_claim_on_blocked_issue_raises_not_claimable(self):
        tx.apply_transition(
            self.issue, self.lock, target_state="blocked",
            branch=None, reason="waiting", lock_timeout=2.0,
        )
        with self.assertRaises(errors.NotClaimableError) as cm:
            self._claim()
        self.assertEqual(cm.exception.status, "blocked")

    def test_claim_on_done_issue_raises_not_claimable(self):
        tx.apply_transition(
            self.issue, self.lock, target_state="in-progress",
            branch=None, reason=None, lock_timeout=2.0,
        )
        tx.apply_transition(
            self.issue, self.lock, target_state="done",
            branch=None, reason=None, lock_timeout=2.0,
        )
        with self.assertRaises(errors.NotClaimableError):
            self._claim()

    def test_claim_with_stale_generation_raises_stale_view(self):
        self._claim(claimant="carol")
        self._expire_lease()
        self._claim(claimant="claude")  # era 2
        with self.assertRaises(errors.StaleViewError) as cm:
            self._claim(claimant="carol", generation=1)
        self.assertEqual(cm.exception.current_generation, 2)
        self.assertEqual(cm.exception.caller_generation, 1)

    def test_claim_with_matching_generation_proceeds(self):
        self._claim(claimant="carol")
        self._expire_lease()
        result = self._claim(claimant="claude", generation=1)
        self.assertEqual(result["claim_generation"], 2)

    def test_missing_issue_raises_issue_not_found(self):
        missing = self.root / "IM-404.md"
        with self.assertRaises(errors.IssueNotFoundError):
            tx.apply_lease_op(
                missing, self.locks / "IM-404.lock", "IM-404", "claim",
                "carol", 3600, None, 2.0,
            )

    # -- renew ---------------------------------------------------------

    def test_renew_extends_lease_and_preserves_generation(self):
        first = self._claim(ttl=60)
        result = self._renew(ttl=7200)
        self.assertEqual(result["claim_generation"], 1)
        self.assertEqual(result["claim"]["assignee"], "carol")
        self.assertGreater(
            lease.parse_ts(result["claim"]["lease_until"]),
            lease.parse_ts(first["claim"]["lease_until"]),
        )
        # claimed_at untouched by renew.
        self.assertEqual(
            result["claim"]["claimed_at"], first["claim"]["claimed_at"]
        )

    def test_renew_without_claim_raises_not_claimable(self):
        with self.assertRaises(errors.NotClaimableError) as cm:
            self._renew()
        self.assertEqual(cm.exception.reason, "no-claim")

    def test_renew_expired_lease_raises_lease_expired(self):
        self._claim()
        self._expire_lease()
        with self.assertRaises(errors.LeaseExpiredError) as cm:
            self._renew()
        self.assertEqual(cm.exception.actor, "carol")

    def test_renew_with_stale_generation_raises_stale_view(self):
        self._claim()
        with self.assertRaises(errors.StaleViewError):
            self._renew(generation=0)

    # -- release -------------------------------------------------------

    def test_release_clears_claim_and_preserves_generation(self):
        self._claim()
        result = self._release()
        fm = self._fm()
        self.assertIsNone(lease.get_claim(fm))
        self.assertIsNone(lease.get_lease_until(fm))
        self.assertEqual(lease.get_claim_generation(fm), 1)
        self.assertIsNone(result["claim"])
        self.assertEqual(result["claim_generation"], 1)
        self.assertIn("claim: released (was carol", self._read())

    def test_release_does_not_change_status(self):
        self._claim()
        self._release()
        fm = self._fm()
        self.assertEqual(frontmatter.get_field(fm, "status"), "in-progress")

    def test_release_unclaimed_is_noop(self):
        before = self._read()
        result = self._release()
        self.assertTrue(result["no_op"])
        self.assertEqual(self._read(), before)

    def test_release_with_stale_generation_raises_stale_view(self):
        self._claim()
        with self.assertRaises(errors.StaleViewError):
            self._release(generation=5)

    def test_reclaim_after_release_bumps_generation(self):
        self._claim(claimant="carol")
        self._release()
        result = self._claim(claimant="claude")
        self.assertEqual(result["claim_generation"], 2)


class GenerationGuardOnTransitions(unittest.TestCase):
    """--claim-generation guard on plain state transitions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.locks = self.root / ".locks"
        self.locks.mkdir()
        self.issue = self.root / "IM-999.md"
        self.lock = self.locks / "IM-999.lock"
        self.issue.write_text(SAMPLE, encoding="utf-8")

    def _claim(self):
        return tx.apply_lease_op(
            self.issue, self.lock, "IM-999", "claim", "carol", 3600,
            None, 2.0,
        )

    def test_transition_with_matching_generation_and_active_lease(self):
        self._claim()
        summary, _ = tx.apply_transition(
            self.issue, self.lock, target_state="done",
            branch=None, reason=None, lock_timeout=2.0,
            caller_generation=1,
        )
        self.assertEqual(summary["id_state_change"]["new"], "done")

    def test_transition_with_stale_generation_raises_stale_view(self):
        self._claim()
        with self.assertRaises(errors.StaleViewError) as cm:
            tx.apply_transition(
                self.issue, self.lock, target_state="done",
                branch=None, reason=None, lock_timeout=2.0,
                caller_generation=0,
            )
        self.assertEqual(cm.exception.current_generation, 1)
        self.assertEqual(cm.exception.caller_generation, 0)

    def test_transition_matching_generation_expired_lease_raises(self):
        self._claim()
        fm_lines, body = frontmatter.read_issue(self.issue)
        lease.set_lease_until(fm_lines, "2020-01-01T00:00:00Z")
        self.issue.write_text(
            frontmatter.serialize_issue(fm_lines, body), encoding="utf-8"
        )
        with self.assertRaises(errors.LeaseExpiredError):
            tx.apply_transition(
                self.issue, self.lock, target_state="done",
                branch=None, reason=None, lock_timeout=2.0,
                caller_generation=1,
            )

    def test_transition_without_flag_skips_guard(self):
        self._claim()
        summary, _ = tx.apply_transition(
            self.issue, self.lock, target_state="done",
            branch=None, reason=None, lock_timeout=2.0,
        )
        self.assertEqual(summary["id_state_change"]["new"], "done")

    def test_guard_on_never_claimed_issue_generation_zero_passes(self):
        summary, _ = tx.apply_transition(
            self.issue, self.lock, target_state="in-progress",
            branch=None, reason=None, lock_timeout=2.0,
            caller_generation=0,
        )
        self.assertEqual(summary["id_state_change"]["new"], "in-progress")


class LeaseCliArgValidation(unittest.TestCase):
    """main() validation for the lease flags (no filesystem effects)."""

    def test_claim_plus_state_exits_2(self):
        rc = tx_main(["IM-999", "done", "--claim", "carol"])
        self.assertEqual(rc, 2)

    def test_claim_plus_renew_exits_2(self):
        rc = tx_main(["IM-999", "--claim", "carol", "--renew"])
        self.assertEqual(rc, 2)

    def test_renew_plus_release_exits_2(self):
        rc = tx_main(["IM-999", "--renew", "--release"])
        self.assertEqual(rc, 2)

    def test_invalid_claimant_exits_2(self):
        rc = tx_main(["IM-999", "--claim", "two words"])
        self.assertEqual(rc, 2)

    def test_lease_ttl_without_lease_op_exits_2(self):
        rc = tx_main(["IM-999", "in-progress", "--lease-ttl", "60"])
        self.assertEqual(rc, 2)

    def test_lease_ttl_with_release_exits_2(self):
        rc = tx_main(["IM-999", "--release", "--lease-ttl", "60"])
        self.assertEqual(rc, 2)

    def test_lease_ttl_zero_exits_2(self):
        rc = tx_main(["IM-999", "--claim", "carol", "--lease-ttl", "0"])
        self.assertEqual(rc, 2)

    def test_negative_claim_generation_exits_2(self):
        rc = tx_main(
            ["IM-999", "--claim", "carol", "--claim-generation", "-1"]
        )
        self.assertEqual(rc, 2)

    def test_branch_with_claim_exits_2(self):
        rc = tx_main(["IM-999", "--claim", "carol", "--branch", "b"])
        self.assertEqual(rc, 2)


class CliArgValidation(unittest.TestCase):
    """Light coverage of main() input validation (no filesystem effects)."""

    def test_blocked_without_reason_exits_2(self):
        rc = tx_main(["IM-999", "blocked"])
        self.assertEqual(rc, 2)

    def test_unknown_state_exits_2(self):
        rc = tx_main(["IM-999", "triaged"])
        self.assertEqual(rc, 2)

    def test_missing_id_exits_2(self):
        rc = tx_main([])
        self.assertEqual(rc, 2)

    def test_missing_state_exits_2(self):
        rc = tx_main(["IM-999"])
        self.assertEqual(rc, 2)

    def test_invalid_id_format_exits_2(self):
        rc = tx_main(["not-a-real-id", "in-progress"])
        self.assertEqual(rc, 2)

    def test_version_exits_0(self):
        rc = tx_main(["--version"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
