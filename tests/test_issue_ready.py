#!/usr/bin/env python3
"""Unit tests for scripts/issue-ready (frontmatter-native
readiness filter).

The ready set is computed directly from the markdown store's
frontmatter -- there is no bd binary, no .beads/ index, and no stub. Each
test writes issue files into a temp store and asserts on the CLI output.
End-to-end coverage against real CLIs lives in
tests/verification/issue-ready.sh.

Run:
    python3 -m unittest tests.test_issue_ready -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The flat `scripts/issue-ready` is now a tombstone stub; the
# readiness filter is the consolidated `issue ready` verb. Drive it through
# the dispatcher.
ISSUE_CLI = REPO_ROOT / "scripts" / "issue"


CLAIM_ALICE_ACTIVE = (
    "claim:\n"
    "  assignee: alice\n"
    "  claimed_at: 2026-07-08T00:00:00Z\n"
    "lease_until: 2099-01-01T00:00:00Z\n"
    "claim_generation: 1"
)

CLAIM_ALICE_EXPIRED = (
    "claim:\n"
    "  assignee: alice\n"
    "  claimed_at: 2026-07-01T00:00:00Z\n"
    "lease_until: 2026-07-01T01:00:00Z\n"
    "claim_generation: 1"
)


def issue_md(issue_id, status="open", priority="medium",
             created="2026-07-01", title=None, deps=None, claim=None):
    """Build an issue file. `deps` is [(target_id, kind), ...]; `claim`
    is a raw frontmatter claim block (see the CLAIM_* constants)."""
    lines = [
        "---",
        "id: %s" % issue_id,
        'title: "%s"' % (title if title is not None else "Issue %s" % issue_id),
        "type: chore",
        "status: %s" % status,
        "priority: %s" % priority,
        "created: %s" % created,
        "updated: 2026-07-01",
    ]
    if deps:
        lines.append("depends_on:")
        for to, kind in deps:
            lines.append("  - to: %s" % to)
            lines.append("    type: %s" % kind)
    if claim:
        lines.append(claim)
    lines += ["---", "", "## Summary", "", "Body of %s." % issue_id]
    return "\n".join(lines) + "\n"


class IssueReadyBase(unittest.TestCase):
    """Temp store (no bd, no .beads) and a subprocess runner."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-ready-test.")
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"
        self.store.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def write_issue(self, issue_id, **kwargs):
        prefix = issue_id.split("-")[0]
        pdir = self.store / prefix
        pdir.mkdir(exist_ok=True)
        (pdir / ("%s.md" % issue_id)).write_text(
            issue_md(issue_id, **kwargs), encoding="utf-8",
        )

    def run_cli(self, *argv, store=None, extra_env=None):
        env = dict(os.environ)
        env["ISSUE_STORE_ROOT"] = str(store if store else self.store)
        env.pop("ISSUE_MGMT_NO_BD_BRIDGE", None)
        # Ensure no bd binary is reachable, proving the native read path
        # needs none.
        env["PATH"] = str(self.tmp / "empty-bin")
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(ISSUE_CLI), "ready"] + list(argv),
            capture_output=True, text=True, env=env, cwd=str(self.tmp),
        )

    def run_json(self, *argv, expect_exit=0):
        # IM-241: `ready` is text-by-default like every other verb, so a
        # caller that wants the JSON shape opts in explicitly.
        if not any(a == "--json" or a.startswith("--json=") for a in argv):
            argv = argv + ("--json",)
        # IM-249: `filtered_out` is opt-in explanatory output. These tests
        # assert on the readiness COMPUTATION, so they ask for the section
        # explicitly; the argv-shape behavior of the flag itself is pinned
        # by TestFilteredSection below.
        if not any(a == "--filtered" for a in argv):
            argv = argv + ("--filtered", "all")
        r = self.run_cli(*argv)
        self.assertEqual(r.returncode, expect_exit, r.stderr)
        return json.loads(r.stdout)

    def ready_ids(self, payload):
        return [entry["id"] for entry in payload["ready"]]

    def filtered_ids(self, payload):
        return [entry["id"] for entry in payload["filtered_out"]]


