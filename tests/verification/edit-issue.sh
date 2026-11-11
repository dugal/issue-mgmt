#!/usr/bin/env bash
# Verification script for the edit-issue subcommand.
#
# Exercises the acceptance criteria that need real subprocesses and a
# real filesystem:
#   - Frontmatter --set in legacy and store modes
#   - 'updated' field auto-stamps to today's date
#   - Round-trip preserves unrelated frontmatter and body byte-for-byte
#   - Section append leaves adjacent sections untouched
#   - Section replace and prepend ordering
#   - Guarded fields (status, id, created) refuse with exit 2 and clear
#     stderr naming the owning command
#   - Missing issue file exits 1
#   - Bad arguments exit 2
#   - Concurrent lock holder makes a contending edit-issue exit 3 with
#     'concurrent-edit' in stderr
#   - Auto-reindex by default; --no-reindex suppresses it
#   - --json output is parseable and reports the changes
#
# Run from the project root:
#     tests/verification/edit-issue.sh

set -u

# Sentinel-resolution sections (G, parts of E) build temp stores and rely on
# the sentinel-based store-root precedence. Unset any inherited
# ISSUE_STORE_ROOT from the caller's shell so it does not silently win the
# resolution ladder (--store ROOT -> ISSUE_STORE_ROOT -> sentinel -> ...).
# Sections that legitimately use the env var set it per-invocation.
unset ISSUE_STORE_ROOT

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
EDIT_ISSUE="$(resolve_cli edit-issue)"
CONCURRENCY_PY="$PROJECT_ROOT/lib/concurrency.py"  # Tier 2: stack-coupled test adversary; see docs/design/concurrency-test-hook.md
REINDEX="$(resolve_cli reindex-issues)"

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
    # assert_contains <needle> <haystack> <label>
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
        fail "$3: '$1' not in $2:"
        sed -n '1,40p' "$2"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im023-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# Put the CLI dir under test on PATH so edit-issue's auto-reindex can
# find reindex-issues (which it invokes by relative sibling lookup).
export PATH="$ISSUE_MGMT_BIN:$PATH"

TODAY=$(date +%Y-%m-%d)

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

First paragraph.

Second paragraph.

## Implementation Notes

## Archived Plan Content
EOF
}

# ---------------------------------------------------------------------------
# Section A: legacy mode (.issues/ in tempdir)
# ---------------------------------------------------------------------------
echo
echo "[A] Legacy mode"

LEGACY="$TMPDIR_ROOT/legacy-project"
mkdir -p "$LEGACY/.issues"
echo "IM" > "$LEGACY/.issues/.prefix"
make_issue "$LEGACY/.issues/IM-001.md" "IM-001"

(
    cd "$LEGACY"
    RC=0
    "$EDIT_ISSUE" IM-001 --set priority=medium --no-reindex || RC=$?
    if [ "$RC" -ne 0 ]; then
        echo "FAIL: legacy --set returned $RC"
        exit 1
    fi
)
# COVERS: cli.subcommand-surface
assert_file_contains "priority: medium" "$LEGACY/.issues/IM-001.md" "legacy --set updates field"
assert_file_contains "updated: $TODAY" "$LEGACY/.issues/IM-001.md" "legacy --set auto-stamps updated"
# Unrelated fields preserved byte-for-byte:
# COVERS: cli.frontmatter-byte-preservation
assert_file_contains "title: \"Sample issue\"" "$LEGACY/.issues/IM-001.md" "title preserved"
assert_file_contains "tags: [a, b]" "$LEGACY/.issues/IM-001.md" "tags preserved"
assert_file_contains "First paragraph." "$LEGACY/.issues/IM-001.md" "body preserved"

# Quoted-empty field preserves its quotes when set
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-001 --set branch=phase-test --no-reindex
)
assert_file_contains 'branch: "phase-test"' "$LEGACY/.issues/IM-001.md" "quoted-empty preserves quotes"

# Inline comment line preserves its comment after setting a value
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-001 --set changelog=skip --no-reindex
)
assert_file_contains "changelog: skip   # set by /update-changelog -- do not edit manually" \
    "$LEGACY/.issues/IM-001.md" "inline comment preserved across --set"

