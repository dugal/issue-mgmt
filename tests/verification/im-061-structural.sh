#!/usr/bin/env bash
# Verification script for IM-061 -- edit-issue structural section ops.
#
# Exercises the five new capabilities and their composition:
#   (a) multi-section atomic edit  -- repeatable --section ... --op TEXT
#   (b) --add-section [--body TEXT] [--at-end | --after | --before | --to-position]
#   (c) --rename-section OLD NEW
#   (d) --remove-section NAME
#   (e) --move-section NAME --after / --before / --to-position
#
# Plus: single-commit guarantee under composition, error paths for
# missing anchors / duplicates / malformed flag groupings, and the
# IM-063 --if-unchanged-since compatibility guard (rejected when paired
# with multi-section or structural ops).
#
# Run from the project root:
#     tests/verification/im-061-structural.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
EDIT_ISSUE="$(resolve_cli edit-issue)"
CONCURRENCY_PY="$PROJECT_ROOT/lib/concurrency.py"  # Tier 2: stack-coupled test adversary; see docs/design/concurrency-test-hook.md

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then
        pass "$3 ($2)"
    else
        fail "$3: expected '$1', got '$2'"
    fi
}

assert_contains() {
    if echo "$2" | grep -qF -- "$1"; then
        pass "$3 (contains '$1')"
    else
        fail "$3: '$1' not found in '$2'"
    fi
}

assert_file_contains() {
    if grep -qF -- "$1" "$2"; then
        pass "$3 (file contains '$1')"
    else
        fail "$3: '$1' not in $2"
        sed -n '1,80p' "$2"
    fi
}

assert_file_not_contains() {
    if grep -qF -- "$1" "$2"; then
        fail "$3: '$1' unexpectedly present in $2"
    else
        pass "$3 (file does not contain '$1')"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im061-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

export PATH="$ISSUE_MGMT_BIN:$PATH"
# Hard-isolate from the caller's real store -- otherwise ISSUE_STORE_ROOT
# in the user's environment shadows the test's per-project sentinel
# routing (resolution order is env > sentinel).
unset ISSUE_STORE_ROOT

make_issue() {
    # make_issue <path> <id>
    cat > "$1" <<EOF
---
id: $2
title: "Sample issue"
type: feature
status: open
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: [a, b]
changelog:    # set by /update-changelog -- do not edit manually
---

## Summary

Original summary.

## Implementation Notes

- item one
- item two

## Archived Plan Content
EOF
}

# ---------------------------------------------------------------------------
# Store fixture: a real git-managed store with one fixture project.
# ---------------------------------------------------------------------------
STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM"
(
    cd "$STORE"
    git init -q
    git config user.email "$FIXTURE_EMAIL"
    git config user.name test
    printf 'fixture-proj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)

PROJ="$TMPDIR_ROOT/fixture-proj"
mkdir -p "$PROJ/.issues"
echo "IM" > "$PROJ/.issues/.prefix"
echo "$STORE" > "$PROJ/.issues/.migrated-to-store"

seed_issue() {
    # seed_issue <id>
    local id="$1"
    make_issue "$STORE/IM/$id.md" "$id"
    (
        cd "$STORE"
        git add "IM/$id.md"
        git commit -q -m "seed $id"
    )
}

# ---------------------------------------------------------------------------
# [1] --add-section --after / --before / --to-position / default (at-end)
# ---------------------------------------------------------------------------
echo
echo "[1] --add-section variants"

seed_issue IM-100
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-100 --add-section Approach --body "An approach." --after Summary
) >/dev/null
# COVERS: cli.edit-multi-section-atomic
assert_file_contains "## Approach" "$STORE/IM/IM-100.md" "add-section creates heading"
assert_file_contains "An approach." "$STORE/IM/IM-100.md" "add-section keeps body"
# Approach must appear before Implementation Notes (i.e. after Summary)
APPROACH_POS=$(grep -n "## Approach" "$STORE/IM/IM-100.md" | head -1 | cut -d: -f1)
IN_POS=$(grep -n "## Implementation Notes" "$STORE/IM/IM-100.md" | head -1 | cut -d: -f1)
if [ "$APPROACH_POS" -lt "$IN_POS" ]; then
    pass "--after Summary lands before Implementation Notes ($APPROACH_POS < $IN_POS)"
else
    fail "--after Summary positioning: Approach=$APPROACH_POS Impl=$IN_POS"
fi

seed_issue IM-101
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-101 --add-section Bonus --body "Bonus body." --before "Implementation Notes"
) >/dev/null
BONUS_POS=$(grep -n "## Bonus" "$STORE/IM/IM-101.md" | head -1 | cut -d: -f1)
IN_POS=$(grep -n "## Implementation Notes" "$STORE/IM/IM-101.md" | head -1 | cut -d: -f1)
if [ "$BONUS_POS" -lt "$IN_POS" ]; then
    pass "--before Implementation Notes lands above it ($BONUS_POS < $IN_POS)"
