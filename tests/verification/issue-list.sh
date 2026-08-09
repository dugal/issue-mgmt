#!/usr/bin/env bash
# Verification script for `issue list` -- the filter-only enumeration verb
# (design 5.2). `list` is a NEW v3 verb with no flat binary,
# so it is exercised directly against the consolidated `issue` dispatcher
# (independent of ISSUE_MGMT_CLI_STYLE).
#
# Exercises:
#   - --help documents the filter flags + examples; no positional QUERY
#   - --version prints a version string
#   - store mode: list every issue; --repo/--prefix scoping; --status and
#     --type filters; comma OR-within-flag; AND-across-flags
#   - --json emits a top-level array with body_matches always empty
#   - grep exit convention: 0 with matches, 1 with zero matches
#
# Run from the project root:
#     tests/verification/issue-list.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
# `list` has no flat binary; drive the dispatcher directly.
ISSUE="$ISSUE_MGMT_BIN/issue"

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

TMPDIR_ROOT=$(mktemp -d -t im-list-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT
unset ISSUE_STORE_ROOT

make_issue() {
    # make_issue <path> <id> <status> <type> <priority>
    local path=$1 id=$2 status=$3 type=$4 priority=$5
    mkdir -p "$(dirname "$path")"
    cat > "$path" <<EOF
---
id: $id
title: "Sample $id"
type: $type
status: $status
priority: $priority
created: 2026-05-14
updated: 2026-05-14
branch: ""
tags: []
changelog:
---

## Summary

Body for $id.

## Implementation Notes
EOF
}

# ---------------------------------------------------------------------------
echo
echo "[A] Help and version"

HELP_OUTPUT=$("$ISSUE" list --help 2>&1)
# COVERS: cli.list
assert_contains "list" "$HELP_OUTPUT" "--help prints the verb banner"
assert_contains "--status" "$HELP_OUTPUT" "--help documents --status"
assert_contains "--type" "$HELP_OUTPUT" "--help documents --type"
assert_contains "--repo" "$HELP_OUTPUT" "--help documents --repo alias"
assert_contains "--json" "$HELP_OUTPUT" "--help documents --json"
assert_contains "Examples:" "$HELP_OUTPUT" "--help has an Examples block"

VERSION_OUTPUT=$("$ISSUE" list --version)
assert_contains "v" "$VERSION_OUTPUT" "--version prints a version string"

# ---------------------------------------------------------------------------
echo
echo "[B] Store mode -- filter-only enumeration"

STORE="$TMPDIR_ROOT/store"
make_issue "$STORE/IM/IM-001.md"     "IM-001"   "open"        "bug"     "high"
make_issue "$STORE/IM/IM-002.md"     "IM-002"   "in-progress" "feature" "medium"
make_issue "$STORE/IM/IM-003.md"     "IM-003"   "done"        "bug"     "low"
make_issue "$STORE/DOUG/DOUG-001.md" "DOUG-001" "open"        "feature" "medium"
make_issue "$STORE/DOUG/DOUG-002.md" "DOUG-002" "blocked"     "bug"     "high"
make_issue "$STORE/RM/RM-001.md"     "RM-001"   "open"        "chore"   "medium"
echo "decoy" > "$STORE/INDEX.md"
echo "decoy" > "$STORE/IM/INDEX.md"

export ISSUE_STORE_ROOT="$STORE"

# No filters -> every issue across every prefix.
OUT=$("$ISSUE" list)
RC=$?
# COVERS: cli.list
assert_eq "0" "$RC" "exit 0 when the store is non-empty"
assert_contains "IM-001"   "$OUT" "no-filter list includes IM-001"
assert_contains "DOUG-001" "$OUT" "no-filter list includes DOUG-001"
assert_contains "RM-001"   "$OUT" "no-filter list includes RM-001"
assert_not_contains "decoy" "$OUT" "INDEX.md decoys excluded"

# TSV shape: <id>\t<status>\t<priority>\t<title>
FIRST_LINE=$(echo "$OUT" | head -1)
STATUS_COL=$(echo "$FIRST_LINE" | cut -f2)
case "$STATUS_COL" in
    open|in-progress|blocked|done|cancelled) pass "TSV status column parses ($STATUS_COL)" ;;
    *) fail "TSV status column unexpected: $STATUS_COL" ;;
esac

# --repo scoping (design 2a name)
OUT=$("$ISSUE" list --repo IM)
# COVERS: cli.list
assert_contains "IM-001" "$OUT" "--repo IM includes IM rows"
assert_not_contains "DOUG-001" "$OUT" "--repo IM excludes DOUG"
assert_not_contains "RM-001"   "$OUT" "--repo IM excludes RM"

