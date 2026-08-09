#!/usr/bin/env bash
# Verification script for the activity-issues subcommand.
#
# Exercises the acceptance criteria that benefit from a real
# filesystem, a real git repo, and a real subprocess:
#   - Groups by issue with most-recent commit shown
#   - Default time window is 7 days; --since overrides
#   - --prefix scopes to one or more prefixes (OR within the flag)
#   - --limit caps issues, not commits
#   - JSON output via --json is parseable and complete
#   - Handles non-issue commits (INDEX.md, .repos, rebased migration
#     commits) gracefully by ignoring them
#   - Zero-activity case has empty stdout + stderr note + exit 0
#   - Argument validation: bad --prefix, bad --limit (exit 2)
#   - Missing prefix dir reports clearly (exit 1, distinct from "no
#     activity")
#   - Legacy-mode error includes the exact `git log` fallback command
#   - Sentinel-based store-mode detection works
#   - --help / --version
#   - Does not acquire any locks (read-only: no .locks/ entries)
#
# Run from the project root:
#     tests/verification/activity-issues.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ACTIVITY="$(resolve_cli activity-issues)"

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
        fail "$3: unexpectedly found '$1' in:"
        echo "$2" | sed 's/^/  | /'
    else
        pass "$3 (does not contain '$1')"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im033-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# ---------------------------------------------------------------------------
# Build a synthetic issue store as a real git repo with backdated commits
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

# Compute absolute ISO 8601 timestamps via Python (portable). GIT_AUTHOR_DATE
# does not accept approxidate forms ("1.hour.ago"), so we precompute.
abs_date() {
    # $1 = seconds offset (negative for past)
    python3 -c "
import datetime, sys
delta = datetime.timedelta(seconds=int(sys.argv[1]))
t = datetime.datetime.now(datetime.timezone.utc) + delta
print(t.strftime('%Y-%m-%dT%H:%M:%S+0000'))
" "$1"
}

# Backdated commits inside the default 7-day window. abs_date offsets in
# seconds keep ordering deterministic and well inside the window.
T_60M_AGO=$(abs_date -3600)
T_30M_AGO=$(abs_date -1800)
T_20M_AGO=$(abs_date -1200)
T_10M_AGO=$(abs_date -600)
T_05M_AGO=$(abs_date -300)
T_02M_AGO=$(abs_date -120)

commit_file() {
    local rel="$1" content="$2" message="$3" date="$4"
    mkdir -p "$STORE/$(dirname "$rel")"
    echo "$content" > "$STORE/$rel"
    (
        cd "$STORE"
        git add "$rel" || exit 1
        GIT_AUTHOR_DATE="$date" GIT_COMMITTER_DATE="$date" \
            git commit --quiet -m "$message" || exit 1
    ) || { echo "FIXTURE FAILED: commit $rel ($date)"; exit 2; }
}

# IM-099 is committed FIRST in the DAG (with a backdated author date)
# so that the recent commits ride on top of it. Git's `--since` is a
# walk-stop condition rather than a filter -- it stops descending past
# the first commit older than the cutoff. With IM-099 as the deepest
# ancestor, the walker emits every recent commit and then stops at
# IM-099 naturally. Inverting this order makes `--since 7.days.ago`
# return nothing because IM-099 is the very first commit the walker
# visits.
commit_file "IM/IM-099.md" "v1" "feat: ancient IM-099" "2026-01-01T00:00:00+0000"

commit_file "IM/IM-001.md"    "v1"   "feat: first IM-001"  "$T_60M_AGO"
commit_file "DOUG/DOUG-001.md" "v1"  "feat: DOUG-001 init" "$T_30M_AGO"
commit_file "IM/IM-002.md"    "v1"   "feat: IM-002"        "$T_20M_AGO"
commit_file "IM/IM-001.md"    "v2"   "fix: IM-001 again"   "$T_10M_AGO"

# A store-maintenance commit: INDEX.md only. Should not appear.
echo "# index" > "$STORE/INDEX.md"
(
    cd "$STORE"
    git add INDEX.md
    GIT_AUTHOR_DATE="$T_05M_AGO" GIT_COMMITTER_DATE="$T_05M_AGO" \
        git commit --quiet -m "chore: regenerate INDEX"
)

