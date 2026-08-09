#!/usr/bin/env bash
# im-057-auto-commit.sh -- pre-merge verification for IM-057.
#
# Exercises the auto-commit-per-mutation + self-healing catchup sweep
# machinery added by IM-057. The five scenarios mirror the acceptance
# criteria in .issues/IM-057.md:
#
#   1. Kill-mid-mutation recovery: a write that completed but never got
#      committed (process killed between write and commit) is reconciled
#      by the next mutation as a "catchup: ..." commit before that
#      mutation's own commit lands.
#   2. Hook-rejection warning: when a pre-commit hook rejects the
#      commit, the tool exits 0 with a stderr warning, the file stays
#      on disk, and the next mutation (after the hook is removed)
#      catches up cleanly.
#   3. Two-concurrent-prefix serialization: two background mutations on
#      different prefixes serialize on the store commit lock and produce
#      two clean sequential commits, neither racing on .git/index.lock.
#   4. migrate-to-store two-commit preservation: migrate still produces
#      exactly two commits per project (migration + .migrations).
#   5. issue-snapshot no-op on clean: standalone tool exits 0 with no
#      commit when the store is already clean.
#
# Run from the project root:
#   tests/verification/im-057-auto-commit.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
NEW_ISSUE="$(resolve_cli new-issue)"
INIT_ISSUE_PROJECT="$(resolve_cli init-issue-project)"
MIGRATE_TO_STORE="$(resolve_cli migrate-to-store)"
ISSUE_SNAPSHOT="$(resolve_cli issue-snapshot)"

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t im-057-auto-commit.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

# Isolate user config / env so the tests do not consult ~/.config or
# inherit a stray ISSUE_STORE_ROOT.
export HOME="$WORKDIR/home"
mkdir -p "$HOME"
unset ISSUE_STORE_ROOT
unset ISSUE_MGMT_NO_AUTOCOMMIT

# Inline git identity so the commits the tools drive can find an author.
export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="t@t"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="t@t"

mk_store() {
    local dir="$WORKDIR/$1"
    mkdir -p "$dir"
    git init -q -b main "$dir" 1>&2
    git -C "$dir" commit -q --allow-empty -m "store init" 1>&2
    echo "$dir"
}

mk_project() {
    # mk_project <name> <prefix> <store>  -- prints project dir on stdout.
    # All diagnostics go to stderr so callers can capture the dir path
    # cleanly via $(mk_project ...). init-issue-project requires that
    # cwd be inside the target project's git repo (calls
    # git rev-parse --show-toplevel).
    local name="$1" prefix="$2" store="$3"
    local dir="$WORKDIR/$name"
    mkdir -p "$dir/.issues"
    git init -q -b main "$dir" 1>&2
    git -C "$dir" commit -q --allow-empty -m "project init" 1>&2
    ( cd "$dir" && "$INIT_ISSUE_PROJECT" \
        --repo "$name" --prefix "$prefix" \
        --store "$store" --non-interactive ) 1>&2 \
        || { echo "mk_project: init-issue-project failed for $name:$prefix" 1>&2; exit 1; }
    echo "$store" > "$dir/.issues/.migrated-to-store"
    echo "$dir"
}

# ---------------------------------------------------------------------
# Scenario 1: kill-mid-mutation recovery
# ---------------------------------------------------------------------
echo "--- Scenario 1: kill-mid-mutation recovery ---"
S1="$(mk_store s1)"
P1="$(mk_project p1 PA "$S1")"
( cd "$P1" && "$NEW_ISSUE" "first" feature low >/dev/null )

# Simulate a killed mutation: drop a tracked file into the store
# without committing. Models a write that landed on disk before the
# commit step ran (process killed mid-mutation).
echo "orphan" > "$S1/PA/PA-099.md"
status_dirty=$(git -C "$S1" status --porcelain -- PA/PA-099.md | head -c1)
[[ -n "$status_dirty" ]] \
    && pass "orphan write left store working tree dirty" \
    || fail "orphan write did not appear in git status"

# Next mutation should sweep the orphan as a catchup commit, then
# land its own commit. The orphan's PA-099 ID intentionally bumps
# next_id() past 099, so the new file lands at PA-100 (or higher);
# the exact number is incidental -- the ordering invariant is what
# matters.
( cd "$P1" && "$NEW_ISSUE" "second" feature low >/dev/null )
top_subject=$(git -C "$S1" log --format='%s' -1)
prev_subject=$(git -C "$S1" log --format='%s' -1 HEAD~1)
# COVERS: concurrency.catchup-sweep
# COVERS: concurrency.auto-commit-per-mutation
[[ "$top_subject" == create\ PA-* \
    && "$prev_subject" == "catchup: reconcile pending store changes" ]] \
    && pass "catchup commit precedes the next mutation's own commit" \
    || fail "expected [catchup, create PA-*] at HEAD~1..HEAD (got prev=$prev_subject top=$top_subject)"

