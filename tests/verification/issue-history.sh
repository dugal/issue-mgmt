#!/usr/bin/env bash
# Verification script for IM-030 issue-history subcommand.
#
# Exercises the acceptance criteria that benefit from a real
# filesystem, a real git repo, and a real subprocess:
#   - Returns history for issues that have commits in the store
#   - Commits appear newest-first; full sha + ISO date + author + subject
#   - --limit N caps results
#   - Empty history (file exists, no commits) prints a "no history"
#     message to stderr and keeps stdout empty (acceptance criterion)
#   - Errors clearly when ID is malformed (exit 2)
#   - Errors clearly when the prefix directory does not exist (exit 1)
#   - Errors clearly when the issue file does not exist (exit 1)
#   - --json output is parseable and carries the expected fields
#   - Legacy-mode error includes the exact `git log` fallback command
#     so a calling model can run it against the originating project repo
#   - --help / --version
#   - Does not acquire any locks (read-only: no .locks/ entries created)
#
# Run from the project root:
#     tests/verification/issue-history.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE_HISTORY="$(resolve_cli issue-history)"

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
        fail "$3: '$1' not found in:"
        echo "$2" | sed 's/^/  | /'
    fi
}

assert_not_contains() {
    if echo "$2" | grep -qF -- "$1"; then
        fail "$3: '$1' should not appear in:"
        echo "$2" | sed 's/^/  | /'
    else
        pass "$3 (does not contain '$1')"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im030-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# ---------------------------------------------------------------------------
# Build a synthetic issue store as a real git repo
# ---------------------------------------------------------------------------
STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM" "$STORE/DOUG"

(
    cd "$STORE"
    git init --quiet --initial-branch=main
    git config user.email "$FIXTURE_EMAIL"
    git config user.name "Verify User"
    git config commit.gpgsign false
)

make_issue_content() {
    cat <<EOF
---
id: $1
title: "Sample $1"
type: chore
status: open
priority: medium
created: 2026-05-14
updated: 2026-05-14
branch: ""
tags: []
changelog:
---

$2
EOF
}

# IM-100 -- three commits, will exercise --limit and reverse chrono order
make_issue_content "IM-100" "v1" > "$STORE/IM/IM-100.md"
(cd "$STORE" && git add IM/IM-100.md && git commit -m "feat: add IM-100" --quiet)

make_issue_content "IM-100" "v2" > "$STORE/IM/IM-100.md"
(cd "$STORE" && git add IM/IM-100.md && git commit -m "chore: update IM-100 body" --quiet)

make_issue_content "IM-100" "v3" > "$STORE/IM/IM-100.md"
(cd "$STORE" && git add IM/IM-100.md && git commit -m "fix: correct IM-100 wording" --quiet)

# IM-200 -- exists on disk but never committed; exercises empty-history case
make_issue_content "IM-200" "uncommitted body" > "$STORE/IM/IM-200.md"

export ISSUE_STORE_ROOT="$STORE"

# ---------------------------------------------------------------------------
# Section A: --help / --version
# ---------------------------------------------------------------------------
echo
echo "[A] Help and version"

HELP_OUTPUT=$("$ISSUE_HISTORY" --help 2>&1)
# COVERS: surface.self-naming
# The retired flat name appears in the DESCRIPTION as a qualified
# lineage note, which the clause permits, so matching on it proved
# nothing about the banner. Assert the current form (IM-239).
assert_contains "issue history show" "$HELP_OUTPUT" "--help names the current verb"
assert_contains "--limit" "$HELP_OUTPUT" "--help documents --limit flag"
assert_contains "--json" "$HELP_OUTPUT" "--help documents --json flag"
assert_contains "Examples:" "$HELP_OUTPUT" "--help has an Examples block"
assert_contains "ISSUE_STORE_ROOT" "$HELP_OUTPUT" "--help documents store mode"
assert_contains "out of scope for this surface" "$HELP_OUTPUT" "--help states the legacy-mode boundary"
assert_contains "git log --follow" "$HELP_OUTPUT" \
    "--help shows the legacy fallback command"

VERSION_OUTPUT=$("$ISSUE_HISTORY" --version)
assert_contains "v" "$VERSION_OUTPUT" "--version prints a version string"

# ---------------------------------------------------------------------------
# Section B: Store mode -- happy path (TSV)
# ---------------------------------------------------------------------------
echo
echo "[B] Store mode -- happy path (TSV)"

OUT=$("$ISSUE_HISTORY" IM-100)
RC=$?
# COVERS: history.subcommands
assert_eq "0" "$RC" "exit 0 when history exists"

LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "3" "$LINE_COUNT" "TSV emits one line per commit (3 commits)"

# Newest first
FIRST_LINE=$(echo "$OUT" | head -1)
LAST_LINE=$(echo "$OUT" | tail -1)
assert_contains "fix: correct IM-100 wording" "$FIRST_LINE" \
    "newest commit is first"
assert_contains "feat: add IM-100" "$LAST_LINE" \
    "oldest commit is last"

# TSV shape: <sha>\t<date>\t<author>\t<subject>
SHA_COL=$(echo "$FIRST_LINE" | cut -f1)
DATE_COL=$(echo "$FIRST_LINE" | cut -f2)
AUTHOR_COL=$(echo "$FIRST_LINE" | cut -f3)
SUBJECT_COL=$(echo "$FIRST_LINE" | cut -f4)

case "$SHA_COL" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
        if [ ${#SHA_COL} -eq 40 ]; then
            pass "sha column is full 40-char hex"
        else
            fail "sha column wrong length: ${#SHA_COL}"
        fi
        ;;
    *) fail "sha column not hex: $SHA_COL" ;;
esac
case "$DATE_COL" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*) pass "date column is ISO8601 ($DATE_COL)" ;;
    *) fail "date column not ISO8601: $DATE_COL" ;;
