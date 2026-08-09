#!/usr/bin/env bash
# Verification script for the transition-issue subcommand.
#
# Exercises the acceptance criteria that need real subprocesses and a
# real filesystem:
#   - Legal transitions update status, branch, updated, and append a
#     timestamped note in ## Implementation Notes
#   - Illegal transitions exit 2 with stderr listing the legal next
#     states from the current state
#   - --reason required for transitions to 'blocked' (exit 2 without)
#   - Same-state transitions are no-op (exit 0, no mtime change)
#   - --branch sets the field; --branch "" clears it
#   - Concurrent lock holder makes a contender exit 3 with
#     'concurrent-edit' in stderr
#   - Auto-reindex by default; --no-reindex suppresses
#   - --json emits a parseable summary
#   - Store mode resolves under ISSUE_STORE_ROOT
#   - --help / --version
#
# Run from the project root:
#     tests/verification/transition-issue.sh

set -u

# Sentinel-resolution sections (H, parts of F) build temp stores and rely on
# the sentinel-based store-root precedence. Unset any inherited
# ISSUE_STORE_ROOT from the caller's shell so it does not silently win the
# resolution ladder (--store ROOT -> ISSUE_STORE_ROOT -> sentinel -> ...).
# Sections that legitimately use the env var set it per-invocation.
unset ISSUE_STORE_ROOT

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
TRANSITION="$(resolve_cli transition-issue)"
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
        fail "$3: '$1' not in $2:"
        sed -n '1,40p' "$2"
    fi
}

# Portable fractional mtime probe. BSD/macOS `stat -f %Fm` and GNU
# `stat -c %Y` are incompatible (and `stat -f` on GNU means --file-system,
# silently returning garbage rather than erroring), so use python3 -- a hard
# dependency of this project -- for a single portable, sub-second reading.
file_mtime() {
    python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_mtime)' "$1"
}

