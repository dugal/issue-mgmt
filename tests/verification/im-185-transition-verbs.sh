#!/usr/bin/env bash
# Verification script for WP3 (IM-185) NEW behaviors on the consolidated
# `issue` dispatcher:
#
#   - `issue complete` open -> done pass-through (design 5.5): an `open`
#     issue is completed as open -> in-progress -> done in ONE atomic
#     operation -- one lock, one commit, BOTH audit lines. The `transition`
#     primitive stays strict (open -> done still refuses; covered in
#     transition-issue.sh Section D).
#   - `issue start` and `issue ready` auto-reap expired leases before they
#     act (design 2a "Reap amendment").
#
# These verbs exist ONLY on the consolidated dispatcher (they were skills,
# never flat binaries), so this script drives `issue <verb>` directly and
# is style-independent -- it does not go through resolve_cli.
#
# Run from the project root:
#     tests/verification/im-185-transition-verbs.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE="$ISSUE_MGMT_BIN/issue"

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then pass "$3 ($2)"; else
        fail "$3: expected '$1', got '$2'"; fi
}
assert_file_contains() {
    if grep -qF -- "$1" "$2"; then pass "$3 (file contains '$1')"; else
        fail "$3: '$1' not in $2"; fi
}
assert_file_missing() {
    if grep -qF -- "$1" "$2"; then fail "$3: unexpected '$1' in $2"; else
        pass "$3 (no '$1')"; fi
}
assert_contains() {
    if echo "$2" | grep -qF -- "$1"; then pass "$3 (contains '$1')"; else
        fail "$3: '$1' not found in: $2"; fi
}

WORKDIR="$(mktemp -d -t im185.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT
unset ISSUE_STORE_ROOT

STORE="$WORKDIR/store"
mkdir -p "$STORE/IM"
(
    cd "$STORE"
    git init -q
    git config user.email test@example.com
    git config user.name test
    git commit -q --allow-empty -m "init store"
)
TODAY=$(date +%F)

# make_issue <id> <status> [extra frontmatter lines...]
make_issue() {
    local id="$1" status="$2"; shift 2
    {
        echo "---"
        echo "id: $id"
        echo "title: \"Fixture $id\""
        echo "type: chore"
        echo "status: $status"
        echo "priority: medium"
        echo "created: 2026-07-01"
        echo "updated: 2026-07-01"
        echo "branch: \"\""
        for line in "$@"; do printf '%s\n' "$line"; done
        echo "---"
        echo ""
        echo "## Summary"
        echo ""
        echo "Body for $id."
    } > "$STORE/IM/$id.md"
}

EXPIRED_CLAIM=(
    "claim:"
    "  assignee: ghost"
    "  claimed_at: 2020-01-01T00:00:00Z"
    "lease_until: 2020-01-01T01:00:00Z"
    "claim_generation: 3"
)

echo "== [A] issue complete: open -> done pass-through (one commit, both notes) =="
make_issue IM-001 open
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-001 >/dev/null 2>&1 || RC=$?
assert_eq 0 "$RC" "complete on open issue exits 0"
# COVERS: state.complete-passthrough
assert_file_contains "status: done" "$STORE/IM/IM-001.md" \
    "open issue ends in done"
# COVERS: state.complete-passthrough
assert_file_contains "$TODAY: status: open -> in-progress" "$STORE/IM/IM-001.md" \
    "pass-through appends the open -> in-progress audit line"
# COVERS: state.complete-passthrough
assert_file_contains "$TODAY: status: in-progress -> done" "$STORE/IM/IM-001.md" \
    "pass-through appends the in-progress -> done audit line"
# One user-visible action = one commit: the pass-through commits
# once, message `transition IM-001 done`.
LASTMSG=$(cd "$STORE" && git log -1 --format=%s)
# COVERS: state.complete-passthrough
assert_eq "transition IM-001 done" "$LASTMSG" \
    "pass-through produces exactly one commit named transition IM-001 done"

echo "== [A2] issue complete: --summary records the closing note; --reason is an accepted synonym =="
make_issue IM-011 in-progress
ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-011 --summary "all acceptance checks passed" >/dev/null 2>&1
# COVERS: state.complete-summary-flag
assert_file_contains "$TODAY: status: in-progress -> done -- all acceptance checks passed" "$STORE/IM/IM-011.md" \
    "--summary is recorded in the closing audit line"