# ---------------------------------------------------------------------------
# Section B: section editing
# ---------------------------------------------------------------------------
echo
echo "[B] Section editing"

make_issue "$LEGACY/.issues/IM-002.md" "IM-002"

(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-002 --section "Implementation Notes" --append \
        "2026-05-13: started work" --no-reindex
)
assert_file_contains "2026-05-13: started work" "$LEGACY/.issues/IM-002.md" \
    "section append inserts text"
# Confirm adjacent headings are intact and order is preserved
ADJACENT=$(awk '/## Implementation Notes/,/## Archived Plan Content/' \
    "$LEGACY/.issues/IM-002.md")
assert_contains "## Implementation Notes" "$ADJACENT" "Implementation Notes heading intact"
assert_contains "## Archived Plan Content" "$ADJACENT" "Archived Plan Content heading intact"
assert_contains "2026-05-13: started work" "$ADJACENT" "appended text is inside section"

# Replace operation
make_issue "$LEGACY/.issues/IM-003.md" "IM-003"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-003 --section Summary --replace "Brand new summary." --no-reindex
)
assert_file_contains "Brand new summary." "$LEGACY/.issues/IM-003.md" "section replace inserts text"
if grep -qF "First paragraph." "$LEGACY/.issues/IM-003.md"; then
    fail "section replace did not remove old content"
else
    pass "section replace removed old content"
fi

# Prepend operation
make_issue "$LEGACY/.issues/IM-004.md" "IM-004"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-004 --section Summary --prepend "Top line." --no-reindex
)
PREPEND_BODY=$(awk '/## Summary/,/## Implementation Notes/' "$LEGACY/.issues/IM-004.md")
TOP_POS=$(echo "$PREPEND_BODY" | grep -n "Top line." | head -1 | cut -d: -f1)
ORIG_POS=$(echo "$PREPEND_BODY" | grep -n "First paragraph." | head -1 | cut -d: -f1)
if [ -n "$TOP_POS" ] && [ -n "$ORIG_POS" ] && [ "$TOP_POS" -lt "$ORIG_POS" ]; then
    pass "prepend places text before original content"
else
    fail "prepend ordering: top_pos=$TOP_POS orig_pos=$ORIG_POS"
fi

# ---------------------------------------------------------------------------
# Section C: error paths
# ---------------------------------------------------------------------------
echo
echo "[C] Error paths"

# Guarded fields -- status
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-001 --set status=done --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "guarded field 'status' exits 2"
# COVERS: surface.self-naming
# The owning command must be named in its CURRENT form. This assertion pinned
# the retired flat name `transition-issue` through v3.3.1, encoding a
# surface.self-naming violation as intended behavior (IM-254 / IM-239).
assert_contains "issue transition" "$STDERR" "stderr names owning command for status"

# Guarded fields -- id
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-001 --set id=IM-999 --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "guarded field 'id' exits 2"

# Guarded fields -- created
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-001 --set created=1999-01-01 --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "guarded field 'created' exits 2"

# Missing issue file
RC=0
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-999 --set priority=low --no-reindex 2>/dev/null
) || RC=$?
assert_eq "1" "$RC" "missing issue file exits 1"

# Bad ID format
RC=0
(
    cd "$LEGACY"
    "$EDIT_ISSUE" notanid --set priority=low --no-reindex 2>/dev/null
) || RC=$?
assert_eq "2" "$RC" "bad ID format exits 2"

# Nothing to do
RC=0
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-001 --no-reindex 2>/dev/null
) || RC=$?
assert_eq "2" "$RC" "nothing-to-do exits 2"

# Section not found
RC=0
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-001 --section "Bogus Section" --append "x" --no-reindex 2>/dev/null
) || RC=$?
assert_eq "1" "$RC" "section-not-found exits 1"

# ---------------------------------------------------------------------------
# Section D: concurrent-edit (lock contention)
# ---------------------------------------------------------------------------
echo
echo "[D] Concurrent-edit"

make_issue "$LEGACY/.issues/IM-005.md" "IM-005"
LOCK="$LEGACY/.issues/.locks/IM-005.lock"
mkdir -p "$LEGACY/.issues/.locks"

# Background holder: keeps the lock for 3s.
(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 3"
) &
HOLDER_PID=$!