esac
assert_eq "Verify User" "$AUTHOR_COL" "author column populated"
assert_contains "IM-100" "$SUBJECT_COL" "subject column populated"

# ---------------------------------------------------------------------------
# Section C: --limit caps results
# ---------------------------------------------------------------------------
echo
echo "[C] --limit caps results"

OUT=$("$ISSUE_HISTORY" IM-100 --limit 2)
LIMIT_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "2" "$LIMIT_COUNT" "--limit 2 returns exactly two commits"

OUT=$("$ISSUE_HISTORY" IM-100 --limit 1)
LIMIT_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "1" "$LIMIT_COUNT" "--limit 1 returns exactly one commit"

# IM-241: `--limit 0` means unlimited on every verb that takes --limit
# (it was exit 2 here through v3.3.x, while index and ready already read
# it as unlimited).
OUT=$("$ISSUE_HISTORY" IM-100 --limit 0 2>/dev/null)
RC=$?
UNLIMITED_COUNT=$(echo "$OUT" | grep -c .)
FULL_COUNT=$("$ISSUE_HISTORY" IM-100 2>/dev/null | grep -c .)
# COVERS: surface.vocabulary-registry
assert_eq "0" "$RC" "--limit 0 is unlimited, not a usage error"
assert_eq "$FULL_COUNT" "$UNLIMITED_COUNT" "--limit 0 matches the unbounded default"

