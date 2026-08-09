#!/usr/bin/env bash
# im-090-repo-name-worktree.sh -- pre-merge verification for IM-090
#
# Bug: `new-issue` (and `init-issue-project`) used
#   basename $(git rev-parse --show-toplevel)
# to derive the repo identity for <store>/.repos lookup. In any linked
# worktree, --show-toplevel returns the worktree's own root, so the
# lookup failed with "repo '<worktree-basename>' is not registered ...".
#
# Fix: lib/repo.sh / lib/repo.py expose `repo::main_basename` (bash) and
# `repo.resolve_main_basename()` (python) which use
# `git rev-parse --git-common-dir` to land on the shared .git dir
# regardless of which worktree the call originates from.
#
# This script exercises:
#   T1: new-issue from primary checkout (regression control)
#   T2: new-issue from linked worktree (the bug)
#   T3: new-issue from sub-worktree (worktree-of-worktree)
#   T4: init-issue-project from linked worktree registers the main
#       repo's basename, not the worktree directory's name
#   T5: bash helper repo::main_basename returns the same name from
#       primary + linked + sub-worktree
#   T6: python helper repo.resolve_main_basename() returns the same
#
# Run from the project root:
#   tests/verification/im-090-repo-name-worktree.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
NEW_ISSUE="$(resolve_cli new-issue)"
INIT_PROJECT="$(resolve_cli init-issue-project)"
TEMPLATE_SRC="$REPO_ROOT/ISSUE-TEMPLATE.md"
LIB_REPO_SH="$REPO_ROOT/lib/repo.sh"
LIB_DIR="$REPO_ROOT/lib"

[[ -x "$NEW_ISSUE" ]] || { echo "FAIL: $NEW_ISSUE not executable"; exit 1; }
[[ -x "$INIT_PROJECT" ]] || { echo "FAIL: $INIT_PROJECT not executable"; exit 1; }
[[ -f "$TEMPLATE_SRC" ]] || { echo "FAIL: $TEMPLATE_SRC missing"; exit 1; }
[[ -f "$LIB_REPO_SH" ]] || { echo "FAIL: $LIB_REPO_SH missing"; exit 1; }

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t im-090-repo-name.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

# Build a fresh git repo with .issues/.prefix + .migrated-to-store
# committed, plus a baseline commit so `git worktree add` succeeds.
# Pre-registers <repo_name>:<prefix> in the store and initializes the
# store as a git repo (needed for auto-commits).
mk_registered_project() {
    local repo_name="$1" store_path="$2" prefix="$3"
    local path="$WORKDIR/$repo_name"
    mkdir -p "$path/.issues"
    git -C "$path" init -q
    git -C "$path" config user.email test@example.com
    git -C "$path" config user.name test
    echo "$prefix" > "$path/.issues/.prefix"
    echo "$store_path" > "$path/.issues/.migrated-to-store"
    echo "baseline" > "$path/README.md"
    git -C "$path" add .
    git -C "$path" commit -q -m "init"
    mkdir -p "$store_path"
    git -C "$store_path" init -q
    git -C "$store_path" config user.email test@example.com
    git -C "$store_path" config user.name test
    printf '%s:%s\n' "$repo_name" "$prefix" > "$store_path/.repos"
    cp "$TEMPLATE_SRC" "$store_path/ISSUE-TEMPLATE.md"
    git -C "$store_path" add .
    git -C "$store_path" commit -q -m "init store"
    echo "$path"
}

STORE="$WORKDIR/store"
PROJ="$(mk_registered_project sample-proj "$STORE" SP)"

# --- T1: primary checkout ---------------------------------------------
out=$( cd "$PROJ" && unset ISSUE_STORE_ROOT; "$NEW_ISSUE" "From primary" chore low 2>&1 )
rc=$?
# COVERS: substrate.store-root-resolution
if [[ $rc -eq 0 ]] && ls "$STORE/SP/"SP-001.md >/dev/null 2>&1; then
    pass "T1 primary checkout: SP-001.md created"
else
    fail "T1 primary checkout (rc=$rc): $out"
fi

# --- T2: linked worktree ----------------------------------------------
git -C "$PROJ" worktree add -q "$WORKDIR/wt1" -b wt1
out=$( cd "$WORKDIR/wt1" && unset ISSUE_STORE_ROOT; "$NEW_ISSUE" "From wt1" chore low 2>&1 )
rc=$?
# COVERS: registry.unregistered-fail
if [[ $rc -eq 0 ]] && ls "$STORE/SP/"SP-002.md >/dev/null 2>&1; then
    pass "T2 linked worktree: SP-002.md created (no 'wt1 is not registered' error)"
