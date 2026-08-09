#!/usr/bin/env bash
# Verification script for the search-history subcommand.
#
# Exercises the acceptance criteria that benefit from a real
# filesystem, a real git repo, and a real subprocess:
#   - Cross-store search works without --prefix
#   - --prefix accepts a comma-separated list and scopes the search
#     (single prefix and multiple-prefix union)
#   - Honors --since accepting both relative (`Y.days.ago`) and
#     absolute (`YYYY-MM-DD`) forms
#   - JSON output via --json is parseable and carries the expected
#     fields (term, store, prefixes, since, commits)
#   - Zero-match case keeps stdout empty and writes a clear "no
#     matches" note to stderr
#   - Errors clearly when --prefix names a missing directory (exit 1)
#   - Errors clearly when --prefix shape is invalid (exit 2)
#   - Errors clearly when TERM is missing (exit 2)
#   - Legacy-mode error includes the exact `git log -S` fallback so
#     a calling model can run it against the originating project repo
#   - --help / --version
#   - Sub-second on a synthetic corpus
#   - Does not acquire any locks (read-only: no .locks/ entries created)
#
# Run from the project root:
#     tests/verification/search-history.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
SEARCH_HISTORY="$(resolve_cli search-history)"

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

TMPDIR_ROOT=$(mktemp -d -t im031-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# ---------------------------------------------------------------------------
# Build a synthetic issue store as a real git repo
# ---------------------------------------------------------------------------
STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM" "$STORE/DOUG"

(
    cd "$STORE"
    git init --quiet --initial-branch=main
    git config user.email "verify@example.com"
    git config user.name "Verify User"
    git config commit.gpgsign false
)

# Helper: commit a file with a deterministic message.  If a date is
# given (3rd arg), it overrides both author and committer dates.
commit_file() {
    local rel="$1" content="$2" message="$3" date="${4:-}"
    mkdir -p "$STORE/$(dirname "$rel")"
    printf "%s" "$content" > "$STORE/$rel"
    if [ -n "$date" ]; then
        (cd "$STORE" \
            && git add "$rel" \
            && GIT_AUTHOR_DATE="$date" GIT_COMMITTER_DATE="$date" \
               git commit -m "$message" --quiet)
    else
        (cd "$STORE" && git add "$rel" && git commit -m "$message" --quiet)
    fi
}

# Corpus:
#   IM-001: gains and loses MAGIC_TOKEN; gains UNRELATED_WORD
#   IM-002: contains MAGIC_TOKEN only briefly
#   DOUG-001: contains MAGIC_TOKEN
#   DOUG-002: no MAGIC_TOKEN at any point
#
# Commits are made in strict chronological order to match real-world
# history shape -- `git log --since` does history-simplification and
# stops walking once it hits an ancestor older than the cutoff. If
# commit dates are interleaved with topology, --since under-counts.
commit_file "IM/IM-001.md"    "plain body"                    "feat: IM-001 initial"          "2026-01-10T00:00:00+0000"
commit_file "IM/IM-001.md"    "now has MAGIC_TOKEN"           "feat: IM-001 add MAGIC"        "2026-01-20T00:00:00+0000"
commit_file "IM/IM-002.md"    "plain"                         "feat: IM-002 initial"          "2026-01-25T00:00:00+0000"
commit_file "DOUG/DOUG-001.md" "carol body with MAGIC_TOKEN"   "feat: DOUG-001 add MAGIC"      "2026-02-01T00:00:00+0000"
commit_file "IM/IM-001.md"    "the token is now gone"         "fix: IM-001 remove MAGIC"      "2026-02-10T00:00:00+0000"
commit_file "IM/IM-002.md"    "transient MAGIC_TOKEN"         "feat: IM-002 brief MAGIC"      "2026-03-01T00:00:00+0000"
commit_file "DOUG/DOUG-002.md" "carol body without the token"  "feat: DOUG-002 initial"        "2026-03-15T00:00:00+0000"
commit_file "IM/IM-001.md"    "MAGIC_TOKEN again plus UNRELATED_WORD" "feat: IM-001 readd MAGIC + UNRELATED" "2026-04-15T00:00:00+0000"

export ISSUE_STORE_ROOT="$STORE"

# ---------------------------------------------------------------------------
# Section A: --help / --version
# ---------------------------------------------------------------------------
echo
echo "[A] Help and version"

HELP_OUTPUT=$("$SEARCH_HISTORY" --help 2>&1)
assert_contains "search-history" "$HELP_OUTPUT" "--help prints tool banner"
assert_contains "--prefix" "$HELP_OUTPUT" "--help documents --prefix"
assert_contains "--since" "$HELP_OUTPUT" "--help documents --since"
assert_contains "--json" "$HELP_OUTPUT" "--help documents --json"
assert_contains "Examples:" "$HELP_OUTPUT" "--help has an Examples block"
assert_contains "ISSUE_STORE_ROOT" "$HELP_OUTPUT" "--help documents store mode"
assert_contains "out of scope for this surface" "$HELP_OUTPUT" "--help states the legacy-mode boundary"
assert_contains "git log -S" "$HELP_OUTPUT" "--help shows the legacy fallback"
assert_contains "2.weeks.ago" "$HELP_OUTPUT" "--help shows a relative --since example"
assert_contains "2026-04-01" "$HELP_OUTPUT" "--help shows an absolute --since example"

VERSION_OUTPUT=$("$SEARCH_HISTORY" --version)
assert_contains "v" "$VERSION_OUTPUT" "--version prints a version string"

# ---------------------------------------------------------------------------
# Section B: Cross-store happy path (TSV)
# ---------------------------------------------------------------------------
echo
echo "[B] Cross-store search (no --prefix)"

OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN")
RC=$?
assert_eq "0" "$RC" "exit 0 when matches exist"

# Expected matches for MAGIC_TOKEN (any commit that changes the count):
#   IM-001 add MAGIC, IM-001 remove MAGIC, IM-001 readd MAGIC + UNRELATED,
#   IM-002 brief MAGIC, DOUG-001 add MAGIC
# Note: IM-002's "brief MAGIC" commit added MAGIC_TOKEN.  IM-002 only
# has that one MAGIC commit (no follow-up removal in our corpus).
LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "5" "$LINE_COUNT" "cross-store TSV emits 5 pickaxe matches"

# Cross-prefix coverage: both IM and DOUG should appear in the subjects.
assert_contains "IM-001" "$OUT" "cross-store result includes IM-001 commits"
assert_contains "DOUG-001" "$OUT" "cross-store result includes DOUG-001 commits"

# TSV shape check on the first line.
FIRST_LINE=$(echo "$OUT" | head -1)
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

# ---------------------------------------------------------------------------
# Section C: --prefix scoping (single + comma-separated)
# ---------------------------------------------------------------------------
echo
echo "[C] --prefix scoping"

# Single prefix: only IM commits should appear
OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix IM)
RC=$?
assert_eq "0" "$RC" "--prefix IM exit 0"
LINE_COUNT=$(echo "$OUT" | grep -c .)
# IM has: IM-001 (add, remove, readd) + IM-002 (brief MAGIC) = 4
assert_eq "4" "$LINE_COUNT" "--prefix IM returns 4 IM-only matches"
assert_not_contains "DOUG-001" "$OUT" "--prefix IM excludes DOUG commits"

