#!/usr/bin/env bash
# Concurrent-edit smoke test (originally the v2.0.0 release gate).
#
# Exercises the mutating CLI verbs (`issue edit`, `issue transition`)
# end-to-end against a REAL store LAYOUT -- a git-managed store root with a
# per-prefix directory, a .locks/ dir and auto-commit-per-mutation live --
# so the release ceremony has evidence that multi-session contention yields
# the documented exit 3 (concurrent-edit) on the loser and exit 0 on the
# winner, with no torn writes.
#
# TWO MODES (IM-255).
#
#   DEFAULT -- hermetic. Builds a throwaway store under `mktemp -d`,
#   git-inits it, gives it the suite's fixture identity, and runs the whole
#   race there. HOME is redirected into the same temp tree so no step of the
#   store-resolution precedence can reach the operator's store. A
#   caller-supplied ISSUE_STORE_ROOT is deliberately IGNORED here: honoring
#   it would put the script back in the business of guessing whether the
#   store it was handed is safe to write to.
#
#   REAL STORE -- opt-in, and it takes two deliberate acts:
#       ISSUE_MGMT_SMOKE_REAL_STORE=1 ISSUE_STORE_ROOT=/path/to/store \
#           tests/verification/v2-release-smoke.sh
#   The opt-in flag alone is an error: the store must be named explicitly,
#   because the whole defect this guard exists to prevent was a default that
#   resolved to the operator's own store.
#
# WHY THE DEFAULT HAD TO CHANGE. This script used to resolve
# `${ISSUE_STORE_ROOT:-$HOME/issues-store}`, so a bare run mutated the
# operator's real store. Its fixture writes cannot be undone by the cleanup
# trap, because `concurrency.auto-commit-per-mutation` has already committed
# each one by the time the trap fires: 447 commits touching SMOKE/ had
# accumulated in the production store by 2026-11-12, the newest of them from
# the v3.4.0 release ceremony's own verification sweep. The script sits in
# tests/verification/ with the executable bit set, so every
# `for f in tests/verification/*.sh` sweep picked it up by glob. The
# executable bit is retained precisely because the default mode is now safe
# to sweep; the dangerous mode is reachable only on purpose.
#
# Safety in both modes: creates a temporary SMOKE/ prefix in the target
# store, runs the race, and removes it on exit (trap fires on success,
# failure, or interrupt). Refuses to start if SMOKE/ already exists, so a
# stale fixture from a crashed prior run is never silently clobbered. Uses
# --no-reindex on every CLI invocation so the store's INDEX.md is left
# untouched.
#
# Run from the project root:
#     tests/verification/v2-release-smoke.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"

# resolve_cli is the suite's ISSUE_MGMT_BIN seam (IM-149): it maps a flat
# NAME onto the `issue <verb>` dispatcher by default, and an alternate
# implementation supplied via ISSUE_MGMT_BIN can still ship flat binaries.
# The flat NAMES are the seam's vocabulary, not a claim that the pre-v3.0.0
# binaries are what runs -- the variables below are named for the verbs that
# actually execute.
ISSUE_EDIT="$(resolve_cli edit-issue)"
ISSUE_TRANSITION="$(resolve_cli transition-issue)"
ISSUE_INIT_STORE="$(resolve_cli init-issue-store)"
CONCURRENCY_PY="$PROJECT_ROOT/lib/concurrency.py"  # Tier 2: stack-coupled test adversary; see docs/design/concurrency-test-hook.md

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then pass "$3 ($2)"; else fail "$3: expected '$1', got '$2'"; fi
}

[ -x "$ISSUE_EDIT" ] || { echo "FAIL: edit verb not executable: $ISSUE_EDIT"; exit 1; }
[ -x "$ISSUE_TRANSITION" ] || { echo "FAIL: transition verb not executable: $ISSUE_TRANSITION"; exit 1; }
[ -x "$ISSUE_INIT_STORE" ] || { echo "FAIL: init-store verb not executable: $ISSUE_INIT_STORE"; exit 1; }
[ -f "$CONCURRENCY_PY" ] || { echo "FAIL: concurrency.py missing: $CONCURRENCY_PY"; exit 1; }

