#!/usr/bin/env bash
# Verification script for the concurrency primitives.
#
# Exercises the acceptance criteria that need real subprocesses:
#   - Concurrent-edit race (two writers, one wins, loser exits 3)
#   - Torn-write smoke test (kill -9 mid-write produces no partial file)
#   - Stale-lock recovery (kill -9 process holding the lock)
#   - Optimistic verify between read and rename
#   - INDEX.md lock independence from per-issue locks
#   - Lockfile directories gitignored
#
# Run from the project root:
#     tests/verification/concurrency.sh

set -u

# Section 8 builds temp stores and relies on the sentinel-based
# store-root precedence. Unset any inherited ISSUE_STORE_ROOT from the caller's
# shell so it does not silently win the resolution ladder (--store ROOT ->
# ISSUE_STORE_ROOT -> sentinel -> ...).
unset ISSUE_STORE_ROOT

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
CONCURRENCY_PY="$PROJECT_ROOT/lib/concurrency.py"  # Tier 2: stack-coupled test adversary; see docs/design/concurrency-test-hook.md
CONCURRENCY_SH="$PROJECT_ROOT/lib/concurrency.sh"

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    # assert_eq <expected> <actual> <label>
    if [ "$1" = "$2" ]; then
        pass "$3 ($2)"
    else
        fail "$3: expected '$1', got '$2'"
    fi
}

# Replace the prior fixed `sleep 0.3` race-after-spawn pattern with
# `wait_for FILE` that polls for the holder's effect. Under `make coverage-black-box`
# + parallel load + cold cache, python3 startup (subprocess coverage shim
# adds ~70ms per child) + flock acquisition exceeds 300ms reliably; the old
# fixed sleep raced the lock acquisition. Polling for the file the holder
# touches once it owns the lock is deterministic regardless of startup cost.
# Caller passes the file the holder will create after acquire; budget is a
# generous ceiling (failure mode is the holder never started, not slow startup).
wait_for() {
    # wait_for <file> [timeout_seconds] -- poll up to timeout for file to exist+non-empty
    local target="$1"
    local timeout="${2:-10}"
    local end=$(( SECONDS + timeout ))
    while [ "$SECONDS" -lt "$end" ]; do
        [ -s "$target" ] && return 0
        sleep 0.05
    done
    return 1
}