else
    fail "--before positioning failed"
fi

seed_issue IM-102
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-102 --add-section First --body "First body." --to-position 0
) >/dev/null
FIRST_LINE=$(grep -n "^## " "$STORE/IM/IM-102.md" | head -1)
assert_contains "## First" "$FIRST_LINE" "--to-position 0 lands first"

seed_issue IM-103
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-103 --add-section Last --body "Last body."
) >/dev/null
LAST_LINE=$(grep -n "^## " "$STORE/IM/IM-103.md" | tail -1)
assert_contains "## Last" "$LAST_LINE" "default at-end lands last"

# Empty --body produces a heading-only section
seed_issue IM-104
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-104 --add-section Empty
) >/dev/null
assert_file_contains "## Empty" "$STORE/IM/IM-104.md" "add-section without --body adds heading"

# ---------------------------------------------------------------------------
# [2] --rename-section preserves body byte-for-byte
# ---------------------------------------------------------------------------
echo
echo "[2] --rename-section"

seed_issue IM-110
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-110 --rename-section "Implementation Notes" Notes
) >/dev/null
assert_file_contains "## Notes" "$STORE/IM/IM-110.md" "rename produces new heading"
assert_file_not_contains "## Implementation Notes" "$STORE/IM/IM-110.md" "old heading is gone"
assert_file_contains "- item one" "$STORE/IM/IM-110.md" "body preserved after rename"
assert_file_contains "- item two" "$STORE/IM/IM-110.md" "second body line preserved"

# ---------------------------------------------------------------------------
# [3] --remove-section
# ---------------------------------------------------------------------------
echo
echo "[3] --remove-section"

seed_issue IM-120
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-120 --remove-section "Archived Plan Content"
) >/dev/null
assert_file_not_contains "## Archived Plan Content" "$STORE/IM/IM-120.md" "remove drops heading"
assert_file_contains "## Implementation Notes" "$STORE/IM/IM-120.md" "preceding section intact"

# Remove a middle section -- adjacent sections must remain
seed_issue IM-121
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-121 --remove-section "Implementation Notes"
) >/dev/null
assert_file_not_contains "## Implementation Notes" "$STORE/IM/IM-121.md" "middle section removed"
assert_file_not_contains "- item one" "$STORE/IM/IM-121.md" "removed body gone too"
assert_file_contains "## Summary" "$STORE/IM/IM-121.md" "preceding section intact"
assert_file_contains "## Archived Plan Content" "$STORE/IM/IM-121.md" "following section intact"

# ---------------------------------------------------------------------------
# [4] --move-section --after / --to-position
# ---------------------------------------------------------------------------
echo
echo "[4] --move-section"

seed_issue IM-130
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-130 --move-section "Archived Plan Content" --after Summary
) >/dev/null
# Order should now be: Summary, Archived Plan Content, Implementation Notes
ORDER=$(grep -n "^## " "$STORE/IM/IM-130.md" | cut -d: -f2 | tr -d ' ')
if [ "$ORDER" = "$(printf '##Summary\n##ArchivedPlanContent\n##ImplementationNotes')" ]; then
    pass "move --after Summary reorders correctly"
else
    fail "move --after order wrong: got '$ORDER'"
fi
# Body of the moved section must survive
assert_file_contains "- item one" "$STORE/IM/IM-130.md" "moved section's siblings keep their bodies"

seed_issue IM-131
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-131 --move-section "Implementation Notes" --to-position 0
) >/dev/null
FIRST=$(grep -n "^## " "$STORE/IM/IM-131.md" | head -1)
assert_contains "## Implementation Notes" "$FIRST" "move to_position 0 lands first"
assert_file_contains "- item one" "$STORE/IM/IM-131.md" "moved body preserved"

# ---------------------------------------------------------------------------
# [5] Composed: rename + remove + add + multi-section edit, ONE commit.
# ---------------------------------------------------------------------------
echo
echo "[5] Composition: single commit per invocation"