class TestBasics(IssueReadyBase):
    def test_open_issues_are_ready(self):
        self.write_issue("IM-001")
        self.write_issue("IM-002")
        payload = self.run_json()
        self.assertEqual(self.ready_ids(payload), ["IM-001", "IM-002"])
        self.assertEqual(payload["filtered_out"], [])

    def test_ready_entry_shape_id_title_prefix(self):
        self.write_issue("IM-001", title="TTL lease primitive")
        payload = self.run_json()
        self.assertEqual(payload["ready"], [{
            "id": "IM-001",
            "title": "TTL lease primitive",
            "prefix": "IM",
        }])

    def test_non_open_statuses_are_not_candidates(self):
        self.write_issue("IM-001", status="open")
        self.write_issue("IM-002", status="in-progress")
        self.write_issue("IM-003", status="blocked")
        self.write_issue("IM-004", status="done")
        self.write_issue("IM-005", status="cancelled")
        payload = self.run_json()
        self.assertEqual(self.ready_ids(payload), ["IM-001"])
        # Only open issues are candidates; non-open ones are neither
        # ready nor listed in filtered_out.
        self.assertEqual(payload["filtered_out"], [])

    def test_exit_zero_when_ready_non_empty(self):
        self.write_issue("IM-001")
        r = self.run_cli()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")

    def test_no_beads_dir_and_no_bd_binary(self):
        # succeeds with no bd binary on PATH and no .beads/.
        self.assertFalse((self.store / ".beads").exists())
        self.write_issue("IM-001")
        payload = self.run_json()
        self.assertEqual(self.ready_ids(payload), ["IM-001"])


class TestBlocksGating(IssueReadyBase):
    def test_blocks_dep_done_passes(self):
        self.write_issue("DOUG-001", status="done")
        self.write_issue("IM-001", deps=[("DOUG-001", "blocks")])
        payload = self.run_json()
        self.assertEqual(self.ready_ids(payload), ["IM-001"])
        self.assertEqual(payload["filtered_out"], [])

    def test_blocks_dep_cancelled_passes(self):
        self.write_issue("DOUG-001", status="cancelled")
        self.write_issue("IM-001", deps=[("DOUG-001", "blocks")])
        payload = self.run_json()
        self.assertEqual(self.ready_ids(payload), ["IM-001"])

    def test_cross_prefix_open_blocker_filters(self):
        self.write_issue("DOUG-001", status="open")
        self.write_issue("IM-001", deps=[("DOUG-001", "blocks")])
        self.write_issue("IM-002")
        # --repo IM: the open cross-prefix blocker is itself a valid
        # candidate store-wide, so scope the ready set to IM.
        payload = self.run_json("--repo", "IM")
        self.assertEqual(self.ready_ids(payload), ["IM-002"])
        self.assertEqual(payload["filtered_out"], [{
            "id": "IM-001",
            "reason": "blocked by DOUG-001 (open)",
        }])

    def test_same_prefix_open_blocker_filters(self):
        # same-prefix blocks are gated by the same uniform
        # path as cross-prefix ones (bd excluded these from
        # its ready set entirely).
        self.write_issue("IM-009", status="open")
        self.write_issue("IM-001", deps=[("IM-009", "blocks")])
        payload = self.run_json()
        self.assertEqual(self.ready_ids(payload), ["IM-009"])
        self.assertEqual(self.filtered_ids(payload), ["IM-001"])
        self.assertEqual(
            payload["filtered_out"][0]["reason"],
            "blocked by IM-009 (open)",
        )

    def test_blocks_dep_in_progress_filters(self):
        self.write_issue("DOUG-001", status="in-progress")
        self.write_issue("IM-001", deps=[("DOUG-001", "blocks")])
        self.write_issue("IM-002")  # ready filler keeps exit 0
        payload = self.run_json("--repo", "IM")
        self.assertEqual(self.filtered_ids(payload), ["IM-001"])
        self.assertEqual(
            payload["filtered_out"][0]["reason"],
            "blocked by DOUG-001 (in-progress)",
        )

    def test_blocks_dep_missing_store_file_filters_unknown(self):
        # Conservative: an unverifiable dep is not ready.
        self.write_issue("IM-001", deps=[("DOUG-404", "blocks")])
        self.write_issue("IM-002")  # ready filler keeps exit 0
        payload = self.run_json("--repo", "IM")
        self.assertEqual(self.filtered_ids(payload), ["IM-001"])
        self.assertEqual(
            payload["filtered_out"][0]["reason"],
            "blocked by DOUG-404 (unknown)",
        )

    def test_non_blocks_dep_ignored(self):
        self.write_issue("DOUG-001", status="open")
        self.write_issue("IM-001", deps=[
            ("DOUG-001", "related"),
            ("DOUG-001", "parent-child"),
            ("DOUG-001", "discovered-from"),
        ])
        payload = self.run_json("--repo", "IM")
        self.assertEqual(self.ready_ids(payload), ["IM-001"])
        self.assertEqual(payload["filtered_out"], [])

    def test_mixed_candidates(self):
        self.write_issue("DOUG-001", status="done")
        self.write_issue("DOUG-002", status="open")
        self.write_issue("IM-001")
        self.write_issue("IM-002", deps=[("DOUG-001", "blocks")])
        self.write_issue("IM-003", deps=[("DOUG-002", "blocks")])
        self.write_issue("IM-004", deps=[
            ("DOUG-001", "blocks"), ("DOUG-002", "blocks"),
        ])
        payload = self.run_json("--repo", "IM")
        self.assertEqual(self.ready_ids(payload), ["IM-001", "IM-002"])
        self.assertEqual(self.filtered_ids(payload), ["IM-003", "IM-004"])

    def test_multiple_open_blockers_joined_reason(self):
        self.write_issue("DOUG-001", status="open")
        self.write_issue("MT-001", status="in-progress")
        self.write_issue("IM-001", deps=[
            ("DOUG-001", "blocks"), ("MT-001", "blocks"),
        ])
        payload = self.run_json()
        self.assertEqual(
            payload["filtered_out"][0]["reason"],
            "blocked by DOUG-001 (open); blocked by MT-001 (in-progress)",
        )

    def test_transitivity_flows_through_status(self):
        # A blocker that is itself blocked stays open -> still gates.
        self.write_issue("IM-001", status="open",
                         deps=[("IM-002", "blocks")])
        self.write_issue("IM-002", status="open",
                         deps=[("IM-003", "blocks")])
        self.write_issue("IM-003", status="done")
        payload = self.run_json()
        # IM-002's blocker is done -> IM-002 ready; IM-001's blocker
        # (IM-002) is open -> IM-001 filtered.
        self.assertEqual(self.ready_ids(payload), ["IM-002"])
        self.assertEqual(self.filtered_ids(payload), ["IM-001"])