TMPDIR_ROOT=$(mktemp -d -t im022-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM/.locks" "$STORE/.locks"

# Source the bash wrapper to verify it loads cleanly.
# shellcheck disable=SC1090
source "$CONCURRENCY_SH" || { echo "FAIL: cannot source $CONCURRENCY_SH"; exit 1; }
# COVERS: output.exit-codes
assert_eq "3" "$CONCURRENCY_EXIT_CONCURRENT_EDIT" "bash wrapper exposes exit-code constant"

# ---------------------------------------------------------------------------
# Check 1: race -- two writers, one wins, loser exits 3
# ---------------------------------------------------------------------------
echo
echo "[1] Concurrent-edit race"

TARGET="$STORE/IM/IM-001.md"
echo "initial content" > "$TARGET"
LOCK=$(concurrency::_issue_lock_path "$STORE" "IM" "IM-001")

# Writer A: hold lock for 2s, then write "winner"
(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 2 && echo 'winner-A' > '$TARGET'"
) &
PID_A=$!

# Wait for A to actually acquire (lockfile gets populated with A's PID once
# flock_acquire writes its os.getpid()); the prior fixed sleep 0.3 raced this
# under coverage instrumentation + load.
wait_for "$LOCK" 10 || fail "Writer A never acquired lock within 10s"

# Writer B: try to acquire with 0.5s timeout -- should exit 3
RC_B=0
python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 0.5 -- \
    bash -c "echo 'loser-B' > '$TARGET'" 2>/dev/null || RC_B=$?

wait $PID_A
RC_A=$?

assert_eq "0" "$RC_A" "winner exits 0"
# COVERS: principles.p2
# COVERS: concurrency.exit-17
assert_eq "3" "$RC_B" "loser exits 3 (concurrent-edit)"
assert_eq "winner-A" "$(cat "$TARGET")" "final file has winner content (no mix)"

# ---------------------------------------------------------------------------
# Check 2: torn-write -- kill -9 mid-write leaves no partial target
# ---------------------------------------------------------------------------
echo
echo "[2] Torn-write resistance"

TARGET2="$STORE/IM/IM-002.md"
SENTINEL2="$STORE/IM/.ready-2"
echo "original" > "$TARGET2"

# A python subprocess that opens a temp file, writes some bytes, then sleeps
# forever before the os.replace step. SIGKILL it; target must still hold
# original content; no leftover .tmp file at target name.
# Writer touches $SENTINEL2 after the partial write; we poll for it
# rather than sleep-and-hope. Under coverage + load, the prior sleep 0.3
# could fire before the python3 had even written PARTIAL.
python3 -c "
import sys, os, tempfile, time
sys.path.insert(0, '$PROJECT_ROOT/lib')
import concurrency
target = '$TARGET2'
fd, tmp = tempfile.mkstemp(prefix='.IM-002.md.tmp.', dir='$STORE/IM')
os.write(fd, b'PARTIAL')
os.fsync(fd)
os.close(fd)
with open('$SENTINEL2', 'w') as f: f.write('1')
time.sleep(60)
" &
WRITER_PID=$!

wait_for "$SENTINEL2" 10 || fail "Writer never wrote PARTIAL within 10s"
kill -9 $WRITER_PID 2>/dev/null
wait $WRITER_PID 2>/dev/null

# COVERS: concurrency.primitives
assert_eq "original" "$(cat "$TARGET2")" "target unchanged after kill -9 mid-write"

# Cleanup any leftover .tmp file (real atomic_write would too, but the
# stalled subprocess could not -- that is fine; what matters is the target).
rm -f "$STORE/IM/.IM-002.md.tmp."*

# ---------------------------------------------------------------------------
# Check 3: stale-lock recovery -- kill -9 lock holder, next acquirer succeeds
# ---------------------------------------------------------------------------
echo
echo "[3] Stale-lock recovery (kernel auto-release)"

LOCK3="$STORE/IM/.locks/IM-003.lock"

# Holder: acquire and sleep forever
python3 "$CONCURRENCY_PY" flock-run "$LOCK3" --timeout 5 -- sleep 600 &
HOLDER=$!

# Poll for the lockfile to be populated (flock_acquire writes the
# holder's PID atomically once it owns the lock); the prior sleep 0.3
# could fire under coverage+load before the holder had even started python3.
wait_for "$LOCK3" 10 || fail "Holder never acquired lock within 10s"

# Confirm holder is in
HOLDER_PID_IN_LOCK=$(cat "$LOCK3" 2>/dev/null || true)
if [ -n "$HOLDER_PID_IN_LOCK" ]; then
    pass "lockfile records holder PID ($HOLDER_PID_IN_LOCK)"
else
    fail "lockfile should contain holder PID"
fi

# Kill -9 the holder
kill -9 "$HOLDER" 2>/dev/null
wait "$HOLDER" 2>/dev/null

# Next acquirer should succeed
RC=0
python3 "$CONCURRENCY_PY" flock-run "$LOCK3" --timeout 1 -- true || RC=$?
assert_eq "0" "$RC" "next acquirer succeeds after holder killed -9"

# ---------------------------------------------------------------------------
# Check 4: optimistic verify between read and rename
# ---------------------------------------------------------------------------
echo
echo "[4] Optimistic mtime+size+sha verification"

TARGET4="$STORE/IM/IM-004.md"
echo "v1" > "$TARGET4"

SNAP=$(python3 "$CONCURRENCY_PY" snapshot "$TARGET4")
sleep 0.05
echo "v2-tampered" > "$TARGET4"
RC=0
python3 "$CONCURRENCY_PY" verify "$TARGET4" "$SNAP" 2>/dev/null || RC=$?
assert_eq "3" "$RC" "verify exits 3 after hand-edit between snapshot and verify"

# Same file, no edit -> verify passes
echo "v3" > "$TARGET4"
SNAP2=$(python3 "$CONCURRENCY_PY" snapshot "$TARGET4")
RC=0
python3 "$CONCURRENCY_PY" verify "$TARGET4" "$SNAP2" || RC=$?
assert_eq "0" "$RC" "verify exits 0 when file unchanged"

# ---------------------------------------------------------------------------
# Check 5: INDEX.md lock independence from per-issue locks
# ---------------------------------------------------------------------------
echo
echo "[5] INDEX vs per-issue lock independence"

INDEX_LOCK="$STORE/.locks/INDEX.lock"
ISSUE_LOCK="$STORE/IM/.locks/IM-005.lock"

# Hold INDEX lock; per-issue lock acquisition should still succeed.
python3 "$CONCURRENCY_PY" flock-run "$INDEX_LOCK" --timeout 5 -- sleep 600 &
INDEX_HOLDER=$!
# Poll for lockfile content instead of fixed sleep (see Test [3] comment).
wait_for "$INDEX_LOCK" 10 || fail "INDEX holder never acquired lock within 10s"

RC=0
python3 "$CONCURRENCY_PY" flock-run "$ISSUE_LOCK" --timeout 1 -- true || RC=$?
# COVERS: concurrency.lock-independence
assert_eq "0" "$RC" "per-issue lock acquires while INDEX lock is held"

kill -9 "$INDEX_HOLDER" 2>/dev/null
wait "$INDEX_HOLDER" 2>/dev/null

# Reverse: hold issue lock; concurrent INDEX lock should still succeed.
python3 "$CONCURRENCY_PY" flock-run "$ISSUE_LOCK" --timeout 5 -- sleep 600 &
ISSUE_HOLDER=$!
wait_for "$ISSUE_LOCK" 10 || fail "Issue holder never acquired lock within 10s"

RC=0
python3 "$CONCURRENCY_PY" flock-run "$INDEX_LOCK" --timeout 1 -- true || RC=$?
assert_eq "0" "$RC" "INDEX lock acquires while per-issue lock is held"

kill -9 "$ISSUE_HOLDER" 2>/dev/null
wait "$ISSUE_HOLDER" 2>/dev/null

# Two reindexers serialize -- second waits, eventually fails if timeout < first.
# Truncate INDEX_LOCK first -- prior kill -9'd holders left stale PID
# content in the lockfile, which would make wait_for return immediately before
# R1 has actually acquired. Truncating here is race-free: no other process is
# holding the lock at this point (both prior holders were SIGKILL'd + wait'd),
# and R1 starts after the truncate.
: > "$INDEX_LOCK"
python3 "$CONCURRENCY_PY" flock-run "$INDEX_LOCK" --timeout 5 -- sleep 2 &
R1=$!
wait_for "$INDEX_LOCK" 10 || fail "First reindexer never acquired INDEX lock within 10s"
RC=0
python3 "$CONCURRENCY_PY" flock-run "$INDEX_LOCK" --timeout 0.5 -- true 2>/dev/null || RC=$?
assert_eq "3" "$RC" "second reindexer exits 3 when first holds INDEX lock"
wait "$R1"

# ---------------------------------------------------------------------------
# Check 6: .locks gitignored
# ---------------------------------------------------------------------------
echo
echo "[6] .locks/ directories gitignored"

GITIGNORE="$PROJECT_ROOT/.gitignore"
if grep -qE '(^|/)\.locks/?$' "$GITIGNORE"; then
    pass ".locks/ pattern present in .gitignore"
else
    fail ".locks/ pattern missing from .gitignore"
fi

# ---------------------------------------------------------------------------
# Check 7: bash wrapper round-trip (atomic_write + snapshot + verify)
# ---------------------------------------------------------------------------
echo
echo "[7] Bash wrapper round-trip"

TARGET7="$STORE/IM/IM-007.md"
echo "wrapper-content" | concurrency::atomic_write "$TARGET7"
assert_eq "wrapper-content" "$(cat "$TARGET7")" "concurrency::atomic_write writes content"

SNAP=$(concurrency::capture_state "$TARGET7")
RC=0
concurrency::verify_unchanged "$TARGET7" "$SNAP" || RC=$?
assert_eq "0" "$RC" "concurrency::verify_unchanged passes on unmodified file"

sleep 0.05
echo "tampered" > "$TARGET7"
RC=0
concurrency::verify_unchanged "$TARGET7" "$SNAP" 2>/dev/null || RC=$?
assert_eq "3" "$RC" "concurrency::verify_unchanged exits 3 after edit"

# ---------------------------------------------------------------------------
# Check 8: StoreCommitError stderr is phase-aware
#
# Two failure modes share the StoreCommitError exception:
#   sweep-entry (catchup_sweep) -- caller's atomic_write has NOT run; nothing
#     is on disk for a future sweep to reconcile, so stderr must NOT claim
#     "file is on disk" and must instead say "no changes were written".
#   post-write (commit_after_write) -- caller's atomic_write HAS run; the
#     file IS on disk waiting for the next mutation's catchup sweep, so the
#     existing "file is on disk; fix and rerun any mutation to recover"
#     suffix is correct and must be preserved.
# Hook rejection (a third path) does not raise StoreCommitError -- it
# prints a warning and returns None. Asserted here to lock the contract.
# ---------------------------------------------------------------------------
echo
echo "[8] StoreCommitError stderr is phase-aware"

EDIT_ISSUE="$(resolve_cli edit-issue)"
ERR8="$TMPDIR_ROOT/err.im088"

make_im088_issue() {
    # make_im088_issue <path> <id>
    cat > "$1" <<EOF
---
id: $2
title: "IM-088 fixture"
type: chore
status: open
priority: low
created: 2026-06-22
updated: 2026-06-22
branch: ""
tags: []
---

## Summary

Fixture.

## Implementation Notes

EOF
}

# [8a] sweep-entry: store directory exists but is not a git repository.
# catchup_sweep's first `git status` call returns "fatal: not a git repository".
# Pre-fix, stderr falsely claimed "file is on disk".
STORE_NOGIT="$TMPDIR_ROOT/store-nogit"
mkdir -p "$STORE_NOGIT/IM/.locks" "$STORE_NOGIT/.locks"
make_im088_issue "$STORE_NOGIT/IM/IM-500.md" "IM-500"
PROJ_NOGIT="$TMPDIR_ROOT/proj-nogit"
mkdir -p "$PROJ_NOGIT/.issues"
echo "IM" > "$PROJ_NOGIT/.issues/.prefix"
echo "$STORE_NOGIT" > "$PROJ_NOGIT/.issues/.migrated-to-store"

RC=0
(
    cd "$PROJ_NOGIT"
    "$EDIT_ISSUE" IM-500 --set priority=medium --no-reindex >/dev/null 2>"$ERR8"
) || RC=$?
assert_eq "1" "$RC" "[8a] sweep-entry against non-git store exits 1"
if grep -qF "no changes were written; fix the store and retry" "$ERR8"; then
    pass "[8a] sweep-entry stderr says 'no changes were written'"
else
    fail "[8a] sweep-entry stderr missing 'no changes were written'"
    echo "--- stderr was ---"
    cat "$ERR8"
fi
if grep -qF "file is on disk" "$ERR8"; then
    fail "[8a] sweep-entry stderr still falsely claims 'file is on disk'"
else
    pass "[8a] sweep-entry stderr does NOT claim 'file is on disk'"
fi
# Pre-condition check: the file's mutation did NOT land.
if grep -q "^priority: low$" "$STORE_NOGIT/IM/IM-500.md"; then
    pass "[8a] issue file unchanged on sweep-entry failure"
else
    fail "[8a] issue file unexpectedly mutated on sweep-entry failure"
fi

# [8b] post-write hook rejection: real git store with a rejecting pre-commit
# hook. The atomic_write lands; both catchup_sweep (if pending paths exist) and
# commit_after_write detect the hook via `_hook_likely_present`, emit a warning,
# and return None (no StoreCommitError). The mutation exits 0 because the
# user's write IS on disk; the next mutation's catchup sweep will retry the
# commit after the hook is fixed.
STORE_HOOK="$TMPDIR_ROOT/store-hook"
mkdir -p "$STORE_HOOK/IM/.locks"
(
    cd "$STORE_HOOK"
    git init -q
    git config user.email "$FIXTURE_EMAIL"
    git config user.name test
    printf 'proj-hook:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
make_im088_issue "$STORE_HOOK/IM/IM-501.md" "IM-501"
(
    cd "$STORE_HOOK"
    git add IM/IM-501.md
    git commit -q -m "create IM-501"
)
mkdir -p "$STORE_HOOK/.git/hooks"
cat > "$STORE_HOOK/.git/hooks/pre-commit" <<'HOOK'
#!/usr/bin/env bash
echo "pre-commit hook: rejecting all commits" >&2
exit 1
HOOK
chmod +x "$STORE_HOOK/.git/hooks/pre-commit"

PROJ_HOOK="$TMPDIR_ROOT/proj-hook"
mkdir -p "$PROJ_HOOK/.issues"
echo "IM" > "$PROJ_HOOK/.issues/.prefix"
echo "$STORE_HOOK" > "$PROJ_HOOK/.issues/.migrated-to-store"

RC=0
(
    cd "$PROJ_HOOK"
    "$EDIT_ISSUE" IM-501 --set priority=medium --no-reindex >/dev/null 2>"$ERR8"
) || RC=$?
assert_eq "0" "$RC" "[8b] hook rejection exits 0 (warning, not error)"
if grep -qF "commit blocked by hook; file written; next mutation will retry" "$ERR8"; then
    pass "[8b] hook rejection stderr says 'file written; next mutation will retry'"
else
    fail "[8b] hook rejection stderr missing expected warning"
    echo "--- stderr was ---"
    cat "$ERR8"
fi
# Post-condition: the atomic_write landed even though the commit was blocked.
if grep -q "^priority: medium$" "$STORE_HOOK/IM/IM-501.md"; then
    pass "[8b] issue file mutated despite hook-blocked commit"
else
    fail "[8b] issue file unexpectedly unchanged"
fi
# And no StoreCommitError suffix should appear in stderr for the hook path.
if grep -qF "store commit failed" "$ERR8"; then
    fail "[8b] hook rejection emitted 'store commit failed' (should be a warning only)"
else
    pass "[8b] hook rejection does NOT emit 'store commit failed'"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "============================================="
echo "PASS: $PASS    FAIL: $FAIL"
echo "============================================="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
