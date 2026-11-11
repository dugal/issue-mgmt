#!/usr/bin/env bash
# im-045-init-issue-store.sh -- pre-merge verification for IM-045
#
# Exercises the green-field, existing-repo (idempotent re-run), and
# template-less-existing-store (lazy seed) paths against the dev checkout's
# scripts/init-issue-store and scripts/init-issue-project. Sets up temp
# directories, prints PASS:/FAIL: per check, cleans up, exits non-zero on any
# FAIL.
#
# Run from the project root:
#   tests/verification/im-045-init-issue-store.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
INIT_STORE="$(resolve_cli init-issue-store)"
INIT_PROJECT="$(resolve_cli init-issue-project)"
NEW_ISSUE="$(resolve_cli new-issue)"

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t im-045.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

mk_repo() {
    local name="$1"
    local path="$WORKDIR/$name"
    mkdir -p "$path"
    git -C "$path" init -q
    git -C "$path" config user.email "$FIXTURE_EMAIL"
    git -C "$path" config user.name test
    echo "$path"
}

# --- Test 1: --help ---
"$INIT_STORE" --help >/dev/null 2>&1 && pass "--help exits 0" || fail "--help exits 0"

# --- Test 2: --version ---
v=$("$INIT_STORE" --version 2>&1)
[[ "$v" =~ ^v[0-9] ]] && pass "--version prints v<digits> ($v)" \
    || fail "--version prints v<digits> (got: $v)"

# --- Test 3: missing path argument exits non-zero ---
out=$("$INIT_STORE" 2>&1); rc=$?
[[ $rc -ne 0 ]] && pass "no path argument fails" \
    || fail "no path argument fails (rc=$rc)"

# --- Test 4: green-field scaffold creates all files + initial commit ---
GREEN="$WORKDIR/green"
out=$("$INIT_STORE" "$GREEN" 2>&1); rc=$?
if [[ $rc -eq 0 ]]; then
    # COVERS: substrate.init-issue-store
    pass "green-field exits 0"
else
    fail "green-field exits 0 (rc=$rc out=$out)"
fi

for f in README.md LAYOUT.md .gitignore INDEX.md ISSUE-TEMPLATE.md; do
    [[ -f "$GREEN/$f" ]] && pass "green-field seeded $f" \
        || fail "green-field seeded $f"
done

[[ -d "$GREEN/.git" ]] && pass "green-field initialized .git" \
    || fail "green-field initialized .git"

branch=$(git -C "$GREEN" branch --show-current)
[[ "$branch" == "main" ]] && pass "green-field is on branch main" \
    || fail "green-field is on branch main (got: $branch)"

logmsg=$(git -C "$GREEN" log -1 --format=%s)
[[ "$logmsg" == "chore: scaffold issue store" ]] && pass "initial commit message" \
    || fail "initial commit message (got: $logmsg)"

# --- Test 5: .gitignore content ---
grep -qx ".locks/" "$GREEN/.gitignore" && pass ".gitignore excludes .locks/" \
    || fail ".gitignore excludes .locks/"

# --- Test 6: README.md carries P1/P2 inline ---
grep -q "Distribution model (P1)" "$GREEN/README.md" \
    && pass "README has P1 inline" || fail "README has P1 inline"
grep -q "Concurrency baseline (P2)" "$GREEN/README.md" \
    && pass "README has P2 inline" || fail "README has P2 inline"
grep -q "Concurrency baseline (P2)" "$GREEN/LAYOUT.md" \
    && pass "LAYOUT has P2 inline" || fail "LAYOUT has P2 inline"

# --- Test 7: ISSUE-TEMPLATE.md byte-identical to canonical source ---
if cmp -s "$GREEN/ISSUE-TEMPLATE.md" "$REPO_ROOT/ISSUE-TEMPLATE.md"; then
    pass "ISSUE-TEMPLATE.md byte-identical to canonical"
else
    fail "ISSUE-TEMPLATE.md byte-identical to canonical"
fi

