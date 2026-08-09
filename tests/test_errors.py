#!/usr/bin/env python3
"""Unit tests for lib/errors.py.

Covers:
  - Exit-code constants match the contiguous 3-7 contract
    (design Section 4.5); EXIT_CONCURRENT_EDIT flipped 17 -> 3.
  - Typed exception hierarchy (LeaseExpiredError and
    IssueNotFoundError subclass NotClaimableError; everything is a
    ContractError).
  - format_stderr key=value default shape per class (design Section
    4.6) and the one-line JSON opt-in shape (exit + class fields).
  - Value quoting rules for key=value tokens.

Run:
    python3 -m unittest tests.test_errors -v
"""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import errors  # noqa: E402


class ExitCodeConstants(unittest.TestCase):
    def test_contiguous_scheme_values(self):
        self.assertEqual(errors.EXIT_SUCCESS, 0)
        self.assertEqual(errors.EXIT_PRECONDITION, 1)
        self.assertEqual(errors.EXIT_USAGE, 2)
        self.assertEqual(errors.EXIT_CONCURRENT_EDIT, 3)
        self.assertEqual(errors.EXIT_LOST_RACE, 4)
        self.assertEqual(errors.EXIT_STALE_VIEW, 5)
        self.assertEqual(errors.EXIT_DEP_CHANGED, 6)
        self.assertEqual(errors.EXIT_NOT_CLAIMABLE, 7)

    def test_legacy_alias_matches_precondition(self):
        self.assertEqual(
            errors.EXIT_STORE_COMMIT_FAILED, errors.EXIT_PRECONDITION
        )


class ExceptionHierarchy(unittest.TestCase):
    def test_all_are_contract_errors(self):
        for cls in (
            errors.ConcurrentEdit,
            errors.LostRaceError,
            errors.StaleViewError,
            errors.DependencyChangedError,
            errors.NotClaimableError,
            errors.LeaseExpiredError,
            errors.IssueNotFoundError,
        ):
            self.assertTrue(issubclass(cls, errors.ContractError), cls)

    def test_lease_expired_subclasses_not_claimable(self):
        self.assertTrue(
            issubclass(errors.LeaseExpiredError, errors.NotClaimableError)
        )

    def test_issue_not_found_subclasses_not_claimable(self):
        self.assertTrue(
            issubclass(errors.IssueNotFoundError, errors.NotClaimableError)
        )

    def test_exit_codes_per_class(self):
        self.assertEqual(errors.ConcurrentEdit("x").exit_code, 3)
        self.assertEqual(errors.LostRaceError("x").exit_code, 4)
        self.assertEqual(errors.StaleViewError("x").exit_code, 5)
        self.assertEqual(errors.DependencyChangedError("x").exit_code, 6)
        self.assertEqual(errors.NotClaimableError("x").exit_code, 7)
        self.assertEqual(errors.LeaseExpiredError("x").exit_code, 7)
        self.assertEqual(errors.IssueNotFoundError("x").exit_code, 7)


class ConcurrentEditShape(unittest.TestCase):
    def test_constructor_signature_backward_compatible(self):
        e = errors.ConcurrentEdit(
            "lock timeout", resource="IM-001", contender_pid=99
        )
        self.assertEqual(e.message, "lock timeout")
        self.assertEqual(e.resource, "IM-001")
        self.assertEqual(e.contender_pid, 99)

    def test_extra_fields_render(self):
        e = errors.ConcurrentEdit(
            "section drifted", resource="f.md",
            section="Summary", caller_sha="abc123",
        )
        line = e.format_stderr("edit-issue")
        self.assertIn("section=Summary", line)
        self.assertIn("caller_sha=abc123", line)


