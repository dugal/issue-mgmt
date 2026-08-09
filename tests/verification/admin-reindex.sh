#!/usr/bin/env bash
# Verification script for `issue admin reindex`, the
# dispatcher port of the flat reindex-issues.sh bash binary onto the
# in-process reindex seam (lib/cli/admin_reindex.py + lib/reindex_core.py).
#
# The standalone reindex verb is otherwise exercised only incidentally
# (mutating verbs auto-reindex in-process), so its own dispatch modes --
# explicit legacy dir, no-arg store vs legacy, --all walk, and the
# FileNotFoundError paths -- had no direct black-box coverage. This script
# drives every branch of admin_reindex.run() end-to-end.
#
# Run from the project root:
#     tests/verification/admin-reindex.sh

set -u

# Unset any inherited store root so no-arg legacy detection is not
# short-circuited into store mode by the caller's shell.
unset ISSUE_STORE_ROOT

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
REINDEX="$(resolve_cli reindex-issues)"

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then pass "$3 ($2)"; else
        fail "$3: expected '$1', got '$2'"; fi
}
assert_file_contains() {
    if grep -qF -- "$1" "$2"; then pass "$3 (file contains '$1')"; else
        fail "$3: '$1' not in $2"; fi
}
assert_contains() {
    if echo "$2" | grep -qF -- "$1"; then pass "$3 (contains '$1')"; else
        fail "$3: '$1' not found in: $2"; fi
}

WORKDIR="$(mktemp -d -t admin-reindex.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

make_issue() {
    # make_issue <path> <id> <status>
    cat > "$1" <<EOF
---
id: $2
title: "Reindex fixture $2"
type: chore
status: $3
priority: medium
created: 2026-07-01
updated: 2026-07-01
branch: ""
---

## Summary

Body for $2.
EOF
}

# ---------------------------------------------------------------------------
echo "[A] Explicit legacy directory argument"
# ---------------------------------------------------------------------------
LEG="$WORKDIR/legacy-proj"
mkdir -p "$LEG/.issues"
echo "IM" > "$LEG/.issues/.prefix"
make_issue "$LEG/.issues/IM-001.md" IM-001 open
make_issue "$LEG/.issues/IM-002.md" IM-002 in-progress
rm -f "$LEG/.issues/INDEX.md"
OUT=$("$REINDEX" "$LEG/.issues" 2>&1)
RC=$?
assert_eq 0 "$RC" "explicit legacy dir reindex exits 0"
assert_file_contains "IM-001" "$LEG/.issues/INDEX.md" \
    "explicit legacy reindex writes INDEX.md with the issues"
assert_file_contains "IM-002" "$LEG/.issues/INDEX.md" \
    "explicit legacy reindex includes the in-progress issue"

# ---------------------------------------------------------------------------
echo "[B] Explicit missing directory -> exit 1"
# ---------------------------------------------------------------------------
RC=0
ERR=$("$REINDEX" "$WORKDIR/does-not-exist/.issues" 2>&1 >/dev/null) || RC=$?
assert_eq 1 "$RC" "reindex of a missing legacy dir exits 1"
assert_contains "Error" "$ERR" "missing-dir stderr carries an Error message"

# ---------------------------------------------------------------------------
echo "[C] No-arg legacy mode (nearest .issues/ from cwd)"
# ---------------------------------------------------------------------------
LEG2="$WORKDIR/legacy-proj2"
mkdir -p "$LEG2/.issues"
echo "IM" > "$LEG2/.issues/.prefix"
make_issue "$LEG2/.issues/IM-010.md" IM-010 open
rm -f "$LEG2/.issues/INDEX.md"
RC=0
( cd "$LEG2" && "$REINDEX" >/dev/null 2>&1 ) || RC=$?
assert_eq 0 "$RC" "no-arg legacy reindex exits 0"
assert_file_contains "IM-010" "$LEG2/.issues/INDEX.md" \
    "no-arg legacy mode reindexes the nearest .issues/"

