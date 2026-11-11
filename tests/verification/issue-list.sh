#!/usr/bin/env bash
# Verification script for `issue list` -- the filter-only enumeration verb
# (design 5.2). `list` is a NEW v3 verb with no flat binary,
# so it is exercised directly against the consolidated `issue` dispatcher
# (independent of ISSUE_MGMT_CLI_STYLE).
#
# Exercises:
#   - --help documents the filter flags + examples; no positional QUERY
#   - --version prints a version string
#   - store mode: list every issue; --prefix scoping (--repo is the retired
#     synonym); --status and
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
# COVERS: surface.one-meaning-per-flag
# --repo is prefix scoping's retired spelling: still accepted, never
# presented as a current form (surface.self-naming). It means the repo
# basename on create / admin init-project / admin migrate.
# COVERS: surface.self-naming
assert_not_contains "--repo" "$HELP_OUTPUT" "--help does not present the retired --repo"
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

# --prefix scoping
OUT=$("$ISSUE" list --prefix IM)
# COVERS: cli.list
assert_contains "IM-001" "$OUT" "--prefix IM includes IM rows"
assert_not_contains "DOUG-001" "$OUT" "--prefix IM excludes DOUG"
assert_not_contains "RM-001"   "$OUT" "--prefix IM excludes RM"

# The retired spelling still resolves for one release (surface.stability).
REPO_OUT=$("$ISSUE" list --repo IM)
# COVERS: surface.stability
assert_eq "$OUT" "$REPO_OUT" "retired --repo IM yields the same rows as --prefix IM"

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

# Repeated --status accumulates, same as the comma form and same as
# `issue index` (IM-269). Before the fix this returned ONLY blocked, with
# exit 0 -- a plausible shorter answer, not an error.
OUT=$("$ISSUE" list --status open --status blocked)
# COVERS: surface.one-meaning-per-flag
assert_contains "IM-001"   "$OUT" "repeated --status hits open IM-001"
assert_contains "DOUG-002" "$OUT" "repeated --status hits blocked DOUG-002"
assert_not_contains "IM-002" "$OUT" "repeated --status excludes in-progress"
# Mixing the two forms in one invocation resolves to the union.
OUT=$("$ISSUE" list --status open --status "blocked,done")
# COVERS: surface.one-meaning-per-flag
assert_contains "IM-001"   "$OUT" "mixed --status forms hit open IM-001"
assert_contains "DOUG-002" "$OUT" "mixed --status forms hit blocked DOUG-002"
assert_contains "IM-003"   "$OUT" "mixed --status forms hit done IM-003"