# ---------------------------------------------------------------------------
# Mode selection + store construction
# ---------------------------------------------------------------------------
SCRATCH=""
ERR_FILE=""

cleanup() {
    [ -n "${SMOKE_DIR:-}" ] && rm -rf "$SMOKE_DIR"
    [ -n "$ERR_FILE" ] && rm -f "$ERR_FILE"
    [ -n "$SCRATCH" ] && rm -rf "$SCRATCH"
    return 0
}
trap cleanup EXIT

if [ "${ISSUE_MGMT_SMOKE_REAL_STORE:-0}" = "1" ]; then
    MODE_LABEL="opt-in real store"
    if [ -z "${ISSUE_STORE_ROOT:-}" ]; then
        echo "FAIL: ISSUE_MGMT_SMOKE_REAL_STORE=1 requires ISSUE_STORE_ROOT to name"
        echo "      the store explicitly -- this mode has no default store, by design."
        exit 2
    fi
    STORE="$ISSUE_STORE_ROOT"
    # On a clean box / CI / container the named store may be absent. SKIP
    # (exit 0) rather than FAIL so a full suite run stays green.
    [ -d "$STORE" ] || { echo "SKIP: store directory not found: $STORE (opt-in real-store mode)"; exit 0; }
else
    MODE_LABEL="throwaway store"
    SCRATCH=$(mktemp -d -t im-255-smoke.XXXXXX) || { echo "FAIL: mktemp -d failed"; exit 1; }
    STORE="$SCRATCH/store"
    # Redirect HOME into the temp tree so neither a `$HOME`-rooted path nor
    # ~/.config/issue-mgmt/config can route a mutation at the operator store.
    # ISSUE_STORE_ROOT is exported below and takes precedence regardless;
    # this is the belt to that braces.
    export HOME="$SCRATCH/home"
    mkdir -p "$HOME"
    # The suite's one throwaway git identity (IM-268), in env form because
    # the store repo does not exist yet and the scaffolder commits for us.
    export GIT_AUTHOR_NAME="issue-mgmt smoke fixture"
    export GIT_AUTHOR_EMAIL="$FIXTURE_EMAIL"
    export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
    export GIT_COMMITTER_EMAIL="$FIXTURE_EMAIL"
    # Scaffold through the project's OWN store scaffolder rather than a
    # hand-rolled `git init`. Two reasons: the store must be a git repo with
    # an identity at all, because every mutation commits itself
    # (concurrency.auto-commit-per-mutation) and a non-repo store fails the
    # commit and exits 1 -- the fixture gap IM-086/IM-087 closed elsewhere --
    # and the layout must be the REAL one, which is this script's whole
    # premise. Hand-rolling got the repo but not the seeded `.gitignore`, so
    # the store-wide commit lock's `.locks/` showed up as an untracked path
    # and failed check [5] against a scratch store while passing against the
    # operator store. Scaffolding with init-store means the two modes
    # exercise the same layout by construction.
    "$ISSUE_INIT_STORE" "$STORE" --non-interactive >/dev/null \
        || { echo "FAIL: issue admin init-store failed for $STORE"; exit 1; }
fi

echo "store: $STORE ($MODE_LABEL)"

PREFIX="SMOKE"
SMOKE_DIR="$STORE/$PREFIX"

if [ -e "$SMOKE_DIR" ]; then
    echo "FAIL: $SMOKE_DIR already exists; refusing to clobber (stale fixture from a prior crash?)"
    SMOKE_DIR=""   # do not let cleanup remove a directory this run did not create
    exit 1
fi

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

Fixture for the concurrent-edit smoke test. Removed on exit.

## Implementation Notes

EOF

export ISSUE_STORE_ROOT="$STORE"

ERR_FILE=$(mktemp)

# ---------------------------------------------------------------------------
# [1] issue edit against a held lock -> exit 3, file unchanged
# ---------------------------------------------------------------------------
echo
echo "[1] issue edit blocked by held lock ($MODE_LABEL)"