# --prefix alias behaves identically, and accepts a comma list (OR-within)
OUT=$("$ISSUE" list --prefix "IM,DOUG")
# COVERS: cli.list
assert_contains "IM-001"   "$OUT" "--prefix IM,DOUG includes IM"
assert_contains "DOUG-001" "$OUT" "--prefix IM,DOUG includes DOUG"
assert_not_contains "RM-001" "$OUT" "--prefix IM,DOUG excludes RM"

# --status filter, comma OR-within
OUT=$("$ISSUE" list --status "open,blocked")
# COVERS: cli.list
assert_contains "IM-001"   "$OUT" "--status open,blocked hits open IM-001"
assert_contains "DOUG-002" "$OUT" "--status open,blocked hits blocked DOUG-002"
assert_not_contains "IM-002" "$OUT" "--status open,blocked excludes in-progress"
assert_not_contains "IM-003" "$OUT" "--status open,blocked excludes done"

# AND-across-flags: open IM bugs -> IM-001 only
OUT=$("$ISSUE" list --repo IM --status open --type bug)
# COVERS: cli.list
assert_contains "IM-001" "$OUT" "AND-across: open IM bug includes IM-001"
assert_not_contains "IM-002" "$OUT" "AND-across: excludes IM-002 (feature/in-progress)"
assert_not_contains "IM-003" "$OUT" "AND-across: excludes IM-003 (done)"

# ---------------------------------------------------------------------------
echo
echo "[C] --json shape (body_matches always empty)"

JSON_OUT=$("$ISSUE" list --status open --json)
# COVERS: cli.list
PY_CHECK=$(python3 -c '
import json, sys
data = json.loads(sys.stdin.read())
assert isinstance(data, list), "not a list"
assert len(data) >= 1, "no entries"
required = {"id", "file", "title", "status", "priority", "type", "body_matches"}
for e in data:
    assert not (required - set(e.keys())), "missing keys"
    assert e["body_matches"] == [], "body_matches must be empty for list"
    assert e["status"] == "open", "status filter leaked"
print("ok")
' <<< "$JSON_OUT")
assert_eq "ok" "$PY_CHECK" "--json shape validates and body_matches empty"

# ---------------------------------------------------------------------------
echo
echo "[D] Exit codes"

"$ISSUE" list --status cancelled >/dev/null
RC=$?
# COVERS: cli.list
assert_eq "1" "$RC" "zero matches exits 1 (grep convention)"

# ---------------------------------------------------------------------------
echo
echo "[F] --json field selection"

# --json <fields> projects each object to exactly those top-level keys, in
# the caller-specified order.
FSEL=$("$ISSUE" list --repo IM --json id,title,status)
# COVERS: output.json-field-selection
PY_CHECK=$(python3 -c '
import json, sys
data = json.loads(sys.stdin.read())
assert isinstance(data, list) and data, "not a non-empty list"
for e in data:
    assert list(e.keys()) == ["id", "title", "status"], "keys/order wrong: %r" % list(e.keys())
print("ok")
' <<< "$FSEL")
assert_eq "ok" "$PY_CHECK" "--json id,title,status projects keys in caller order"

# Bare --json is unchanged: still the full documented per-object shape.
FULL=$("$ISSUE" list --repo IM --json)
# COVERS: output.json-field-selection
PY_CHECK=$(python3 -c '
import json, sys
data = json.loads(sys.stdin.read())
required = {"id", "file", "title", "status", "priority", "type", "body_matches"}
assert data and set(data[0].keys()) == required, "bare --json shape changed"
print("ok")
' <<< "$FULL")
assert_eq "ok" "$PY_CHECK" "bare --json still emits the full shape (backward compatible)"

# Unknown field name is a usage error (exit 2), naming the bad field.
ERR_OUT=$("$ISSUE" list --repo IM --json id,bogus 2>&1 >/dev/null)
RC=$?
# COVERS: output.json-field-selection
assert_eq "2" "$RC" "unknown --json field exits 2"
assert_contains "bogus" "$ERR_OUT" "unknown-field error names the bad field"

# An empty field list (--json '') is likewise a usage error, not a silent
# fallback to text output.
"$ISSUE" list --repo IM --json "" >/dev/null 2>&1
RC=$?
# COVERS: output.json-field-selection
assert_eq "2" "$RC" "empty --json field list exits 2"

# ---------------------------------------------------------------------------
echo
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=============================================="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