class TestOrdering(IssueReadyBase):
    def test_priority_then_created_then_id(self):
        # Deliberately write in a scrambled order; expect deterministic
        # priority > created > id ordering out.
        self.write_issue("IM-050", priority="low", created="2026-01-01")
        self.write_issue("IM-010", priority="high", created="2026-05-01")
        self.write_issue("IM-020", priority="high", created="2026-03-01")
        self.write_issue("IM-030", priority="medium", created="2026-02-01")
        self.write_issue("IM-040", priority="medium", created="2026-02-01")
        payload = self.run_json()
        self.assertEqual(
            self.ready_ids(payload),
            # high (by created): IM-020(Mar) then IM-010(May);
            # medium (same created, by id): IM-030 then IM-040;
            # low: IM-050
            ["IM-020", "IM-010", "IM-030", "IM-040", "IM-050"],
        )

    def test_unknown_priority_sorts_after_low(self):
        self.write_issue("IM-001", priority="low")
        self.write_issue("IM-002", priority="frobnicate")
        payload = self.run_json()
        self.assertEqual(self.ready_ids(payload), ["IM-001", "IM-002"])


class TestRepoFilter(IssueReadyBase):
    def test_repo_restricts_to_prefix(self):
        self.write_issue("IM-001")
        self.write_issue("DOUG-001")
        payload = self.run_json("--repo", "IM")
        self.assertEqual(self.ready_ids(payload), ["IM-001"])

    def test_repo_excluded_candidates_not_in_filtered_out(self):
        self.write_issue("IM-001")
        self.write_issue("DOUG-001", deps=[("MT-999", "blocks")])
        payload = self.run_json("--repo", "IM")
        self.assertEqual(payload["filtered_out"], [])

    def test_no_repo_returns_all_prefixes(self):
        self.write_issue("IM-001")
        self.write_issue("DOUG-001")
        payload = self.run_json()
        self.assertEqual(
            sorted(self.ready_ids(payload)), ["DOUG-001", "IM-001"]
        )

    def test_cross_prefix_blocker_resolved_under_repo(self):
        # --repo scopes candidates but blocker status is still resolved
        # against the whole store.
        self.write_issue("DOUG-001", status="open")
        self.write_issue("IM-001", deps=[("DOUG-001", "blocks")])
        payload = self.run_json("--repo", "IM", expect_exit=1)
        self.assertEqual(self.ready_ids(payload), [])
        self.assertEqual(self.filtered_ids(payload), ["IM-001"])

    def test_lowercase_repo_prefix_resolves_case_insensitively(self):
        # `--repo im` now case-folds to the IM prefix instead of
        # erroring, and announces the normalization on stderr.
        self.write_issue("IM-001")
        r = self.run_cli("--repo", "im")
        self.assertEqual(r.returncode, 0)
        self.assertIn("resolved im -> IM", r.stderr)

    def test_malformed_repo_prefix_usage_error(self):
        # A prefix that cannot be a valid prefix even upper-cased (leading
        # digit) is still an exit-2 usage error (canonicalization applies only to
        # provably-valid case variants, never malformed input).
        self.write_issue("IM-001")
        r = self.run_cli("--repo", "1x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid --prefix", r.stderr)


