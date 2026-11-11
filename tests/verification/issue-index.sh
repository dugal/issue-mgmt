#!/usr/bin/env bash
# Verification script for `issue index` -- the grouped-by-status board verb
# (design Q9). `index` is a NEW v3 verb with no flat
# binary, so it is exercised directly against the consolidated `issue`
# dispatcher (independent of ISSUE_MGMT_CLI_STYLE).
#
# Exercises:
#   - --help / --version
#   - store mode: grouped board with workflow-ordered ## headers + counts
#   - --prefix scoping to one prefix (--repo is the retired synonym)
#   - --status restricts which groups render
#   - bounded default: --limit caps each group and announces suppression
#   - --json shape {store, prefix, repo, generated_at, groups[{status,count,issues}]}
#   - legacy mode (no store) exits 1 with a branchable payload (IM-253)
#   - usage errors exit 2 (bad --prefix, bad --status, negative --limit)
#
# Run from the project root:
#     tests/verification/issue-index.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
# `index` has no flat binary; drive the dispatcher directly.
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

TMPDIR_ROOT=$(mktemp -d -t im-index-verify.XXXXXX)
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
EOF
}

# ---------------------------------------------------------------------------
echo
echo "[A] Help and version"

HELP_OUTPUT=$("$ISSUE" index --help 2>&1)
# COVERS: cli.index
assert_contains "index" "$HELP_OUTPUT" "--help prints the verb banner"
# COVERS: surface.one-meaning-per-flag
assert_contains "--prefix" "$HELP_OUTPUT" "--help documents --prefix"
# The retired --repo prefix-scoping spelling still works but must not be
# presented as a current form (surface.self-naming); --repo means the repo
# basename on create / admin init-project / admin migrate.
# COVERS: surface.self-naming
assert_not_contains "--repo" "$HELP_OUTPUT" "--help does not present the retired --repo"
assert_contains "--status" "$HELP_OUTPUT" "--help documents --status"
assert_contains "--limit" "$HELP_OUTPUT" "--help documents --limit"
assert_contains "--json" "$HELP_OUTPUT" "--help documents --json"

VERSION_OUTPUT=$("$ISSUE" index --version)
assert_contains "v" "$VERSION_OUTPUT" "--version prints a version string"

# ---------------------------------------------------------------------------
echo
echo "[B] Store mode -- grouped board"

STORE="$TMPDIR_ROOT/store"
make_issue "$STORE/IM/IM-001.md"     "IM-001"   "open"        "bug"     "high"
make_issue "$STORE/IM/IM-002.md"     "IM-002"   "open"        "feature" "medium"
make_issue "$STORE/IM/IM-003.md"     "IM-003"   "in-progress" "bug"     "high"
make_issue "$STORE/IM/IM-004.md"     "IM-004"   "done"        "bug"     "low"
make_issue "$STORE/DOUG/DOUG-001.md" "DOUG-001" "open"        "feature" "medium"
make_issue "$STORE/DOUG/DOUG-002.md" "DOUG-002" "blocked"     "bug"     "high"
echo "decoy" > "$STORE/INDEX.md"

export ISSUE_STORE_ROOT="$STORE"

OUT=$("$ISSUE" index)
RC=$?
# COVERS: cli.index
assert_eq "0" "$RC" "resolvable store exits 0"
assert_contains "## In Progress (1)" "$OUT" "In Progress group header + count"
assert_contains "## Open (3)"         "$OUT" "Open group header + count"
assert_contains "## Blocked (1)"      "$OUT" "Blocked group header + count"
assert_contains "## Done (1)"         "$OUT" "Done group header + count"
assert_contains "IM-001"   "$OUT" "board lists IM-001"
assert_contains "DOUG-002" "$OUT" "board lists DOUG-002"
assert_not_contains "decoy" "$OUT" "INDEX.md decoy excluded"

# Within a group, high priority sorts before medium.
OPEN_BLOCK=$(echo "$OUT" | awk '/## Open/{f=1;next} /^## /{f=0} f')
FIRST_OPEN=$(echo "$OPEN_BLOCK" | grep -E '^[[:space:]]+[A-Z]' | head -1)
# COVERS: cli.index
assert_contains "IM-001" "$FIRST_OPEN" "Open group ordered by priority (high IM-001 first)"

# ---------------------------------------------------------------------------
echo
echo "[C] --prefix and --status scoping"

OUT=$("$ISSUE" index --prefix IM)
# COVERS: cli.index
assert_contains "IM-001" "$OUT" "--prefix IM includes IM issues"
assert_not_contains "DOUG-001" "$OUT" "--prefix IM excludes DOUG"

# The board labels its own scope with the current spelling, whichever flag
# the caller reached for (surface.self-naming).
# COVERS: surface.self-naming
assert_contains "(prefix IM)" "$OUT" "board header names the scope 'prefix'"
assert_not_contains "(repo IM)" "$OUT" "board header does not name the retired 'repo'"
assert_contains "(prefix IM)" "$("$ISSUE" index --repo IM)" \
    "retired --repo still reports the scope as 'prefix'"