make_issue IM-012 in-progress
ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-012 --reason "closed via reason synonym" >/dev/null 2>&1
# COVERS: state.complete-summary-flag
assert_file_contains "$TODAY: status: in-progress -> done -- closed via reason synonym" "$STORE/IM/IM-012.md" \
    "--reason still works as a synonym for --summary (back-compat)"

echo "== [A3] issue complete: --note records the note; terminal transition clears branch =="
make_issue IM-013 in-progress
ISSUE_STORE_ROOT="$STORE" "$ISSUE" edit IM-013 --set branch="feat-note" >/dev/null 2>&1
ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-013 --note "done via note flag" >/dev/null 2>&1
# COVERS: state.complete-summary-flag
assert_file_contains "$TODAY: status: in-progress -> done -- done via note flag" "$STORE/IM/IM-013.md" \
    "--note is accepted as a closing-note synonym"
# COVERS: state.terminal-clears-branch
assert_file_contains 'branch: ""' "$STORE/IM/IM-013.md" \
    "completing clears the stale branch field"
make_issue IM-014 in-progress
ISSUE_STORE_ROOT="$STORE" "$ISSUE" edit IM-014 --set branch="feat-cancel" >/dev/null 2>&1
ISSUE_STORE_ROOT="$STORE" "$ISSUE" cancel IM-014 --reason "not pursued" >/dev/null 2>&1
# COVERS: state.terminal-clears-branch
assert_file_contains 'branch: ""' "$STORE/IM/IM-014.md" \
    "cancelling clears the stale branch field"

echo "== [B] issue complete: in-progress -> done is a plain single-line transition =="
make_issue IM-002 in-progress
ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-002 >/dev/null 2>&1
assert_file_contains "status: done" "$STORE/IM/IM-002.md" "in-progress completes to done"
# COVERS: state.complete-passthrough
assert_file_missing "status: open -> in-progress" "$STORE/IM/IM-002.md" \
    "no synthetic open -> in-progress line when already in-progress"

echo "== [C] issue complete: primitive matrix still governs blocked/cancelled =="
make_issue IM-003 blocked
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-003 >/dev/null 2>"$WORKDIR/c.err" || RC=$?
# COVERS: state.complete-passthrough
assert_eq 2 "$RC" "complete on a blocked issue is an illegal transition (exit 2)"
assert_contains "illegal transition" "$(cat "$WORKDIR/c.err")" \
    "blocked -> done refused with illegal-transition message"
assert_file_contains "status: blocked" "$STORE/IM/IM-003.md" \
    "refused complete left the blocked issue unchanged"

echo "== [D] issue complete: done -> done is a no-op (exit 0) =="
make_issue IM-004 done
BEFORE=$(md5 -q "$STORE/IM/IM-004.md" 2>/dev/null || md5sum "$STORE/IM/IM-004.md" | cut -d' ' -f1)
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-004 >/dev/null 2>&1 || RC=$?
AFTER=$(md5 -q "$STORE/IM/IM-004.md" 2>/dev/null || md5sum "$STORE/IM/IM-004.md" | cut -d' ' -f1)
assert_eq 0 "$RC" "complete on a done issue is a no-op exit 0"
assert_eq "$BEFORE" "$AFTER" "no-op complete does not rewrite the file"

echo "== [D2] issue complete: status-less frontmatter is a precondition failure =="
# The pass-through reads status; an issue missing the field surfaces exit 1
# (ValueError -> precondition), not a crash.
cat > "$STORE/IM/IM-005.md" <<'EOF'
---
id: IM-005
title: "No status"
type: chore
priority: medium
created: 2026-07-01
updated: 2026-07-01
---

## Summary

Body.
EOF
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-005 >/dev/null 2>&1 || RC=$?
assert_eq 1 "$RC" "complete on a status-less issue exits 1"

echo "== [E] issue start: auto-reaps expired leases in the prefix before claiming =="
make_issue IM-010 open "${EXPIRED_CLAIM[@]}"   # stale ghost claim, expired
make_issue IM-011 open                          # the claim target
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" start IM-011 --claim alice --branch feat-x \
    >/dev/null 2>&1 || RC=$?