else
    fail "T2 linked worktree (rc=$rc): $out"
fi

# --- T3: sub-worktree -------------------------------------------------
git -C "$WORKDIR/wt1" worktree add -q "$WORKDIR/wt2" -b wt2
out=$( cd "$WORKDIR/wt2" && unset ISSUE_STORE_ROOT; "$NEW_ISSUE" "From wt2" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && ls "$STORE/SP/"SP-003.md >/dev/null 2>&1; then
    pass "T3 sub-worktree: SP-003.md created"
else
    fail "T3 sub-worktree (rc=$rc): $out"
fi

# --- T4: init-issue-project from a linked worktree --------------------
# Fresh repo with no .issues/.prefix yet. From a linked worktree, the
# registration should use the main repo's basename ("fresh-proj"), not
# the worktree directory's name ("fresh-wt").
FRESH_STORE="$WORKDIR/fresh-store"
mkdir -p "$FRESH_STORE"
git -C "$FRESH_STORE" init -q
git -C "$FRESH_STORE" config user.email test@example.com
git -C "$FRESH_STORE" config user.name test
git -C "$FRESH_STORE" commit -q --allow-empty -m "init store"

FRESH="$WORKDIR/fresh-proj"
mkdir -p "$FRESH"
git -C "$FRESH" init -q
git -C "$FRESH" config user.email test@example.com
git -C "$FRESH" config user.name test
echo "x" > "$FRESH/README.md"
git -C "$FRESH" add .
git -C "$FRESH" commit -q -m "init"
git -C "$FRESH" worktree add -q "$WORKDIR/fresh-wt" -b fresh-wt

out=$( cd "$WORKDIR/fresh-wt" && unset ISSUE_STORE_ROOT; \
       "$INIT_PROJECT" --store "$FRESH_STORE" --prefix FX --non-interactive 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && grep -qx "fresh-proj:FX" "$FRESH_STORE/.repos"; then
    pass "T4 init-issue-project from worktree: registry records main basename ('fresh-proj:FX')"
else
    fail "T4 init-issue-project from worktree (rc=$rc): out=$out; registry=$(cat "$FRESH_STORE/.repos" 2>&1)"
fi
# Negative assertion: the worktree's basename ('fresh-wt') must NOT be
# in the registry.
if grep -q "^fresh-wt:" "$FRESH_STORE/.repos" 2>/dev/null; then
    fail "T4 negative: 'fresh-wt' leaked into <store>/.repos"
else
    pass "T4 negative: worktree basename 'fresh-wt' is absent from registry"
fi

# --- T5: bash helper directly -----------------------------------------
helper_basename() {
    ( cd "$1" && bash -c "source $LIB_REPO_SH && repo::main_basename" 2>&1 )
}
b_primary=$(helper_basename "$PROJ")
b_wt1=$(helper_basename "$WORKDIR/wt1")
b_wt2=$(helper_basename "$WORKDIR/wt2")
if [[ "$b_primary" == "sample-proj" && "$b_wt1" == "sample-proj" && "$b_wt2" == "sample-proj" ]]; then
    pass "T5 bash helper returns 'sample-proj' from primary, wt1, and wt2"
else
    fail "T5 bash helper (primary=$b_primary wt1=$b_wt1 wt2=$b_wt2)"
fi

# --- T6: python helper directly ---------------------------------------
python_helper() {
    ( cd "$1" && python3 -c "import sys; sys.path.insert(0, '$LIB_DIR'); import repo; print(repo.resolve_main_basename())" 2>&1 )
}
p_primary=$(python_helper "$PROJ")
p_wt1=$(python_helper "$WORKDIR/wt1")
p_wt2=$(python_helper "$WORKDIR/wt2")
if [[ "$p_primary" == "sample-proj" && "$p_wt1" == "sample-proj" && "$p_wt2" == "sample-proj" ]]; then
    pass "T6 python helper returns 'sample-proj' from primary, wt1, and wt2"
else
    fail "T6 python helper (primary=$p_primary wt1=$p_wt1 wt2=$p_wt2)"
fi

# --- Summary ----------------------------------------------------------
if [[ $fail_count -eq 0 ]]; then
    echo "All checks passed."
    exit 0
else
    echo "$fail_count check(s) failed."
    exit 1
fi