# A mixed commit: INDEX.md + IM-003. IM-003 should count once.
echo "## updated" >> "$STORE/INDEX.md"
echo "v1" > "$STORE/IM/IM-003.md"
(
    cd "$STORE"
    git add INDEX.md IM/IM-003.md
    GIT_AUTHOR_DATE="$T_02M_AGO" GIT_COMMITTER_DATE="$T_02M_AGO" \
        git commit --quiet -m "feat: IM-003 + reindex"
)

export ISSUE_STORE_ROOT="$STORE"

# ---------------------------------------------------------------------------
# Section A: --help / --version
# ---------------------------------------------------------------------------
echo
echo "[A] Help and version"

HELP_OUTPUT=$("$ACTIVITY" --help 2>&1)
assert_contains "activity-issues" "$HELP_OUTPUT" "--help prints tool banner"
assert_contains "--since" "$HELP_OUTPUT" "--help documents --since flag"
assert_contains "--prefix" "$HELP_OUTPUT" "--help documents --prefix flag"
assert_contains "--limit" "$HELP_OUTPUT" "--help documents --limit flag"
assert_contains "--json" "$HELP_OUTPUT" "--help documents --json flag"
assert_contains "7.days.ago" "$HELP_OUTPUT" "--help documents 7-day default"
assert_contains "Examples:" "$HELP_OUTPUT" "--help has an Examples block"
assert_contains "ISSUE_STORE_ROOT" "$HELP_OUTPUT" "--help documents store mode"
assert_contains "out of scope for this surface" "$HELP_OUTPUT" "--help states the legacy-mode boundary"
assert_contains "git log" "$HELP_OUTPUT" "--help shows the legacy fallback command"

VERSION_OUTPUT=$("$ACTIVITY" --version)
assert_contains "v" "$VERSION_OUTPUT" "--version prints a version string"

# ---------------------------------------------------------------------------
# Section B: Default window TSV output
# ---------------------------------------------------------------------------
echo
echo "[B] Default 7-day window, all prefixes"

OUT=$("$ACTIVITY")
RC=$?
assert_eq "0" "$RC" "default invocation exits 0"

# Should see IM-001 (twice), IM-002, IM-003, DOUG-001. NOT IM-099 (old).
LINE_COUNT=$(printf '%s' "$OUT" | awk 'NF{n++} END{print n+0}')
assert_eq "4" "$LINE_COUNT" "default output has 4 issue rows (IM-001/2/3 + DOUG-001)"
assert_contains "IM-001" "$OUT" "default output includes IM-001"
assert_contains "IM-002" "$OUT" "default output includes IM-002"
assert_contains "IM-003" "$OUT" "default output includes IM-003"
assert_contains "DOUG-001" "$OUT" "default output includes DOUG-001"
assert_not_contains "IM-099" "$OUT" "default output excludes IM-099 (older than 7 days)"

# Most-recent commit first: IM-003 was touched most recently (2.minutes.ago).
FIRST_ID=$(echo "$OUT" | head -1 | cut -f1)
assert_eq "IM-003" "$FIRST_ID" "newest-first sort: IM-003 leads"

# TSV column shape: id, date, count, author, sha, subject (6 tab-separated)
# COVERS: history.subcommands
COL_COUNT=$(echo "$OUT" | head -1 | awk -F'\t' '{print NF}')
assert_eq "6" "$COL_COUNT" "TSV row has 6 tab-separated columns"

# Every row carries the same six tab-separated columns (no jagged rows).
JAGGED=$(echo "$OUT" | awk -F'\t' 'NF && NF != 6 {bad++} END {print bad+0}')
assert_eq "0" "$JAGGED" "every TSV row has exactly 6 columns (no jagged rows)"

# IM-001 count is 2 (touched twice)
IM001_COUNT=$(echo "$OUT" | awk -F'\t' '$1 == "IM-001" {print $3}')
assert_eq "2" "$IM001_COUNT" "IM-001 commit_count is 2 (touched twice in window)"

# Latest subject for IM-001 should be the more recent one
IM001_SUBJECT=$(echo "$OUT" | awk -F'\t' '$1 == "IM-001" {print $6}')
assert_eq "fix: IM-001 again" "$IM001_SUBJECT" \
    "IM-001 latest subject is the newer commit's"

