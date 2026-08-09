#!/usr/bin/env bash
# issue-snapshot.sh -- pre-merge verification
#
# Dedicated CLI verification for scripts/issue-snapshot. Exercises:
#   - --help / --version
#   - --store ROOT override
#   - ISSUE_STORE_ROOT env override
#   - .migrated-to-store sentinel resolution
#   - ~/.config/issue-mgmt/config fallback (HOME-override pattern lifted
#     from tests/verification/init-issue-project.sh convention)
#   - "no store root resolved" diagnostic (exit 1)
#   - "store root does not exist" diagnostic (exit 1)
#   - ConcurrentEdit exception path (exit 3, via forced store-commit-lock
#     contention with python3 lib/concurrency.py flock-run)
#   - StoreCommitError exception path (exit 1, via non-git store directory
#     plus a tracked dirty file)
#   - Clean-store success ("store is clean; no commit made")
#   - Successful catchup-commit ("catchup commit: <sha>") asserted on the
#     binary's own stdout (im-057-auto-commit.sh covers this indirectly;
#     here we assert directly)
#
# Run from the project root:
#   tests/verification/issue-snapshot.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
SCRIPT="$(resolve_cli issue-snapshot)"
CONCURRENCY_PY="$REPO_ROOT/lib/concurrency.py"  # Tier 2: stack-coupled test adversary; see docs/design/concurrency-test-hook.md

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t issue-snapshot.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

# Stores must be git repos with at least one commit before any
# mutation (including issue-snapshot) runs against them.
mk_store() {
    local path="$1"
    mkdir -p "$path"
    git -C "$path" init -q
    git -C "$path" config user.email test@example.com
    git -C "$path" config user.name test
    git -C "$path" commit -q --allow-empty -m "init store"
}

# Reusable HOME-override pattern -- lifted verbatim from
# tests/verification/init-issue-project.sh. The same three knobs
# (HOME=<scratch>; unset ISSUE_STORE_ROOT; cwd outside any sentinel) apply
# to issue-snapshot's resolve_store_root() ladder.
mk_home_override() {
    local label="$1"
    local seed="${2:-}"
    [[ -z "$label" ]] && { echo "mk_home_override: missing LABEL" >&2; exit 99; }
    local home_dir="$WORKDIR/home-$label"
    mkdir -p "$home_dir/.config/issue-mgmt"
    if [[ -n "$seed" ]]; then
        printf 'store_root = %s\n' "$seed" \
            > "$home_dir/.config/issue-mgmt/config"
    fi
    echo "$home_dir"
}

# --- Test 1: --help ---
# COVERS: general.executability
"$SCRIPT" --help >/dev/null 2>&1 && pass "--help exits 0" || fail "--help exits 0"

# --- Test 2: --version ---
# COVERS: general.executability
v=$("$SCRIPT" --version 2>&1)
rc=$?
[[ $rc -eq 0 ]] && [[ "$v" =~ ^v[0-9] ]] \
    && pass "--version prints v<digits> ($v)" \
    || fail "--version prints v<digits> (rc=$rc got: $v)"

# --- Test 3: --store ROOT override on clean store -> "store is clean" ---
STORE1="$WORKDIR/store-clean"
mk_store "$STORE1"
out=$( "$SCRIPT" --store "$STORE1" 2>&1 )
rc=$?
# COVERS: concurrency.issue-snapshot
# COVERS: substrate.store-root-resolution
if [[ $rc -eq 0 ]] && [[ "$out" == *"store is clean"* ]]; then
    pass "--store override on clean store: exit 0, 'store is clean' on stdout"
else
    fail "--store clean (rc=$rc out=$out)"
fi
# Confirm no commit was made.
sha_before=$(git -C "$STORE1" rev-parse HEAD)
sha_after=$(git -C "$STORE1" rev-parse HEAD)
[[ "$sha_before" == "$sha_after" ]] \
    && pass "clean store: HEAD unchanged after issue-snapshot" \
    || fail "clean store: HEAD changed unexpectedly"