TMPDIR_ROOT=$(mktemp -d -t im024-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

export PATH="$ISSUE_MGMT_BIN:$PATH"

TODAY=$(date +%Y-%m-%d)

make_issue() {
    # make_issue <path> <id> <status>
    cat > "$1" <<EOF
---
id: $2
title: "Sample issue"
type: feature
status: $3
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: [a, b]
changelog:    # set by /update-changelog -- do not edit manually
---

## Summary

First paragraph.

## Implementation Notes

## Archived Plan Content
EOF
}

# ---------------------------------------------------------------------------
# Section A: legal transitions in legacy mode
# ---------------------------------------------------------------------------
echo
echo "[A] Legal transitions (legacy mode)"

LEGACY="$TMPDIR_ROOT/legacy-project"
mkdir -p "$LEGACY/.issues"
echo "IM" > "$LEGACY/.issues/.prefix"

# open -> in-progress with --branch
make_issue "$LEGACY/.issues/IM-001.md" "IM-001" "open"
(
    cd "$LEGACY"
    "$TRANSITION" IM-001 in-progress --branch phase-test --no-reindex
)
# COVERS: state.canonical-states
assert_file_contains "status: in-progress" "$LEGACY/.issues/IM-001.md" \
    "open -> in-progress sets status"
# COVERS: state.auto-stamp-notes
assert_file_contains "updated: $TODAY" "$LEGACY/.issues/IM-001.md" \
    "transition stamps updated"
assert_file_contains 'branch: "phase-test"' "$LEGACY/.issues/IM-001.md" \
    "--branch sets branch field (preserves quoting style)"
# COVERS: state.auto-stamp-notes
assert_file_contains "$TODAY: status: open -> in-progress" \
    "$LEGACY/.issues/IM-001.md" \
    "transition note appended to Implementation Notes"

# in-progress -> blocked with --reason
(
    cd "$LEGACY"
    "$TRANSITION" IM-001 blocked --reason "waiting on RM-038" --no-reindex
)
assert_file_contains "status: blocked" "$LEGACY/.issues/IM-001.md" \
    "in-progress -> blocked sets status"
assert_file_contains "$TODAY: status: in-progress -> blocked -- waiting on RM-038" \
    "$LEGACY/.issues/IM-001.md" \
    "transition note includes reason"

# blocked -> in-progress (unblock)
(
    cd "$LEGACY"
    "$TRANSITION" IM-001 in-progress --no-reindex
)
assert_file_contains "status: in-progress" "$LEGACY/.issues/IM-001.md" \
    "blocked -> in-progress unblocks"

# in-progress -> done with --branch ""
(
    cd "$LEGACY"
    "$TRANSITION" IM-001 done --branch "" --no-reindex
)
assert_file_contains "status: done" "$LEGACY/.issues/IM-001.md" \
    "in-progress -> done"
assert_file_contains 'branch: ""' "$LEGACY/.issues/IM-001.md" \
    '--branch "" clears branch field'

# done -> in-progress (reopen)
(
    cd "$LEGACY"
    "$TRANSITION" IM-001 in-progress --no-reindex
)
assert_file_contains "status: in-progress" "$LEGACY/.issues/IM-001.md" \
    "done -> in-progress (reopen)"

# done -> open (reopen-no-claim)
make_issue "$LEGACY/.issues/IM-002.md" "IM-002" "done"
(
    cd "$LEGACY"
    "$TRANSITION" IM-002 open --no-reindex
)
assert_file_contains "status: open" "$LEGACY/.issues/IM-002.md" \
    "done -> open (reopen-no-claim)"

# cancelled -> in-progress
# COVERS: state.no-delete  (cancelled file persists on disk and is reopenable)
make_issue "$LEGACY/.issues/IM-003.md" "IM-003" "cancelled"
(
    cd "$LEGACY"
    "$TRANSITION" IM-003 in-progress --no-reindex
)
assert_file_contains "status: in-progress" "$LEGACY/.issues/IM-003.md" \
    "cancelled -> in-progress"

# ---------------------------------------------------------------------------
# Section B: illegal transitions
# ---------------------------------------------------------------------------
echo
echo "[B] Illegal transitions"

# open -> done is illegal
make_issue "$LEGACY/.issues/IM-010.md" "IM-010" "open"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$TRANSITION" IM-010 done --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "open -> done exits 2"
assert_contains "illegal transition" "$STDERR" "stderr says 'illegal transition'"
assert_contains "in-progress" "$STDERR" "stderr lists in-progress as legal"
assert_contains "blocked" "$STDERR" "stderr lists blocked as legal"
assert_contains "cancelled" "$STDERR" "stderr lists cancelled as legal"
# File not modified
assert_file_contains "status: open" "$LEGACY/.issues/IM-010.md" \
    "illegal transition does not modify file"

# done -> cancelled is illegal (terminal -> terminal)
# COVERS: state.terminal-transitions
make_issue "$LEGACY/.issues/IM-011.md" "IM-011" "done"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$TRANSITION" IM-011 cancelled --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "done -> cancelled exits 2"
assert_contains "illegal transition" "$STDERR" "terminal-to-terminal stderr"

# Same-state no-op exits 0 and does not change mtime
make_issue "$LEGACY/.issues/IM-012.md" "IM-012" "open"
BEFORE_MTIME=$(file_mtime "$LEGACY/.issues/IM-012.md")
sleep 0.1
RC=0
(
    cd "$LEGACY"
    "$TRANSITION" IM-012 open --no-reindex
) || RC=$?
assert_eq "0" "$RC" "same-state no-op exits 0"
AFTER_MTIME=$(file_mtime "$LEGACY/.issues/IM-012.md")
assert_eq "$BEFORE_MTIME" "$AFTER_MTIME" "same-state no-op does not touch the file"

# ---------------------------------------------------------------------------
# Section C: --reason required for blocked
# ---------------------------------------------------------------------------
echo
echo "[C] --reason required for blocked"

make_issue "$LEGACY/.issues/IM-020.md" "IM-020" "open"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$TRANSITION" IM-020 blocked --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "blocked without --reason exits 2"
assert_contains "--reason" "$STDERR" "stderr names --reason"
# File untouched
assert_file_contains "status: open" "$LEGACY/.issues/IM-020.md" \
    "missing --reason does not modify file"

# With --reason, succeeds
(
    cd "$LEGACY"
    "$TRANSITION" IM-020 blocked --reason "needs review" --no-reindex
)
assert_file_contains "status: blocked" "$LEGACY/.issues/IM-020.md" \
    "blocked with --reason succeeds"
assert_file_contains "needs review" "$LEGACY/.issues/IM-020.md" \
    "reason recorded in note"

# Same-state transition WITH --reason is not a no-op: it stamps a note
# recording the reason (the no_op-but-reason branch), unlike a bare
# same-state transition which is a silent exit-0 no-op.
make_issue "$LEGACY/.issues/IM-021.md" "IM-021" "open"
RC=0
(
    cd "$LEGACY"
    "$TRANSITION" IM-021 open --reason "still blocked upstream, noting" --no-reindex
) || RC=$?
assert_eq "0" "$RC" "same-state transition with --reason exits 0"
# COVERS: state.auto-stamp-notes
assert_file_contains "$TODAY: status: open -> open -- still blocked upstream, noting" \
    "$LEGACY/.issues/IM-021.md" \
    "same-state --reason appends an audit note"
assert_file_contains "status: open" "$LEGACY/.issues/IM-021.md" \
    "same-state --reason leaves status unchanged"

# ---------------------------------------------------------------------------
# Section D: missing file / bad ID / unknown state
# ---------------------------------------------------------------------------
echo
echo "[D] Error paths"

RC=0
(
    cd "$LEGACY"
    "$TRANSITION" IM-999 in-progress --no-reindex 2>/dev/null
) || RC=$?
assert_eq "1" "$RC" "missing file exits 1"

RC=0
(
    cd "$LEGACY"
    "$TRANSITION" notanid in-progress --no-reindex 2>/dev/null
) || RC=$?
assert_eq "2" "$RC" "bad ID format exits 2"

RC=0
STDERR=$(
    cd "$LEGACY"
    "$TRANSITION" IM-001 triaged --no-reindex 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "unknown state exits 2"
assert_contains "open" "$STDERR" "unknown-state stderr lists canonical states"

# An issue whose frontmatter is missing the status field is a precondition
# failure (exit 1), not a crash: the transition core reads status and
# raises a ValueError the CLI maps to exit 1.
cat > "$LEGACY/.issues/IM-013.md" <<'EOF'
---
id: IM-013
title: "No status field"
type: chore
priority: medium
created: 2026-05-12
updated: 2026-05-12
---

## Summary

Body.
EOF
RC=0
(
    cd "$LEGACY"
    "$TRANSITION" IM-013 in-progress --no-reindex 2>/dev/null
) || RC=$?
assert_eq "1" "$RC" "transition on a status-less issue exits 1"

# ---------------------------------------------------------------------------
# Section E: concurrent-edit (lock contention)
# ---------------------------------------------------------------------------
echo
echo "[E] Concurrent-edit"

make_issue "$LEGACY/.issues/IM-030.md" "IM-030" "open"
LOCK="$LEGACY/.issues/.locks/IM-030.lock"
mkdir -p "$LEGACY/.issues/.locks"

(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 5 -- \
        bash -c "sleep 3"
) &
HOLDER_PID=$!
sleep 0.3

RC=0
STDERR=$(
    cd "$LEGACY"
    "$TRANSITION" IM-030 in-progress --lock-timeout 0.5 --no-reindex 2>&1 >/dev/null
) || RC=$?

wait $HOLDER_PID 2>/dev/null

assert_eq "3" "$RC" "contender exits 3 on lock timeout"
assert_contains "concurrent-edit" "$STDERR" "stderr includes 'concurrent-edit'"

# Subsequent transition succeeds after release
(
    cd "$LEGACY"
    "$TRANSITION" IM-030 in-progress --no-reindex
)
assert_file_contains "status: in-progress" "$LEGACY/.issues/IM-030.md" \
    "transition succeeds after holder releases"

# ---------------------------------------------------------------------------
# Section F: store mode
# ---------------------------------------------------------------------------
echo
echo "[F] Store mode"

STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM"
# Store must be a git repo before any auto-commit mutation
# runs. Without this, transition-issue's store_mutation context fails at
# sweep-entry time with "fatal: not a git repository" and no write lands.
(
    cd "$STORE"
    git init -q
    git config user.email test@example.com
    git config user.name test
    git commit -q --allow-empty -m "init store"
)
make_issue "$STORE/IM/IM-100.md" "IM-100" "open"

(
    cd "$TMPDIR_ROOT"
    ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-100 in-progress --no-reindex
)
assert_file_contains "status: in-progress" "$STORE/IM/IM-100.md" \
    "store mode updates file under <store>/<PREFIX>/"
if [ -d "$STORE/IM/.locks" ]; then
    pass "store mode created per-prefix .locks/ dir"
else
    fail "store mode did not create $STORE/IM/.locks"
fi

# ---------------------------------------------------------------------------
# Section G: --json output
# ---------------------------------------------------------------------------
echo
echo "[G] --json output"

make_issue "$LEGACY/.issues/IM-040.md" "IM-040" "open"
JSON=$(
    cd "$LEGACY"
    "$TRANSITION" IM-040 in-progress --branch phase-test --json --no-reindex
)
if python3 -c "import json,sys; json.loads(sys.stdin.read())" <<< "$JSON" 2>/dev/null; then
    pass "--json output is parseable"
else
    fail "--json output is not parseable: $JSON"
fi
assert_contains "\"old\": \"open\"" "$JSON" "--json reports old state"
assert_contains "\"new\": \"in-progress\"" "$JSON" "--json reports new state"
assert_contains "\"no_op\": false" "$JSON" "--json reports no_op flag"
assert_contains "\"branch_change\"" "$JSON" "--json reports branch change"

# ---------------------------------------------------------------------------
# Section H: auto-reindex
# ---------------------------------------------------------------------------
echo
echo "[H] Auto-reindex"

make_issue "$LEGACY/.issues/IM-050.md" "IM-050" "open"
rm -f "$LEGACY/.issues/INDEX.md"
(
    cd "$LEGACY"
    "$TRANSITION" IM-050 in-progress
)
if [ -f "$LEGACY/.issues/INDEX.md" ]; then
    pass "default mode auto-reindexes"
else
    fail "default mode did not create INDEX.md"
fi

# regression: the auto-reindex must place IM-050 under the In
# Progress section, not just create an INDEX.md that omits it. Prior to
# the mutation-pipeline rework, INDEX.md was regenerated stale --
# the file existed but the just-transitioned ID was missing.
if [ -f "$LEGACY/.issues/INDEX.md" ]; then
    in_progress_section=$(awk '/^## In Progress/{f=1; next} /^## /{f=0} f' "$LEGACY/.issues/INDEX.md")
    if echo "$in_progress_section" | grep -qF "| IM-050 "; then
        pass "regression: transitioned ID lands in In Progress section (legacy)"
    else
        fail "regression: IM-050 not in In Progress section after transition (legacy)"
        echo "--- INDEX.md content ---"
        cat "$LEGACY/.issues/INDEX.md"
        echo "------------------------"
    fi
fi

make_issue "$LEGACY/.issues/IM-051.md" "IM-051" "open"
rm -f "$LEGACY/.issues/INDEX.md"
(
    cd "$LEGACY"
    "$TRANSITION" IM-051 in-progress --no-reindex
)
if [ -f "$LEGACY/.issues/INDEX.md" ]; then
    fail "--no-reindex did not suppress INDEX.md regeneration"
else
    pass "--no-reindex suppresses INDEX.md regeneration"
fi

# regression in store mode: requires a real git-managed store so
# the mutation pipeline (catchup sweep + commit_after_write) can
# run. Builds a one-shot store + registered repo inline; if anything in
# the chain regresses to "creates an INDEX file but does not include the
# new state," this check catches it.
STORE_AR="$TMPDIR_ROOT/store-autoreindex"
mkdir -p "$STORE_AR/IM"
(
    cd "$STORE_AR"
    git init -q
    git config user.email test@example.com
    git config user.name test
    printf 'autoreindex-proj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
make_issue "$STORE_AR/IM/IM-200.md" "IM-200" "open"
PROJ_AR="$TMPDIR_ROOT/autoreindex-proj"
mkdir -p "$PROJ_AR/.issues"
echo "IM" > "$PROJ_AR/.issues/.prefix"
echo "$STORE_AR" > "$PROJ_AR/.issues/.migrated-to-store"
(
    cd "$PROJ_AR"
    "$TRANSITION" IM-200 in-progress --branch foo
)
if [ -f "$STORE_AR/IM/INDEX.md" ] && [ -f "$STORE_AR/INDEX.md" ]; then
    pass "store mode auto-reindexes both per-prefix and root INDEX"
else
    fail "store mode did not create per-prefix and/or root INDEX"
fi
prefix_section=$(awk '/^## In Progress/{f=1; next} /^## /{f=0} f' "$STORE_AR/IM/INDEX.md" 2>/dev/null)
if echo "$prefix_section" | grep -qF "| IM-200 "; then
    pass "regression: transitioned ID lands in per-prefix In Progress (store)"
else
    fail "regression: IM-200 not in <store>/IM/INDEX.md In Progress section"
    echo "--- per-prefix INDEX content ---"
    cat "$STORE_AR/IM/INDEX.md" 2>/dev/null
    echo "--------------------------------"
fi
root_section=$(awk '/^## In Progress/{f=1; next} /^## /{f=0} f' "$STORE_AR/INDEX.md" 2>/dev/null)
if echo "$root_section" | grep -qF "| IM-200 "; then
    pass "regression: transitioned ID lands in root aggregate In Progress (store)"
else
    fail "regression: IM-200 not in <store>/INDEX.md aggregate In Progress section"
    echo "--- root INDEX content ---"
    cat "$STORE_AR/INDEX.md" 2>/dev/null
    echo "--------------------------"
fi

# ---------------------------------------------------------------------------
# Section I: help / version
# ---------------------------------------------------------------------------
echo
echo "[I] Help and version"

RC=0
"$TRANSITION" --help >/dev/null || RC=$?
assert_eq "0" "$RC" "--help exits 0"

RC=0
HELP=$("$TRANSITION" --help)
assert_contains "open" "$HELP" "--help lists 'open'"
assert_contains "in-progress" "$HELP" "--help lists 'in-progress'"
assert_contains "blocked" "$HELP" "--help lists 'blocked'"
assert_contains "done" "$HELP" "--help lists 'done'"
assert_contains "cancelled" "$HELP" "--help lists 'cancelled'"
assert_contains "Legal transition matrix" "$HELP" "--help shows transition matrix"

RC=0
"$TRANSITION" --version >/dev/null || RC=$?
assert_eq "0" "$RC" "--version exits 0"

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