class LostRaceShape(unittest.TestCase):
    def test_key_value_line(self):
        e = errors.LostRaceError(
            "claim held by another actor",
            contender="claude-code-abc",
            claimed_at="2026-06-21T14:00:00Z",
            lease_until="2026-06-21T15:00:00Z",
        )
        line = e.format_stderr("transition-issue")
        self.assertTrue(line.startswith("transition-issue: error=lost-race"))
        self.assertIn("contender=claude-code-abc", line)
        self.assertIn("claimed_at=2026-06-21T14:00:00Z", line)
        self.assertIn("lease_until=2026-06-21T15:00:00Z", line)

    def test_json_line(self):
        e = errors.LostRaceError(
            "claim held by another actor",
            contender="claude-code-abc",
            claimed_at="2026-06-21T14:00:00Z",
            lease_until="2026-06-21T15:00:00Z",
        )
        obj = json.loads(
            e.format_stderr("transition-issue", json_mode=True)
        )
        self.assertEqual(obj["exit"], 4)
        self.assertEqual(obj["class"], "lost-race")
        self.assertEqual(obj["error"], "lost-race")
        self.assertEqual(obj["contender"], "claude-code-abc")
        self.assertEqual(obj["lease_until"], "2026-06-21T15:00:00Z")


class StaleViewShape(unittest.TestCase):
    def test_generation_fields(self):
        e = errors.StaleViewError(
            "generation drifted",
            current_generation=2, caller_generation=1,
        )
        line = e.format_stderr("transition-issue")
        self.assertIn("error=stale-local-view", line)
        self.assertIn("current_generation=2", line)
        self.assertIn("caller_generation=1", line)
        self.assertNotIn("current_sha", line)

    def test_sha_fields(self):
        e = errors.StaleViewError(
            "sha drifted", current_sha="aaa", caller_sha="bbb",
        )
        line = e.format_stderr()
        self.assertIn("current_sha=aaa", line)
        self.assertIn("caller_sha=bbb", line)


class DependencyChangedShape(unittest.TestCase):
    def test_from_to_keys(self):
        e = errors.DependencyChangedError(
            "blocker moved", blocker="IM-002",
            from_status="open", to_status="done",
        )
        line = e.format_stderr("issue-ready")
        self.assertIn("error=dependency-state-changed", line)
        self.assertIn("blocker=IM-002", line)
        self.assertIn("from=open", line)
        self.assertIn("to=done", line)


class NotClaimableShape(unittest.TestCase):
    def test_status_field(self):
        e = errors.NotClaimableError("not claimable", status="blocked")
        line = e.format_stderr("transition-issue")
        self.assertIn("error=not-claimable", line)
        self.assertIn("status=blocked", line)

    def test_lease_expired_reason(self):
        e = errors.LeaseExpiredError(
            "lease died", actor="carol",
            lease_until="2026-06-21T15:00:00Z",
        )
        line = e.format_stderr("transition-issue")
        self.assertIn("error=not-claimable", line)
        self.assertIn("reason=lease-expired", line)
        self.assertIn("actor=carol", line)

    def test_issue_not_found_reason(self):
        e = errors.IssueNotFoundError("no file", issue_id="IM-404")
        line = e.format_stderr("transition-issue")
        self.assertIn("error=not-claimable", line)
        self.assertIn("reason=not-found", line)
        self.assertIn("id=IM-404", line)
        obj = json.loads(e.format_stderr(json_mode=True))
        self.assertEqual(obj["exit"], 7)
        self.assertEqual(obj["reason"], "not-found")


class ValueQuoting(unittest.TestCase):
    def test_bare_value_unquoted(self):
        self.assertEqual(errors._fmt_value("simple"), "simple")

    def test_whitespace_value_quoted(self):
        self.assertEqual(errors._fmt_value("two words"), '"two words"')

    def test_empty_value_quoted(self):
        self.assertEqual(errors._fmt_value(""), '""')

    def test_inner_quote_escaped(self):
        self.assertEqual(errors._fmt_value('a"b c'), '"a\\"b c"')

    def test_none_fields_dropped_from_line(self):
        e = errors.NotClaimableError("x", status=None, reason="no-claim")
        line = e.format_stderr()
        self.assertNotIn("status=", line)
        self.assertIn("reason=no-claim", line)


if __name__ == "__main__":
    unittest.main()