(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 2"
) &
HOLDER_PID=$!
sleep 0.3

RC_EDIT=0
"$ISSUE_EDIT" "$ISSUE_ID" --set priority=medium --no-reindex --lock-timeout 0.5 \
    >/dev/null 2>"$ERR_FILE" || RC_EDIT=$?
wait $HOLDER_PID
RC_HOLDER=$?

assert_eq "0" "$RC_HOLDER" "lock holder exits 0"
assert_eq "3" "$RC_EDIT" "issue edit blocked by held lock exits 3"

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
# [2] issue transition against a held lock -> exit 3, status unchanged
# ---------------------------------------------------------------------------
echo
echo "[2] issue transition blocked by held lock ($MODE_LABEL)"

(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 2"
) &
HOLDER_PID=$!
sleep 0.3

RC_TRANS=0
"$ISSUE_TRANSITION" "$ISSUE_ID" in-progress --no-reindex --lock-timeout 0.5 \
    >/dev/null 2>"$ERR_FILE" || RC_TRANS=$?
wait $HOLDER_PID

assert_eq "3" "$RC_TRANS" "issue transition blocked by held lock exits 3"

if grep -q "issue transition: error=concurrent-edit" "$ERR_FILE"; then
    pass "issue transition stderr names 'issue transition: error=concurrent-edit'"
else
    fail "issue transition stderr missing 'issue transition: error=concurrent-edit'"
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
# [3] Uncontested writes succeed end-to-end
# ---------------------------------------------------------------------------
echo
echo "[3] Uncontested writes succeed and persist"

RC=0
"$ISSUE_EDIT" "$ISSUE_ID" --set priority=medium --no-reindex >/dev/null 2>"$ERR_FILE" || RC=$?
assert_eq "0" "$RC" "uncontested issue edit exits 0"
if grep -q "^priority: medium$" "$ISSUE_FILE"; then
    pass "uncontested edit applied (priority: low -> medium)"
else
    fail "uncontested edit did not persist"
    cat "$ERR_FILE"
fi

RC=0
"$ISSUE_TRANSITION" "$ISSUE_ID" in-progress --no-reindex >/dev/null 2>"$ERR_FILE" || RC=$?
assert_eq "0" "$RC" "uncontested issue transition exits 0"
if grep -q "^status: in-progress$" "$ISSUE_FILE"; then
    pass "uncontested transition applied (status: open -> in-progress)"
else
    fail "uncontested transition did not persist"
    cat "$ERR_FILE"
fi

# ---------------------------------------------------------------------------
# [4] Real-process race -- two `issue edit` invocations on the same file
# ---------------------------------------------------------------------------
echo
echo "[4] Two real 'issue edit' processes race for the same lock"
#
# Hold the lock externally so one of the two real `issue edit` processes
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
    "$ISSUE_EDIT" "$ISSUE_ID" --set tags=alpha --no-reindex --lock-timeout 3 >/dev/null 2>&1 || rc=$?
    echo $rc > "$RC1_FILE"
) &
P1=$!

(
    rc=0
    "$ISSUE_EDIT" "$ISSUE_ID" --set tags=beta --no-reindex --lock-timeout 0.2 >/dev/null 2>&1 || rc=$?
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
# [5] No side-effect contamination of the store outside SMOKE/
# ---------------------------------------------------------------------------
echo
echo "[5] Store untouched outside SMOKE/"

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

# Store git status: SMOKE/ is the only pending path (it goes away on cleanup;
# the mutations' own auto-commits may already have committed it).
if command -v git >/dev/null 2>&1 && [ -d "$STORE/.git" ]; then
    STATUS_OUT=$(cd "$STORE" && git status --porcelain 2>/dev/null)
    NON_SMOKE_LINES=$(printf "%s\n" "$STATUS_OUT" | grep -v "^.. *$PREFIX/" | grep -v "^$" || true)
    if [ -z "$NON_SMOKE_LINES" ]; then
        pass "store git status: only SMOKE/ is pending (no other side effects)"
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