class TestNoReadyWork(IssueReadyBase):
    def test_empty_set_exits_1_with_kv_stderr(self):
        self.write_issue("DOUG-001", status="open")
        self.write_issue("IM-001", deps=[("DOUG-001", "blocks")])
        r = self.run_cli("--repo", "IM")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error=no-ready-work", r.stderr)

    def test_empty_store_stdout_still_carries_shape(self):
        # IM-241: the documented JSON shape is now the --json opt-in.
        r = self.run_cli("--json")
        self.assertEqual(r.returncode, 1)
        payload = json.loads(r.stdout)
        self.assertEqual(payload, {"ready": [], "filtered_out": []})

    def test_default_output_is_human_readable_text(self):
        # IM-241 / surface.explicit-output-mode: text is the default on
        # every verb; `ready` was the surface's only inverted verb.
        self.write_issue("IM-001")
        r = self.run_cli()
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("Ready:"), r.stdout)
        self.assertNotIn('"ready"', r.stdout)

    def test_retired_text_flag_still_accepted(self):
        # --text names the default now; kept working for one release.
        self.write_issue("IM-001")
        r = self.run_cli("--text")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, self.run_cli().stdout)

    def test_empty_set_json_flag_emits_json_stderr(self):
        r = self.run_cli("--json")
        self.assertEqual(r.returncode, 1)
        obj = json.loads(r.stderr.strip())
        self.assertEqual(obj["exit"], 1)
        self.assertEqual(obj["class"], "no-ready-work")
        self.assertEqual(obj["error"], "no-ready-work")

    def test_empty_after_actor_filter_exits_1(self):
        self.write_issue("IM-001", claim=CLAIM_ALICE_ACTIVE)
        r = self.run_cli("--actor", "bob")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error=no-ready-work", r.stderr)


class TestActorFilter(IssueReadyBase):
    def test_actor_filters_issue_claimed_by_other(self):
        self.write_issue("IM-001", claim=CLAIM_ALICE_ACTIVE)
        self.write_issue("IM-002")
        payload = self.run_json("--actor", "bob")
        self.assertEqual(self.ready_ids(payload), ["IM-002"])
        self.assertEqual(payload["filtered_out"], [{
            "id": "IM-001",
            "reason": "claim held by alice, not bob",
        }])

    def test_actor_keeps_own_claim(self):
        self.write_issue("IM-001", claim=CLAIM_ALICE_ACTIVE)
        payload = self.run_json("--actor", "alice")
        self.assertEqual(self.ready_ids(payload), ["IM-001"])

    def test_actor_keeps_unclaimed_issue(self):
        self.write_issue("IM-001")
        payload = self.run_json("--actor", "bob")
        self.assertEqual(self.ready_ids(payload), ["IM-001"])

    def test_actor_keeps_expired_lease(self):
        self.write_issue("IM-001", claim=CLAIM_ALICE_EXPIRED)
        payload = self.run_json("--actor", "bob")
        self.assertEqual(self.ready_ids(payload), ["IM-001"])

    def test_actor_applies_after_blocks_filter(self):
        # A blocked candidate reports the dep reason, not the claim
        # reason.
        self.write_issue("DOUG-001", status="open")
        self.write_issue("IM-001", claim=CLAIM_ALICE_ACTIVE,
                         deps=[("DOUG-001", "blocks")])
        self.write_issue("IM-002")
        payload = self.run_json("--actor", "bob")
        self.assertEqual(
            payload["filtered_out"][0]["reason"],
            "blocked by DOUG-001 (open)",
        )