# A negative cap is still a usage error.
OUT_ERR=$("$ISSUE_HISTORY" IM-100 --limit -3 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "--limit -3 -> exit 2"
assert_contains "0 (unlimited) or a positive" "$OUT_ERR" \
    "negative-limit error names the accepted range"

OUT_ERR=$("$ISSUE_HISTORY" IM-100 --limit notanint 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "--limit notanint -> exit 2"
assert_contains "0 (unlimited) or a positive" "$OUT_ERR" \
    "--limit notanint error names the accepted range"

# ---------------------------------------------------------------------------
# Section D: Empty history (file exists, no commits)
# ---------------------------------------------------------------------------
echo
echo "[D] Empty history -- file exists but never committed"

# Run separately to inspect stdout and stderr independently
OUT_STDOUT=$("$ISSUE_HISTORY" IM-200 2>/dev/null)
RC=$?
OUT_STDERR=$("$ISSUE_HISTORY" IM-200 2>&1 >/dev/null)

assert_eq "0" "$RC" "exit 0 on empty history"
assert_eq "" "$OUT_STDOUT" "stdout is empty on empty history"
assert_contains "no history" "$OUT_STDERR" "stderr carries clear 'no history' message"
assert_contains "IM-200" "$OUT_STDERR" "stderr names the issue"

# JSON mode: empty commits array is well-formed
JSON_OUT=$("$ISSUE_HISTORY" IM-200 --json)
RC=$?
assert_eq "0" "$RC" "--json empty-history exit 0"
echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['id'] == 'IM-200', d
assert d['commits'] == [], d
assert d['file'] == 'IM/IM-200.md', d
print('json-empty-shape-ok')
" >"$TMPDIR_ROOT/jsoncheck.out" 2>&1
if grep -q "json-empty-shape-ok" "$TMPDIR_ROOT/jsoncheck.out"; then
    pass "--json on empty history emits {id, file, commits:[]}"
else
    fail "--json empty-history shape wrong: $(cat "$TMPDIR_ROOT/jsoncheck.out")"
fi

# ---------------------------------------------------------------------------
# Section E: --json shape (populated)
# ---------------------------------------------------------------------------
echo
echo "[E] --json populated shape"

JSON_OUT=$("$ISSUE_HISTORY" IM-100 --json)
echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['id'] == 'IM-100'
assert d['file'] == 'IM/IM-100.md'
assert isinstance(d['commits'], list)
assert len(d['commits']) == 3
for c in d['commits']:
    for k in ('sha', 'date', 'author', 'subject'):
        assert k in c, c
    assert len(c['sha']) == 40
# Newest first preserved
assert 'fix' in d['commits'][0]['subject']
assert 'feat' in d['commits'][-1]['subject']
print('json-populated-ok')
" >"$TMPDIR_ROOT/jsoncheck2.out" 2>&1
if grep -q "json-populated-ok" "$TMPDIR_ROOT/jsoncheck2.out"; then
    pass "--json populated shape has id/file/store/commits with full fields"
else
    fail "--json populated shape wrong: $(cat "$TMPDIR_ROOT/jsoncheck2.out")"
fi

# --json --limit interaction
JSON_OUT=$("$ISSUE_HISTORY" IM-100 --json --limit 2)
COUNT=$(echo "$JSON_OUT" | python3 -c "import json, sys; print(len(json.load(sys.stdin)['commits']))")
assert_eq "2" "$COUNT" "--json --limit 2 returns 2 commits in array"

# ---------------------------------------------------------------------------
# Section F: Missing prefix dir / missing file
# ---------------------------------------------------------------------------
echo
echo "[F] Missing prefix dir and missing file"

# Prefix dir does not exist
OUT_ERR=$("$ISSUE_HISTORY" ZZZ-001 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "missing prefix dir -> exit 1"
assert_contains "prefix directory" "$OUT_ERR" \
    "missing prefix error names the prefix-directory concept"
assert_contains "ZZZ" "$OUT_ERR" "missing prefix error names the prefix"

# Prefix exists but file does not
OUT_ERR=$("$ISSUE_HISTORY" IM-999 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "missing issue file -> exit 1"
assert_contains "issue file not found" "$OUT_ERR" \
    "missing-file error message is explicit"
assert_contains "IM-999" "$OUT_ERR" "missing-file error names the id"

# ---------------------------------------------------------------------------
# Section G: Malformed ID
# ---------------------------------------------------------------------------
echo
echo "[G] Malformed ID"

# `im-030` (lowercase) is no longer malformed -- it case-folds to
# IM-030. Only genuinely malformed shapes remain exit-2 here.
for bad_id in "I-030" "IM-" "IM030" "IM-03a" "foo"; do
    OUT_ERR=$("$ISSUE_HISTORY" "$bad_id" 2>&1 >/dev/null)
    RC=$?
    assert_eq "2" "$RC" "bad ID '$bad_id' -> exit 2"
    assert_contains "invalid ID" "$OUT_ERR" "bad ID '$bad_id' error names 'invalid ID'"
done

# Missing argument
OUT_ERR=$("$ISSUE_HISTORY" 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "missing ID -> exit 2"
assert_contains "missing ID" "$OUT_ERR" "missing-ID error is explicit"

# ---------------------------------------------------------------------------
# Section H: Legacy mode error -- includes the exact fallback command
# ---------------------------------------------------------------------------
echo
echo "[H] Legacy-mode error carries the git log fallback"

# Run from a tmpdir with no .issues/ in the walk-up path, and no
# ISSUE_STORE_ROOT.
LEGACY_DIR="$TMPDIR_ROOT/legacy-cwd"
mkdir -p "$LEGACY_DIR"
(
    cd "$LEGACY_DIR"
    unset ISSUE_STORE_ROOT
    "$ISSUE_HISTORY" IM-030 2>"$TMPDIR_ROOT/legacy.err" >"$TMPDIR_ROOT/legacy.out"
    echo $? > "$TMPDIR_ROOT/legacy.rc"
)
LEGACY_RC=$(cat "$TMPDIR_ROOT/legacy.rc")
LEGACY_ERR=$(cat "$TMPDIR_ROOT/legacy.err")
LEGACY_OUT=$(cat "$TMPDIR_ROOT/legacy.out")

# COVERS: history.legacy-refuse
assert_eq "1" "$LEGACY_RC" "legacy mode -> exit 1"
assert_eq "" "$LEGACY_OUT" "legacy mode keeps stdout empty"
assert_contains "store mode not active" "$LEGACY_ERR" \
    "legacy error names the precondition"
assert_contains "git log --follow -- .issues/IM-030.md" "$LEGACY_ERR" \
    "legacy error carries the exact fallback command (with the requested ID)"
assert_contains "operates only on the issue store" "$LEGACY_ERR" \
    "legacy error names the store-only boundary"

# ---------------------------------------------------------------------------
# Section I: Read-only -- no .locks/ entries created
# ---------------------------------------------------------------------------
echo
echo "[I] Read-only: no locks acquired"

# Re-run a happy-path invocation, then scan for any lock artifact.
"$ISSUE_HISTORY" IM-100 >/dev/null 2>&1
"$ISSUE_HISTORY" IM-100 --json >/dev/null 2>&1
"$ISSUE_HISTORY" IM-200 >/dev/null 2>&1

if find "$STORE" -type d -name ".locks" 2>/dev/null | grep -q .; then
    fail ".locks/ directory was created by issue-history (should not happen)"
else
    # COVERS: history.no-locks
    pass "no .locks/ directory created by issue-history"
fi

# ---------------------------------------------------------------------------
# Section J: Sentinel-based store mode (cover the non-env path)
# ---------------------------------------------------------------------------
echo
echo "[J] Sentinel-based store-mode detection"

SENTINEL_DIR="$TMPDIR_ROOT/sentinel-proj/.issues"
mkdir -p "$SENTINEL_DIR"
printf "%s" "$STORE" > "$SENTINEL_DIR/.migrated-to-store"

(
    cd "$TMPDIR_ROOT/sentinel-proj"
    unset ISSUE_STORE_ROOT
    "$ISSUE_HISTORY" IM-100 >"$TMPDIR_ROOT/sentinel.out" 2>"$TMPDIR_ROOT/sentinel.err"
    echo $? > "$TMPDIR_ROOT/sentinel.rc"
)
SENTINEL_RC=$(cat "$TMPDIR_ROOT/sentinel.rc")
SENTINEL_OUT=$(cat "$TMPDIR_ROOT/sentinel.out")
assert_eq "0" "$SENTINEL_RC" "sentinel-mode happy-path -> exit 0"
SENTINEL_LINES=$(echo "$SENTINEL_OUT" | grep -c .)
assert_eq "3" "$SENTINEL_LINES" "sentinel-mode returns the same 3 commits"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "----- issue-history verification summary -----"
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
