#!/usr/bin/env bash
# Verification script for the search-issues subcommand.
#
# Exercises the acceptance criteria that benefit from a real
# filesystem and a real subprocess:
#   - Cross-prefix scan in store mode when --prefix is absent
#   - --prefix PFX[,PFX] comma-separated scoping (acceptance)
#   - Body query + frontmatter filters AND together (acceptance)
#   - TSV default output is parseable (acceptance: "one match per line")
#   - --json emits a top-level array with the expected per-issue shape
#   - --syntax regex + --case-sensitive
#   - Exit codes: 0 (match), 1 (no match), 2 (arg error / bad regex)
#   - --help documents all flags with examples (acceptance)
#   - --version prints the version string
#   - Sub-second performance against a large local issue corpus
#     (acceptance: sub-second on ~40 issues)
#
# Run from the project root:
#     tests/verification/search-issues.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
SEARCH="$(resolve_cli search-issues)"

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

TMPDIR_ROOT=$(mktemp -d -t im025-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

export PATH="$ISSUE_MGMT_BIN:$PATH"

make_issue() {
    # make_issue <path> <id> <status> <type> <body>
    local path=$1 id=$2 status=$3 type=$4 body=$5
    mkdir -p "$(dirname "$path")"
    cat > "$path" <<EOF
---
id: $id
title: "Sample $id"
type: $type
status: $status
priority: medium
created: 2026-05-14
updated: 2026-05-14
branch: ""
tags: []
changelog:
---

## Summary

$body

## Implementation Notes

## Archived Plan Content
EOF
}

# ---------------------------------------------------------------------------
# Section A: --help / --version (cheap)
# ---------------------------------------------------------------------------
echo
echo "[A] Help and version"

HELP_OUTPUT=$("$SEARCH" --help 2>&1)
# COVERS: surface.self-naming
# Matched the retired flat name, which survives only as a qualified
# lineage note in the DESCRIPTION -- so it proved nothing about the
# banner. Assert the current form (IM-239).
assert_contains "issue search" "$HELP_OUTPUT" "--help names the current verb"
assert_contains "--syntax" "$HELP_OUTPUT" "--help documents --syntax flag"
assert_contains "--prefix" "$HELP_OUTPUT" "--help documents --prefix flag"
assert_contains "--status" "$HELP_OUTPUT" "--help documents --status flag"
assert_contains "--type" "$HELP_OUTPUT" "--help documents --type flag"
assert_contains "--json" "$HELP_OUTPUT" "--help documents --json flag"
assert_contains "Examples:" "$HELP_OUTPUT" "--help has an Examples block"
assert_contains 'search-issues "concurrent edit"' "$HELP_OUTPUT" \
    "--help shows a substring example"
assert_contains "--prefix ABC,XYZ" "$HELP_OUTPUT" \
    "--help shows a comma-separated --prefix example"

VERSION_OUTPUT=$("$SEARCH" --version)
assert_contains "v" "$VERSION_OUTPUT" "--version prints a version string"

# ---------------------------------------------------------------------------
# Section B: store mode -- cross-prefix scan
# ---------------------------------------------------------------------------
echo
echo "[B] Store mode -- cross-prefix scan"

STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE"
make_issue "$STORE/IM/IM-001.md"    "IM-001"    "open"        "bug"     "Body mentions concurrent edits."
make_issue "$STORE/IM/IM-002.md"    "IM-002"    "in-progress" "feature" "Unrelated text here."
make_issue "$STORE/IM/IM-003.md"    "IM-003"    "done"        "bug"     "Concurrent fix landed."
make_issue "$STORE/DOUG/DOUG-001.md" "DOUG-001" "open"        "feature" "Concurrent rollout strategy."
make_issue "$STORE/DOUG/DOUG-002.md" "DOUG-002" "blocked"     "bug"     "No match here."
make_issue "$STORE/RM/RM-001.md"    "RM-001"    "open"        "chore"   "Concurrent installs are tricky."

# Decoys -- should NOT appear in any result
echo "decoy" > "$STORE/INDEX.md"
echo "decoy" > "$STORE/IM/INDEX.md"
echo "decoy" > "$STORE/.repos"
mkdir -p "$STORE/lowercase"
make_issue "$STORE/lowercase/x-001.md" "x-001" "open" "bug" "concurrent in a non-prefix dir"

export ISSUE_STORE_ROOT="$STORE"

# Cross-prefix scan: no --prefix means every PREFIX dir is scanned.
OUT=$("$SEARCH" "concurrent")
RC=$?
assert_eq "0" "$RC" "exit 0 when there are matches"
# Each prefix should contribute at least one match
assert_contains "IM-001" "$OUT" "cross-prefix scan hits IM-001"
assert_contains "DOUG-001" "$OUT" "cross-prefix scan hits DOUG-001"
assert_contains "RM-001" "$OUT" "cross-prefix scan hits RM-001"
# Decoy non-prefix dir is excluded
assert_not_contains "x-001" "$OUT" "lowercase non-prefix dir is skipped"
# INDEX.md decoys excluded
assert_not_contains "decoy" "$OUT" "INDEX.md decoys excluded"

# TSV shape: <id>\t<status>\t<priority>\t<title>
FIRST_LINE=$(echo "$OUT" | head -1)
ID_COL=$(echo "$FIRST_LINE" | cut -f1)
STATUS_COL=$(echo "$FIRST_LINE" | cut -f2)
PRIORITY_COL=$(echo "$FIRST_LINE" | cut -f3)
TITLE_COL=$(echo "$FIRST_LINE" | cut -f4)
case "$ID_COL" in
    DOUG-*|IM-*|RM-*) pass "TSV id column parses (got $ID_COL)" ;;
    *) fail "TSV id column unexpected: $ID_COL" ;;