# Single prefix that has only one match
OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix DOUG)
LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "1" "$LINE_COUNT" "--prefix DOUG returns the single DOUG match"
assert_contains "DOUG-001" "$OUT" "--prefix DOUG match names DOUG-001"

# Comma-separated: union behavior
OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix IM,DOUG)
LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "5" "$LINE_COUNT" "--prefix IM,DOUG returns the union (5)"

# Whitespace and trailing comma tolerance
OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix " IM , DOUG ,")
LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "5" "$LINE_COUNT" "--prefix tolerates whitespace and trailing commas"

# ---------------------------------------------------------------------------
# Section D: --since (relative + absolute)
# ---------------------------------------------------------------------------
echo
echo "[D] --since filtering"

# Absolute: 2026-03-01 -- includes 2026-04-15 readd (and IM-002 if commit
# date >= cutoff).  IM-002's MAGIC commit is exactly 2026-03-01 -- git's
# --since is "show commits more recent than", which is >= the cutoff.
OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --since 2026-03-15)
LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "1" "$LINE_COUNT" "--since 2026-03-15 returns 1 commit (just the April readd)"
assert_contains "readd" "$OUT" "--since 2026-03-15 result includes the April commit"

# Wider absolute cutoff
OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --since 2026-02-15)
# Includes IM-002 brief MAGIC (2026-03-01) and IM-001 readd (2026-04-15)
LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "2" "$LINE_COUNT" "--since 2026-02-15 returns 2 commits"

# Relative form: 5.years.ago easily encompasses the synthetic corpus.
# (Larger offsets like 100.years.ago trip git's approxidate parser
# and return zero matches.)
OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --since 5.years.ago)
LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "5" "$LINE_COUNT" "--since 5.years.ago returns all 5 matches (relative form)"