# After the catchup, the orphan should be in git history and not
# dirty in the working tree.
git -C "$S1" log --name-only --format=  | grep -q "^PA/PA-099.md$" \
    && pass "orphan PA-099.md appears in git history" \
    || fail "orphan PA-099.md not in git history"
git -C "$S1" status --porcelain -- PA/PA-099.md | grep -q . \
    && fail "orphan PA-099.md still dirty after catchup" \
    || pass "orphan PA-099.md no longer dirty"

# ---------------------------------------------------------------------
# Scenario 2: hook-rejection warning
# ---------------------------------------------------------------------
echo "--- Scenario 2: hook-rejection warning ---"
S2="$(mk_store s2)"
P2="$(mk_project p2 PB "$S2")"

# Install a pre-commit hook that always rejects.
mkdir -p "$S2/.git/hooks"
cat > "$S2/.git/hooks/pre-commit" <<'EOF'
#!/bin/sh
echo "hook says no" >&2
exit 1
EOF
chmod +x "$S2/.git/hooks/pre-commit"

( cd "$P2" && \
  "$NEW_ISSUE" "hooked" feature low \
    >"$WORKDIR/p2.out" 2>"$WORKDIR/p2.err" )
rc=$?
[[ $rc -eq 0 ]] \
    && pass "new-issue exits 0 under hook rejection (got rc=$rc)" \
    || fail "new-issue exit under hook rejection (rc=$rc)"

# COVERS: concurrency.commit-failure-semantics
grep -q "commit blocked by hook" "$WORKDIR/p2.err" \
    && pass "stderr contains hook-rejection warning" \
    || fail "stderr missing hook-rejection warning"

# File should still be on disk.
[[ -f "$S2/PB/PB-001.md" ]] \
    && pass "issue file landed on disk despite hook rejection" \
    || fail "issue file missing after hook-rejected mutation"

# No commit landed for it.
git -C "$S2" log --oneline | grep -q "create PB-001" \
    && fail "unexpected create PB-001 commit landed" \
    || pass "no commit landed for hook-rejected mutation"

# Remove the hook, then run another mutation. The catchup sweep should
# pick up PB-001 as a catchup commit.
rm "$S2/.git/hooks/pre-commit"
( cd "$P2" && "$NEW_ISSUE" "after fix" feature low >/dev/null )
git -C "$S2" log --format='%s' | grep -q "^catchup: reconcile pending store changes$" \
    && pass "post-fix mutation produced a catchup commit" \
    || fail "post-fix mutation did not produce a catchup commit"

# ---------------------------------------------------------------------
# Scenario 3: two-concurrent-prefix serialization
# ---------------------------------------------------------------------
echo "--- Scenario 3: two-concurrent-prefix serialization ---"
S3="$(mk_store s3)"
P3A="$(mk_project p3a PC "$S3")"
P3B="$(mk_project p3b PD "$S3")"

# Run two new-issue invocations in parallel across different prefixes.
# They share the store commit lock, so they must serialize cleanly.
(
    cd "$P3A" && "$NEW_ISSUE" "concurrent a" feature low \
        >"$WORKDIR/p3a.out" 2>"$WORKDIR/p3a.err"
) &
PID_A=$!
(
    cd "$P3B" && "$NEW_ISSUE" "concurrent b" feature low \
        >"$WORKDIR/p3b.out" 2>"$WORKDIR/p3b.err"
) &
PID_B=$!
wait $PID_A; rc_a=$?
wait $PID_B; rc_b=$?
[[ $rc_a -eq 0 && $rc_b -eq 0 ]] \
    && pass "both concurrent mutations exited 0 (rc_a=$rc_a, rc_b=$rc_b)" \
    || fail "concurrent mutation exit (rc_a=$rc_a, rc_b=$rc_b)"

# Both prefix files exist.
[[ -f "$S3/PC/PC-001.md" && -f "$S3/PD/PD-001.md" ]] \
    && pass "both PC-001 and PD-001 landed in the store" \
    || fail "one or both concurrent issue files missing"

