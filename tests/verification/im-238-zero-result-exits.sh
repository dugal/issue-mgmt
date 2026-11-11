#!/usr/bin/env bash
# Verification script for IM-238 -- the uniform zero-result exit rule.
#
# The rule (docs/requirements.md output.exit-codes, decided 2026-09-06):
#
#   enumeration read  -- search, list, ready, index, history search,
#                        history activity
#                        0 = emitted at least one row
#                        1 = emitted none (grep convention)
#   point read        -- show, history show, history at
#                        0 = the named record was found
#                        1 = it was not
#   any verb taking --prefix
#                        2 = the prefix names nothing in the store
#                        (error=unknown-prefix), never a zero-result
#
# Before this landed the surface split 3/3: search / list / ready exited 1
# on empty while index / history search / history activity exited 0, so no
# rule an agent held was correct across the surface. Absent-prefix scoping
# produced four different behaviors, including a hard error on
# `history activity`.
#
# Run from the project root:
#     tests/verification/im-238-zero-result-exits.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE="$ISSUE_MGMT_BIN/issue"

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_exit() {
    # assert_exit <expected> <label> <cmd...>
    expected=$1; label=$2; shift 2
    "$@" >/dev/null 2>&1
    rc=$?
    if [ "$rc" = "$expected" ]; then
        pass "$label (exit $rc)"
    else
        fail "$label: expected exit $expected, got $rc"
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

WORKDIR=$(mktemp -d -t im-238-verify.XXXXXX)
trap 'rm -rf "$WORKDIR"' EXIT
unset ISSUE_STORE_ROOT

STORE="$WORKDIR/store"

make_issue() {
    # make_issue <id> <prefix> <status>
    mkdir -p "$STORE/$2"
    cat > "$STORE/$2/$1.md" <<EOF
---
id: $1
title: "Sample $1"
type: bug
status: $3
priority: medium
created: 2026-01-01
updated: 2026-01-01
branch: ""
tags: []
changelog:
---

## Summary

Body for $1 mentioning zebrafish.

## Implementation Notes
EOF
}

mkdir -p "$STORE"
git init -q "$STORE"
make_issue "IM-001" "IM" "open"
make_issue "IM-002" "IM" "done"
git -C "$STORE" add -A
git -C "$STORE" -c user.email="$FIXTURE_EMAIL" -c user.name=verify \
    commit -qm "seed"

export ISSUE_STORE_ROOT="$STORE"

# ---------------------------------------------------------------------------
echo
echo "[A] Enumeration reads: at least one row -> exit 0"

# COVERS: output.exit-codes
assert_exit 0 "search with a match"        "$ISSUE" search zebrafish
assert_exit 0 "list with rows"             "$ISSUE" list
assert_exit 0 "index with rows"            "$ISSUE" index
assert_exit 0 "ready with ready work"      "$ISSUE" ready
assert_exit 0 "history search with a hit"  "$ISSUE" history search "IM-001"
assert_exit 0 "history activity with rows" "$ISSUE" history activity --since ""

# ---------------------------------------------------------------------------
echo
echo "[B] Enumeration reads: zero rows -> exit 1 (grep convention)"

# `index` and the two history verbs exited 0 on empty through v3.3.x; this
# section is the regression guard for the convergence.
# COVERS: output.exit-codes
assert_exit 1 "search with no match"          "$ISSUE" search zzzznosuchterm
assert_exit 1 "list with no rows"             "$ISSUE" list --status blocked
assert_exit 1 "index with an empty board"     "$ISSUE" index --status blocked
assert_exit 1 "history search with no hits"   "$ISSUE" history search zzzznosuchterm
assert_exit 1 "history activity, empty window" \
    "$ISSUE" history activity --since 2099-01-01

# Empty ready set: close the only open issue.
sed -i.bak 's/^status: open/status: done/' "$STORE/IM/IM-001.md"
rm -f "$STORE/IM/IM-001.md.bak"
# COVERS: output.exit-codes
assert_exit 1 "ready with an empty ready set" "$ISSUE" ready
sed -i.bak 's/^status: done/status: open/' "$STORE/IM/IM-001.md"
rm -f "$STORE/IM/IM-001.md.bak"

# An empty board still RENDERS -- only the exit code reports emptiness.
OUT=$("$ISSUE" index --status blocked 2>/dev/null)
# COVERS: output.exit-codes
assert_contains "Blocked (0)" "$OUT" "empty board still renders its groups"

# ---------------------------------------------------------------------------
echo
echo "[C] Point reads: found -> 0, not found -> 1"

# COVERS: output.exit-codes
assert_exit 0 "show an existing issue"     "$ISSUE" show IM-001
assert_exit 1 "show a missing issue"       "$ISSUE" show IM-999
assert_exit 0 "history show an existing issue" "$ISSUE" history show IM-001
assert_exit 1 "history show a missing issue"   "$ISSUE" history show IM-999
assert_exit 0 "history at an existing issue"   "$ISSUE" history at IM-001 --at HEAD
assert_exit 1 "history at a missing issue"     "$ISSUE" history at IM-999 --at HEAD

# ---------------------------------------------------------------------------
echo
echo "[D] Absent --prefix scope -> exit 2, not a zero-result"

# COVERS: output.exit-codes
assert_exit 2 "search --prefix ZZZZ"   "$ISSUE" search zebrafish --prefix ZZZZ
assert_exit 2 "list --prefix ZZZZ"     "$ISSUE" list --prefix ZZZZ
assert_exit 2 "index --prefix ZZZZ"    "$ISSUE" index --prefix ZZZZ
assert_exit 2 "ready --prefix ZZZZ"    "$ISSUE" ready --prefix ZZZZ
assert_exit 2 "history search --prefix ZZZZ" \
    "$ISSUE" history search zebrafish --prefix ZZZZ
assert_exit 2 "history activity --prefix ZZZZ" \
    "$ISSUE" history activity --prefix ZZZZ
assert_exit 2 "admin reap --prefix ZZZZ" \
    "$ISSUE" admin reap --prefix ZZZZ --dry-run

# The error is machine-branchable: a stable key=value line naming the
# condition, the unknown value, and the known set.
ERR=$("$ISSUE" list --prefix ZZZZ 2>&1 >/dev/null)
# COVERS: output.exit-codes
assert_contains "error=unknown-prefix" "$ERR" "structured stderr names the condition"
assert_contains "unknown=ZZZZ"         "$ERR" "structured stderr names the bad value"
assert_contains "known=IM"             "$ERR" "structured stderr names the known set"

# ... and the same condition as one JSON object under --json.
JERR=$("$ISSUE" list --prefix ZZZZ --json 2>&1 >/dev/null)
JCHECK=$(python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
assert d["exit"] == 2, "exit field"
assert d["error"] == "unknown-prefix", "error class"
assert d["unknown"] == "ZZZZ", "unknown value"
print("ok")
' <<< "$JERR" 2>/dev/null)
# COVERS: output.exit-codes
if [ "$JCHECK" = "ok" ]; then
    pass "--json emits the same condition as one JSON object"
else
    fail "--json error object: got '$JERR'"
fi

# A prefix that IS in the store resolves normally (case-folded).
# COVERS: output.exit-codes
assert_exit 0 "list --prefix im case-folds and resolves" "$ISSUE" list --prefix im

# ---------------------------------------------------------------------------
echo
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=============================================="
[ "$FAIL" -eq 0 ] || exit 1
