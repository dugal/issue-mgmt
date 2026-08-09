#!/usr/bin/env bash
# Pre-merge concurrent-edit smoke test for the v2.0.0 release gate.
#
# Exercises the production CLI binaries (edit-issue, transition-issue)
# end-to-end against a real issues store (ISSUE_STORE_ROOT, or ~/issues-store)
# so the v2.0.0 release ceremony has evidence that multi-session
# contention against the real layout (not a temp fixture) yields the
# documented exit 3 (concurrent-edit) on the loser and exit 0 on the
# winner, with no torn writes.
#
# Safety: creates a temporary SMOKE/ prefix in the real store, runs the
# race, and removes SMOKE/ on exit (trap fires on success, failure, or
# interrupt). Refuses to start if SMOKE/ already exists, so a stale
# fixture from a crashed prior run does not get silently clobbered.
# Uses --no-reindex on every CLI invocation so the store's INDEX.md is
# left untouched.
#
# Run from the project root:
#     tests/verification/v2-release-smoke.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
STORE="${ISSUE_STORE_ROOT:-$HOME/issues-store}"
EDIT_ISSUE="$(resolve_cli edit-issue)"
TRANSITION_ISSUE="$(resolve_cli transition-issue)"
CONCURRENCY_PY="$PROJECT_ROOT/lib/concurrency.py"  # Tier 2: stack-coupled test adversary; see docs/design/concurrency-test-hook.md

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then pass "$3 ($2)"; else fail "$3: expected '$1', got '$2'"; fi
}

# Pre-flight
# This smoke test is deliberately non-hermetic: it exercises the real store
# layout as release-gate evidence. On a clean box / CI / container where the
# real store is absent, SKIP (exit 0) rather than FAIL so the full suite stays
# green.
[ -d "$STORE" ] || { echo "SKIP: store directory not found: $STORE (non-hermetic release-gate smoke; run against a real store)"; exit 0; }
[ -x "$EDIT_ISSUE" ] || { echo "FAIL: edit-issue not executable: $EDIT_ISSUE"; exit 1; }
[ -x "$TRANSITION_ISSUE" ] || { echo "FAIL: transition-issue not executable: $TRANSITION_ISSUE"; exit 1; }
[ -f "$CONCURRENCY_PY" ] || { echo "FAIL: concurrency.py missing: $CONCURRENCY_PY"; exit 1; }

PREFIX="SMOKE"
SMOKE_DIR="$STORE/$PREFIX"

if [ -e "$SMOKE_DIR" ]; then
    echo "FAIL: $SMOKE_DIR already exists; refusing to clobber (stale fixture from a prior crash?)"
    exit 1
fi

cleanup() {
    rm -rf "$SMOKE_DIR"
}
trap cleanup EXIT

mkdir -p "$SMOKE_DIR/.locks"

ISSUE_ID="SMOKE-001"
ISSUE_FILE="$SMOKE_DIR/$ISSUE_ID.md"
LOCK="$SMOKE_DIR/.locks/$ISSUE_ID.lock"
TODAY=$(date +%Y-%m-%d)

cat > "$ISSUE_FILE" <<EOF
---
id: $ISSUE_ID
title: "concurrent-edit smoke test fixture"
type: chore
status: open
priority: low
created: $TODAY
updated: $TODAY
branch: ""
tags: []
---

## Summary

Fixture for the v2.0.0 release concurrent-edit smoke test. Removed on
exit.

## Implementation Notes

EOF

export ISSUE_STORE_ROOT="$STORE"

ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"; cleanup' EXIT

# ---------------------------------------------------------------------------
# [1] edit-issue against a held lock -> exit 3, file unchanged
# ---------------------------------------------------------------------------
echo
echo "[1] edit-issue blocked by held lock against real store"

(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 2"
) &
HOLDER_PID=$!
sleep 0.3

RC_EDIT=0
"$EDIT_ISSUE" "$ISSUE_ID" --set priority=medium --no-reindex --lock-timeout 0.5 \
    >/dev/null 2>"$ERR_FILE" || RC_EDIT=$?
wait $HOLDER_PID
RC_HOLDER=$?

assert_eq "0" "$RC_HOLDER" "lock holder exits 0"
assert_eq "3" "$RC_EDIT" "edit-issue blocked by held lock exits 3"

# v3.0.0: the consolidated `issue edit` verb emits the
# `issue edit:` human prefix (the verb name); exit codes + --json are
# byte-identical to the retired flat `edit-issue`. The `issue transition`
# primitive deliberately kept the `transition-issue:` prefix (WP3
# byte-identical), so only this edit assertion moved.
if grep -q "issue edit: error=concurrent-edit" "$ERR_FILE"; then
    pass "issue edit stderr names 'issue edit: error=concurrent-edit'"
else
    fail "issue edit stderr missing 'issue edit: error=concurrent-edit'"
    echo "--- stderr was: ---"
    cat "$ERR_FILE"
    echo "-------------------"
fi

if grep -q "^priority: low$" "$ISSUE_FILE"; then
    pass "issue file unchanged after blocked edit"
else
    fail "issue file mutated despite exit 3"
fi

# ---------------------------------------------------------------------------
# [2] transition-issue against a held lock -> exit 3, status unchanged
# ---------------------------------------------------------------------------
echo
echo "[2] transition-issue blocked by held lock against real store"

(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 2"
) &
HOLDER_PID=$!
sleep 0.3