esac
case "$STATUS_COL" in
    open|in-progress|blocked|done|cancelled) pass "TSV status column parses ($STATUS_COL)" ;;
    *) fail "TSV status column unexpected: $STATUS_COL" ;;
esac
assert_eq "medium" "$PRIORITY_COL" "TSV priority column parses"
assert_contains "Sample" "$TITLE_COL" "TSV title column parses"

# ---------------------------------------------------------------------------
# Section C: --prefix PFX,PFX scoping
# ---------------------------------------------------------------------------
echo
echo "[C] --prefix PFX,PFX scoping"

OUT=$("$SEARCH" "concurrent" --prefix "IM,DOUG")
assert_contains "IM-001"   "$OUT" "--prefix IM,DOUG includes IM"
assert_contains "DOUG-001" "$OUT" "--prefix IM,DOUG includes DOUG"
assert_not_contains "RM-001" "$OUT" "--prefix IM,DOUG excludes RM"

# Single prefix
OUT=$("$SEARCH" "concurrent" --prefix IM)
COUNT_IM=$(echo "$OUT" | grep -c '^IM-' || true)
COUNT_NON_IM=$(echo "$OUT" | grep -cv '^IM-' || true)
if [ "$COUNT_IM" -ge 2 ] && [ "$COUNT_NON_IM" -le 1 ]; then
    pass "--prefix IM scoped to IM rows only (saw $COUNT_IM IM rows)"
else
    fail "--prefix IM scoping wrong (saw $COUNT_IM IM, $COUNT_NON_IM non-IM)"
fi

# ---------------------------------------------------------------------------
# Section D: AND-of-filters with body + frontmatter
# ---------------------------------------------------------------------------
echo
echo "[D] Body + frontmatter filters AND together"

# IM bugs that mention "concurrent" -> IM-001 and IM-003
OUT=$("$SEARCH" "concurrent" --prefix IM --type bug)
assert_contains "IM-001" "$OUT" "concurrent + IM + bug includes IM-001"
assert_contains "IM-003" "$OUT" "concurrent + IM + bug includes IM-003"
assert_not_contains "IM-002" "$OUT" "concurrent + IM + bug excludes IM-002 (feature)"
assert_not_contains "DOUG-001" "$OUT" "concurrent + IM + bug excludes DOUG"

