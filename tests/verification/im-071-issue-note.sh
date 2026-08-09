#!/usr/bin/env bash
# Verification script for IM-071 issue-note convenience binary.
#
# Exercises the acceptance criteria that need real subprocesses and a
# real filesystem:
#   - Bare text append: produces `<today>: <text>` under ## Implementation Notes
#   - Stdin variant: positional `-` reads from stdin (trailing newline stripped)
#   - --date YYYY-MM-DD override produces back-dated note
#   - --section retargets to a non-default section
#   - Invalid --date format (e.g. 2026/01/01) exits 2 with clear message
#   - Invalid --date calendar (e.g. 2026-02-30) exits 2
#   - Missing issue file exits 1
#   - Missing section exits 1
#   - Empty text exits 2
#   - --json output is parseable and reports date/section/note
#   - Concurrent lock holder makes a contending issue-note exit 3
#   - --no-reindex suppresses INDEX.md regeneration; default writes it
#   - --version and --help exit 0
#
# Run from the project root:
#     tests/verification/im-071-issue-note.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE_NOTE="$(resolve_cli issue-note)"
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
        fail "$3: '$1' not found in: $2"
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

TMPDIR_ROOT=$(mktemp -d -t im071-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# Put the CLIs under test on PATH so reindex-issues (which issue-note
# auto-invokes through edit-issue's run_reindex) resolves correctly.
export PATH="$ISSUE_MGMT_BIN:$PATH"
# Hard-isolate from the caller's real store: legacy-mode sections walk
# from .issues/ and must not be rerouted to whatever the caller's shell
# exports. Section I sets up its own store via the .migrated-to-store
# sentinel and is unaffected.
unset ISSUE_STORE_ROOT

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
tags: []
changelog:
---

## Summary

Initial summary text.

## Implementation Notes

## Archived Plan Content
EOF
}

LEGACY="$TMPDIR_ROOT/legacy-project"
mkdir -p "$LEGACY/.issues"
echo "IM" > "$LEGACY/.issues/.prefix"

# ---------------------------------------------------------------------------
# Section A: bare text append (legacy mode)
# ---------------------------------------------------------------------------
echo
echo "[A] Bare text append"

make_issue "$LEGACY/.issues/IM-001.md" "IM-001"
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-001 "started work on IM-071" --no-reindex
)
# COVERS: cli.subcommand-surface
assert_file_contains "$TODAY: started work on IM-071" "$LEGACY/.issues/IM-001.md" \
    "bare text appended under Implementation Notes with today's date"
assert_file_contains "updated: $TODAY" "$LEGACY/.issues/IM-001.md" \
    "updated: field auto-stamps to today"
# Confirm the note landed under Implementation Notes, not somewhere else.
NOTES_BLOCK=$(awk '/## Implementation Notes/,/## Archived Plan Content/' "$LEGACY/.issues/IM-001.md")
assert_contains "$TODAY: started work on IM-071" "$NOTES_BLOCK" \
    "note line is inside ## Implementation Notes"
# Summary section must be unchanged.
assert_file_contains "Initial summary text." "$LEGACY/.issues/IM-001.md" "Summary preserved"

# ---------------------------------------------------------------------------
# Section B: stdin variant
# ---------------------------------------------------------------------------
echo
echo "[B] Stdin via '-'"

make_issue "$LEGACY/.issues/IM-002.md" "IM-002"
(
    cd "$LEGACY"
    printf 'note text via stdin\n' | "$ISSUE_NOTE" IM-002 - --no-reindex
)
assert_file_contains "$TODAY: note text via stdin" "$LEGACY/.issues/IM-002.md" \
    "stdin text appended with today's date"

# ---------------------------------------------------------------------------
# Section C: --date override
# ---------------------------------------------------------------------------
echo
echo "[C] --date override"

make_issue "$LEGACY/.issues/IM-003.md" "IM-003"
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-003 "back-dated note" --date 2026-04-15 --no-reindex
)
assert_file_contains "2026-04-15: back-dated note" "$LEGACY/.issues/IM-003.md" \
    "--date overrides the date prefix"