# The retired spelling is still accepted for one release (surface.stability).
# The board stamps a per-invocation `generated <ISO-second>` line, so two
# separate runs differ whenever they straddle a second boundary; compare the
# board proper, not the stamp.
board_body() { echo "$1" | grep -v '^generated '; }
REPO_OUT=$("$ISSUE" index --repo IM)
# COVERS: surface.stability
assert_eq "$(board_body "$OUT")" "$(board_body "$REPO_OUT")" \
    "retired --repo IM yields the same board as --prefix IM"

OUT=$("$ISSUE" index --status open)
# COVERS: cli.index
assert_contains "## Open" "$OUT" "--status open renders the Open group"
assert_not_contains "## Done" "$OUT" "--status open omits the Done group"
assert_not_contains "## Blocked" "$OUT" "--status open omits the Blocked group"

# Repeated --status accumulates here too. This verb was always correct; the
# assertion pins it so the convergence IM-269 made cannot drift back by
# fixing the wrong side.
OUT=$("$ISSUE" index --status open --status done)
# COVERS: surface.one-meaning-per-flag
assert_contains "## Open" "$OUT" "repeated --status renders the Open group"
assert_contains "## Done" "$OUT" "repeated --status renders the Done group"
assert_not_contains "## Blocked" "$OUT" "repeated --status omits Blocked"

# ---------------------------------------------------------------------------
echo
echo "[D] Bounded default: --limit caps a group, never silently"

OUT=$("$ISSUE" index --status open --limit 1)
# COVERS: cli.index
SHOWN=$(echo "$OUT" | grep -E '^[[:space:]]+[A-Z]+-' | wc -l | tr -d ' ')
assert_eq "1" "$SHOWN" "--limit 1 shows exactly one Open row"
assert_contains "2 more" "$OUT" "suppressed rows announced (never silent)"

# --limit 0 shows all
OUT=$("$ISSUE" index --status open --limit 0)
SHOWN=$(echo "$OUT" | grep -E '^[[:space:]]+[A-Z]+-' | wc -l | tr -d ' ')
# COVERS: cli.index
assert_eq "3" "$SHOWN" "--limit 0 shows the full Open group"

# ---------------------------------------------------------------------------
echo
echo "[E] --json shape"

JSON_OUT=$("$ISSUE" index --prefix IM --status open --json)
# COVERS: cli.index
PY_CHECK=$(python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
assert set(d.keys()) == {"store", "prefix", "repo", "generated_at", "groups"}, "top keys"
assert d["prefix"] == "IM", "prefix echoed"
assert d["repo"] == d["prefix"], "retired repo key duplicates prefix"
assert isinstance(d["groups"], list) and len(d["groups"]) == 1, "one group"
g = d["groups"][0]
assert g["status"] == "open", "group status"
assert g["count"] == 2, "count is the full group size"
for it in g["issues"]:
    assert set(it.keys()) == {"id", "title", "priority", "file"}, "issue keys"
print("ok")
' <<< "$JSON_OUT")
assert_eq "ok" "$PY_CHECK" "--json shape validates"

# ---------------------------------------------------------------------------
echo
echo "[F] Legacy mode refusal + usage errors"

# Bad --prefix / --status / --limit are usage errors (exit 2), store-independent.
# `--prefix lower` now case-folds to LOWER (accepted); only a shape
# that is invalid even upper-cased (leading digit) stays exit 2.
"$ISSUE" index --prefix "1x" >/dev/null 2>&1
RC=$?
# COVERS: cli.index
assert_eq "2" "$RC" "invalid --prefix exits 2"

"$ISSUE" index --status "bogus" >/dev/null 2>&1
RC=$?
assert_eq "2" "$RC" "unknown --status exits 2"

"$ISSUE" index --limit "-3" >/dev/null 2>&1
RC=$?
assert_eq "2" "$RC" "negative --limit exits 2"

# Legacy mode: no store, no sentinel -> exit 1 with a hint.
unset ISSUE_STORE_ROOT
LEGACY="$TMPDIR_ROOT/legacy-project"
mkdir -p "$LEGACY/.issues"
(
    cd "$LEGACY"
    ERR=$("$ISSUE" index 2>&1 >/dev/null)
    RC=$?
    # COVERS: surface.structured-errors
    if [ "$RC" = "1" ] \
       && echo "$ERR" | grep -qF "error=precondition-failed" \
       && echo "$ERR" | grep -qF "reason=store-unresolvable"; then
        echo "PASS: legacy mode exits 1 with a branchable store payload"
    else
        echo "FAIL: legacy mode rc=$RC err=$ERR"
    fi
) | tee "$TMPDIR_ROOT/legacy.txt"
case "$(cat "$TMPDIR_ROOT/legacy.txt")" in
    PASS:*) PASS=$((PASS+1)) ;;
    *) FAIL=$((FAIL+1)) ;;
esac

# ---------------------------------------------------------------------------
echo
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=============================================="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