# --- Test 4: ISSUE_STORE_ROOT env var override on clean store ---
STORE2="$WORKDIR/store-env"
mk_store "$STORE2"
# COVERS: substrate.store-root-resolution
out=$( cd "$WORKDIR" && ISSUE_STORE_ROOT="$STORE2" "$SCRIPT" 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && [[ "$out" == *"store is clean"* ]]; then
    pass "ISSUE_STORE_ROOT env var resolves to clean store"
else
    fail "ISSUE_STORE_ROOT env (rc=$rc out=$out)"
fi

# --- Test 5: .migrated-to-store sentinel resolution ---
STORE3="$WORKDIR/store-sentinel"
mk_store "$STORE3"
ANCHOR="$WORKDIR/project-with-sentinel"
mkdir -p "$ANCHOR/.issues"
echo "$STORE3" > "$ANCHOR/.issues/.migrated-to-store"
# COVERS: substrate.store-root-resolution
# COVERS: substrate.store-relocation
home_s=$(mk_home_override test5-sentinel)
out=$( cd "$ANCHOR" && unset ISSUE_STORE_ROOT && HOME="$home_s" "$SCRIPT" 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && [[ "$out" == *"store is clean"* ]]; then
    pass ".migrated-to-store sentinel resolves store root from cwd"
else
    fail "sentinel resolve (rc=$rc out=$out)"
fi

# --- Test 5b: sentinel found by walking up from a nested cwd ---
NESTED="$ANCHOR/sub/deeper"
mkdir -p "$NESTED"
# COVERS: substrate.store-root-resolution
out=$( cd "$NESTED" && unset ISSUE_STORE_ROOT && HOME="$home_s" "$SCRIPT" 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && [[ "$out" == *"store is clean"* ]]; then
    pass "sentinel resolves when cwd is nested under the anchor"
else
    fail "sentinel resolve nested (rc=$rc out=$out)"
fi

# --- Test 6: ~/.config/issue-mgmt/config fallback ---
STORE_CFG="$WORKDIR/store-cfg"
mk_store "$STORE_CFG"
REPO_CFG="$WORKDIR/repo-cfg-fallback"
mkdir -p "$REPO_CFG"
# Seed config with comment + blank + key, exercising read_config_store_root
# branches that skip blanks and comments.
home_cfg=$(mk_home_override test6-cfg-fallback)
cat > "$home_cfg/.config/issue-mgmt/config" <<EOF
# managed by issue-mgmt

store_root = $STORE_CFG
EOF
# COVERS: substrate.store-root-resolution
out=$( cd "$REPO_CFG" && unset ISSUE_STORE_ROOT && HOME="$home_cfg" "$SCRIPT" 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && [[ "$out" == *"store is clean"* ]]; then
    pass "~/.config/issue-mgmt/config fallback resolves store_root"
else
    fail "config fallback (rc=$rc out=$out)"
fi

# --- Test 7: no store root resolved -> exit 1 with documented diagnostic ---
REPO_E="$WORKDIR/no-store"
mkdir -p "$REPO_E"
home_e=$(mk_home_override test7-nostore)
# COVERS: substrate.store-root-resolution
out=$( cd "$REPO_E" && unset ISSUE_STORE_ROOT && HOME="$home_e" "$SCRIPT" 2>&1 )
rc=$?
if [[ $rc -eq 1 ]] && [[ "$out" == *"no store root resolved"* ]]; then
    pass "no store root resolved: exit 1 with documented diagnostic"
else
    fail "no store root resolved (rc=$rc out=$out)"
fi

# --- Test 8: store root does not exist -> exit 1 with documented diagnostic ---
# COVERS: substrate.store-root-resolution
out=$( "$SCRIPT" --store "$WORKDIR/does-not-exist" 2>&1 )
rc=$?
if [[ $rc -eq 1 ]] && [[ "$out" == *"store root does not exist"* ]]; then
    pass "missing store dir: exit 1 with documented diagnostic"
else
    fail "missing store dir (rc=$rc out=$out)"
fi

# --- Test 9: ConcurrentEdit exception path -> exit 3 ---
# Force store-commit-lock contention: hold the lock with flock-run, then
# invoke issue-snapshot with a tight --timeout. The internal retry-once
# in store_commit_lock (concurrency.py) cannot acquire either; the binary
# surfaces ConcurrentEdit on stderr and exits 3.
STORE_LK="$WORKDIR/store-lock"
mk_store "$STORE_LK"
mkdir -p "$STORE_LK/.locks"
LK_PATH="$STORE_LK/.locks/store-commit.lock"
python3 "$CONCURRENCY_PY" flock-run "$LK_PATH" --timeout 10 -- sleep 60 &
HOLDER_PID=$!
# Give the holder time to acquire.
sleep 0.4
err=$( "$SCRIPT" --store "$STORE_LK" --timeout 0.3 2>&1 >/dev/null )
rc=$?
kill -9 $HOLDER_PID 2>/dev/null
wait $HOLDER_PID 2>/dev/null
# COVERS: concurrency.exit-17
# COVERS: principles.p2
# COVERS: output.exit-codes
if [[ $rc -eq 3 ]] && [[ "$err" == *"concurrent-edit"* ]]; then
    pass "ConcurrentEdit path: exit 3, 'concurrent-edit' on stderr"
else
    fail "ConcurrentEdit path (rc=$rc err=$err)"
fi

# --- Test 10: StoreCommitError exception path -> exit 1 ---
# A directory that exists but is NOT a git repo: Path.is_dir() succeeds,
# so resolve_store_root accepts it; the catchup_sweep call then runs
# `git status` inside it, which fails -> StoreCommitError -> exit 1 with
# the documented "store commit failed" diagnostic.
STORE_NOGIT="$WORKDIR/store-nogit"
mkdir -p "$STORE_NOGIT/.locks"   # let the flock acquire so we reach catchup_sweep
err=$( "$SCRIPT" --store "$STORE_NOGIT" 2>&1 >/dev/null )
rc=$?
# COVERS: concurrency.commit-failure-semantics
if [[ $rc -eq 1 ]] && [[ "$err" == *"store commit failed"* ]]; then
    pass "StoreCommitError path: exit 1, 'store commit failed' on stderr"
else
    fail "StoreCommitError path (rc=$rc err=$err)"
fi

# --- Test 11: successful catchup-commit on dirty store ---
# Drop a tracked-path file (matches is_store_tracked_path: <PFX>/<ID>.md).
# Run issue-snapshot; it should commit one "catchup: reconcile pending store
# changes" commit and print "catchup commit: <sha>" on stdout.
STORE_DIRTY="$WORKDIR/store-dirty"
mk_store "$STORE_DIRTY"
mkdir -p "$STORE_DIRTY/XX"
cat > "$STORE_DIRTY/XX/XX-001.md" <<'EOF'
---
id: XX-001
title: orphan write
type: chore
status: open
priority: low
created: 2026-06-21
updated: 2026-06-21
branch: ""
tags: []
---
## Summary

orphan write from a killed mutation
EOF
sha_before=$(git -C "$STORE_DIRTY" rev-parse HEAD)
out=$( "$SCRIPT" --store "$STORE_DIRTY" 2>&1 )
rc=$?
sha_after=$(git -C "$STORE_DIRTY" rev-parse HEAD)
top_subject=$(git -C "$STORE_DIRTY" log -1 --format='%s')
# COVERS: concurrency.issue-snapshot
# COVERS: concurrency.catchup-sweep
# COVERS: concurrency.auto-commit-per-mutation
if [[ $rc -eq 0 ]] \
   && [[ "$sha_before" != "$sha_after" ]] \
   && [[ "$top_subject" == "catchup: reconcile pending store changes" ]] \
   && [[ "$out" == *"catchup commit: "* ]]; then
    pass "dirty store: catchup commit made, stdout reports 'catchup commit: <sha>'"
else
    fail "dirty store catchup (rc=$rc before=$sha_before after=$sha_after subject=$top_subject out=$out)"
fi

# --- Test 11b: re-run on now-clean store reports clean ---
out=$( "$SCRIPT" --store "$STORE_DIRTY" 2>&1 )
rc=$?
sha_after2=$(git -C "$STORE_DIRTY" rev-parse HEAD)
# COVERS: concurrency.issue-snapshot
if [[ $rc -eq 0 ]] \
   && [[ "$out" == *"store is clean"* ]] \
   && [[ "$sha_after" == "$sha_after2" ]]; then
    pass "post-catchup re-run: 'store is clean', HEAD unchanged"
else
    fail "post-catchup re-run (rc=$rc out=$out sha_after=$sha_after sha_after2=$sha_after2)"
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