# Relative form that excludes everything (tight window from now)
OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --since 1.minutes.ago 2>/dev/null)
RC=$?
LINE_COUNT=$(echo "$OUT" | grep -c .)
assert_eq "0" "$LINE_COUNT" "--since 1.minutes.ago excludes the synthetic corpus (TSV empty)"
assert_eq "0" "$RC" "--since 1.minutes.ago exit 0 even with no matches"

# ---------------------------------------------------------------------------
# Section E: --json shape
# ---------------------------------------------------------------------------
echo
echo "[E] --json shape"

JSON_OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix IM --since 2026-02-15 --json)
echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['term'] == 'MAGIC_TOKEN', d
assert d['prefixes'] == ['IM'], d
assert d['since'] == '2026-02-15', d
assert isinstance(d['commits'], list)
assert isinstance(d['store'], str) and d['store'], d
for c in d['commits']:
    for k in ('sha', 'date', 'author', 'subject'):
        assert k in c, c
    assert len(c['sha']) == 40
print('json-shape-ok')
" >"$TMPDIR_ROOT/jsoncheck.out" 2>&1
if grep -q "json-shape-ok" "$TMPDIR_ROOT/jsoncheck.out"; then
    pass "--json carries term/store/prefixes/since/commits with full fields"
else
    fail "--json shape wrong: $(cat "$TMPDIR_ROOT/jsoncheck.out")"
fi

# JSON nulls when flags omitted
JSON_OUT=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --json)
echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['prefixes'] is None, d['prefixes']
assert d['since'] is None, d['since']
print('json-nulls-ok')
" >"$TMPDIR_ROOT/jsoncheck.out" 2>&1
if grep -q "json-nulls-ok" "$TMPDIR_ROOT/jsoncheck.out"; then
    pass "--json prefixes/since are null when flags omitted"
else
    fail "--json null defaults wrong: $(cat "$TMPDIR_ROOT/jsoncheck.out")"
fi

# JSON zero-match shape: commits is [] not null
JSON_OUT=$("$SEARCH_HISTORY" "NEVER_APPEARS_ANYWHERE" --json)
echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['commits'] == [], d
print('json-empty-ok')
" >"$TMPDIR_ROOT/jsoncheck.out" 2>&1
if grep -q "json-empty-ok" "$TMPDIR_ROOT/jsoncheck.out"; then
    pass "--json zero-match emits commits:[] (not null)"
else
    fail "--json zero-match shape wrong: $(cat "$TMPDIR_ROOT/jsoncheck.out")"
fi

# ---------------------------------------------------------------------------
# Section F: Zero-match handling (TSV)
# ---------------------------------------------------------------------------
echo
echo "[F] Zero-match TSV"

OUT_STDOUT=$("$SEARCH_HISTORY" "NEVER_APPEARS_ANYWHERE" 2>/dev/null)
RC=$?
OUT_STDERR=$("$SEARCH_HISTORY" "NEVER_APPEARS_ANYWHERE" 2>&1 >/dev/null)

assert_eq "0" "$RC" "exit 0 on zero matches"
assert_eq "" "$OUT_STDOUT" "stdout empty on zero matches"
assert_contains "no matches" "$OUT_STDERR" "stderr carries 'no matches' message"
assert_contains "NEVER_APPEARS_ANYWHERE" "$OUT_STDERR" "stderr names the term"

# ---------------------------------------------------------------------------
# Section G: Missing prefix directory and bad prefix shape
# ---------------------------------------------------------------------------
echo
echo "[G] Missing prefix dir and bad prefix shape"

# Prefix dir does not exist
OUT_ERR=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix ZZZ 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "--prefix ZZZ (missing dir) -> exit 1"
assert_contains "prefix directory does not exist" "$OUT_ERR" \
    "missing-prefix-dir error names the concept"
assert_contains "ZZZ" "$OUT_ERR" "missing-prefix-dir error names the prefix"

# `--prefix im` (lowercase) now case-folds to IM; a shape that is
# invalid even upper-cased (leading digit) still errors.
OUT_ERR=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix 1x 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "--prefix 1x (bad shape) -> exit 2"
assert_contains "invalid prefix" "$OUT_ERR" "bad-shape error names 'invalid prefix'"

# Bad prefix shape among valid ones
OUT_ERR=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix IM,bad-shape 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "--prefix IM,bad-shape -> exit 2 (any bad entry fails)"
assert_contains "invalid prefix" "$OUT_ERR" "mixed-shape error names 'invalid prefix'"

