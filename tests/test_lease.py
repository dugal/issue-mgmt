#!/usr/bin/env python3
"""Unit tests for lib/lease.py.

Covers:
  - Timestamp helpers: format/parse round-trip, Z suffix, naive-as-UTC.
  - Claim accessors: absent claim, present claim, generation default 0
    (W0a F5), malformed generation raises ValueError.
  - lease_is_active: future lease active; past/missing/malformed
    lease_until inactive.
  - Mutators: set_claim writes the block + top-level siblings and
    replaces in place; clear_claim removes claim + lease_until but
    preserves claim_generation (W0a F1); untouched frontmatter lines
    are byte-preserved.

Run:
    python3 -m unittest tests.test_lease -v
"""

import datetime
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import frontmatter  # noqa: E402
import lease  # noqa: E402


SAMPLE_UNCLAIMED = """---
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

Body.
"""

SAMPLE_CLAIMED = """---
id: IM-999
title: "Sample"
type: feature
status: in-progress
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: [a]
changelog:
claim:
  assignee: carol
  claimed_at: 2026-06-20T13:46:00Z
lease_until: 2099-01-01T00:00:00Z
claim_generation: 8
---

## Summary

Body.
"""


def fm_of(text):
    fm_lines, _body = frontmatter.parse_issue(text)
    return fm_lines


class Timestamps(unittest.TestCase):
    def test_format_parse_roundtrip(self):
        now = lease.now_utc()
        self.assertEqual(lease.parse_ts(lease.format_ts(now)), now)

    def test_parse_z_suffix(self):
        dt = lease.parse_ts("2026-06-20T13:46:00Z")
        self.assertEqual(dt.tzinfo, datetime.timezone.utc)
        self.assertEqual(dt.hour, 13)

    def test_parse_naive_reads_as_utc(self):
        dt = lease.parse_ts("2026-06-20T13:46:00")
        self.assertEqual(dt.tzinfo, datetime.timezone.utc)

    def test_parse_malformed_raises(self):
        with self.assertRaises(ValueError):
            lease.parse_ts("not-a-timestamp")

    def test_now_utc_second_precision(self):
        self.assertEqual(lease.now_utc().microsecond, 0)


class ClaimAccessors(unittest.TestCase):
    def test_unclaimed_reads_none(self):
        fm = fm_of(SAMPLE_UNCLAIMED)
        self.assertIsNone(lease.get_claim(fm))
        self.assertIsNone(lease.get_lease_until(fm))

    def test_claimed_reads_fields(self):
        fm = fm_of(SAMPLE_CLAIMED)
        claim = lease.get_claim(fm)
        self.assertEqual(claim["assignee"], "carol")
        self.assertEqual(claim["claimed_at"], "2026-06-20T13:46:00Z")
        self.assertEqual(
            lease.get_lease_until(fm), "2099-01-01T00:00:00Z"
        )

    def test_generation_default_zero_when_absent(self):
        fm = fm_of(SAMPLE_UNCLAIMED)
        self.assertEqual(lease.get_claim_generation(fm), 0)

    def test_generation_reads_value(self):
        fm = fm_of(SAMPLE_CLAIMED)
        self.assertEqual(lease.get_claim_generation(fm), 8)

    def test_generation_malformed_raises(self):
        fm = fm_of(SAMPLE_UNCLAIMED)
        fm.append("claim_generation: eight\n")
        with self.assertRaises(ValueError):
            lease.get_claim_generation(fm)

    def test_generation_negative_raises(self):
        fm = fm_of(SAMPLE_UNCLAIMED)
        fm.append("claim_generation: -1\n")
        with self.assertRaises(ValueError):
            lease.get_claim_generation(fm)

    def test_claim_key_does_not_match_claim_generation(self):
        # `claim_generation:` alone must not read as a `claim:` block.
        fm = fm_of(SAMPLE_UNCLAIMED)
        fm.append("claim_generation: 3\n")
        self.assertIsNone(lease.get_claim(fm))
        self.assertEqual(lease.get_claim_generation(fm), 3)