# ---------------------------------------------------------------------------
# Section D: --section override
# ---------------------------------------------------------------------------
echo
echo "[D] --section override"

make_issue "$LEGACY/.issues/IM-004.md" "IM-004"
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-004 "summary update" --section Summary --no-reindex
)
SUMMARY_BLOCK=$(awk '/## Summary/,/## Implementation Notes/' "$LEGACY/.issues/IM-004.md")
assert_contains "$TODAY: summary update" "$SUMMARY_BLOCK" \
    "--section retargets to Summary"
# Implementation Notes must NOT carry the new line.
NOTES_BLOCK=$(awk '/## Implementation Notes/,/## Archived Plan Content/' "$LEGACY/.issues/IM-004.md")
if echo "$NOTES_BLOCK" | grep -qF "summary update"; then
    fail "--section retarget leaked into ## Implementation Notes"
else
    pass "--section retarget keeps Implementation Notes untouched"
fi

# ---------------------------------------------------------------------------
# Section E: error paths
# ---------------------------------------------------------------------------
echo
echo "[E] Error paths"

# Invalid --date format
RC=0
STDERR=$(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-001 "x" --date 2026/01/01 --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "invalid --date format exits 2"
assert_contains "YYYY-MM-DD" "$STDERR" "stderr names YYYY-MM-DD format"

# Invalid --date calendar value
RC=0
STDERR=$(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-001 "x" --date 2026-02-30 --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "invalid calendar date exits 2"

# Missing issue file
RC=0
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-999 "x" --no-reindex 2>/dev/null
) || RC=$?
assert_eq "1" "$RC" "missing issue file exits 1"

# Missing section
RC=0
STDERR=$(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-001 "x" --section Bogus --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "1" "$RC" "missing section exits 1"
assert_contains "section not found" "$STDERR" "stderr names missing section"

# Missing text positional
RC=0
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-001 --no-reindex 2>/dev/null
) || RC=$?
assert_eq "2" "$RC" "missing text positional exits 2"

# Empty stdin text
RC=0
(
    cd "$LEGACY"
    printf '' | "$ISSUE_NOTE" IM-001 - --no-reindex 2>/dev/null
) || RC=$?
assert_eq "2" "$RC" "empty stdin text exits 2"

# Bad ID format
RC=0
(
    cd "$LEGACY"
    "$ISSUE_NOTE" notanid "x" --no-reindex 2>/dev/null
) || RC=$?
assert_eq "2" "$RC" "bad ID format exits 2"

# IM-174 section-escape guard: a multi-line note carrying a `## ` line
# would reparse as a new top-level section and fragment the file.
E_BEFORE=$(cat "$LEGACY/.issues/IM-001.md")
RC=0
STDERR=$(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-001 $'progress so far\n## escaped heading' \
        --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-error-contracts
assert_eq "2" "$RC" "IM-174 note with ## line exits 2"
assert_contains "would escape the level-2 section" "$STDERR" \
    "IM-174 stderr names the escape"
E_AFTER=$(cat "$LEGACY/.issues/IM-001.md")
assert_eq "$E_BEFORE" "$E_AFTER" "IM-174 rejected note left the file untouched"

# IM-174 positive path: ### nests fine
RC=0
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-001 $'detail below\n### sub-point' --no-reindex \
        >/dev/null 2>&1
) || RC=$?
assert_eq "0" "$RC" "IM-174 note with ### accepted"

# ---------------------------------------------------------------------------
# Section F: --json output
# ---------------------------------------------------------------------------
echo
echo "[F] --json output"

make_issue "$LEGACY/.issues/IM-005.md" "IM-005"
JSON=$(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-005 "json check" --json --no-reindex
)
if python3 -c "import json,sys; json.loads(sys.stdin.read())" <<< "$JSON" 2>/dev/null; then
    pass "--json output is parseable"
else
    fail "--json output is not parseable: $JSON"
fi
assert_contains "\"date\": \"$TODAY\"" "$JSON" "--json reports date"
assert_contains "\"section\": \"Implementation Notes\"" "$JSON" "--json reports section"
assert_contains "\"note\": \"$TODAY: json check\"" "$JSON" "--json reports full note payload"
assert_contains "\"op\": \"append\"" "$JSON" "--json reports section op = append"

# ---------------------------------------------------------------------------
# Section G: concurrent-edit (lock contention)
# ---------------------------------------------------------------------------
echo
echo "[G] Concurrent-edit"

make_issue "$LEGACY/.issues/IM-006.md" "IM-006"
LOCK="$LEGACY/.issues/.locks/IM-006.lock"
mkdir -p "$LEGACY/.issues/.locks"

# Background holder holds the lock for 3s.
(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 3"
) &
HOLDER_PID=$!

sleep 0.3

RC=0
STDERR=$(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-006 "contended" --lock-timeout 0.5 --no-reindex 2>&1 >/dev/null
) || RC=$?

wait $HOLDER_PID 2>/dev/null

assert_eq "3" "$RC" "contender exits 3 on lock timeout"
assert_contains "concurrent-edit" "$STDERR" "stderr names concurrent-edit"

# After release, the next call succeeds.
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-006 "after release" --no-reindex
)
assert_file_contains "$TODAY: after release" "$LEGACY/.issues/IM-006.md" \
    "issue-note succeeds after lock holder releases"