# Give the holder time to acquire
sleep 0.3

RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-005 --set priority=low --lock-timeout 0.5 --no-reindex 2>&1 >/dev/null
) || RC=$?

wait $HOLDER_PID 2>/dev/null

assert_eq "3" "$RC" "contender exits 3 on lock timeout"
assert_contains "concurrent-edit" "$STDERR" "stderr includes 'concurrent-edit'"

# After the holder releases, a subsequent edit succeeds
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-005 --set priority=low --no-reindex
)
assert_file_contains "priority: low" "$LEGACY/.issues/IM-005.md" "edit succeeds after holder releases"

# ---------------------------------------------------------------------------
# Section E: store mode (ISSUE_STORE_ROOT)
# ---------------------------------------------------------------------------
echo
echo "[E] Store mode"

STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM"
# Store must be a git repo before any auto-commit mutation
# runs. Without this, edit-issue's store_mutation context fails at
# sweep-entry time with "fatal: not a git repository" and no write lands.
(
    cd "$STORE"
    git init -q
    git config user.email "$FIXTURE_EMAIL"
    git config user.name test
    git commit -q --allow-empty -m "init store"
)
make_issue "$STORE/IM/IM-006.md" "IM-006"

# Run from outside any .issues/ project so legacy walking does not bias resolution
(
    cd "$TMPDIR_ROOT"
    ISSUE_STORE_ROOT="$STORE" "$EDIT_ISSUE" IM-006 --set priority=medium --no-reindex
)
assert_file_contains "priority: medium" "$STORE/IM/IM-006.md" "store mode --set updates file"
# Lock path should sit under the store, not the (nonexistent) legacy anchor
if [ -d "$STORE/IM/.locks" ]; then
    pass "store mode created per-prefix .locks/ dir"
else
    fail "store mode did not create $STORE/IM/.locks"
fi

# ---------------------------------------------------------------------------
# Section F: --json output
# ---------------------------------------------------------------------------
echo
echo "[F] --json output"

make_issue "$LEGACY/.issues/IM-007.md" "IM-007"
JSON=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-007 --set priority=low --json --no-reindex
)
# Parseable
if python3 -c "import json,sys; json.loads(sys.stdin.read())" <<< "$JSON" 2>/dev/null; then
    # COVERS: output.json-stable
    pass "--json output is parseable"
else
    fail "--json output is not parseable: $JSON"
fi
assert_contains "\"field\": \"priority\"" "$JSON" "--json names the field"
assert_contains "\"new\": \"low\"" "$JSON" "--json reports new value"
assert_contains "\"old\": \"high\"" "$JSON" "--json reports old value"

# ---------------------------------------------------------------------------
# Section G: auto-reindex
# ---------------------------------------------------------------------------
echo
echo "[G] Auto-reindex"

# Default behaviour: a successful edit refreshes INDEX.md.
make_issue "$LEGACY/.issues/IM-008.md" "IM-008"
rm -f "$LEGACY/.issues/INDEX.md"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-008 --set priority=low
)
if [ -f "$LEGACY/.issues/INDEX.md" ]; then
    pass "default mode auto-reindexes (INDEX.md created)"
else
    fail "default mode did not create INDEX.md"
fi

# regression: the row for IM-008 in INDEX.md must reflect the
# just-edited value (priority=low), not the pre-edit value (priority=high
# from make_issue's template). Previously the INDEX could be regenerated
# stale, omitting or misrepresenting the edited row.
if [ -f "$LEGACY/.issues/INDEX.md" ]; then
    im008_row=$(grep -F "| IM-008 " "$LEGACY/.issues/INDEX.md" || true)
    if echo "$im008_row" | grep -qE '\| (low) \|'; then
        pass "regression: edited priority=low surfaces in INDEX row (legacy)"
    else
        fail "regression: IM-008 INDEX row does not show priority=low: '$im008_row'"
        echo "--- INDEX.md content ---"
        cat "$LEGACY/.issues/INDEX.md"
        echo "------------------------"
    fi
fi

# --no-reindex suppresses
make_issue "$LEGACY/.issues/IM-009.md" "IM-009"
rm -f "$LEGACY/.issues/INDEX.md"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-009 --set priority=low --no-reindex
)
if [ -f "$LEGACY/.issues/INDEX.md" ]; then
    fail "--no-reindex did not suppress INDEX.md regeneration"