class TestTextRendering(IssueReadyBase):
    def test_text_ready_section(self):
        self.write_issue("IM-001", title="TTL lease primitive")
        r = self.run_cli("--text")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Ready:\n", r.stdout)
        self.assertIn("  IM-001  TTL lease primitive", r.stdout)

    def test_text_filtered_section(self):
        self.write_issue("DOUG-001", status="open")
        self.write_issue("IM-001")
        self.write_issue("IM-002", deps=[("DOUG-001", "blocks")])
        r = self.run_cli("--text", "--filtered", "all")
        self.assertIn("Filtered out (not ready):", r.stdout)
        self.assertIn("  IM-002  blocked by DOUG-001 (open)", r.stdout)

    def test_text_claim_filtered_line(self):
        self.write_issue("IM-001", claim=CLAIM_ALICE_ACTIVE)
        self.write_issue("IM-002")
        r = self.run_cli("--text", "--actor", "bob", "--filtered", "all")
        self.assertIn("  IM-001  claim held by alice", r.stdout)

    def test_text_empty_ready_prints_none(self):
        r = self.run_cli("--text")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Ready:\n  (none)", r.stdout)

    def test_text_no_filtered_section_when_nothing_filtered(self):
        self.write_issue("IM-001")
        r = self.run_cli("--text", "--filtered", "all")
        self.assertNotIn("Filtered out", r.stdout)