# AND-across-flags: open IM bugs -> IM-001 only
OUT=$("$ISSUE" list --prefix IM --status open --type bug)
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
required = {"id", "file", "title", "status", "priority", "type", "title_match", "body_matches"}
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
FSEL=$("$ISSUE" list --prefix IM --json id,title,status)
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
FULL=$("$ISSUE" list --prefix IM --json)
# COVERS: output.json-field-selection
PY_CHECK=$(python3 -c '
import json, sys
data = json.loads(sys.stdin.read())
required = {"id", "file", "title", "status", "priority", "type", "title_match", "body_matches"}
assert data and set(data[0].keys()) == required, "bare --json shape changed"
print("ok")
' <<< "$FULL")
assert_eq "ok" "$PY_CHECK" "bare --json still emits the full shape (backward compatible)"

# Unknown field name is a usage error (exit 2), naming the bad field AND the
# valid set. IM-263: the valid set is the half that was wrong -- `list`
# declared seven fields while emitting eight, so `--json title_match` was
# rejected on a key every row carried. The old assertion checked only that
# the error named the BAD field, which stayed true throughout the defect.
ERR_OUT=$("$ISSUE" list --prefix IM --json id,bogus 2>&1 >/dev/null)
RC=$?
# COVERS: output.json-field-selection
assert_eq "2" "$RC" "unknown --json field exits 2"
assert_contains "bogus" "$ERR_OUT" "unknown-field error names the bad field"
for f in id file title status priority type title_match body_matches; do
    # COVERS: output.json-field-selection
    assert_contains "$f" "$ERR_OUT" "unknown-field error names valid field '$f'"
done

# Every field the error advertises must actually be selectable -- the error
# text and the accepted set are the same declaration, so a field named there
# and refused here is the IM-263 defect.
for f in id file title status priority type title_match body_matches; do
    "$ISSUE" list --prefix IM --json "$f" >/dev/null 2>&1
    RC=$?
    # COVERS: output.json-field-selection
    assert_eq "0" "$RC" "advertised --json field '$f' is selectable"
done

# An empty field list (--json '') is likewise a usage error, not a silent
# fallback to text output.
"$ISSUE" list --prefix IM --json "" >/dev/null 2>&1
RC=$?
# COVERS: output.json-field-selection
assert_eq "2" "$RC" "empty --json field list exits 2"


# ---------------------------------------------------------------------------
# Section: `list` is `search ""` -- shape and exit parity
# ---------------------------------------------------------------------------
# `cli.list` promises that `list`'s output shape (TSV + --json) and its grep
# exit convention MATCH `issue search` run with an empty query. Before IM-263
# this script never invoked `search` at all, so the parity clause had no
# coverage -- and the two verbs had in fact diverged: their VALID_JSON_FIELDS
# were separate hand-maintained copies of one row builder's key list, and
# `search` alone was updated when `title_match` was added (IM-232).
echo
echo "[G] list / search \"\" parity (cli.list)"

for SCOPE in "--prefix IM" "--prefix DOUG" "--status open" "--type bug" ""; do
    LIST_TSV=$("$ISSUE" list $SCOPE 2>/dev/null); LIST_RC=$?
    SEARCH_TSV=$("$ISSUE" search "" $SCOPE 2>/dev/null); SEARCH_RC=$?
    # COVERS: cli.list
    assert_eq "$LIST_RC" "$SEARCH_RC" \
        "list and search \"\" agree on exit code for scope '${SCOPE:-<none>}'"
    # COVERS: cli.list
    assert_eq "$LIST_TSV" "$SEARCH_TSV" \
        "list and search \"\" agree on TSV output for scope '${SCOPE:-<none>}'"
done

# The --json shapes must agree key-for-key, which is what diverged.
PARITY=$(python3 -c '
import json, subprocess, sys
issue = sys.argv[1]
def keys(argv):
    out = subprocess.run([issue] + argv, capture_output=True, text=True).stdout
    rows = json.loads(out)
    assert rows, "fixture produced no rows: %r" % argv
    return sorted(rows[0].keys())
lk = keys(["list", "--prefix", "IM", "--json"])
sk = keys(["search", "", "--prefix", "IM", "--json"])
assert lk == sk, "list=%s search=%s" % (lk, sk)
print("ok")
' "$ISSUE" 2>&1)
# COVERS: cli.list
assert_eq "ok" "$PARITY" "list and search \"\" emit the same --json keys"

# Field selection is part of that shape: a field selectable on one must be
# selectable on the other. This is the assertion the IM-263 defect failed.
for f in id file title status priority type title_match body_matches; do
    "$ISSUE" list --prefix IM --json "$f" >/dev/null 2>&1;        LIST_RC=$?
    "$ISSUE" search "" --prefix IM --json "$f" >/dev/null 2>&1;   SEARCH_RC=$?
    # COVERS: cli.list
    assert_eq "$LIST_RC" "$SEARCH_RC" \
        "list and search \"\" agree on selectability of --json '$f'"
done

# ---------------------------------------------------------------------------
# Section: an unusable store is a precondition failure, not a zero-result
# ---------------------------------------------------------------------------
# IM-253: `issue list` exits 1 both for "matched nothing" (the grep convention)
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
    HOME="$UNRESOLVABLE_HOME" "$ISSUE" list 2>&1 >/dev/null )
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
    HOME="$UNRESOLVABLE_HOME" "$ISSUE" list --json 2>&1 >/dev/null )
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
    HOME="$UNRESOLVABLE_HOME" "$ISSUE" list 2>&1 >/dev/null )
# COVERS: surface.structured-errors
assert_contains "store=$TMPDIR_ROOT/no-such-store" "$ERR_OUT" \
    "an absent store root is named in the payload"

# Legacy .issues/ mode is still a usable corpus: a genuine zero-result there
# stays a silent exit 1, so the new gate did not swallow legacy mode.
LEGACY_PROJ="$TMPDIR_ROOT/legacy-corpus"
mkdir -p "$LEGACY_PROJ/.issues"
printf 'LG\n' > "$LEGACY_PROJ/.issues/.prefix"
ERR_OUT=$( cd "$LEGACY_PROJ" && env -u ISSUE_STORE_ROOT \
    HOME="$UNRESOLVABLE_HOME" "$ISSUE" list 2>&1 >/dev/null )
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