# IM-002 count is 1
IM002_COUNT=$(echo "$OUT" | awk -F'\t' '$1 == "IM-002" {print $3}')
assert_eq "1" "$IM002_COUNT" "IM-002 commit_count is 1"

# IM-003 count is 1 (from the mixed commit -- INDEX.md doesn't bump it)
IM003_COUNT=$(echo "$OUT" | awk -F'\t' '$1 == "IM-003" {print $3}')
assert_eq "1" "$IM003_COUNT" "IM-003 commit_count is 1 (mixed commit counts once)"

# Full 40-char SHA in column 5
IM001_SHA=$(echo "$OUT" | awk -F'\t' '$1 == "IM-001" {print $5}')
assert_eq "40" "${#IM001_SHA}" "TSV column 5 is full 40-char SHA"

# Author in column 4
IM001_AUTHOR=$(echo "$OUT" | awk -F'\t' '$1 == "IM-001" {print $4}')
assert_eq "Verify User" "$IM001_AUTHOR" "TSV column 4 is author name"

# ISO date in column 2
IM001_DATE=$(echo "$OUT" | awk -F'\t' '$1 == "IM-001" {print $2}')
if echo "$IM001_DATE" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'; then
    pass "TSV column 2 is ISO8601 date ($IM001_DATE)"
else
    fail "TSV column 2 is not ISO8601: '$IM001_DATE'"
fi