RC_TRANS=0
"$TRANSITION_ISSUE" "$ISSUE_ID" in-progress --no-reindex --lock-timeout 0.5 \
    >/dev/null 2>"$ERR_FILE" || RC_TRANS=$?
wait $HOLDER_PID

assert_eq "3" "$RC_TRANS" "transition-issue blocked by held lock exits 3"

if grep -q "transition-issue: error=concurrent-edit" "$ERR_FILE"; then
    pass "transition-issue stderr names 'transition-issue: error=concurrent-edit'"
else
    fail "transition-issue stderr missing 'transition-issue: error=concurrent-edit'"
    echo "--- stderr was: ---"
    cat "$ERR_FILE"
    echo "-------------------"
fi

if grep -q "^status: open$" "$ISSUE_FILE"; then
    pass "status unchanged after blocked transition"
else
    fail "status mutated despite exit 3"
fi

# ---------------------------------------------------------------------------
# [3] Uncontested writes succeed end-to-end against real store
# ---------------------------------------------------------------------------
echo
echo "[3] Uncontested writes succeed and persist"

RC=0
"$EDIT_ISSUE" "$ISSUE_ID" --set priority=medium --no-reindex >/dev/null 2>"$ERR_FILE" || RC=$?
assert_eq "0" "$RC" "uncontested edit-issue exits 0"
if grep -q "^priority: medium$" "$ISSUE_FILE"; then
    pass "uncontested edit applied (priority: low -> medium)"
else
    fail "uncontested edit did not persist"
fi

RC=0
"$TRANSITION_ISSUE" "$ISSUE_ID" in-progress --no-reindex >/dev/null 2>"$ERR_FILE" || RC=$?
assert_eq "0" "$RC" "uncontested transition-issue exits 0"
if grep -q "^status: in-progress$" "$ISSUE_FILE"; then
    pass "uncontested transition applied (status: open -> in-progress)"
else
    fail "uncontested transition did not persist"
fi

# ---------------------------------------------------------------------------
# [4] Real-process race -- two edit-issue invocations on the same file
# ---------------------------------------------------------------------------
echo
echo "[4] Two real edit-issue processes race for the same lock"
#
# Hold the lock externally so one of the two real edit-issue processes
# is guaranteed to time out on lock acquisition. With a 5s holder and a
# 0.5s loser timeout, the loser exits 3; the winner runs after the
# holder releases and exits 0.

(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 1"
) &
HOLDER_PID=$!
sleep 0.3

RC1_FILE=$(mktemp)
RC2_FILE=$(mktemp)

(
    rc=0
    "$EDIT_ISSUE" "$ISSUE_ID" --set tags=alpha --no-reindex --lock-timeout 3 >/dev/null 2>&1 || rc=$?
    echo $rc > "$RC1_FILE"
) &
P1=$!

(
    rc=0
    "$EDIT_ISSUE" "$ISSUE_ID" --set tags=beta --no-reindex --lock-timeout 0.2 >/dev/null 2>&1 || rc=$?
    echo $rc > "$RC2_FILE"
) &
P2=$!

wait $HOLDER_PID
wait $P1
wait $P2

RC1=$(cat "$RC1_FILE")
RC2=$(cat "$RC2_FILE")
rm -f "$RC1_FILE" "$RC2_FILE"

# P2 has a 0.2s timeout and the holder runs for ~1s, so P2 must time out
# (exit 3). P1 has a 3s timeout, so it waits out the holder and writes.
assert_eq "3" "$RC2" "short-timeout writer exits 3"
assert_eq "0" "$RC1" "long-timeout writer exits 0"

# Final tags should be "alpha" (P1 was the writer that succeeded)
if grep -q "^tags: alpha$" "$ISSUE_FILE"; then
    pass "final state matches the winning writer (tags: alpha)"
else
    fail "final state does not match the winning writer"
    grep "^tags:" "$ISSUE_FILE"
fi

# ---------------------------------------------------------------------------
# [5] No side-effect contamination of the real store
# ---------------------------------------------------------------------------
echo
echo "[5] Real store untouched outside SMOKE/"

# INDEX.md should not mention SMOKE -- we used --no-reindex throughout.
if grep -q "SMOKE" "$STORE/INDEX.md" 2>/dev/null; then
    fail "store INDEX.md references SMOKE (--no-reindex was bypassed?)"
else
    pass "store INDEX.md does not reference SMOKE"
fi

# Registry should not mention SMOKE
if [ -f "$STORE/.repos" ] && grep -q "SMOKE" "$STORE/.repos"; then
    fail "store registry references SMOKE"
else
    pass "store registry does not reference SMOKE"
fi

# Store git status: SMOKE/ is the only untracked path (will go away on cleanup)
if command -v git >/dev/null 2>&1 && [ -d "$STORE/.git" ]; then
    STATUS_OUT=$(cd "$STORE" && git status --porcelain 2>/dev/null)
    NON_SMOKE_LINES=$(printf "%s\n" "$STATUS_OUT" | grep -v "^.. *$PREFIX/" | grep -v "^$" || true)
    if [ -z "$NON_SMOKE_LINES" ]; then
        pass "store git status: only SMOKE/ is untracked (no other side effects)"
    else
        fail "store git status shows non-SMOKE side effects:"
        printf "%s\n" "$NON_SMOKE_LINES"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "==============================="
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo "==============================="

if [ $FAIL -ne 0 ]; then
    exit 1
fi
exit 0