seed_issue IM-140
SHA_BEFORE=$(git -C "$STORE" rev-parse HEAD)
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-140 \
        --rename-section "Implementation Notes" Notes \
        --remove-section "Archived Plan Content" \
        --add-section Approach --body "Composed approach." --after Summary \
        --section Summary --replace "Replaced summary." \
        --section Notes --append "- new note"
) >/dev/null
SHA_AFTER=$(git -C "$STORE" rev-parse HEAD)

# Exactly one new commit (auto-reindex commits land inside the same store_mutation)
COMMIT_COUNT=$(git -C "$STORE" rev-list "${SHA_BEFORE}..${SHA_AFTER}" | wc -l | tr -d ' ')
assert_eq "1" "$COMMIT_COUNT" "composed invocation produces exactly one new commit"
MSG=$(git -C "$STORE" log -1 --format=%s)
assert_eq "edit IM-140" "$MSG" "commit message names the issue"

assert_file_contains "## Approach" "$STORE/IM/IM-140.md" "add-section landed"
assert_file_contains "Composed approach." "$STORE/IM/IM-140.md" "add-section body landed"
assert_file_contains "## Notes" "$STORE/IM/IM-140.md" "rename landed"
assert_file_not_contains "## Implementation Notes" "$STORE/IM/IM-140.md" "old heading gone"
assert_file_not_contains "## Archived Plan Content" "$STORE/IM/IM-140.md" "remove landed"
assert_file_contains "Replaced summary." "$STORE/IM/IM-140.md" "section replace landed"
assert_file_not_contains "Original summary." "$STORE/IM/IM-140.md" "replaced content gone"
assert_file_contains "- new note" "$STORE/IM/IM-140.md" "section append landed"
assert_file_contains "- item one" "$STORE/IM/IM-140.md" "preserved Notes body lines"

# ---------------------------------------------------------------------------
# [6] Multi-section atomic edit (two --section --op groups)
# ---------------------------------------------------------------------------
echo
echo "[6] Multi-section atomic edit"

seed_issue IM-150
SHA_BEFORE=$(git -C "$STORE" rev-parse HEAD)
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-150 \
        --section Summary --replace "New summary." \
        --section "Implementation Notes" --append "- added"
) >/dev/null
SHA_AFTER=$(git -C "$STORE" rev-parse HEAD)
COMMIT_COUNT=$(git -C "$STORE" rev-list "${SHA_BEFORE}..${SHA_AFTER}" | wc -l | tr -d ' ')
assert_eq "1" "$COMMIT_COUNT" "multi-section edit produces one commit"
assert_file_contains "New summary." "$STORE/IM/IM-150.md" "first section replace landed"
assert_file_contains "- added" "$STORE/IM/IM-150.md" "second section append landed"
assert_file_contains "- item one" "$STORE/IM/IM-150.md" "preserved Implementation Notes body"

# ---------------------------------------------------------------------------
# [7] Error paths
# ---------------------------------------------------------------------------
echo
echo "[7] Error paths"

seed_issue IM-160

# Duplicate section -> exit 1, no file change
SHA_BEFORE=$(git -C "$STORE" rev-parse HEAD)
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-160 --add-section Summary 2>&1 >/dev/null
) || RC=$?
assert_eq "1" "$RC" "add-section duplicate -> exit 1"
assert_contains "section already exists" "$STDERR" "stderr explains duplicate"
SHA_AFTER=$(git -C "$STORE" rev-parse HEAD)
assert_eq "$SHA_BEFORE" "$SHA_AFTER" "duplicate add did not commit"

# Missing anchor -> exit 1
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-160 --add-section New --after Nonexistent 2>&1 >/dev/null
) || RC=$?
assert_eq "1" "$RC" "add-section missing anchor -> exit 1"
assert_contains "section not found" "$STDERR" "stderr explains missing anchor"

# Rename to existing -> exit 1
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-160 --rename-section Summary "Implementation Notes" 2>&1 >/dev/null
) || RC=$?
assert_eq "1" "$RC" "rename to existing -> exit 1"

# Move missing destination -> exit 2 (caught at interpret time)
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-160 --move-section Summary 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "move missing destination -> exit 2"
assert_contains "--after/--before/--to-position" "$STDERR" "stderr lists destinations"

# --append without --section -> exit 2
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-160 --append "x" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--append without --section -> exit 2"
assert_contains "--append requires a preceding --section" "$STDERR" \
    "stderr explains anchor requirement"

