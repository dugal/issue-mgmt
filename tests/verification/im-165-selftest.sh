#!/usr/bin/env bash
# im-165-selftest.sh -- verify the issue-mgmt-selftest post-install smoke (IM-165).
#
# Exercises the selftest binary itself:
#   A. --help / --version / unknown-arg contracts
#   B. full run passes (frontmatter-native ready-set transition asserted)
#   C. bd-independence: identical pass with a bogus bd hint, no skips
#   D. isolation: a decoy operator store handed in via ISSUE_STORE_ROOT is
#      ignored and left byte-identical (the load-bearing safety property)
#   E. idempotent re-run + no leftover temp directories
#
# Run from the project root:
#   tests/verification/im-165-selftest.sh

set -u

. "$(dirname "$0")/lib.sh"

SELFTEST=$(resolve_cli issue-mgmt-selftest) || exit 1

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL + 1)); }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/im165-verify.XXXXXX")
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo "[A] flag contracts"

# COVERS: dist.selftest
if "$SELFTEST" --help | grep -q "throwaway"; then
    pass "--help exits 0 and documents the throwaway-store isolation"
else
    fail "--help missing or does not document isolation"
fi

if "$SELFTEST" --version | grep -q "issue-mgmt-selftest"; then
    pass "--version prints the tool name"
else
    fail "--version output wrong"
fi

"$SELFTEST" --bogus-flag >/dev/null 2>&1
if [ $? -eq 2 ]; then
    pass "unknown argument exits 2"
else
    fail "unknown argument did not exit 2"
fi

echo
echo "[B] full run"

FULL_OUT="$TMP/full.out"
if "$SELFTEST" > "$FULL_OUT" 2>&1; then
    # COVERS: dist.selftest
    pass "selftest exits 0"
else
    fail "selftest exited non-zero:"
    sed 's/^/    /' "$FULL_OUT"
fi
if grep -q "^issue-mgmt-selftest: PASS" "$FULL_OUT"; then
    pass "summary PASS line printed"
else
    fail "no summary PASS line"
fi
if grep -q "issue-ready excludes blocked" "$FULL_OUT" \
   && grep -q "issue-ready includes" "$FULL_OUT"; then
    pass "frontmatter-native ready-set transition asserted (before and after)"
else
    fail "ready-set checks not asserted"
fi

echo
echo "[C] bd-independence (no bd-gated steps)"

SKIP_OUT="$TMP/skip.out"
# The selftest computes readiness from frontmatter; a bogus bd hint must
# change nothing -- same pass, no skipped steps.
# COVERS: dist.selftest
if ISSUE_MGMT_BD_BIN=/nonexistent-bd "$SELFTEST" > "$SKIP_OUT" 2>&1; then
    pass "selftest exits 0 regardless of any bd binary hint"
else
    fail "selftest failed with a bogus bd hint:"
    sed 's/^/    /' "$SKIP_OUT"
fi
if grep -q "^skip:" "$SKIP_OUT"; then
    fail "selftest reported skip: lines -- no step should be bd-gated"
else
    pass "no skipped steps (every check is frontmatter-native)"
fi
if grep -q "issue-ready excludes blocked" "$SKIP_OUT" \
   && grep -q "transition-issue walked" "$SKIP_OUT" \
   && grep -q "search-issues found" "$SKIP_OUT"; then
    pass "ready-set and markdown-native checks all ran"
else
    fail "expected checks missing from the run"
fi

echo
echo "[D] isolation: decoy operator store is ignored and untouched"

DECOY="$TMP/decoy-store"
mkdir -p "$DECOY"
git -C "$DECOY" init -q
echo "operator data -- must survive untouched" > "$DECOY/marker.md"
git -C "$DECOY" add -A
git -C "$DECOY" -c user.name=decoy -c user.email=decoy@example.invalid \
    commit -qm "decoy baseline"
HEAD_BEFORE=$(git -C "$DECOY" rev-parse HEAD)

# COVERS: dist.selftest
if ISSUE_STORE_ROOT="$DECOY" "$SELFTEST" > "$TMP/decoy.out" 2>&1; then
    pass "selftest passes with a decoy ISSUE_STORE_ROOT inherited"
else
    fail "selftest failed under decoy ISSUE_STORE_ROOT:"
    sed 's/^/    /' "$TMP/decoy.out"
fi
HEAD_AFTER=$(git -C "$DECOY" rev-parse HEAD)
if [ "$HEAD_BEFORE" = "$HEAD_AFTER" ] \
   && [ -z "$(git -C "$DECOY" status --porcelain)" ] \
   && [ "$(ls "$DECOY")" = "marker.md" ]; then
    pass "decoy store byte-identical after the run (no commits, no files, no dirt)"
else
    fail "decoy store was modified -- isolation property violated"
fi

echo
echo "[E] idempotent re-run + no temp-dir residue"

before_count=$(ls "${TMPDIR:-/tmp}" 2>/dev/null | grep -c "^issue-mgmt-selftest\.")
"$SELFTEST" >/dev/null 2>&1; rc1=$?
"$SELFTEST" >/dev/null 2>&1; rc2=$?
after_count=$(ls "${TMPDIR:-/tmp}" 2>/dev/null | grep -c "^issue-mgmt-selftest\.")
# COVERS: dist.selftest
if [ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ]; then
    pass "back-to-back runs both exit 0 (idempotent)"
else
    fail "re-run not idempotent (rc1=$rc1 rc2=$rc2)"
fi
if [ "$after_count" -le "$before_count" ]; then
    pass "no issue-mgmt-selftest temp directories left behind"
else
    fail "temp directories leaked (before=$before_count after=$after_count)"
fi

echo
echo "[F] git-absent prerequisite"

# Shim PATH carrying only what the selftest needs before its git check
# (bash for the shebang, python3 for sibling resolution) -- git absent.
SHIM="$TMP/shim"
mkdir -p "$SHIM"
ln -s "$(command -v python3)" "$SHIM/python3"
ln -s "$(command -v bash)" "$SHIM/bash"
GIT_OUT="$TMP/nogit.out"
PATH="$SHIM" "$SELFTEST" > "$GIT_OUT" 2>&1
rc=$?
# COVERS: dist.selftest
if [ "$rc" -eq 1 ] && grep -q "^FAIL: git not found on PATH" "$GIT_OUT"; then
    pass "git absent: fails by name with exit 1 before any work"
else
    fail "git absent: expected named FAIL exit 1 (rc=$rc):"
    sed 's/^/    /' "$GIT_OUT"
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