# ---------------------------------------------------------------------------
echo "[D] No-arg store mode (ISSUE_STORE_ROOT resolves the store)"
# ---------------------------------------------------------------------------
STORE="$WORKDIR/store"
mkdir -p "$STORE/IM"
(
    cd "$STORE"
    git init -q
    git config user.email test@example.com
    git config user.name test
    printf 'reindex-proj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
make_issue "$STORE/IM/IM-100.md" IM-100 open
make_issue "$STORE/IM/IM-101.md" IM-101 done
PROJ="$WORKDIR/reindex-proj"
mkdir -p "$PROJ/.issues"
echo "IM" > "$PROJ/.issues/.prefix"
echo "$STORE" > "$PROJ/.issues/.migrated-to-store"
rm -f "$STORE/IM/INDEX.md" "$STORE/INDEX.md"
RC=0
( cd "$PROJ" && ISSUE_STORE_ROOT="$STORE" "$REINDEX" >/dev/null 2>&1 ) || RC=$?
assert_eq 0 "$RC" "no-arg store reindex exits 0"
assert_file_contains "IM-100" "$STORE/IM/INDEX.md" \
    "store mode writes the per-prefix INDEX.md"
assert_file_contains "IM-100" "$STORE/INDEX.md" \
    "store mode writes the root aggregate INDEX.md"

# ---------------------------------------------------------------------------
echo "[E] --all walk: every .issues/ dir plus the env-resolved store"
# ---------------------------------------------------------------------------
ALLROOT="$WORKDIR/allroot"
mkdir -p "$ALLROOT/projA/.issues" "$ALLROOT/projB/.issues"
echo "IM" > "$ALLROOT/projA/.issues/.prefix"
echo "IM" > "$ALLROOT/projB/.issues/.prefix"
make_issue "$ALLROOT/projA/.issues/IM-300.md" IM-300 open
make_issue "$ALLROOT/projB/.issues/IM-301.md" IM-301 open
rm -f "$ALLROOT/projA/.issues/INDEX.md" "$ALLROOT/projB/.issues/INDEX.md"
RC=0
OUT=$( cd "$ALLROOT" && ISSUE_MGMT_ROOT="$ALLROOT" ISSUE_STORE_ROOT="$STORE" \
    "$REINDEX" --all 2>&1 ) || RC=$?
assert_eq 0 "$RC" "--all reindex exits 0"
assert_file_contains "IM-300" "$ALLROOT/projA/.issues/INDEX.md" \
    "--all reindexed projA"
assert_file_contains "IM-301" "$ALLROOT/projB/.issues/INDEX.md" \
    "--all reindexed projB"
assert_file_contains "IM-100" "$STORE/IM/INDEX.md" \
    "--all also ran the env-resolved store reindex"

# ---------------------------------------------------------------------------
echo "[F] --all with nothing to reindex reports and exits 0"
# ---------------------------------------------------------------------------
EMPTY="$WORKDIR/empty-root"
mkdir -p "$EMPTY"
RC=0
OUT=$( cd "$EMPTY" && ISSUE_MGMT_ROOT="$EMPTY" "$REINDEX" --all 2>&1 ) || RC=$?
assert_eq 0 "$RC" "--all with no .issues dirs exits 0"
assert_contains "No .issues/" "$OUT" "--all reports nothing found"

# ---------------------------------------------------------------------------
echo "[G] --help / --version"
# ---------------------------------------------------------------------------
RC=0; "$REINDEX" --help >/dev/null 2>&1 || RC=$?
assert_eq 0 "$RC" "--help exits 0"
VER=$("$REINDEX" --version 2>/dev/null)
[ -n "$VER" ] && pass "--version prints ($VER)" || fail "--version empty"

echo
echo "============================================="
echo "admin-reindex: PASS: $PASS  FAIL: $FAIL"
echo "============================================="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