# Two body ops on one --section -> exit 2
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-160 --section Summary --append "x" --replace "y" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "two ops on one --section -> exit 2"

# Two positions on --add-section -> exit 2
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-160 --add-section X --after Summary --to-position 0 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "two positions on --add-section -> exit 2"

# ---------------------------------------------------------------------------
# [8] compatibility: --if-unchanged-since refuses multi-op invocations
# ---------------------------------------------------------------------------
echo
echo "[8] compatibility"

seed_issue IM-170
SHA=$(git -C "$STORE" rev-parse HEAD)
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-170 \
        --section Summary --replace "x" \
        --section "Implementation Notes" --append "y" \
        --if-unchanged-since "$SHA" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--if-unchanged-since with multi-section -> exit 2"
assert_contains "requires --section NAME --replace TEXT" "$STDERR" \
    "stderr explains required combo"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-170 \
        --section Summary --replace "x" \
        --add-section Bonus --body "y" \
        --if-unchanged-since "$SHA" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--if-unchanged-since with --add-section -> exit 2"

# A bare single --section --replace --if-unchanged-since SHA still works
RC=0
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-170 --section Summary --replace "Fresh." --if-unchanged-since "$SHA"
) >/dev/null || RC=$?
assert_eq "0" "$RC" "single --section --replace --if-unchanged-since still works"
assert_file_contains "Fresh." "$STORE/IM/IM-170.md" "single SHA-guarded replace landed"

# ---------------------------------------------------------------------------
# [9] Concurrent-edit: structural ops surface exit 3 on lock contention
# ---------------------------------------------------------------------------
echo
echo "[9] Concurrent-edit on structural ops"

seed_issue IM-180
LOCK="$STORE/IM/.locks/IM-180.lock"
mkdir -p "$STORE/IM/.locks"

run_with_held_lock() {
    # run_with_held_lock <label> <expected-rc> <expected-stderr-substr> -- edit-issue args...
    local label="$1"; shift
    local want_rc="$1"; shift
    local want_substr="$1"; shift
    [ "$1" = "--" ] && shift

    # Hold the per-issue lock externally for 3s.
    ( python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 3" ) &
    local holder_pid=$!
    sleep 0.3

    local rc=0
    local stderr
    stderr=$(
        cd "$PROJ"
        "$EDIT_ISSUE" IM-180 --lock-timeout 0.5 "$@" 2>&1 >/dev/null
    ) || rc=$?

    wait "$holder_pid" 2>/dev/null

    assert_eq "$want_rc" "$rc" "$label -> exit $want_rc"
    assert_contains "$want_substr" "$stderr" "$label stderr names concurrent-edit"
}

# Baseline file state (so we can confirm nothing landed under contention).
SUMMARY_BEFORE=$(sed -n '/^## Summary/,/^## /p' "$STORE/IM/IM-180.md")

run_with_held_lock "--add-section blocked" 3 "concurrent-edit" -- \
    --add-section NewSection --body "should not land" --after Summary

run_with_held_lock "--rename-section blocked" 3 "concurrent-edit" -- \
    --rename-section "Implementation Notes" Notes

run_with_held_lock "--remove-section blocked" 3 "concurrent-edit" -- \
    --remove-section "Archived Plan Content"

run_with_held_lock "--move-section blocked" 3 "concurrent-edit" -- \
    --move-section "Implementation Notes" --after Summary

run_with_held_lock "multi-section + structural composed blocked" 3 "concurrent-edit" -- \
    --section Summary --replace "new" \
    --add-section X --body "x" \
    --rename-section "Implementation Notes" Notes \
    --remove-section "Archived Plan Content"

# Nothing from any of the above attempts should have landed on disk.
assert_file_not_contains "## NewSection" "$STORE/IM/IM-180.md" \
    "blocked --add-section left no trace"
assert_file_contains "## Implementation Notes" "$STORE/IM/IM-180.md" \
    "blocked --rename-section left old heading intact"
assert_file_contains "## Archived Plan Content" "$STORE/IM/IM-180.md" \
    "blocked --remove-section left target heading intact"
assert_file_not_contains "## X" "$STORE/IM/IM-180.md" \
    "blocked composed invocation did not partially apply"

# After contention clears, a structural edit succeeds.
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-180 --add-section LaterAdded --body "Lands after contention."
) >/dev/null
assert_file_contains "## LaterAdded" "$STORE/IM/IM-180.md" \
    "structural edit succeeds after lock released"

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
