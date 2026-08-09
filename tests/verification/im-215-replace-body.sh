#!/usr/bin/env bash
# Verification script for IM-215 -- issue edit --replace-body-file PATH:
# whole-body atomic replace through the locked mutation path.
#
# The op is the escape hatch for body changes the section-scoped ops do
# not cover (e.g. level-3 subsection edits): the caller assembles the
# ENTIRE new body and submits it through the real mutation path (per-issue
# lock -> snapshot -> atomic write -> `edit <id>` commit), instead of a
# raw hand-edit + manual git commit that a parallel session's catchup
# sweep would absorb. The body is opaque: `## ` lines are legitimate new
# top-level sections. Frontmatter passes through untouched (bar the
# `updated:` stamp). Exclusive with section ops; composes with --set and
# dep ops; composes with --if-unchanged-since SHA as a whole-BODY etag.
#
# Run from the project root:
#     tests/verification/im-215-replace-body.sh

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

TMPDIR_ROOT=$(mktemp -d -t im215-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

export PATH="$ISSUE_MGMT_BIN:$PATH"
# Hard-isolate from the caller's real store (env > sentinel resolution).
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

### Deep subsection

UNDECIDED marker.

## Implementation Notes
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
    git config user.email test@example.com
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

NEWBODY="$TMPDIR_ROOT/newbody.md"
cat > "$NEWBODY" <<'EOF'

## Summary

Rewritten summary.

### Deep subsection

DECIDED marker.

## Brand New Section

Added wholesale by the whole-body replace.
EOF

# ---------------------------------------------------------------------------
# Scenario 1: whole-body replace lands verbatim under one edit commit.
# ---------------------------------------------------------------------------
echo
echo "[1] Whole-body replace: opaque body, edit commit, frontmatter kept"

RC=0
JSON=$(cd "$PROJ" && "$EDIT_ISSUE" IM-100 --replace-body-file "$NEWBODY" \
    --json 2>/dev/null) || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "0" "$RC" "whole-body replace exits 0"
# COVERS: cli.edit-replace-body
assert_file_contains "## Brand New Section" "$STORE/IM/IM-100.md" \
    "## line in submitted body lands as a real section"
assert_file_contains "DECIDED marker." "$STORE/IM/IM-100.md" \
    "level-3 subsection edit landed via opaque body"
assert_file_not_contains "UNDECIDED marker." "$STORE/IM/IM-100.md" \
    "old body fully replaced"
# COVERS: cli.edit-replace-body
assert_file_contains 'title: "Sample issue"' "$STORE/IM/IM-100.md" \
    "frontmatter untouched by body replace"
assert_file_contains "changelog:    # set by /update-changelog" \
    "$STORE/IM/IM-100.md" "frontmatter comment bytes preserved"
HEAD_MSG=$(git -C "$STORE" log -1 --format=%s -- IM/IM-100.md)
# COVERS: cli.edit-replace-body
assert_eq "edit IM-100" "$HEAD_MSG" "committed under stable edit message"
CLEAN=$(git -C "$STORE" status --porcelain -- IM/IM-100.md)
assert_eq "" "$CLEAN" "no limbo write: issue file clean after mutation"
# COVERS: cli.edit-replace-body
assert_contains '"kind": "replace-body"' "$JSON" \
    "--json reports body_actions replace-body"
assert_contains '"bytes":' "$JSON" "--json reports operand byte count"

# ---------------------------------------------------------------------------
# Scenario 2: composes with --set (frontmatter plane) in one commit.
# ---------------------------------------------------------------------------
echo
echo "[2] Composes with --set under a single lock + commit"

COMMITS_BEFORE=$(git -C "$STORE" rev-list --count HEAD)
RC=0
(cd "$PROJ" && "$EDIT_ISSUE" IM-100 --replace-body-file "$NEWBODY" \
    --set priority=low) >/dev/null 2>&1 || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "0" "$RC" "replace-body + --set exits 0"
assert_file_contains "priority: low" "$STORE/IM/IM-100.md" \
    "--set applied alongside body replace"
COMMITS_AFTER=$(git -C "$STORE" rev-list --count HEAD)
assert_eq "$((COMMITS_BEFORE + 1))" "$COMMITS_AFTER" \
    "one commit for the composed mutation"

# ---------------------------------------------------------------------------
# Scenario 3: exclusive with section ops -- refuse exit 2, no write.
# ---------------------------------------------------------------------------
echo
echo "[3] Section ops refused alongside --replace-body-file (exit 2)"

RC=0
STDERR=$(cd "$PROJ" && "$EDIT_ISSUE" IM-100 --replace-body-file "$NEWBODY" \
    --section "Summary" --append "sneaky" 2>&1 >/dev/null) || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "2" "$RC" "section op + replace-body-file -> exit 2"
assert_contains "exclusive with section ops" "$STDERR" \
    "stderr names the exclusivity rule"
assert_file_not_contains "sneaky" "$STORE/IM/IM-100.md" \
    "refused invocation wrote nothing"

# ---------------------------------------------------------------------------
# Scenario 4: operand edge cases -- missing file (exit 1), empty file,
# stdin.
# ---------------------------------------------------------------------------
echo
echo "[4] Operand edge cases: missing file, empty file, stdin"

RC=0
(cd "$PROJ" && "$EDIT_ISSUE" IM-100 \
    --replace-body-file "$TMPDIR_ROOT/nope.md") >/dev/null 2>&1 || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "1" "$RC" "missing operand file -> exit 1"

: > "$TMPDIR_ROOT/empty.md"
RC=0
(cd "$PROJ" && "$EDIT_ISSUE" IM-100 \
    --replace-body-file "$TMPDIR_ROOT/empty.md") >/dev/null 2>&1 || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "0" "$RC" "empty operand file -> exit 0, empty body"
LAST_LINE=$(tail -1 "$STORE/IM/IM-100.md")
assert_eq "---" "$LAST_LINE" "file ends at the closing fence (empty body)"

RC=0
(cd "$PROJ" && printf '\n## Via Stdin\n\nstdin body\n' | \
    "$EDIT_ISSUE" IM-100 --replace-body-file -) >/dev/null 2>&1 || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "0" "$RC" "PATH='-' reads stdin"
assert_file_contains "stdin body" "$STORE/IM/IM-100.md" \
    "stdin operand landed"

# ---------------------------------------------------------------------------
# Scenario 5: --if-unchanged-since composes as a whole-BODY etag.
# ---------------------------------------------------------------------------
echo
echo "[5] --if-unchanged-since: whole-body etag guard"

SHA_FRESH=$(git -C "$STORE" rev-parse HEAD)
RC=0
(cd "$PROJ" && "$EDIT_ISSUE" IM-100 --replace-body-file "$NEWBODY" \
    --if-unchanged-since "$SHA_FRESH") >/dev/null 2>&1 || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "0" "$RC" "fresh SHA -> replace lands"

# Frontmatter-only drift between ref and now must NOT trip the guard.
SHA_FM=$(git -C "$STORE" rev-parse HEAD)
(cd "$PROJ" && "$EDIT_ISSUE" IM-100 --set tags='[a]') >/dev/null 2>&1
RC=0
(cd "$PROJ" && "$EDIT_ISSUE" IM-100 --replace-body-file "$NEWBODY" \
    --if-unchanged-since "$SHA_FM" --set priority=high) \
    >/dev/null 2>&1 || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "0" "$RC" "frontmatter-only drift ignored by body etag"

# Body drift between ref and now -> exit 3, disk untouched.
SHA_STALE=$(git -C "$STORE" rev-parse HEAD)
(cd "$PROJ" && printf '\n## Sibling Win\n\nsibling content\n' | \
    "$EDIT_ISSUE" IM-100 --replace-body-file -) >/dev/null 2>&1
RC=0
STDERR=$(cd "$PROJ" && "$EDIT_ISSUE" IM-100 --replace-body-file "$NEWBODY" \
    --if-unchanged-since "$SHA_STALE" 2>&1 >/dev/null) || RC=$?
# COVERS: cli.edit-replace-body
assert_eq "3" "$RC" "stale SHA + body drift -> exit 3 concurrent-edit"
assert_contains "body changed since" "$STDERR" \
    "stderr names the whole-body drift"
assert_file_contains "sibling content" "$STORE/IM/IM-100.md" \
    "sibling's body survives the refused replace"

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