# Open IM bugs -> IM-001 only (IM-003 is done)
OUT=$("$SEARCH" "" --prefix IM --status open --type bug)
assert_contains "IM-001" "$OUT" "frontmatter-only filter: open IM bugs includes IM-001"
assert_not_contains "IM-003" "$OUT" "frontmatter-only filter: done IM-003 excluded"
assert_not_contains "IM-002" "$OUT" "frontmatter-only filter: feature IM-002 excluded"

# Comma-separated --status (OR-within-flag): open OR blocked
OUT=$("$SEARCH" "" --status "open,blocked")
assert_contains "IM-001"  "$OUT"  "OR-within --status hits open IM-001"
assert_contains "DOUG-002" "$OUT" "OR-within --status hits blocked DOUG-002"
assert_not_contains "IM-002" "$OUT" "OR-within --status excludes in-progress"
assert_not_contains "IM-003" "$OUT" "OR-within --status excludes done"

# Repeated --status accumulates, matching the comma form and `issue index`
# (IM-269). Before the fix this silently returned only the last value.
OUT=$("$SEARCH" "" --status open --status blocked)
# COVERS: surface.one-meaning-per-flag
assert_contains "IM-001"  "$OUT"  "repeated --status hits open IM-001"
assert_contains "DOUG-002" "$OUT" "repeated --status hits blocked DOUG-002"
assert_not_contains "IM-002" "$OUT" "repeated --status excludes in-progress"
OUT=$("$SEARCH" "" --status open --status "blocked,done")
# COVERS: surface.one-meaning-per-flag
assert_contains "IM-003" "$OUT" "mixed --status forms hit done IM-003"

# ---------------------------------------------------------------------------
# Section E: --json shape
# ---------------------------------------------------------------------------
echo
echo "[E] --json output shape"