else
    pass "--no-reindex suppresses INDEX.md regeneration"
fi

# regression in store mode: edit-issue against a real git-managed
# store must update both per-prefix and root INDEX with the new value.
STORE_AR="$TMPDIR_ROOT/store-autoreindex"
mkdir -p "$STORE_AR/IM"
(
    cd "$STORE_AR"
    git init -q
    git config user.email "$FIXTURE_EMAIL"
    git config user.name test
    printf 'autoreindex-proj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
make_issue "$STORE_AR/IM/IM-300.md" "IM-300"
PROJ_AR="$TMPDIR_ROOT/autoreindex-proj"
mkdir -p "$PROJ_AR/.issues"
echo "IM" > "$PROJ_AR/.issues/.prefix"
echo "$STORE_AR" > "$PROJ_AR/.issues/.migrated-to-store"
(
    cd "$PROJ_AR"
    "$EDIT_ISSUE" IM-300 --set priority=low
)
if [ -f "$STORE_AR/IM/INDEX.md" ] && [ -f "$STORE_AR/INDEX.md" ]; then
    prefix_row=$(grep -F "| IM-300 " "$STORE_AR/IM/INDEX.md" || true)
    if echo "$prefix_row" | grep -qE '\| (low) \|'; then
        pass "regression: edit surfaces in <store>/IM/INDEX.md row"
    else
        fail "regression: <store>/IM/INDEX.md row missing priority=low: '$prefix_row'"
    fi
    root_row=$(grep -F "| IM-300 " "$STORE_AR/INDEX.md" || true)
    if echo "$root_row" | grep -qE '\| (low) \|'; then
        pass "regression: edit surfaces in <store>/INDEX.md aggregate row"
    else
        fail "regression: <store>/INDEX.md aggregate row missing priority=low: '$root_row'"
    fi
else
    fail "store mode auto-reindex did not create both INDEX files"
fi

# ---------------------------------------------------------------------------
# Section G.5: regression
#
# --no-reindex against a store whose per-prefix INDEX.md does not exist
# must still exit 0 and produce a commit. Pre-fix, commit_after_write
# unconditionally included the missing INDEX.md paths in `git add` /
# `git commit`, which aborted with "pathspec did not match any files"
# even though the issue file's atomic write had already landed.
# Post-fix, missing INDEX.md paths are silently dropped from the
# commit set; the issue file (the only must-exist path) is unaffected.
# ---------------------------------------------------------------------------
echo
echo "[G.5] IM-089: --no-reindex tolerates missing per-prefix INDEX.md"

STORE_NORX="$TMPDIR_ROOT/store-no-reindex"
mkdir -p "$STORE_NORX/IM"
(
    cd "$STORE_NORX"
    git init -q
    git config user.email "$FIXTURE_EMAIL"
    git config user.name test
    printf 'noreindex-proj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
make_issue "$STORE_NORX/IM/IM-400.md" "IM-400"
(
    cd "$STORE_NORX"
    git add IM/IM-400.md
    git commit -q -m "create IM-400"
)
# Pre-condition: neither <store>/INDEX.md nor <store>/IM/INDEX.md exists.
if [ -f "$STORE_NORX/INDEX.md" ] || [ -f "$STORE_NORX/IM/INDEX.md" ]; then
    fail "IM-089 fixture pre-condition: INDEX files unexpectedly exist"
fi

PROJ_NORX="$TMPDIR_ROOT/noreindex-proj"
mkdir -p "$PROJ_NORX/.issues"
echo "IM" > "$PROJ_NORX/.issues/.prefix"
echo "$STORE_NORX" > "$PROJ_NORX/.issues/.migrated-to-store"

RC=0
ERR_FILE_NORX="$TMPDIR_ROOT/err.norx"
(
    cd "$PROJ_NORX"
    "$EDIT_ISSUE" IM-400 --set priority=low --no-reindex 2>"$ERR_FILE_NORX"
) || RC=$?
# COVERS: cli.auto-reindex
assert_eq "0" "$RC" "edit-issue --no-reindex against fresh prefix exits 0"
assert_file_contains "priority: low" "$STORE_NORX/IM/IM-400.md" \
    "issue file mutation persisted"
if [ -s "$ERR_FILE_NORX" ]; then
    fail "edit-issue --no-reindex emitted stderr on success:"
    sed -n '1,10p' "$ERR_FILE_NORX"
else
    pass "edit-issue --no-reindex emitted no stderr"
fi

# Commit landed: HEAD subject is `edit IM-400` and only IM/IM-400.md is in it.
HEAD_SUBJ=$(git -C "$STORE_NORX" log -1 --format=%s)
assert_eq "edit IM-400" "$HEAD_SUBJ" "HEAD subject is 'edit IM-400'"
HEAD_FILES=$(git -C "$STORE_NORX" show --name-only --format= HEAD)
NUM_FILES=$(printf '%s\n' "$HEAD_FILES" | grep -c .)
assert_eq "1" "$NUM_FILES" "HEAD commit touches exactly one file"
if printf '%s\n' "$HEAD_FILES" | grep -qx "IM/IM-400.md"; then
    pass "HEAD commit includes IM/IM-400.md (the issue file)"
else
    fail "HEAD commit missing IM/IM-400.md; got: $HEAD_FILES"
fi
if printf '%s\n' "$HEAD_FILES" | grep -qE '(^|/)INDEX\.md$'; then
    fail "HEAD commit unexpectedly includes INDEX.md under --no-reindex"
else
    pass "HEAD commit does not include any INDEX.md path"
fi

# Post-condition: INDEX files still do not exist on disk (the commit
# did not synthesize them, and reindex was suppressed).
if [ -f "$STORE_NORX/INDEX.md" ] || [ -f "$STORE_NORX/IM/INDEX.md" ]; then
    fail "INDEX files appeared despite --no-reindex"
else
    pass "INDEX files still absent after --no-reindex edit"
fi

# A second --no-reindex edit on the same fresh-prefix store still works,
# i.e. the prior commit did not leave the index in a state that would
# trip the second one.
RC=0
(
    cd "$PROJ_NORX"
    "$EDIT_ISSUE" IM-400 --set priority=medium --no-reindex
) || RC=$?
assert_eq "0" "$RC" "second consecutive --no-reindex edit exits 0"
assert_file_contains "priority: medium" "$STORE_NORX/IM/IM-400.md" \
    "second edit mutation persisted"

# ---------------------------------------------------------------------------
# Section H: help/version
# ---------------------------------------------------------------------------
echo
echo "[H] Help and version"

RC=0
"$EDIT_ISSUE" --help >/dev/null || RC=$?
assert_eq "0" "$RC" "--help exits 0"

RC=0
"$EDIT_ISSUE" --version >/dev/null || RC=$?
assert_eq "0" "$RC" "--version exits 0"

# ---------------------------------------------------------------------------
# Section I: interpret_section_actions argparse error contracts
#
# Each malformed argv shape below is a user-facing CLI error contract:
# ValueError from interpret_section_actions is mapped to exit 2 in main()
# (scripts/edit-issue lines 892-894). The diagnostic substrings asserted
# below are pulled verbatim from the raise sites in scripts/edit-issue
# (lines 398-477, plus _check_no_existing_position at 483-493 and
# _collect_position at 496-509).
# ---------------------------------------------------------------------------
echo
echo "[I] Argparse / interpret_section_actions error contracts"

# Fresh fixture for the error-path cases (the file is never written)
make_issue "$LEGACY/.issues/IM-100.md" "IM-100"

# Reusable error-case runner: runs edit-issue with the given argv tail,
# asserts exit code and stderr substring.
#
# Usage: assert_edit_error <label> <expected_rc> <stderr_substring> -- <argv...>
assert_edit_error() {
    local label="$1"; shift
    local expected_rc="$1"; shift
    local needle="$1"; shift
    # consume the literal '--' separator before argv
    if [ "$1" != "--" ]; then
        fail "$label: assert_edit_error usage bug (missing --)"
        return
    fi
    shift
    local rc=0
    local stderr
    stderr=$(
        cd "$LEGACY"
        "$EDIT_ISSUE" IM-100 --no-reindex "$@" 2>&1 >/dev/null
    ) || rc=$?
    assert_eq "$expected_rc" "$rc" "$label exits $expected_rc"
    assert_contains "$needle" "$stderr" "$label stderr names cause"
}

# COVERS: cli.edit-error-contracts
# 1. --append without a preceding --section anchor
assert_edit_error "I.1 --append no anchor" 2 \
    "--append requires a preceding --section" \
    -- --append "text"

# COVERS: cli.edit-error-contracts
# 2. --prepend without anchor
assert_edit_error "I.2 --prepend no anchor" 2 \
    "--prepend requires a preceding --section" \
    -- --prepend "text"

# COVERS: cli.edit-error-contracts
# 3. --replace without anchor
assert_edit_error "I.3 --replace no anchor" 2 \
    "--replace requires a preceding --section" \
    -- --replace "text"

# COVERS: cli.edit-error-contracts
# 4. --append-file without anchor (file does not need to exist; parse
#    rejects the missing anchor before _read_op_file is called).
assert_edit_error "I.4 --append-file no anchor" 2 \
    "--append-file requires a preceding --section" \
    -- --append-file "/tmp/im114-nope-$$"

# COVERS: cli.edit-error-contracts
# 5. --prepend-file without anchor
assert_edit_error "I.5 --prepend-file no anchor" 2 \
    "--prepend-file requires a preceding --section" \
    -- --prepend-file "/tmp/im114-nope-$$"

# COVERS: cli.edit-error-contracts
# 6. --replace-file without anchor
assert_edit_error "I.6 --replace-file no anchor" 2 \
    "--replace-file requires a preceding --section" \
    -- --replace-file "/tmp/im114-nope-$$"

# COVERS: cli.edit-error-contracts
# 7a. --at-end without --add-section/--move-section anchor
assert_edit_error "I.7a --at-end no anchor" 2 \
    "--at-end requires a preceding --add-section or --move-section" \
    -- --at-end

# COVERS: cli.edit-error-contracts
# 7b. --after without --add-section/--move-section anchor
assert_edit_error "I.7b --after no anchor" 2 \
    "--after requires a preceding --add-section or --move-section" \
    -- --after "Summary"

# COVERS: cli.edit-error-contracts
# 7c. --before without --add-section/--move-section anchor
assert_edit_error "I.7c --before no anchor" 2 \
    "--before requires a preceding --add-section or --move-section" \
    -- --before "Summary"

# COVERS: cli.edit-error-contracts
# 7d. --to-position without --add-section/--move-section anchor
assert_edit_error "I.7d --to-position no anchor" 2 \
    "--to-position requires a preceding --add-section or --move-section" \
    -- --to-position 1

# COVERS: cli.edit-error-contracts
# 8. --body without --add-section anchor (NB: --body only attaches to add;
#    placing it under --section returns the same _require_anchor_for error).
assert_edit_error "I.8 --body no anchor" 2 \
    "--body requires a preceding --add-section" \
    -- --body "text"

# COVERS: cli.edit-error-contracts
# 9. Duplicate body op on same --section (--append then --replace)
assert_edit_error "I.9 duplicate body op" 2 \
    "already has a body op" \
    -- --section "Summary" --append "A" --replace "B"

# COVERS: cli.edit-error-contracts
# 10. Duplicate position flag on one anchor (--after X --before Y)
assert_edit_error "I.10 duplicate position" 2 \
    "position already set by --after" \
    -- --add-section "New" --after "Summary" --before "Implementation Notes"

# COVERS: cli.edit-error-contracts
# 11. --section NAME with no body op (close-anchor enforces one is required)
assert_edit_error "I.11 --section no op" 2 \
    "requires one of --append/--prepend/--replace TEXT" \
    -- --section "Summary"

# COVERS: cli.edit-error-contracts
# 12. --move-section NAME with no position (close-anchor enforces require=True)
assert_edit_error "I.12 --move-section no position" 2 \
    "requires one of --after/--before/--to-position" \
    -- --move-section "Summary"

# COVERS: cli.edit-error-contracts
# 13. --append-file - after stdin already consumed by another --*-file -
#     Stdin must carry at least one byte so the first --append-file consumes
#     it cleanly before the second flag fires its raise. Two seeded sections
#     in the fixture: "Summary" and "Implementation Notes".
RC=0
STDERR=$(
    cd "$LEGACY"
    echo "data" | "$EDIT_ISSUE" IM-100 --no-reindex \
        --section "Summary" --append-file - \
        --section "Implementation Notes" --prepend-file - \
        2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "I.13 stdin double-consume exits 2"
assert_contains "stdin already consumed by --append-file" "$STDERR" \
    "I.13 stderr names original stdin consumer"

# ---------------------------------------------------------------------------
# Section J: IM-174 section-escape guard
#
# Body ops refuse (exit 2, argv/usage contract) a body containing a
# markdown header at or above the target section's level (`# ` / `## `),
# which would reparse as a NEW top-level section and silently fragment
# the file. The check is fence-naive, matching find_section. `###`+
# nests fine and passes.
# ---------------------------------------------------------------------------
echo
echo "[J] IM-174 section-escape guard"

make_issue "$LEGACY/.issues/IM-174.md" "IM-174"
J_BEFORE=$(cat "$LEGACY/.issues/IM-174.md")

j_reject() {
    # j_reject <label> -- <argv...>  : expects exit 2 + guard stderr
    local label="$1"; shift; shift  # consume label and '--'
    local rc=0
    local stderr
    stderr=$(
        cd "$LEGACY"
        "$EDIT_ISSUE" IM-174 --no-reindex "$@" 2>&1 >/dev/null
    ) || rc=$?
    # COVERS: cli.edit-error-contracts
    assert_eq "2" "$rc" "$label exits 2"
    assert_contains "would escape the level-2 section" "$stderr" \
        "$label stderr names the escape"
}

# 1. Inline --append with a ## header (the archive-workflow shape)
j_reject "J.1 --append ## body" -- \
    --section "Archived Plan Content" --append "## phase-1: foo"

# 2. Inline --replace with an # h1
j_reject "J.2 --replace # body" -- \
    --section "Summary" --replace "# Summary"

# 3. --prepend with a ## header mid-body
j_reject "J.3 --prepend mid-body ##" -- \
    --section "Summary" --prepend $'intro line\n## escaped'

# 4. --append-file variant
printf '## phase-1: foo\n\n- [x] item\n' > "$TMPDIR_ROOT/im174-block.md"
j_reject "J.4 --append-file ## body" -- \
    --section "Archived Plan Content" --append-file "$TMPDIR_ROOT/im174-block.md"

# 5. Fence-naive: a ## line inside a code fence still fragments (the
#    section parser does not track fences), so it is also refused.
printf '```markdown\n## fenced\n```\n' > "$TMPDIR_ROOT/im174-fenced.md"
j_reject "J.5 fenced ## still refused" -- \
    --section "Summary" --replace-file "$TMPDIR_ROOT/im174-fenced.md"

# 6. --add-section --body with a ## header (same fragmentation vector)
j_reject "J.6 --add-section --body ##" -- \
    --add-section "Extra" --body "## nope"

# 7. Guard message suggests demotion
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-174 --no-reindex \
        --section "Summary" --append "## x" 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-error-contracts
assert_contains "demote" "$STDERR" "J.7 stderr suggests demoting the header"

# 8. No write happened across all rejects: file is byte-identical
J_AFTER=$(cat "$LEGACY/.issues/IM-174.md")
assert_eq "$J_BEFORE" "$J_AFTER" "J.8 rejected ops left the file untouched"

# 9. ###-and-deeper headers nest fine and pass (positive path)
RC=0
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-174 --no-reindex \
        --section "Archived Plan Content" --append $'### phase-1: foo\n\n- [x] item' \
        >/dev/null 2>&1
) || RC=$?
assert_eq "0" "$RC" "J.9 ### body accepted"
assert_file_contains "### phase-1: foo" "$LEGACY/.issues/IM-174.md" \
    "J.9 ### content landed in the section"

# 10. Inline '##' mid-line (not at line start) passes
RC=0
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-174 --no-reindex \
        --section "Implementation Notes" --append "see the ## marker inline" \
        >/dev/null 2>&1
) || RC=$?
assert_eq "0" "$RC" "J.10 mid-line ## accepted"

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