# --- Test 8: idempotent re-run never overwrites ---
sha_before=$(git -C "$GREEN" rev-parse HEAD)
out=$("$INIT_STORE" "$GREEN" 2>&1); rc=$?
sha_after=$(git -C "$GREEN" rev-parse HEAD)
if [[ $rc -eq 0 ]] \
   && [[ "$out" == *"present: README.md"* ]] \
   && [[ "$sha_before" == "$sha_after" ]]; then
    pass "idempotent re-run reports present, no new commit"
else
    fail "idempotent re-run (rc=$rc sha_changed=$([[ $sha_before == $sha_after ]] && echo no || echo yes))"
fi

# --- Test 9: refuses non-empty non-git path ---
DIRTY="$WORKDIR/dirty"
mkdir -p "$DIRTY"
echo "preexisting" > "$DIRTY/junk.txt"
out=$("$INIT_STORE" "$DIRTY" 2>&1); rc=$?
[[ $rc -ne 0 ]] && [[ "$out" == *"not a git repo and is not empty"* ]] \
    && pass "refuses non-empty non-git path" \
    || fail "refuses non-empty non-git path (rc=$rc out=$out)"

# --- Test 10: accepts empty existing dir ---
EMPTY="$WORKDIR/empty"
mkdir -p "$EMPTY"
"$INIT_STORE" "$EMPTY" >/dev/null 2>&1 && [[ -f "$EMPTY/README.md" ]] \
    && pass "scaffolds into pre-existing empty dir" \
    || fail "scaffolds into pre-existing empty dir"

# --- Test 11: accepts pre-existing git repo (seeds missing files) ---
PREGIT="$WORKDIR/pregit"
mkdir -p "$PREGIT"
git -C "$PREGIT" init -q -b main
git -C "$PREGIT" config user.email "$FIXTURE_EMAIL"
git -C "$PREGIT" config user.name test
echo "preexisting" > "$PREGIT/something.txt"
git -C "$PREGIT" add something.txt
git -C "$PREGIT" commit -q -m "init"
out=$("$INIT_STORE" "$PREGIT" 2>&1); rc=$?
if [[ $rc -eq 0 ]] && [[ -f "$PREGIT/README.md" ]] \
   && [[ -f "$PREGIT/something.txt" ]]; then
    pass "scaffolds into pre-existing git repo, preserves existing content"
else
    fail "scaffolds into pre-existing git repo (rc=$rc)"
fi

# --- Test 12: end-to-end -- green store + init-issue-project + new-issue ---
E2E_STORE="$WORKDIR/e2e-store"
E2E_REPO=$(mk_repo e2e-repo)
"$INIT_STORE" "$E2E_STORE" >/dev/null 2>&1
# init-issue-store git-inits the store but does not set an identity; the
# subsequent auto-commits (init-issue-project, new-issue) need one.
# Set it repo-locally so the test is hermetic on a box with no global git
# identity (matches mk_repo / Test 13).
git -C "$E2E_STORE" config user.email "$FIXTURE_EMAIL"
git -C "$E2E_STORE" config user.name test
( cd "$E2E_REPO" && ISSUE_STORE_ROOT="$E2E_STORE" \
    "$INIT_PROJECT" --repo e2e-repo --prefix E2E --non-interactive ) >/dev/null 2>&1
e2e_rc=$?
( cd "$E2E_REPO" && ISSUE_STORE_ROOT="$E2E_STORE" \
    "$NEW_ISSUE" "smoke test" chore low ) >/dev/null 2>&1
new_rc=$?
if [[ $e2e_rc -eq 0 ]] && [[ $new_rc -eq 0 ]] \
   && [[ -f "$E2E_STORE/E2E/E2E-001.md" ]]; then
    pass "green store -> init-issue-project -> new-issue chains end-to-end"
else
    fail "end-to-end (init-issue-project rc=$e2e_rc, new-issue rc=$new_rc)"
fi