# Both commits exist in git history, in some order.
git -C "$S3" log --oneline | grep -q "create PC-001" \
    && git -C "$S3" log --oneline | grep -q "create PD-001" \
    && pass "both create-commits present in git history" \
    || fail "missing one of create PC-001 or create PD-001 in history"

# Neither stderr should mention .git/index.lock contention.
if grep -q "index.lock" "$WORKDIR/p3a.err" "$WORKDIR/p3b.err"; then
    fail "stderr contains .git/index.lock contention"
else
    pass "no .git/index.lock contention observed"
fi

# ---------------------------------------------------------------------
# Scenario 4: migrate-to-store two-commit preservation
# ---------------------------------------------------------------------
echo "--- Scenario 4: migrate-to-store two-commit preservation ---"
S4="$(mk_store s4)"
M4="$WORKDIR/m4"
mkdir -p "$M4/.issues"
git init -q -b main "$M4"
git -C "$M4" commit -q --allow-empty -m init
echo "MM" > "$M4/.issues/.prefix"
cat > "$M4/.issues/MM-001.md" <<'EOF'
---
id: MM-001
title: "migrated"
type: bug
status: open
priority: low
created: 2026-01-01
updated: 2026-01-01
branch: ""
tags: []
---

## Summary
test
EOF
"$INIT_ISSUE_PROJECT" --repo m4 --prefix MM --store "$S4" \
    --non-interactive >/dev/null 2>&1

before_count=$(git -C "$S4" rev-list --count HEAD)
"$MIGRATE_TO_STORE" --project "$M4" --store "$S4" \
    >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 ]] \
    && pass "migrate-to-store exits 0 (rc=$rc)" \
    || fail "migrate-to-store exit (rc=$rc)"

after_count=$(git -C "$S4" rev-list --count HEAD)
delta=$((after_count - before_count))
# The invariant is "two store commits per migration" (migration
# + .migrations). The pre-IM-057 environment was clean before migrate
# ran, so no catchup is expected.
[[ "$delta" == "2" ]] \
    && pass "migrate-to-store produced exactly two new commits (got $delta)" \
    || fail "migrate-to-store commit count delta=$delta (expected 2)"

git -C "$S4" log --format='%s' -2 | grep -q "^chore: record .migrations entry for MM$" \
    && pass ".migrations follow-up commit present and correctly titled" \
    || fail ".migrations follow-up commit message wrong or missing"

# ---------------------------------------------------------------------
# Scenario 5: issue-snapshot no-op on clean
# ---------------------------------------------------------------------
echo "--- Scenario 5: issue-snapshot no-op on clean ---"
before_snap=$(git -C "$S4" rev-list --count HEAD)
out="$("$ISSUE_SNAPSHOT" --store "$S4" 2>"$WORKDIR/snap.err")"
rc=$?
after_snap=$(git -C "$S4" rev-list --count HEAD)
# COVERS: concurrency.issue-snapshot
[[ $rc -eq 0 ]] \
    && pass "issue-snapshot exits 0 on clean store (rc=$rc)" \
    || fail "issue-snapshot exit on clean store (rc=$rc)"
[[ "$before_snap" == "$after_snap" ]] \
    && pass "issue-snapshot made no commit on clean store" \
    || fail "issue-snapshot unexpectedly created a commit (before=$before_snap after=$after_snap)"
echo "$out" | grep -q "store is clean" \
    && pass "issue-snapshot announced clean store" \
    || fail "issue-snapshot stdout did not say store is clean (got: $out)"

# And the orphan-recovery path: drop an orphan, snapshot, expect one
# catchup commit.
echo "snapshot orphan" > "$S4/MM/MM-099.md"
before_snap=$(git -C "$S4" rev-list --count HEAD)
"$ISSUE_SNAPSHOT" --store "$S4" >/dev/null 2>&1
after_snap=$(git -C "$S4" rev-list --count HEAD)
delta=$((after_snap - before_snap))
[[ "$delta" == "1" ]] \
    && pass "issue-snapshot produced exactly one catchup commit on dirty store" \
    || fail "issue-snapshot commit count delta=$delta (expected 1)"
git -C "$S4" log --format='%s' -1 | grep -q "^catchup: reconcile pending store changes$" \
    && pass "issue-snapshot catchup commit has the canonical title" \
    || fail "issue-snapshot commit title wrong"

# ---------------------------------------------------------------------
echo
if [[ $fail_count -eq 0 ]]; then
    echo "All checks passed."
    exit 0
else
    echo "FAILED: $fail_count check(s)"
    exit 1
fi
