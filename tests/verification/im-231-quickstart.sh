#!/usr/bin/env bash
# Verification script for IM-231: the public README Quickstart works verbatim,
# and the project git-repo precondition is enforced with an actionable error.
#
# Motivation: the shipped selftest git-inits its temp project internally, so it
# never exercised the DOCUMENTED onboarding sequence -- which omitted `git init`
# and failed for a new project. This script runs the quickstart as a new user
# would, from scratch, in a clean environment, so doc/CLI drift fails the suite.
#
# Sections:
#   A. Quickstart happy path -- init-store -> git init project -> init-project
#      -> create -> ready -> show, exactly as the README documents it.
#   B. Precondition guard -- init-project OUTSIDE a git repo exits non-zero with
#      an actionable message that names the fix (`git init`) and the reason.
#
# Run from the project root:
#     tests/verification/im-231-quickstart.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"

INIT_STORE="$(resolve_cli init-issue-store)"
INIT_PROJECT="$(resolve_cli init-issue-project)"
NEW_ISSUE="$(resolve_cli new-issue)"
ISSUE_READY="$(resolve_cli issue-ready)"
ISSUE_SHOW="$(resolve_cli issue-show)"
ISSUE_SEARCH="$(resolve_cli search-issues)"

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
assert_eq() {
    if [ "$1" = "$2" ]; then pass "$3 ($2)"; else
        fail "$3: expected '$1', got '$2'"; fi
}
assert_contains() {
    if echo "$2" | grep -qF -- "$1"; then pass "$3 (contains '$1')"; else
        fail "$3: '$1' not found in: $2"; fi
}

WORK=$(mktemp -d -t im231-verify.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
export PATH="$ISSUE_MGMT_BIN:$PATH"
unset ISSUE_STORE_ROOT

# Clean, isolated HOME so no inherited config/store leaks in; give git an
# identity there so store-mutation commits succeed.
export HOME="$WORK/home"
mkdir -p "$HOME"
git config --global user.name "im231-verify" >/dev/null 2>&1
git config --global user.email "im231@issue-mgmt.invalid" >/dev/null 2>&1
git config --global init.defaultBranch main >/dev/null 2>&1

STORE="$WORK/issues-store"
PROJ="$WORK/myproject"

# ---------------------------------------------------------------------------
echo "[A] README Quickstart, verbatim, from scratch"
# ---------------------------------------------------------------------------

# Step 1: scaffold the store.
RC=0; "$INIT_STORE" "$STORE" --non-interactive >/dev/null 2>&1 || RC=$?
# COVERS: substrate.init-issue-store
assert_eq "0" "$RC" "init-store scaffolds a fresh store"

# Step 2 (as documented): a project MUST be a git repo -- git init a new one,
# then register it.
mkdir -p "$PROJ"
( cd "$PROJ" && git init -q )
RC=0
( cd "$PROJ" && "$INIT_PROJECT" --store "$STORE" --prefix MYP --non-interactive ) \
    >/dev/null 2>&1 || RC=$?
# COVERS: substrate.init-project-sentinel
assert_eq "0" "$RC" "init-project registers a freshly git-init'd project"
# COVERS: substrate.init-project-sentinel
[ -f "$PROJ/.issues/.prefix" ] \
    && pass "init-project wrote .issues/.prefix" \
    || fail "init-project did not write .issues/.prefix"
# COVERS: registry.basename-prefix-unique
if [ -f "$STORE/.repos" ] && grep -q "myproject:MYP" "$STORE/.repos"; then
    pass "registry records myproject:MYP"
else
    fail "registry missing myproject:MYP"
fi

# Step 3: create an issue.
CREATE_OUT=$( cd "$PROJ" && "$NEW_ISSUE" "add --dry-run flag" feature medium 2>&1 )
NEWID=$(echo "$CREATE_OUT" | grep -oE 'MYP-[0-9]+' | head -1)
assert_eq "MYP-001" "$NEWID" "create files MYP-001 in the store"
[ -f "$STORE/MYP/MYP-001.md" ] \
    && pass "MYP-001.md exists under the store" \
    || fail "MYP-001.md not found under the store"

# Step 5: ready set (open + no blockers) lists it.
READY_OUT=$( cd "$PROJ" && "$ISSUE_READY" --repo MYP 2>/dev/null )
assert_contains "MYP-001" "$READY_OUT" "issue ready --repo MYP lists MYP-001"

# Step 6: show + search. Search matches the title, so the quickstart's own
# search example finds MYP-001 by title even though its body is empty.
SHOW_OUT=$( cd "$PROJ" && "$ISSUE_SHOW" MYP-001 2>/dev/null )
assert_contains "add --dry-run flag" "$SHOW_OUT" "issue show MYP-001 renders the issue"

# COVERS: cli.search-modes
SEARCH_OUT=$( cd "$PROJ" && "$ISSUE_SEARCH" "dry-run" 2>/dev/null )
SEARCH_RC=$?
assert_contains "MYP-001" "$SEARCH_OUT" "issue search 'dry-run' matches by title (empty body)"
assert_eq "0" "$SEARCH_RC" "issue search with a match exits 0"

# ---------------------------------------------------------------------------
echo
echo "[B] Precondition guard: init-project outside a git repo"
# ---------------------------------------------------------------------------
NONGIT="$WORK/not-a-repo"
mkdir -p "$NONGIT"
RC=0
ERR=$( cd "$NONGIT" && "$INIT_PROJECT" --store "$STORE" --prefix ZZZ --non-interactive 2>&1 ) \
    || RC=$?
[ "$RC" -ne 0 ] \
    && pass "init-project outside a git repo exits non-zero ($RC)" \
    || fail "init-project outside a git repo unexpectedly exited 0"
assert_contains "git init" "$ERR" "error names the fix (git init)"
assert_contains "git repo" "$ERR" "error names the reason (must be a git repo)"

# ---------------------------------------------------------------------------
echo
echo "==================================================================="
echo "IM-231 quickstart: $PASS passed, $FAIL failed"
echo "==================================================================="
[ "$FAIL" -eq 0 ]