# --- Test 13: lazy template seed in init-issue-project ---
# Store must be a git repo before init-issue-project (the
# auto-commit) runs against it. The test still simulates the pre-IM-045
# state (no ISSUE-TEMPLATE.md) -- only the git-init precondition is added.
LAZY_STORE="$WORKDIR/lazy-store"
mkdir -p "$LAZY_STORE"  # bare dir, no ISSUE-TEMPLATE.md
(
    cd "$LAZY_STORE"
    git init -q
    git config user.email "$FIXTURE_EMAIL"
    git config user.name test
    git commit -q --allow-empty -m "init store"
)
LAZY_REPO=$(mk_repo lazy-repo)
out=$( cd "$LAZY_REPO" && ISSUE_STORE_ROOT="$LAZY_STORE" \
    "$INIT_PROJECT" --repo lazy-repo --prefix LAZY --non-interactive 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] \
   && [[ -f "$LAZY_STORE/ISSUE-TEMPLATE.md" ]] \
   && [[ "$out" == *"seeded"*"ISSUE-TEMPLATE.md"* ]]; then
    # COVERS: substrate.template-lazy-seed
    pass "init-issue-project lazy-seeds ISSUE-TEMPLATE.md with notice"
else
    fail "lazy-seed (rc=$rc out=$out)"
fi

# Verify byte-identical seed
if cmp -s "$LAZY_STORE/ISSUE-TEMPLATE.md" "$REPO_ROOT/ISSUE-TEMPLATE.md"; then
    pass "lazy-seeded template is byte-identical to canonical"
else
    fail "lazy-seeded template is byte-identical to canonical"
fi

# --- Test 14: second init-issue-project against same store is silent ---
LAZY_REPO2=$(mk_repo lazy-repo2)
out=$( cd "$LAZY_REPO2" && ISSUE_STORE_ROOT="$LAZY_STORE" \
    "$INIT_PROJECT" --repo lazy-repo2 --prefix LZ2 --non-interactive 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && [[ "$out" != *"seeded"*"ISSUE-TEMPLATE.md"* ]]; then
    pass "init-issue-project silent on second call (template present)"
else
    fail "init-issue-project silent on second call (rc=$rc out=$out)"
fi

# =====================================================================
# Live-store convergence (idempotent re-run on a populated store)
# =====================================================================

# Hard-isolate from the caller's real store.
unset ISSUE_STORE_ROOT

# --- Test 15: re-run on a populated live store exits 0 unmodified ---
LIVE="$WORKDIR/live-store"
"$INIT_STORE" "$LIVE" >/dev/null 2>&1
git -C "$LIVE" config user.email "$FIXTURE_EMAIL"
git -C "$LIVE" config user.name test
mkdir -p "$LIVE/LV"
cat > "$LIVE/LV/LV-001.md" <<'EOF'
---
id: LV-001
title: "Live store fixture"
type: chore
status: open
priority: low
created: 2026-07-08
updated: 2026-07-08
branch: ""
tags: []
---

## Summary

Populated live-store fixture issue.
EOF
git -C "$LIVE" add LV/LV-001.md
git -C "$LIVE" commit -q -m "seed LV-001"
sha_before=$(git -C "$LIVE" rev-parse HEAD)
issue_sha_before=$(shasum "$LIVE/LV/LV-001.md" | cut -d' ' -f1)
out=$("$INIT_STORE" "$LIVE" 2>&1); rc=$?
sha_after=$(git -C "$LIVE" rev-parse HEAD)
issue_sha_after=$(shasum "$LIVE/LV/LV-001.md" | cut -d' ' -f1)
if [[ $rc -eq 0 ]] && [[ "$sha_before" == "$sha_after" ]] \
   && [[ "$issue_sha_before" == "$issue_sha_after" ]] \
   && [[ "$out" == *"present: README.md"* ]]; then
    # COVERS: substrate.init-issue-store
    pass "re-run on populated live store exits 0 unmodified"
else
    fail "re-run on populated live store (rc=$rc sha_changed=$([[ $sha_before == $sha_after ]] && echo no || echo yes))"
fi

# --- Summary ---
echo "---"
if [[ $fail_count -eq 0 ]]; then
    echo "ALL CHECKS PASSED"
    exit 0
else
    echo "$fail_count CHECK(S) FAILED"
    exit 1
fi
