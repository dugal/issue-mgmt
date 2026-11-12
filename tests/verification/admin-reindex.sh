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
    git config user.email "$FIXTURE_EMAIL"
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

# ---------------------------------------------------------------------------
echo "[H] IM-166: rendered rows read the TOP-LEVEL frontmatter keys"
# ---------------------------------------------------------------------------
# The renderer is a frontmatter-schema consumer, so it must distinguish a
# nested mapping key from a top-level one of the same name. Before IM-166 the
# field scan was nesting-blind and comment-blind: a `depends_on` entry's
# indented `type:` clobbered the issue's own `type:`, and the templated
# `changelog:` guard comment rendered as the Changelog cell's content.
ROWS="$WORKDIR/rows-store"
mkdir -p "$ROWS/IM"
(
    cd "$ROWS"
    git init -q
    git config user.email "$FIXTURE_EMAIL"
    git config user.name test
    printf 'rows-proj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
ROWSPROJ="$WORKDIR/rows-proj"
mkdir -p "$ROWSPROJ/.issues"
echo "IM" > "$ROWSPROJ/.issues/.prefix"
echo "$ROWS" > "$ROWSPROJ/.issues/.migrated-to-store"
cat > "$ROWS/IM/IM-400.md" <<'EOF'
---
id: IM-400
title: "Open issue carrying dependency edges"
type: feature
status: open
priority: high
created: 2026-07-01
updated: 2026-07-01
branch: ""
tags: []
changelog:    # set by /update-changelog only -- do not edit manually
depends_on:
  - to: IM-401
    type: blocks
  - to: IM-402
    type: related
---

## Summary

Its Type column must read `feature`, not the last edge's `related`.
EOF
cat > "$ROWS/IM/IM-401.md" <<'EOF'
---
id: IM-401
title: "Done issue with changelog skip"
type: chore
status: done
priority: low
created: 2026-07-01
updated: 2026-07-02
changelog: skip
---

## Summary

Triggers the six-column Done table.
EOF
cat > "$ROWS/IM/IM-402.md" <<'EOF'
---
id: IM-402
title: "Done issue carrying the templated changelog comment"
type: bug
status: done
priority: medium
created: 2026-07-01
updated: 2026-07-02
changelog:    # set by /update-changelog only -- do not edit manually
depends_on:
  - to: IM-401
    type: discovered-from
---

## Summary

Carries both defects at once.
EOF
RC=0
( cd "$ROWSPROJ" && ISSUE_STORE_ROOT="$ROWS" "$REINDEX" >/dev/null 2>&1 ) || RC=$?
assert_eq 0 "$RC" "rendering-fixture store reindex exits 0"
ROW400=$(grep -F '| IM-400 |' "$ROWS/IM/INDEX.md")
# COVERS: substrate.frontmatter-schema
assert_contains "| feature |" "$ROW400" \
    "IM-166: Type column is the top-level type, not a depends_on edge kind"
if echo "$ROW400" | grep -qF -- "related"; then
    fail "IM-166: IM-400 row leaked a dependency edge kind: $ROW400"
else
    pass "IM-166: IM-400 row carries no dependency edge kind"
fi
ROW402=$(grep -F '| IM-402 |' "$ROWS/IM/INDEX.md")
# COVERS: substrate.frontmatter-schema
assert_contains "| bug |" "$ROW402" \
    "IM-166: Done-table Type column is the top-level type"
if echo "$ROW402" | grep -qF -- "update-changelog"; then
    fail "IM-166: IM-402 row leaked the changelog guard comment: $ROW402"
else
    pass "IM-166: IM-402 row does not leak the changelog guard comment"
fi
# Every data row in the Done table carries the same cell count as its header.
HDR_CELLS=$(awk '/^## Done/{d=1} d && /^\| ID \|/{print gsub(/\|/,"|"); exit}' \
    "$ROWS/IM/INDEX.md")
BAD_CELLS=$(awk -v want="$HDR_CELLS" '
    /^## Done/{d=1; next} /^## /{d=0}
    d && /^\| IM-/ { n=gsub(/\|/,"|"); if (n != want) print }
' "$ROWS/IM/INDEX.md")
if [ -z "$BAD_CELLS" ]; then
    pass "IM-166: every Done row has the header cell count ($HDR_CELLS pipes)"
else
    fail "IM-166: ragged Done rows: $BAD_CELLS"
fi

echo
echo "============================================="
echo "admin-reindex: PASS: $PASS  FAIL: $FAIL"
echo "============================================="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