assert_eq 0 "$RC" "start on an open issue exits 0"
assert_file_contains "status: in-progress" "$STORE/IM/IM-011.md" \
    "start flips the target open -> in-progress"
assert_file_contains "assignee: alice" "$STORE/IM/IM-011.md" \
    "start records the claimant"
assert_file_contains 'branch: "feat-x"' "$STORE/IM/IM-011.md" \
    "start records the working branch in the same write"
# COVERS: workflow.auto-reap
assert_file_missing "assignee: ghost" "$STORE/IM/IM-010.md" \
    "start auto-reaped the sibling's expired claim before claiming"
# COVERS: workflow.auto-reap
assert_file_contains "expired lease for ghost reaped" "$STORE/IM/IM-010.md" \
    "reaper note recorded on the auto-reaped sibling"

echo "== [F] issue ready: auto-reaps expired leases before computing the ready set =="
make_issue IM-020 open "${EXPIRED_CLAIM[@]}"   # expired claim -> should be reaped, then ready
OUT=$(ISSUE_STORE_ROOT="$STORE" "$ISSUE" ready --repo IM --actor bob 2>/dev/null)
READY_IDS=$(echo "$OUT" | python3 -c "import json,sys; print(' '.join(sorted(r['id'] for r in json.load(sys.stdin)['ready'])))")
# COVERS: workflow.auto-reap
assert_contains "IM-020" "$READY_IDS" \
    "an issue whose expired claim was auto-reaped is claimable by actor bob"
# COVERS: workflow.auto-reap
assert_file_missing "assignee: ghost" "$STORE/IM/IM-020.md" \
    "ready auto-reaped the expired claim"

echo "== [G] issue unblock: return a blocked issue to an active state =="
# unblock is a convenience verb that only exists on the dispatcher (it was
# the /issue-block skill's unblock path, never a flat binary); it delegates
# to the transition primitive's _execute_transition core.
make_issue IM-030 blocked
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" unblock IM-030 >/dev/null 2>&1 || RC=$?
assert_eq 0 "$RC" "unblock on a blocked issue exits 0"
# COVERS: state.canonical-states
assert_file_contains "status: in-progress" "$STORE/IM/IM-030.md" \
    "unblock defaults blocked -> in-progress"
# COVERS: state.auto-stamp-notes
assert_file_contains "$TODAY: status: blocked -> in-progress" "$STORE/IM/IM-030.md" \
    "unblock appends the blocked -> in-progress audit line"

make_issue IM-031 blocked
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" unblock IM-031 --to open --reason "RM-038 shipped" \
    >/dev/null 2>&1 || RC=$?
assert_eq 0 "$RC" "unblock --to open exits 0"
# COVERS: state.canonical-states
assert_file_contains "status: open" "$STORE/IM/IM-031.md" \
    "unblock --to open lands in open"
assert_file_contains "RM-038 shipped" "$STORE/IM/IM-031.md" \
    "unblock records the --reason in the audit line"

make_issue IM-032 blocked
JSON=$(ISSUE_STORE_ROOT="$STORE" "$ISSUE" unblock IM-032 --json 2>/dev/null)
echo "$JSON" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null \
    && pass "unblock --json emits one parseable document" \
    || fail "unblock --json not parseable: $JSON"
assert_contains "\"new\": \"in-progress\"" "$JSON" "unblock --json reports the new state"

# unblock targets only open/in-progress, both reachable from any state, so
# it defers cleanly to the primitive: an already in-progress issue is a
# same-state no-op (exit 0), not an error.
make_issue IM-033 in-progress
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" unblock IM-033 >/dev/null 2>&1 || RC=$?
# COVERS: state.canonical-states
assert_eq 0 "$RC" "unblock of an in-progress issue is a same-state no-op (exit 0)"
assert_file_contains "status: in-progress" "$STORE/IM/IM-033.md" \
    "unblock no-op leaves the issue in-progress"

# unblock with no id / a negative claim-generation are usage errors (exit 2)
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" unblock 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "unblock with no issue id exits 2"
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" unblock IM-030 --claim-generation -1 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "unblock with a negative --claim-generation exits 2"