class LeaseActivity(unittest.TestCase):
    def test_future_lease_active(self):
        fm = fm_of(SAMPLE_CLAIMED)
        self.assertTrue(lease.lease_is_active(fm))

    def test_past_lease_inactive(self):
        fm = fm_of(SAMPLE_CLAIMED.replace("2099-01-01", "2020-01-01"))
        self.assertFalse(lease.lease_is_active(fm))

    def test_no_claim_inactive(self):
        fm = fm_of(SAMPLE_UNCLAIMED)
        self.assertFalse(lease.lease_is_active(fm))

    def test_claim_without_lease_until_inactive(self):
        fm = fm_of(SAMPLE_CLAIMED)
        lease._remove_top_level(fm, "lease_until")
        self.assertFalse(lease.lease_is_active(fm))

    def test_malformed_lease_until_inactive(self):
        fm = fm_of(SAMPLE_CLAIMED.replace(
            "lease_until: 2099-01-01T00:00:00Z", "lease_until: garbage"
        ))
        self.assertFalse(lease.lease_is_active(fm))

    def test_explicit_now_boundary(self):
        fm = fm_of(SAMPLE_CLAIMED)
        at_expiry = lease.parse_ts("2099-01-01T00:00:00Z")
        self.assertFalse(lease.lease_is_active(fm, now=at_expiry))
        just_before = at_expiry - datetime.timedelta(seconds=1)
        self.assertTrue(lease.lease_is_active(fm, now=just_before))


class Mutators(unittest.TestCase):
    def test_set_claim_on_unclaimed_appends_all_three(self):
        fm = fm_of(SAMPLE_UNCLAIMED)
        lease.set_claim(
            fm, "carol", "2026-06-20T13:46:00Z",
            "2026-06-20T14:46:00Z", 1,
        )
        self.assertEqual(lease.get_claim(fm)["assignee"], "carol")
        self.assertEqual(
            lease.get_lease_until(fm), "2026-06-20T14:46:00Z"
        )
        self.assertEqual(lease.get_claim_generation(fm), 1)

    def test_set_claim_replaces_existing_block_in_place(self):
        fm = fm_of(SAMPLE_CLAIMED)
        lease.set_claim(
            fm, "claude", "2026-06-21T10:00:00Z",
            "2026-06-21T11:00:00Z", 9,
        )
        claim = lease.get_claim(fm)
        self.assertEqual(claim["assignee"], "claude")
        self.assertEqual(lease.get_claim_generation(fm), 9)
        # Exactly one claim block remains.
        text = "".join(fm)
        self.assertEqual(text.count("claim:\n"), 1)
        self.assertEqual(text.count("assignee:"), 1)

    def test_set_claim_preserves_unrelated_lines(self):
        fm = fm_of(SAMPLE_UNCLAIMED)
        before = [ln for ln in fm]
        lease.set_claim(
            fm, "carol", "2026-06-20T13:46:00Z",
            "2026-06-20T14:46:00Z", 1,
        )
        for line in before:
            self.assertIn(line, fm)

    def test_clear_claim_preserves_generation(self):
        fm = fm_of(SAMPLE_CLAIMED)
        lease.clear_claim(fm)
        self.assertIsNone(lease.get_claim(fm))
        self.assertIsNone(lease.get_lease_until(fm))
        self.assertEqual(lease.get_claim_generation(fm), 8)

    def test_clear_claim_on_unclaimed_is_noop(self):
        fm = fm_of(SAMPLE_UNCLAIMED)
        before = list(fm)
        lease.clear_claim(fm)
        self.assertEqual(fm, before)

    def test_set_lease_until_replaces_value(self):
        fm = fm_of(SAMPLE_CLAIMED)
        lease.set_lease_until(fm, "2099-06-01T00:00:00Z")
        self.assertEqual(
            lease.get_lease_until(fm), "2099-06-01T00:00:00Z"
        )
        # Generation and claim untouched.
        self.assertEqual(lease.get_claim_generation(fm), 8)
        self.assertEqual(lease.get_claim(fm)["assignee"], "carol")

    def test_serialize_roundtrip_after_set_claim(self):
        fm_lines, body = frontmatter.parse_issue(SAMPLE_UNCLAIMED)
        lease.set_claim(
            fm_lines, "carol", "2026-06-20T13:46:00Z",
            "2026-06-20T14:46:00Z", 1,
        )
        text = frontmatter.serialize_issue(fm_lines, body)
        fm2, body2 = frontmatter.parse_issue(text)
        self.assertEqual(body2, body)
        self.assertEqual(lease.get_claim(fm2)["assignee"], "carol")


class ClaimantPattern(unittest.TestCase):
    def test_valid_claimants(self):
        for c in ("carol", "claude-code-abc", "a.b_c@d-e", "X" * 64):
            self.assertTrue(lease.CLAIMANT_RE.match(c), c)

    def test_invalid_claimants(self):
        for c in ("", "two words", "sh$ll", "X" * 65, "tab\tname"):
            self.assertFalse(lease.CLAIMANT_RE.match(c), repr(c))


if __name__ == "__main__":
    unittest.main()