# ---------------------------------------------------------------------------
# Section H: auto-reindex
# ---------------------------------------------------------------------------
echo
echo "[H] Auto-reindex"

make_issue "$LEGACY/.issues/IM-007.md" "IM-007"
rm -f "$LEGACY/.issues/INDEX.md"
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-007 "with reindex"
)
if [ -f "$LEGACY/.issues/INDEX.md" ]; then
    pass "default mode auto-reindexes (INDEX.md created)"
else
    fail "default mode did not create INDEX.md"
fi

make_issue "$LEGACY/.issues/IM-008.md" "IM-008"
rm -f "$LEGACY/.issues/INDEX.md"
(
    cd "$LEGACY"
    "$ISSUE_NOTE" IM-008 "skip reindex" --no-reindex
)
if [ -f "$LEGACY/.issues/INDEX.md" ]; then
    fail "--no-reindex did not suppress INDEX.md regeneration"
else
    pass "--no-reindex suppresses INDEX.md regeneration"
fi

# ---------------------------------------------------------------------------
# Section I: store mode (with auto-commit)
# ---------------------------------------------------------------------------
echo
echo "[I] Store mode (auto-commit)"

STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM"
(
    cd "$STORE"
    git init -q
    git config user.email test@example.com
    git config user.name test
    printf 'storeproj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
make_issue "$STORE/IM/IM-100.md" "IM-100"
(
    cd "$STORE"
    git add IM/IM-100.md
    git commit -q -m "create IM-100"
)

PROJ="$TMPDIR_ROOT/storeproj"
mkdir -p "$PROJ/.issues"
echo "IM" > "$PROJ/.issues/.prefix"
echo "$STORE" > "$PROJ/.issues/.migrated-to-store"

(
    cd "$PROJ"
    "$ISSUE_NOTE" IM-100 "store mode note"
)
assert_file_contains "$TODAY: store mode note" "$STORE/IM/IM-100.md" \
    "store mode appends note"

# Last commit message should be `note IM-100`.
LAST_MSG=$(git -C "$STORE" log -1 --format=%s)
assert_eq "note IM-100" "$LAST_MSG" "store mode commit message is 'note <id>'"

# ---------------------------------------------------------------------------
# Section J: help/version
# ---------------------------------------------------------------------------
echo
echo "[J] Help and version"

RC=0
"$ISSUE_NOTE" --help >/dev/null || RC=$?
assert_eq "0" "$RC" "--help exits 0"

RC=0
VOUT=$("$ISSUE_NOTE" --version) || RC=$?
assert_eq "0" "$RC" "--version exits 0"
assert_contains "v" "$VOUT" "--version output begins with a v-prefixed string"

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