# Empty --prefix value after parsing
OUT_ERR=$("$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix "," 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "--prefix ',' (empty after parse) -> exit 2"
assert_contains "empty" "$OUT_ERR" "empty-prefix error explains the cause"

# ---------------------------------------------------------------------------
# Section H: Missing TERM
# ---------------------------------------------------------------------------
echo
echo "[H] Missing TERM"

OUT_ERR=$("$SEARCH_HISTORY" 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "no TERM -> exit 2"
assert_contains "missing TERM" "$OUT_ERR" "missing-TERM error is explicit"

# ---------------------------------------------------------------------------
# Section I: Legacy-mode error carries the git log fallback
# ---------------------------------------------------------------------------
echo
echo "[I] Legacy-mode error with fallback"

LEGACY_DIR="$TMPDIR_ROOT/legacy-cwd"
mkdir -p "$LEGACY_DIR"
(
    cd "$LEGACY_DIR"
    unset ISSUE_STORE_ROOT
    "$SEARCH_HISTORY" "FALLBACK_TERM" 2>"$TMPDIR_ROOT/legacy.err" >"$TMPDIR_ROOT/legacy.out"
    echo $? > "$TMPDIR_ROOT/legacy.rc"
)
LEGACY_RC=$(cat "$TMPDIR_ROOT/legacy.rc")
LEGACY_ERR=$(cat "$TMPDIR_ROOT/legacy.err")
LEGACY_OUT=$(cat "$TMPDIR_ROOT/legacy.out")

assert_eq "1" "$LEGACY_RC" "legacy mode -> exit 1"
assert_eq "" "$LEGACY_OUT" "legacy mode keeps stdout empty"
assert_contains "store mode not active" "$LEGACY_ERR" \
    "legacy error names the precondition"
assert_contains 'git log -S "FALLBACK_TERM" -- .issues/' "$LEGACY_ERR" \
    "legacy error carries the exact fallback command (with the requested term)"
assert_contains "operates only on the issue store" "$LEGACY_ERR" \
    "legacy error names the store-only boundary"

# ---------------------------------------------------------------------------
# Section J: Performance -- sub-second on synthetic corpus
# ---------------------------------------------------------------------------
echo
echo "[J] Performance"

# Time a wide-scope search.  Use Python for portable millisecond timing
# (macOS bash 3.2 lacks $EPOCHREALTIME).
PERF_MS=$(python3 -c "
import subprocess, time
t0 = time.time()
subprocess.run(['$SEARCH_HISTORY', 'MAGIC_TOKEN', '--json'],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
t1 = time.time()
print(int((t1 - t0) * 1000))
")
if [ "$PERF_MS" -lt 1000 ]; then
    pass "search-history completes sub-second (${PERF_MS}ms)"
else
    fail "search-history slower than 1s: ${PERF_MS}ms"
fi

# ---------------------------------------------------------------------------
# Section K: Read-only -- no .locks/ entries created
# ---------------------------------------------------------------------------
echo
echo "[K] Read-only: no locks acquired"

"$SEARCH_HISTORY" "MAGIC_TOKEN" >/dev/null 2>&1
"$SEARCH_HISTORY" "MAGIC_TOKEN" --json >/dev/null 2>&1
"$SEARCH_HISTORY" "MAGIC_TOKEN" --prefix IM,DOUG --since 1.year.ago >/dev/null 2>&1
"$SEARCH_HISTORY" "NEVER_APPEARS" >/dev/null 2>&1

if find "$STORE" -type d -name ".locks" 2>/dev/null | grep -q .; then
    fail ".locks/ directory was created by search-history (should not happen)"
else
    pass "no .locks/ directory created by search-history"
fi

# ---------------------------------------------------------------------------
# Section L: Sentinel-based store mode (cover the non-env path)
# ---------------------------------------------------------------------------
echo
echo "[L] Sentinel-based store-mode detection"

SENTINEL_DIR="$TMPDIR_ROOT/sentinel-proj/.issues"
mkdir -p "$SENTINEL_DIR"
printf "%s" "$STORE" > "$SENTINEL_DIR/.migrated-to-store"

(
    cd "$TMPDIR_ROOT/sentinel-proj"
    unset ISSUE_STORE_ROOT
    "$SEARCH_HISTORY" "MAGIC_TOKEN" >"$TMPDIR_ROOT/sentinel.out" 2>"$TMPDIR_ROOT/sentinel.err"
    echo $? > "$TMPDIR_ROOT/sentinel.rc"
)
SENTINEL_RC=$(cat "$TMPDIR_ROOT/sentinel.rc")
SENTINEL_OUT=$(cat "$TMPDIR_ROOT/sentinel.out")
assert_eq "0" "$SENTINEL_RC" "sentinel-mode happy-path -> exit 0"
SENTINEL_LINES=$(echo "$SENTINEL_OUT" | grep -c .)
assert_eq "5" "$SENTINEL_LINES" "sentinel-mode returns the same 5 matches"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "----- search-history verification summary -----"
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
