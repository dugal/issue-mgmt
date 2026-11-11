#!/usr/bin/env bash
# Verification script for IM-063 -- edit-issue --if-unchanged-since SHA
# lost-update guard for caller-built --replace bodies.
#
# Reproduces the 2026-05-25 wipe scenario in miniature:
#   Session A captures SHA1, reads section, drafts a replacement body.
#   Session B (sibling) lands an --append between A's read and A's write.
#   Session A submits --replace BODY --if-unchanged-since SHA1.
# Pre-IM-063 (and without --if-unchanged-since): A's --replace wipes B's
# append silently and exits 0.
# Post-IM-063 with --if-unchanged-since: A exits 3 (concurrent-edit) and
# B's append survives on disk.
#
# Run from the project root:
#     tests/verification/im-063-if-unchanged-since.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
EDIT_ISSUE="$(resolve_cli edit-issue)"

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
    fi
}

assert_file_not_contains() {
    if grep -qF -- "$1" "$2"; then
        fail "$3: '$1' unexpectedly present in $2"
    else
        pass "$3 (file does not contain '$1')"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im063-verify.XXXXXX)
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

- [ ] row alpha
- [ ] row beta
- [ ] row gamma

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
make_issue "$STORE/IM/IM-100.md" "IM-100"
(
    cd "$STORE"
    git add IM/IM-100.md
    git commit -q -m "seed IM-100"
)

PROJ="$TMPDIR_ROOT/fixture-proj"
mkdir -p "$PROJ/.issues"
echo "IM" > "$PROJ/.issues/.prefix"
echo "$STORE" > "$PROJ/.issues/.migrated-to-store"

# ---------------------------------------------------------------------------
# Scenario 1: stale SHA -> exit 3, file preserves the intervening edit.
# ---------------------------------------------------------------------------
echo
echo "[1] Stale SHA: --replace blocked by --if-unchanged-since"

SHA1=$(git -C "$STORE" rev-parse HEAD)

# Session B sneaks in an --append to the same section (claim landing).
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-100 --section "Implementation Notes" --append \
        "- [x] row alpha (claimed by sibling)"
) >/dev/null

# Session A had read at SHA1, drafts a full-section replacement built
# from that earlier snapshot (with no awareness of session B's append).
DRAFT_REPLACE='- [ ] row alpha
- [ ] row beta
- [ ] row gamma
- [x] row delta (drafted by A)'

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-100 --section "Implementation Notes" \
        --replace "$DRAFT_REPLACE" --if-unchanged-since "$SHA1" 2>&1 >/dev/null
) || RC=$?

# COVERS: concurrency.if-unchanged-since
assert_eq "3" "$RC" "stale SHA -> exit 3"
assert_contains "concurrent-edit" "$STDERR" "stderr names concurrent-edit"
assert_contains "Implementation Notes" "$STDERR" "stderr names the section"
assert_contains "$SHA1" "$STDERR" "stderr names the SHA"

# The intervening append must survive on disk -- this is the regression
# the 2026-05-25 wipe pattern would have failed.
assert_file_contains "row alpha (claimed by sibling)" \
    "$STORE/IM/IM-100.md" \
    "sibling --append survives the blocked --replace"
assert_file_not_contains "row delta (drafted by A)" \
    "$STORE/IM/IM-100.md" \
    "stale --replace body did not land"

# ---------------------------------------------------------------------------
# Scenario 2: current SHA -> exit 0, replacement lands.
# ---------------------------------------------------------------------------
echo
echo "[2] Current SHA: --replace proceeds"

SHA2=$(git -C "$STORE" rev-parse HEAD)

# Caller re-reads, re-drafts a body that respects what they just saw.
DRAFT_REPLACE2='- [x] row alpha (after retry)
- [ ] row beta
- [ ] row gamma'

RC=0
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-100 --section "Implementation Notes" \
        --replace "$DRAFT_REPLACE2" --if-unchanged-since "$SHA2"
) >/dev/null || RC=$?

assert_eq "0" "$RC" "current SHA -> exit 0"
assert_file_contains "row alpha (after retry)" "$STORE/IM/IM-100.md" \
    "fresh --replace body lands"
assert_file_not_contains "row alpha (claimed by sibling)" \
    "$STORE/IM/IM-100.md" \
    "fresh --replace correctly overwrites prior section content"

# ---------------------------------------------------------------------------
# Scenario 3: unresolvable SHA -> exit 2.
# ---------------------------------------------------------------------------
echo
echo "[3] Unresolvable SHA -> exit 2"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-100 --section Summary \
        --replace "Doesn't matter." --if-unchanged-since "deadbeefdeadbeef" \
        2>&1 >/dev/null
) || RC=$?

assert_eq "2" "$RC" "bogus SHA -> exit 2"
assert_contains "--if-unchanged-since" "$STDERR" "stderr names the flag"

# ---------------------------------------------------------------------------
# Scenario 4: --if-unchanged-since without --replace -> exit 2.
# ---------------------------------------------------------------------------
echo
echo "[4] Flag combo guards"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-100 --section Summary --append "extra" \
        --if-unchanged-since "$SHA2" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--if-unchanged-since with --append -> exit 2"
assert_contains "requires --section NAME --replace TEXT" "$STDERR" \
    "stderr explains required combo"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-100 --set priority=low --if-unchanged-since "$SHA2" \
        2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--if-unchanged-since with --set only -> exit 2"

# ---------------------------------------------------------------------------
# Scenario 5: legacy mode rejects --if-unchanged-since with exit 2.
# ---------------------------------------------------------------------------
echo
echo "[5] Legacy mode rejection"

LEGACY="$TMPDIR_ROOT/legacy-proj"
mkdir -p "$LEGACY/.issues"
echo "IM" > "$LEGACY/.issues/.prefix"
make_issue "$LEGACY/.issues/IM-200.md" "IM-200"

RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-200 --section Summary --replace "x" \
        --if-unchanged-since "deadbeef" --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "legacy mode + --if-unchanged-since -> exit 2"
assert_contains "store mode" "$STDERR" "stderr explains store-mode requirement"

# ---------------------------------------------------------------------------
# Scenario 6: section absent at SHA but present now -> exit 3.
# ---------------------------------------------------------------------------
echo
echo "[6] Section added concurrently"

# Seed an issue that lacks a "Bonus" section.
make_issue "$STORE/IM/IM-101.md" "IM-101"
(
    cd "$STORE"
    git add IM/IM-101.md
    git commit -q -m "seed IM-101"
)
SHA3=$(git -C "$STORE" rev-parse HEAD)

# A sibling session adds the "Bonus" section. Uses the structural
# --add-section op: splicing a `## Bonus` heading through a body op is
# refused by the section-escape guard (exit 2).
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-101 --add-section "Bonus" --body "New section appeared."
) >/dev/null

# Now caller (referencing SHA3, pre-add) tries to --replace the new
# section. Pre-IM-063 the section is in their working copy so --replace
# would land; the IM-063 guard catches that "Bonus" did not exist at
# SHA3 and refuses.
RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-101 --section Bonus --replace "Stale draft." \
        --if-unchanged-since "$SHA3" 2>&1 >/dev/null
) || RC=$?
assert_eq "3" "$RC" "section-added-after-SHA -> exit 3"
assert_contains "added concurrently" "$STDERR" \
    "stderr explains concurrent-add path"
assert_file_contains "New section appeared." "$STORE/IM/IM-101.md" \
    "concurrent-add survives blocked --replace"

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