class TestPreconditionsAndUsage(IssueReadyBase):
    def test_json_and_text_mutually_exclusive(self):
        r = self.run_cli("--json", "--text")
        self.assertEqual(r.returncode, 2)

    def test_unknown_flag_usage_error(self):
        r = self.run_cli("--bogus")
        self.assertEqual(r.returncode, 2)

    def test_unresolvable_store_precondition(self):
        r = self.run_cli(store=self.tmp / "does-not-exist")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error=precondition-failed", r.stderr)
        self.assertIn("reason=store-unresolvable", r.stderr)

    def test_version_flag(self):
        r = self.run_cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.strip().startswith("v"))

    def test_help_flag(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        # The consolidated verb's help identifies as
        # `issue ready` (the flat `issue-ready` name is now a tombstone).
        self.assertIn("issue ready", r.stdout)
        self.assertIn("Examples:", r.stdout)


class TestLimit(IssueReadyBase):
    """any user-facing cap is post-filter and never silent."""

    def test_over_hundred_all_returned(self):
        for i in range(1, 151):
            self.write_issue("IM-%03d" % i)
        payload = self.run_json()
        self.assertEqual(len(payload["ready"]), 150)
        self.assertIn("IM-150", self.ready_ids(payload))

    def test_limit_caps_ready_with_stderr_notice(self):
        for i in range(1, 11):
            self.write_issue("IM-%03d" % i)
        r = self.run_cli("--limit", "3", "--json")
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        self.assertEqual(self.ready_ids(payload),
                         ["IM-001", "IM-002", "IM-003"])
        self.assertIn("7 more ready issue(s) not shown", r.stderr)
        self.assertIn("--limit 3", r.stderr)

    def test_limit_without_suppression_no_notice(self):
        self.write_issue("IM-001")
        self.write_issue("IM-002")
        r = self.run_cli("--limit", "5", "--json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        self.assertEqual(len(json.loads(r.stdout)["ready"]), 2)

    def test_limit_zero_is_unlimited(self):
        for i in range(1, 11):
            self.write_issue("IM-%03d" % i)
        r = self.run_cli("--limit", "0", "--json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        self.assertEqual(len(json.loads(r.stdout)["ready"]), 10)

    def test_limit_negative_usage_error(self):
        self.write_issue("IM-001")
        r = self.run_cli("--limit", "-1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid --limit", r.stderr)

    def test_limit_applies_after_repo_filter(self):
        for i in range(1, 6):
            self.write_issue("DOUG-%03d" % i)
            self.write_issue("IM-%03d" % i)
        r = self.run_cli("--repo", "IM", "--limit", "4", "--json")
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        self.assertEqual(self.ready_ids(payload),
                         ["IM-001", "IM-002", "IM-003", "IM-004"])
        self.assertIn("1 more ready issue(s) not shown", r.stderr)

    def test_limit_never_truncates_filtered_out(self):
        # IM-249 registered --limit's single meaning: it caps this verb's
        # PRIMARY result rows (the ready list) and never bounds
        # explanatory output. Same rule as `index` (per-group primary
        # rows) and `history show` / `history activity` (emitted rows).
        self.write_issue("DOUG-900", status="open")
        for i in range(1, 5):
            self.write_issue("IM-%03d" % i)
        self.write_issue("IM-999", deps=[("DOUG-900", "blocks")])
        r = self.run_cli("--limit", "2", "--json", "--filtered", "all")
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        self.assertEqual(len(payload["ready"]), 2)
        self.assertEqual(self.filtered_ids(payload), ["IM-999"])


class TestFilteredSection(IssueReadyBase):
    """IM-249: `filtered_out` is a secondary explanatory section under its
    own argv control, defaulting to off. The verb answers "what can I
    claim"; "why can I not claim X" is a different question."""

    def blocked_store(self):
        self.write_issue("DOUG-900", status="in-progress")
        self.write_issue("IM-001")
        self.write_issue("IM-999", deps=[("DOUG-900", "blocks")])

    def test_default_omits_section_from_text(self):
        self.blocked_store()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0)
        self.assertIn("  IM-001", r.stdout)
        self.assertNotIn("Filtered out", r.stdout)
        self.assertNotIn("IM-999", r.stdout)

    def test_default_empties_the_json_key_but_keeps_it(self):
        # Output shape is a function of argv, never of store state
        # (surface.explicit-output-mode), so the key is unconditional --
        # a caller never has to branch on its presence.
        self.blocked_store()
        payload = json.loads(self.run_cli("--json").stdout)
        self.assertIn("filtered_out", payload)
        self.assertEqual(payload["filtered_out"], [])

    def test_filtered_all_emits_the_section(self):
        self.blocked_store()
        r = self.run_cli("--text", "--filtered", "all")
        self.assertIn("Filtered out (not ready):", r.stdout)
        self.assertIn("  IM-999  blocked by DOUG-900 (in-progress)", r.stdout)
        payload = json.loads(
            self.run_cli("--json", "--filtered", "all").stdout
        )
        self.assertEqual(self.filtered_ids(payload), ["IM-999"])

    def test_filtered_none_is_the_explicit_default(self):
        self.blocked_store()
        self.assertEqual(
            self.run_cli("--filtered", "none").stdout, self.run_cli().stdout,
        )

    def test_filtered_rejects_unknown_mode(self):
        self.blocked_store()
        r = self.run_cli("--filtered", "some")
        self.assertEqual(r.returncode, 2)

    def test_empty_ready_set_hints_at_the_section_on_stderr(self):
        # The one case where the explanation is the only useful output.
        # The pointer rides on stderr so stdout stays argv-determined.
        self.write_issue("DOUG-900", status="in-progress")
        self.write_issue("IM-999", deps=[("DOUG-900", "blocks")])
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 candidate issue(s) filtered out", r.stderr)
        self.assertIn("--filtered all", r.stderr)

    def test_no_hint_when_nothing_was_filtered(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("filtered out", r.stderr)

    def test_no_hint_when_the_section_was_requested(self):
        self.write_issue("DOUG-900", status="in-progress")
        self.write_issue("IM-999", deps=[("DOUG-900", "blocks")])
        r = self.run_cli("--filtered", "all")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("pass --filtered all", r.stderr)

    def test_empty_ready_set_json_stderr_stays_one_document(self):
        # The hint is a plain informational notice; the structured error
        # line remains parseable as its own JSON document.
        self.write_issue("DOUG-900", status="in-progress")
        self.write_issue("IM-999", deps=[("DOUG-900", "blocks")])
        r = self.run_cli("--json")
        self.assertEqual(r.returncode, 1)
        lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
        obj = json.loads(lines[-1])
        self.assertEqual(obj["error"], "no-ready-work")

    def test_section_is_unaffected_by_json_field_selection(self):
        self.blocked_store()
        payload = json.loads(
            self.run_cli("--json=id", "--filtered", "all").stdout
        )
        self.assertEqual(payload["ready"], [{"id": "IM-001"}])
        self.assertEqual(payload["filtered_out"],
                         [{"id": "IM-999",
                           "reason": "blocked by DOUG-900 (in-progress)"}])


if __name__ == "__main__":
    unittest.main()