# TSV row fields must agree byte-for-byte with the matching `--json`
# entry's `latest` dict. This pins the documented `<id>\t<date>\t<count>
# \t<author>\t<sha>\t<subject>` shape against the JSON source of truth
# so a future refactor that reshuffles TSV emission gets caught.
# COVERS: output.json-stable
JSON_FULL=$("$ACTIVITY" --json)
echo "$JSON_FULL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
im001 = next(i for i in d['issues'] if i['id'] == 'IM-001')
print(f\"{im001['id']}\t{im001['latest']['date']}\t{im001['commit_count']}\t\"
      f\"{im001['latest']['author']}\t{im001['latest']['sha']}\t{im001['latest']['subject']}\")
" > "$TMPDIR_ROOT/im001_from_json.tsv"
IM001_TSV_ROW=$(echo "$OUT" | awk -F'\t' '$1 == "IM-001" {print; exit}')
JSON_DERIVED_ROW=$(cat "$TMPDIR_ROOT/im001_from_json.tsv")
assert_eq "$JSON_DERIVED_ROW" "$IM001_TSV_ROW" \
    "TSV row for IM-001 byte-matches a row reconstructed from --json"

# ---------------------------------------------------------------------------
# Section C: Non-issue commits ignored ("rebased migration" case)
# ---------------------------------------------------------------------------
echo
echo "[C] Non-issue files ignored"

# The INDEX-only commit ("chore: regenerate INDEX") must not appear
# anywhere in the output -- its subject is unique enough to grep.
assert_not_contains "regenerate INDEX" "$OUT" \
    "INDEX-only commit absent from output (non-issue files ignored)"

# The mixed commit's subject ("feat: IM-003 + reindex") DOES appear,
# because IM-003 is an issue file -- but only once (count=1, not 2).
assert_contains "IM-003 + reindex" "$OUT" \
    "mixed commit subject appears as IM-003's latest"

# ---------------------------------------------------------------------------
# Section D: --since cutoff
# ---------------------------------------------------------------------------
echo
echo "[D] --since cutoff (absolute + relative)"

# --since 30.years.ago should include the ancient IM-099 too
OUT_ALL=$("$ACTIVITY" --since 30.years.ago)
assert_contains "IM-099" "$OUT_ALL" "--since 30.years.ago includes the ancient commit"

# Absolute date cutoff: 2020-01-01 includes everything
OUT_ABS=$("$ACTIVITY" --since 2020-01-01)
assert_contains "IM-099" "$OUT_ABS" "--since 2020-01-01 includes IM-099"
assert_contains "IM-001" "$OUT_ABS" "--since 2020-01-01 includes IM-001"

# Empty --since: also includes everything (all history)
OUT_EMPTY=$("$ACTIVITY" --since "")
assert_contains "IM-099" "$OUT_EMPTY" "--since '' (empty) includes IM-099"

# ---------------------------------------------------------------------------
# Section E: --prefix scoping
# ---------------------------------------------------------------------------
echo
echo "[E] --prefix scoping"

OUT_IM=$("$ACTIVITY" --prefix IM)
assert_contains "IM-001" "$OUT_IM" "--prefix IM includes IM-001"
assert_not_contains "DOUG-001" "$OUT_IM" "--prefix IM excludes DOUG-001"

OUT_DOUG=$("$ACTIVITY" --prefix DOUG)
assert_contains "DOUG-001" "$OUT_DOUG" "--prefix DOUG includes DOUG-001"
assert_not_contains "IM-001" "$OUT_DOUG" "--prefix DOUG excludes IM-001"

# Multi-prefix as comma-separated list
OUT_BOTH=$("$ACTIVITY" --prefix IM,DOUG)
assert_contains "IM-001" "$OUT_BOTH" "--prefix IM,DOUG includes IM-001"
assert_contains "DOUG-001" "$OUT_BOTH" "--prefix IM,DOUG includes DOUG-001"

# Whitespace and trailing-comma tolerance
OUT_WS=$("$ACTIVITY" --prefix " IM , DOUG ,")
assert_contains "IM-001" "$OUT_WS" "--prefix tolerates whitespace and trailing comma"
assert_contains "DOUG-001" "$OUT_WS" "--prefix tolerates whitespace (DOUG)"

# Duplicate-prefix tolerance: --prefix IM,IM,DOUG returns the same set
# as --prefix IM,DOUG (no doubled rows, no error). git's pathspec is
# repeat-tolerant; parse_prefix_list preserves order including dupes
# and the underlying scan dedups naturally.
OUT_DUP=$("$ACTIVITY" --prefix "IM,IM,DOUG")
DUP_ROWS=$(printf '%s' "$OUT_DUP" | awk 'NF{n++} END{print n+0}')
PLAIN_ROWS=$(printf '%s' "$OUT_BOTH" | awk 'NF{n++} END{print n+0}')
assert_eq "$PLAIN_ROWS" "$DUP_ROWS" \
    "--prefix IM,IM,DOUG produces same row count as IM,DOUG (duplicate tolerated)"
# IM-001 must appear exactly once even with the duplicated prefix.
IM001_REPS=$(echo "$OUT_DUP" | awk -F'\t' '$1 == "IM-001"' | wc -l | tr -d ' ')
assert_eq "1" "$IM001_REPS" \
    "duplicated --prefix does not duplicate per-issue rows"

# --prefix that matches an existing-but-empty prefix directory ->
# exit 0, no rows. Distinct from the missing-prefix-dir case above
# (which is exit 1). This is the activity-equivalent of the
# zero-activity case scoped to a real prefix.
# COVERS: output.exit-codes
mkdir -p "$STORE/EMPTY"
"$ACTIVITY" --prefix EMPTY >"$TMPDIR_ROOT/empty.out" 2>"$TMPDIR_ROOT/empty.err"
EMPTY_RC=$?
assert_eq "0" "$EMPTY_RC" "--prefix EMPTY (no commits touch EMPTY/) -> exit 0"
EMPTY_STDOUT=$(cat "$TMPDIR_ROOT/empty.out")
assert_eq "" "$EMPTY_STDOUT" \
    "--prefix EMPTY produces no TSV rows"
EMPTY_STDERR=$(cat "$TMPDIR_ROOT/empty.err")
assert_contains "no activity" "$EMPTY_STDERR" \
    "--prefix EMPTY surfaces the standard no-activity stderr note"

# `--prefix im` now case-folds to IM; a shape invalid even
# upper-cased (leading digit) still errors exit 2.
OUT_ERR=$("$ACTIVITY" --prefix 1x 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "bad-shape --prefix -> exit 2"
assert_contains "invalid prefix" "$OUT_ERR" "bad-shape --prefix error names 'invalid prefix'"

OUT_ERR=$("$ACTIVITY" --prefix "IM,bad-prefix" 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "mixed-list with one bad shape -> exit 2"

# Empty --prefix value -> exit 2
OUT_ERR=$("$ACTIVITY" --prefix "," 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "--prefix containing only separators -> exit 2"
assert_contains "empty after parsing" "$OUT_ERR" \
    "empty-prefix error names 'empty after parsing'"

# Missing prefix dir -> exit 1, distinct from "no activity"
OUT_ERR=$("$ACTIVITY" --prefix ZZZ 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "missing prefix dir -> exit 1"
assert_contains "prefix directory does not exist" "$OUT_ERR" \
    "missing-prefix-dir error is explicit"

# ---------------------------------------------------------------------------
# Section F: --limit
# ---------------------------------------------------------------------------
echo
echo "[F] --limit caps issues, not commits"

OUT_L1=$("$ACTIVITY" --limit 1)
ROW_COUNT=$(printf '%s' "$OUT_L1" | awk 'NF{n++} END{print n+0}')
assert_eq "1" "$ROW_COUNT" "--limit 1 returns exactly one row"
FIRST_ID=$(echo "$OUT_L1" | head -1 | cut -f1)
assert_eq "IM-003" "$FIRST_ID" "--limit 1 returns the newest issue (IM-003)"

OUT_L2=$("$ACTIVITY" --limit 2)
ROW_COUNT=$(printf '%s' "$OUT_L2" | awk 'NF{n++} END{print n+0}')
assert_eq "2" "$ROW_COUNT" "--limit 2 returns exactly two rows"

# When the second-row IM-001 is included, its count is still 2 (not
# capped by --limit, which slices after grouping).
OUT_L_FULL=$("$ACTIVITY" --limit 99)
IM001_COUNT=$(echo "$OUT_L_FULL" | awk -F'\t' '$1 == "IM-001" {print $3}')
assert_eq "2" "$IM001_COUNT" "--limit caps issues after grouping (IM-001 keeps count=2)"

# Invalid --limit
OUT_ERR=$("$ACTIVITY" --limit abc 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "non-integer --limit -> exit 2"
assert_contains "positive integer" "$OUT_ERR" "bad-limit error names 'positive integer'"

OUT_ERR=$("$ACTIVITY" --limit 0 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "--limit 0 -> exit 2"

OUT_ERR=$("$ACTIVITY" --limit -3 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "--limit -3 -> exit 2"

# ---------------------------------------------------------------------------
# Section G: --json shape (populated)
# ---------------------------------------------------------------------------
echo
echo "[G] --json shape"

JSON_OUT=$("$ACTIVITY" --json)
RC=$?
assert_eq "0" "$RC" "--json exits 0"

echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['since'] == '7.days.ago', d['since']
assert d['prefixes'] is None, d['prefixes']
assert d['limit'] is None, d['limit']
assert 'store' in d and d['store'].endswith('/store'), d['store']
assert isinstance(d['issues'], list), d
ids = [i['id'] for i in d['issues']]
assert 'IM-001' in ids and 'IM-002' in ids and 'IM-003' in ids and 'DOUG-001' in ids, ids
assert 'IM-099' not in ids, ids
# IM-001 entry: count=2, latest is a Commit dict
im001 = next(i for i in d['issues'] if i['id'] == 'IM-001')
assert im001['prefix'] == 'IM', im001
assert im001['file'] == 'IM/IM-001.md', im001
assert im001['commit_count'] == 2, im001
assert len(im001['latest']['sha']) == 40, im001
assert im001['latest']['subject'] == 'fix: IM-001 again', im001
# Newest first
assert d['issues'][0]['id'] == 'IM-003', d['issues'][0]
print('json-shape-ok')
" >"$TMPDIR_ROOT/jsoncheck.out" 2>&1
if grep -q "json-shape-ok" "$TMPDIR_ROOT/jsoncheck.out"; then
    pass "--json carries since/prefixes/limit/store/issues with correct shape"
else
    fail "--json shape wrong: $(cat "$TMPDIR_ROOT/jsoncheck.out")"
fi

# --json with all flags populated
JSON_OUT=$("$ACTIVITY" --json --prefix IM --since 30.years.ago --limit 3)
echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['since'] == '30.years.ago', d
assert d['prefixes'] == ['IM'], d
assert d['limit'] == 3, d
ids = [i['id'] for i in d['issues']]
# IM only, capped at 3
for iid in ids:
    assert iid.startswith('IM-'), iid
assert len(ids) <= 3, ids
print('json-flags-ok')
" >"$TMPDIR_ROOT/jsoncheck2.out" 2>&1
if grep -q "json-flags-ok" "$TMPDIR_ROOT/jsoncheck2.out"; then
    pass "--json echoes since/prefixes/limit as supplied"
else
    fail "--json flag echo wrong: $(cat "$TMPDIR_ROOT/jsoncheck2.out")"
fi

# ---------------------------------------------------------------------------
# Section H: Zero-activity case
# ---------------------------------------------------------------------------
echo
echo "[H] Zero-activity case"

# `--since` in the future yields no matches deterministically.
"$ACTIVITY" --since 2030-01-01 >"$TMPDIR_ROOT/zero.out" 2>"$TMPDIR_ROOT/zero.err"
ZERO_RC=$?
assert_eq "0" "$ZERO_RC" "zero-activity -> exit 0 (grep convention)"
ZERO_STDOUT=$(cat "$TMPDIR_ROOT/zero.out")
ZERO_STDERR=$(cat "$TMPDIR_ROOT/zero.err")
assert_eq "" "$ZERO_STDOUT" "zero-activity stdout is empty"
assert_contains "no activity" "$ZERO_STDERR" "zero-activity stderr names 'no activity'"

# Same case via --json: stdout has a parseable empty list
JSON_ZERO=$("$ACTIVITY" --since 2030-01-01 --json)
echo "$JSON_ZERO" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['issues'] == [], d
print('json-zero-ok')
" >"$TMPDIR_ROOT/jsonzero.out" 2>&1
if grep -q "json-zero-ok" "$TMPDIR_ROOT/jsonzero.out"; then
    pass "--json zero-activity: stdout has issues:[]"
else
    fail "--json zero shape wrong: $(cat "$TMPDIR_ROOT/jsonzero.out")"
fi

# ---------------------------------------------------------------------------
# Section I: Legacy mode error
# ---------------------------------------------------------------------------
echo
echo "[I] Legacy-mode error carries the git log fallback"

LEGACY_DIR="$TMPDIR_ROOT/legacy-cwd"
mkdir -p "$LEGACY_DIR"
(
    cd "$LEGACY_DIR"
    unset ISSUE_STORE_ROOT
    "$ACTIVITY" --since 14.days.ago \
        2>"$TMPDIR_ROOT/legacy.err" \
        >"$TMPDIR_ROOT/legacy.out"
    echo $? > "$TMPDIR_ROOT/legacy.rc"
)
LEGACY_RC=$(cat "$TMPDIR_ROOT/legacy.rc")
LEGACY_ERR=$(cat "$TMPDIR_ROOT/legacy.err")
LEGACY_OUT=$(cat "$TMPDIR_ROOT/legacy.out")

assert_eq "1" "$LEGACY_RC" "legacy mode -> exit 1"
assert_eq "" "$LEGACY_OUT" "legacy mode keeps stdout empty"
assert_contains "store mode not active" "$LEGACY_ERR" \
    "legacy error names the precondition"
assert_contains "git log --since=14.days.ago --name-only -- .issues/" "$LEGACY_ERR" \
    "legacy error carries the exact fallback command with the requested --since"
assert_contains "operates only on the issue store" "$LEGACY_ERR" \
    "legacy error names the store-only boundary"

# Default --since legacy fallback uses 7.days.ago
(
    cd "$LEGACY_DIR"
    unset ISSUE_STORE_ROOT
    "$ACTIVITY" 2>"$TMPDIR_ROOT/legacy2.err" >/dev/null
)
LEGACY_ERR=$(cat "$TMPDIR_ROOT/legacy2.err")
assert_contains "git log --since=7.days.ago" "$LEGACY_ERR" \
    "legacy default uses 7.days.ago in fallback command"

# ---------------------------------------------------------------------------
# Section J: Sentinel-based store-mode detection
# ---------------------------------------------------------------------------
echo
echo "[J] Sentinel-based store-mode detection"

SENTINEL_DIR="$TMPDIR_ROOT/sentinel-proj/.issues"
mkdir -p "$SENTINEL_DIR"
printf "%s" "$STORE" > "$SENTINEL_DIR/.migrated-to-store"

(
    cd "$TMPDIR_ROOT/sentinel-proj"
    unset ISSUE_STORE_ROOT
    "$ACTIVITY" >"$TMPDIR_ROOT/sentinel.out" 2>"$TMPDIR_ROOT/sentinel.err"
    echo $? > "$TMPDIR_ROOT/sentinel.rc"
)
SENTINEL_RC=$(cat "$TMPDIR_ROOT/sentinel.rc")
SENTINEL_OUT=$(cat "$TMPDIR_ROOT/sentinel.out")
assert_eq "0" "$SENTINEL_RC" "sentinel-mode happy-path -> exit 0"
assert_contains "IM-001" "$SENTINEL_OUT" \
    "sentinel-mode returns the same output as env-mode"

# ---------------------------------------------------------------------------
# Section L: ISSUE_MGMT_ROOT env override
# ---------------------------------------------------------------------------
echo
echo "[L] ISSUE_MGMT_ROOT env override for project-anchor resolution"

# find_project_anchor() falls back to $ISSUE_MGMT_ROOT/.issues when no
# .issues ancestor of cwd exists. Wire a synthetic anchor that points
# at the sentinel file we already wrote in Section J, then run from a
# cwd that has no .issues ancestry and with ISSUE_STORE_ROOT unset --
# the only path that reaches the store is via ISSUE_MGMT_ROOT.
MGMT_DIR="$TMPDIR_ROOT/mgmt-anchor"
mkdir -p "$MGMT_DIR/.issues"
printf "%s" "$STORE" > "$MGMT_DIR/.issues/.migrated-to-store"

ORPHAN_DIR="$TMPDIR_ROOT/orphan-cwd"
mkdir -p "$ORPHAN_DIR"
(
    cd "$ORPHAN_DIR"
    unset ISSUE_STORE_ROOT
    ISSUE_MGMT_ROOT="$MGMT_DIR" "$ACTIVITY" \
        >"$TMPDIR_ROOT/mgmt.out" 2>"$TMPDIR_ROOT/mgmt.err"
    echo $? > "$TMPDIR_ROOT/mgmt.rc"
)
MGMT_RC=$(cat "$TMPDIR_ROOT/mgmt.rc")
MGMT_OUT=$(cat "$TMPDIR_ROOT/mgmt.out")
assert_eq "0" "$MGMT_RC" "ISSUE_MGMT_ROOT -> sentinel -> store: exit 0"
assert_contains "IM-001" "$MGMT_OUT" \
    "ISSUE_MGMT_ROOT-anchored run returns the same store contents"

# Sanity: clearing ISSUE_MGMT_ROOT from the same orphan cwd is legacy.
(
    cd "$ORPHAN_DIR"
    unset ISSUE_STORE_ROOT
    unset ISSUE_MGMT_ROOT
    "$ACTIVITY" >/dev/null 2>"$TMPDIR_ROOT/mgmt-off.err"
    echo $? > "$TMPDIR_ROOT/mgmt-off.rc"
)
MGMT_OFF_RC=$(cat "$TMPDIR_ROOT/mgmt-off.rc")
assert_eq "1" "$MGMT_OFF_RC" \
    "without ISSUE_MGMT_ROOT and without ISSUE_STORE_ROOT: legacy-mode exit 1"
assert_contains "store mode not active" "$(cat "$TMPDIR_ROOT/mgmt-off.err")" \
    "without ISSUE_MGMT_ROOT: legacy diagnostic still surfaces"

# ---------------------------------------------------------------------------
# Section K: Read-only -- no .locks/ entries created
# ---------------------------------------------------------------------------
echo
echo "[K] Read-only: no locks acquired"

"$ACTIVITY" >/dev/null 2>&1
"$ACTIVITY" --prefix IM >/dev/null 2>&1
"$ACTIVITY" --since 30.years.ago --json >/dev/null 2>&1
"$ACTIVITY" --limit 1 >/dev/null 2>&1

if find "$STORE" -type d -name ".locks" 2>/dev/null | grep -q .; then
    fail ".locks/ directory was created by activity-issues (should not happen)"
else
    pass "no .locks/ directory created by activity-issues"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "----- activity-issues verification summary -----"
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