echo "== [H] issue start: usage errors and lease loser paths =="
# start's own argument-validation branches (before it ever touches the store).
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" start 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "start with no issue id exits 2"
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" start IM-040 --claim 'bad claimant!' 2>/dev/null || RC=$?
# COVERS: workflow.ttl-lease
assert_eq 2 "$RC" "start with an invalid claimant exits 2"
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" start IM-040 --claim alice --lease-ttl 0 2>/dev/null || RC=$?
# COVERS: workflow.ttl-lease
assert_eq 2 "$RC" "start with --lease-ttl 0 exits 2"
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" start IM-040 --claim alice --claim-generation -1 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "start with a negative --claim-generation exits 2"

# lost-race: a second actor cannot claim an issue already held under a live lease.
make_issue IM-041 open
ISSUE_STORE_ROOT="$STORE" "$ISSUE" start IM-041 --claim alice >/dev/null 2>&1
RC=0
STDERR=$(ISSUE_STORE_ROOT="$STORE" "$ISSUE" start IM-041 --claim bob 2>&1 >/dev/null) || RC=$?
# COVERS: concurrency.exit-17
assert_eq 4 "$RC" "start on an issue held by another actor exits 4 (lost-race)"
assert_contains "error=lost-race" "$STDERR" "start lost-race stderr names error=lost-race"

# not-claimable: start refuses a blocked issue (exit 7).
make_issue IM-042 blocked
RC=0
STDERR=$(ISSUE_STORE_ROOT="$STORE" "$ISSUE" start IM-042 --claim alice 2>&1 >/dev/null) || RC=$?
# COVERS: concurrency.exit-17
assert_eq 7 "$RC" "start on a blocked issue exits 7 (not-claimable)"
assert_contains "error=not-claimable" "$STDERR" "start not-claimable stderr names the class"

echo "== [I] issue block / cancel: convenience verbs + usage errors =="
# block and cancel are dispatcher-only convenience verbs delegating to the
# transition primitive; exercise the happy path and each verb's own
# argument-validation branches.
make_issue IM-050 open
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" block IM-050 --reason "waiting on RM-038" \
    >/dev/null 2>&1 || RC=$?
assert_eq 0 "$RC" "block on an open issue exits 0"
# COVERS: state.canonical-states
assert_file_contains "status: blocked" "$STORE/IM/IM-050.md" \
    "block flips the issue to blocked"
assert_file_contains "waiting on RM-038" "$STORE/IM/IM-050.md" \
    "block records the --reason"

# block without --reason defers to the primitive's reason requirement (exit 2)
make_issue IM-051 open
RC=0
STDERR=$(ISSUE_STORE_ROOT="$STORE" "$ISSUE" block IM-051 2>&1 >/dev/null) || RC=$?
# COVERS: state.auto-stamp-notes
assert_eq 2 "$RC" "block without --reason exits 2"
assert_contains "--reason" "$STDERR" "block-without-reason stderr names --reason"

# block usage errors: missing id, negative claim-generation
RC=0; ISSUE_STORE_ROOT="$STORE" "$ISSUE" block 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "block with no issue id exits 2"
RC=0; ISSUE_STORE_ROOT="$STORE" "$ISSUE" block IM-050 --claim-generation -1 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "block with a negative --claim-generation exits 2"

make_issue IM-052 in-progress
RC=0
ISSUE_STORE_ROOT="$STORE" "$ISSUE" cancel IM-052 --reason "superseded by IM-999" \
    >/dev/null 2>&1 || RC=$?
assert_eq 0 "$RC" "cancel on an in-progress issue exits 0"
# COVERS: state.no-delete
assert_file_contains "status: cancelled" "$STORE/IM/IM-052.md" \
    "cancel flips the issue to cancelled (file persists)"

# cancel usage errors: missing id, negative claim-generation
RC=0; ISSUE_STORE_ROOT="$STORE" "$ISSUE" cancel 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "cancel with no issue id exits 2"
RC=0; ISSUE_STORE_ROOT="$STORE" "$ISSUE" cancel IM-052 --claim-generation -1 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "cancel with a negative --claim-generation exits 2"

# complete usage errors: missing id, negative claim-generation
RC=0; ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "complete with no issue id exits 2"
RC=0; ISSUE_STORE_ROOT="$STORE" "$ISSUE" complete IM-001 --claim-generation -1 2>/dev/null || RC=$?
# COVERS: output.exit-codes
assert_eq 2 "$RC" "complete with a negative --claim-generation exits 2"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