JSON_OUT=$("$SEARCH" "concurrent" --prefix IM --json)
# Top-level array, each entry has the expected keys
PY_CHECK=$(python3 -c '
import json, sys
data = json.loads(sys.stdin.read())
assert isinstance(data, list), "not a list"
assert len(data) >= 1, "no entries"
required = {"id", "file", "title", "status", "priority", "type", "body_matches"}
for entry in data:
    missing = required - set(entry.keys())
    assert not missing, f"missing keys: {missing}"
    for bm in entry["body_matches"]:
        assert "line" in bm and "text" in bm, "bad body_match"
        assert isinstance(bm["line"], int) and bm["line"] >= 1, "bad line number"
print("ok")
' <<< "$JSON_OUT")
assert_eq "ok" "$PY_CHECK" "--json shape validates"

# Body-match line numbers are file-absolute: opening file at reported line
# should contain the query.
PY_CHECK=$(python3 -c '
import json, sys
data = json.loads(sys.stdin.read())
hit = data[0]
with open(hit["file"]) as f:
    lines = f.read().splitlines()
for bm in hit["body_matches"]:
    if "concurrent" not in lines[bm["line"] - 1].lower():
        print("MISMATCH")
        sys.exit(0)
print("ok")
' <<< "$JSON_OUT")
assert_eq "ok" "$PY_CHECK" "--json body_match line numbers are file-absolute"

# Frontmatter-only filter -> body_matches is empty list
JSON_OUT=$("$SEARCH" "" --prefix IM --status open --json)
PY_CHECK=$(python3 -c '
import json, sys
data = json.loads(sys.stdin.read())
for entry in data:
    if entry["body_matches"] != []:
        print("NONEMPTY")
        sys.exit(0)
print("ok")
' <<< "$JSON_OUT")
assert_eq "ok" "$PY_CHECK" "frontmatter-only --json yields empty body_matches"

# ---------------------------------------------------------------------------
# Section F: --syntax regex and --case-sensitive
# ---------------------------------------------------------------------------
echo
echo "[F] --syntax regex + --case-sensitive"

# Add an issue with mixed-case text for case checks
make_issue "$STORE/IM/IM-004.md" "IM-004" "open" "bug" "TODO: implement Lucene support."

# Substring regex metachars are literal: '.' should only match lines with literal '.'
OUT_LITERAL=$("$SEARCH" "implement Lucene")
# COVERS: cli.search-modes
assert_contains "IM-004" "$OUT_LITERAL" "literal substring matches IM-004"

# Regex with anchors: \bTODO\b matches only word-boundary TODOs
OUT_REGEX=$("$SEARCH" '\bTODO\b' --syntax regex)
assert_contains "IM-004" "$OUT_REGEX" "regex \\bTODO\\b matches IM-004"

# Invalid regex -> exit 2 with stderr
ERR=$("$SEARCH" '[unterminated' --syntax regex 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "invalid regex exits 2"
assert_contains "invalid regex" "$ERR" "invalid regex stderr explains"

# Case sensitivity
make_issue "$STORE/IM/IM-005.md" "IM-005" "open" "bug" "Concurrent edits matter."
OUT=$("$SEARCH" "concurrent")
assert_contains "IM-005" "$OUT" "case-insensitive matches Concurrent"
OUT=$("$SEARCH" "concurrent" --case-sensitive)
assert_not_contains "IM-005" "$OUT" "case-sensitive excludes Concurrent for 'concurrent'"
OUT=$("$SEARCH" "Concurrent" --case-sensitive)
assert_contains "IM-005" "$OUT" "case-sensitive matches Concurrent for 'Concurrent'"

# ---------------------------------------------------------------------------
# Section G: exit codes
# ---------------------------------------------------------------------------
echo
echo "[G] Exit codes"

"$SEARCH" "xyzzy-no-match" >/dev/null
RC=$?
assert_eq "1" "$RC" "no match exits 1"

"$SEARCH" --syntax bogus "x" 2>/dev/null
RC=$?
assert_eq "2" "$RC" "unknown --syntax exits 2"

ERR=$("$SEARCH" 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "missing query exits 2"
assert_contains "QUERY" "$ERR" "missing query stderr names the argument"

# ---------------------------------------------------------------------------
# Section H: store sentinel mode (no env var)
# ---------------------------------------------------------------------------
echo
echo "[H] Store mode via .migrated-to-store sentinel"

unset ISSUE_STORE_ROOT

SENTINEL_PROJECT="$TMPDIR_ROOT/sentinel-project"
mkdir -p "$SENTINEL_PROJECT/.issues"
echo "$STORE" > "$SENTINEL_PROJECT/.issues/.migrated-to-store"

(
    cd "$SENTINEL_PROJECT"
    OUT=$("$SEARCH" "concurrent")
    if echo "$OUT" | grep -q "DOUG-001"; then
        echo "PASS: sentinel triggers store-mode discovery (sees DOUG)"
    else
        echo "FAIL: sentinel did not trigger store mode"
        echo "$OUT" | sed 's/^/  | /'
    fi
) > /tmp/im025-sentinel.txt
cat /tmp/im025-sentinel.txt
case "$(cat /tmp/im025-sentinel.txt)" in
    PASS:*) PASS=$((PASS+1)) ;;
    *) FAIL=$((FAIL+1)) ;;
esac

# ---------------------------------------------------------------------------
# Section I: legacy mode (no store, no sentinel)
# ---------------------------------------------------------------------------
echo
echo "[I] Legacy single-anchor mode"

LEGACY="$TMPDIR_ROOT/legacy-project"
mkdir -p "$LEGACY/.issues"
make_issue "$LEGACY/.issues/IM-001.md" "IM-001" "open" "bug" "Legacy mode concurrent test."
make_issue "$LEGACY/.issues/DOUG-001.md" "DOUG-001" "open" "bug" "Another legacy file."
# Decoys
echo "decoy" > "$LEGACY/.issues/INDEX.md"
echo "decoy" > "$LEGACY/.issues/ISSUE-TEMPLATE.md"

(
    cd "$LEGACY"
    OUT=$("$SEARCH" "concurrent")
    if echo "$OUT" | grep -q "IM-001"; then
        echo "PASS: legacy mode picks up IM-001"
    else
        echo "FAIL: legacy mode did not pick up IM-001"
    fi
    if echo "$OUT" | grep -q "decoy"; then
        echo "FAIL: legacy mode included INDEX.md decoy"
    else
        echo "PASS: legacy mode skipped INDEX.md/ISSUE-TEMPLATE.md"
    fi
    # --prefix filters on stem
    OUT2=$("$SEARCH" "" --prefix DOUG)
    if echo "$OUT2" | grep -q "DOUG-001" && ! echo "$OUT2" | grep -q "IM-001"; then
        echo "PASS: legacy --prefix filters by stem"
    else
        echo "FAIL: legacy --prefix did not scope correctly"
    fi
) | tee /tmp/im025-legacy.txt
while IFS= read -r line; do
    case "$line" in
        PASS:*) PASS=$((PASS+1)) ;;
        FAIL:*) FAIL=$((FAIL+1)) ;;
    esac
done < /tmp/im025-legacy.txt

# ---------------------------------------------------------------------------
# Section J: performance against live corpus
# ---------------------------------------------------------------------------
echo
echo "[J] Performance against a large local issue corpus"

unset ISSUE_STORE_ROOT
LIVE_CORPUS="$PROJECT_ROOT/.issues"
if [ -d "$LIVE_CORPUS" ]; then
    # Warm-up pass: prime the OS page cache and interpreter startup so the
    # timed run measures steady-state search speed, not disk-cache state.
    # Cold runs were observed at ~1.2s vs ~0.14s warm.
    (
        cd "$PROJECT_ROOT"
        "$SEARCH" "concurrent" >/dev/null 2>&1
    )
    START=$(python3 -c 'import time; print(time.time())')
    (
        cd "$PROJECT_ROOT"
        "$SEARCH" "concurrent" >/dev/null 2>&1
    )
    END=$(python3 -c 'import time; print(time.time())')
    ELAPSED=$(python3 -c "print($END - $START)")
    UNDER_ONE=$(python3 -c "print(1 if ($END - $START) < 1.0 else 0)")
    if [ "$UNDER_ONE" = "1" ]; then
        pass "live corpus search ran in ${ELAPSED}s (under 1.0s budget)"
    else
        fail "live corpus search took ${ELAPSED}s (budget 1.0s)"
    fi
else
    echo "SKIP: no live corpus at $LIVE_CORPUS"
fi

# ---------------------------------------------------------------------------
echo
echo "[T] Title matching -- query hits the title, not just the body (IM-232)"
# A distinctive title term that appears in NO body, and an empty body, so a
# match can only come from the title.
TSTORE=$(mktemp -d)
make_issue "$TSTORE/MYP/MYP-001.md" "MYP-001" "open" "feature" ""
# Overwrite the title with the distinctive term (make_issue hardcodes "Sample").
python3 - "$TSTORE/MYP/MYP-001.md" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
p.write_text(p.read_text().replace('title: "Sample MYP-001"',
                                   'title: "add --dry-run kumquat flag"'))
PY
RC=0
OUT=$("$SEARCH" --store "$TSTORE" kumquat 2>/dev/null) || RC=$?
# COVERS: cli.search-modes
assert_eq "0" "$RC" "title-only term exits 0 (found)"
# COVERS: cli.search-modes
assert_contains "MYP-001" "$OUT" "query matches a term that lives only in the title"
JOUT=$("$SEARCH" --store "$TSTORE" kumquat --json 2>/dev/null)
# COVERS: cli.search-modes
assert_contains '"title_match": true' "$JOUT" "--json reports title_match true for a title hit"
assert_contains '"body_matches": []' "$JOUT" "a title-only hit carries no body_matches"
rm -rf "$TSTORE"


# ---------------------------------------------------------------------------
# Section: an unusable store is a precondition failure, not a zero-result
# ---------------------------------------------------------------------------
# IM-253: `issue search` exits 1 both for "matched nothing" (the grep convention)
# and for a store that never resolved. It used to report the second case with
# ZERO bytes on both streams, so an unconfigured store was indistinguishable
# from an empty one -- the exact confusion IM-238 ruled out one level up for
# an absent --prefix, and the degenerate case of the non-branchable error
# surface.structured-errors forbids.
echo
echo "== unusable store is branchable (IM-253) =="

UNRESOLVABLE_HOME="$TMPDIR_ROOT/unresolvable-home"
UNRESOLVABLE_PROJ="$TMPDIR_ROOT/unresolvable-proj"
mkdir -p "$UNRESOLVABLE_HOME" "$UNRESOLVABLE_PROJ"

# No ISSUE_STORE_ROOT, no sentinel, no legacy .issues/ corpus.
ERR_OUT=$( cd "$UNRESOLVABLE_PROJ" && env -u ISSUE_STORE_ROOT \
    HOME="$UNRESOLVABLE_HOME" $SEARCH zzz-no-such-token 2>&1 >/dev/null )
RC=$?
# COVERS: surface.structured-errors
assert_eq "1" "$RC" "unresolvable store is a precondition failure"
# COVERS: surface.structured-errors
assert_contains "error=precondition-failed" "$ERR_OUT" \
    "unresolvable store names the condition"
# COVERS: surface.structured-errors
assert_contains "reason=store-unresolvable" "$ERR_OUT" \
    "unresolvable store carries a branchable reason"

# The same condition under --json is one JSON object on stderr.
JERR=$( cd "$UNRESOLVABLE_PROJ" && env -u ISSUE_STORE_ROOT \
    HOME="$UNRESOLVABLE_HOME" $SEARCH zzz-no-such-token --json 2>&1 >/dev/null )
# COVERS: surface.structured-errors
PY_CHECK=$(python3 -c '
import json, sys
obj = json.loads(sys.stdin.read())
assert obj["exit"] == 1, obj
assert obj["reason"] == "store-unresolvable", obj
print("ok")
' <<< "$JERR")
assert_eq "ok" "$PY_CHECK" "--json renders the condition as one JSON object"

# A store root that resolves but is absent names the path it was pointed at.
ERR_OUT=$( cd "$UNRESOLVABLE_PROJ" && \
    ISSUE_STORE_ROOT="$TMPDIR_ROOT/no-such-store" \
    HOME="$UNRESOLVABLE_HOME" $SEARCH zzz-no-such-token 2>&1 >/dev/null )
# COVERS: surface.structured-errors
assert_contains "store=$TMPDIR_ROOT/no-such-store" "$ERR_OUT" \
    "an absent store root is named in the payload"

# Legacy .issues/ mode is still a usable corpus: a genuine zero-result there
# stays a silent exit 1, so the new gate did not swallow legacy mode.
LEGACY_PROJ="$TMPDIR_ROOT/legacy-corpus"
mkdir -p "$LEGACY_PROJ/.issues"
printf 'LG\n' > "$LEGACY_PROJ/.issues/.prefix"
ERR_OUT=$( cd "$LEGACY_PROJ" && env -u ISSUE_STORE_ROOT \
    HOME="$UNRESOLVABLE_HOME" $SEARCH zzz-no-such-token 2>&1 >/dev/null )
RC=$?
# COVERS: output.exit-codes
assert_eq "1" "$RC" "legacy mode with no match still exits 1 (zero-result)"
# COVERS: output.exit-codes
assert_eq "" "$ERR_OUT" "a genuine zero-result stays silent, not a precondition"

# ---------------------------------------------------------------------------
echo
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=============================================="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
