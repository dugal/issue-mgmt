#!/usr/bin/env bash
# Verification script for IM-205 resolve-time ID/prefix canonicalization.
#
# Exercises the acceptance criteria end-to-end against a real store:
#   - Integer normalization: IM-7 / IM-07 / IM-007 all resolve IM-007.md
#   - Case fold: im-7 / IM-7 resolve the same file
#   - Width-agnostic: a 4-digit-padded on-disk file (IM-0007.md) resolves
#     from IM-7 (the canonical id is the actual on-disk stem)
#   - P2 LOCK-PATH-USES-CANONICAL: holding .locks/IM-007.lock and running
#     `issue note IM-7` yields exit 3 concurrent-edit -- proving the note
#     contends on the canonical lock, not a phantom IM-7.lock
#   - Canonical output: `note IM-7` commit message is `note IM-007` and its
#     --json id is IM-007; `dep add IM-7 blocks:im-84` writes `to: IM-084`
#   - D2 fail-loud: with both IM-007.md and IM-0007.md present, `IM-7`
#     resolution exits 1 naming both files (store-integrity)
#   - IM-999999 (valid shape, no file): exact miss -> glob miss -> not-found
#     exit 1, no crash
#   - Source guard: no verb hand-builds a `.locks/...lock` path from a raw
#     id outside lib/idresolve.py
#
# Run from the project root:
#     tests/verification/im-205-id-canon.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE_SHOW="$(resolve_cli issue-show)"
ISSUE_NOTE="$(resolve_cli issue-note)"
ISSUE_DEP="$(resolve_cli issue-dep)"
TRANSITION="$(resolve_cli transition-issue)"
CONCURRENCY_PY="$PROJECT_ROOT/lib/concurrency.py"  # Tier 2 adversary

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then pass "$3 ($2)"; else
        fail "$3: expected '$1', got '$2'"; fi
}
assert_contains() {
    if echo "$2" | grep -qF -- "$1"; then pass "$3 (contains '$1')"; else
        fail "$3: '$1' not found in: $2"; fi
}
assert_file_contains() {
    if grep -qF -- "$1" "$2"; then pass "$3 (file contains '$1')"; else
        fail "$3: '$1' not in $2:"; sed -n '1,40p' "$2"; fi
}

TMPDIR_ROOT=$(mktemp -d -t im205-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT
export PATH="$ISSUE_MGMT_BIN:$PATH"
unset ISSUE_STORE_ROOT

TODAY=$(date +%Y-%m-%d)

make_issue() {
    # make_issue <path> <id>
    cat > "$1" <<EOF
---
id: $2
title: "Canon fixture"
type: chore
status: open
priority: medium
created: 2026-08-02
updated: 2026-08-02
branch: ""
tags: []
changelog:
---

## Summary

Body.

## Implementation Notes
EOF
}

new_store() {
    # new_store <dir> -- git-initialized store with IM registered.
    mkdir -p "$1/IM/.locks"
    (
        cd "$1"
        git init -q
        git config user.email "$FIXTURE_EMAIL"
        git config user.name test
        printf 'storeproj:IM\n' > .repos
        git add .repos
        git commit -q -m "init store"
    )
}

# ---------------------------------------------------------------------------
# Section A: integer normalization + case fold (read path)
# ---------------------------------------------------------------------------
echo "[A] Integer normalization + case fold"

STORE="$TMPDIR_ROOT/store"
new_store "$STORE"
make_issue "$STORE/IM/IM-007.md" "IM-007"
(cd "$STORE" && git add IM/IM-007.md && git commit -q -m "create IM-007")
export ISSUE_STORE_ROOT="$STORE"

# COVERS: cli.id-canonicalization
OUT=$("$ISSUE_SHOW" IM-7 --no-body --frontmatter 2>/dev/null)
assert_contains "id: IM-007" "$OUT" "IM-7 resolves IM-007.md (integer normalize)"

# COVERS: cli.id-canonicalization
OUT=$("$ISSUE_SHOW" IM-07 --no-body --frontmatter 2>/dev/null)
assert_contains "id: IM-007" "$OUT" "IM-07 resolves IM-007.md (padding-agnostic)"

# COVERS: cli.id-canonicalization
OUT=$("$ISSUE_SHOW" im-7 --no-body --frontmatter 2>/dev/null)
assert_contains "id: IM-007" "$OUT" "im-7 resolves IM-007.md (case fold)"

# COVERS: cli.id-canonicalization
ERR=$("$ISSUE_SHOW" IM-7 --no-body 2>&1 >/dev/null)
assert_contains "resolved IM-7 -> IM-007" "$ERR" "normalization note on stderr when changed"

# Already-canonical input is silent (clean --json contract).
ERR=$("$ISSUE_SHOW" IM-007 --no-body 2>&1 >/dev/null)
assert_eq "" "$ERR" "already-canonical input emits no normalization note"

# COVERS: cli.id-canonicalization
# Width-agnostic: a 4-digit-padded on-disk file resolves from IM-7.
WSTORE="$TMPDIR_ROOT/wstore"
new_store "$WSTORE"
make_issue "$WSTORE/IM/IM-0007.md" "IM-0007"
(cd "$WSTORE" && git add IM/IM-0007.md && git commit -q -m "create IM-0007")
OUT=$(ISSUE_STORE_ROOT="$WSTORE" "$ISSUE_SHOW" IM-7 --no-body --frontmatter 2>/dev/null)
assert_contains "id: IM-0007" "$OUT" "IM-7 resolves a 4-digit-padded on-disk file"

# ---------------------------------------------------------------------------
# Section B: P2 -- the lock path is built from the canonical stem
# ---------------------------------------------------------------------------
echo
echo "[B] P2: lock path uses the canonical stem"

LOCK="$STORE/IM/.locks/IM-007.lock"
(
    python3 "$CONCURRENCY_PY" flock-run "$LOCK" --timeout 8 -- \
        bash -c "sleep 3"
) &
HOLDER_PID=$!
sleep 0.5

RC=0
# COVERS: principles.p2
STDERR=$("$ISSUE_NOTE" IM-7 "contended via IM-7" --lock-timeout 0.5 2>&1 >/dev/null) || RC=$?
wait $HOLDER_PID 2>/dev/null
assert_eq "3" "$RC" "note IM-7 loses IM-007.lock -> exit 3 (canonical lock, not IM-7.lock)"
assert_contains "concurrent-edit" "$STDERR" "P2 loss reports concurrent-edit"

# ---------------------------------------------------------------------------
# Section C: canonical output -- commit message + --json id + dep to:
# ---------------------------------------------------------------------------
echo
echo "[C] Canonical output flows downstream"

# COVERS: cli.id-canonicalization
JSON=$("$ISSUE_NOTE" IM-7 "progress via IM-7" --json 2>/dev/null)
JID=$(printf '%s' "$JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
assert_eq "IM-007" "$JID" "note IM-7 --json reports canonical id IM-007"

LAST_MSG=$(git -C "$STORE" log -1 --format=%s)
assert_eq "note IM-007" "$LAST_MSG" "note IM-7 commit message is 'note IM-007'"

# COVERS: cli.id-canonicalization
"$ISSUE_DEP" add IM-7 blocks:im-84 >/dev/null 2>&1
assert_file_contains "to: IM-084" "$STORE/IM/IM-007.md" \
    "dep add IM-7 blocks:im-84 writes canonical edge 'to: IM-084'"

LAST_MSG=$(git -C "$STORE" log -1 --format=%s)
assert_eq "dep IM-007" "$LAST_MSG" "dep add IM-7 commit message is 'dep IM-007'"

# transition through the canonical id too (open -> in-progress is legal).
# COVERS: cli.id-canonicalization
"$TRANSITION" IM-7 in-progress >/dev/null 2>&1
LAST_MSG=$(git -C "$STORE" log -1 --format=%s)
assert_contains "transition IM-007 in-progress" "$LAST_MSG" \
    "transition IM-7 in-progress commits 'transition IM-007 in-progress'"

# ---------------------------------------------------------------------------
# Section D: D2 -- two paddings of one integer fail loud
# ---------------------------------------------------------------------------
echo
echo "[D] D2: dup-padding collision fails loud"

DSTORE="$TMPDIR_ROOT/dstore"
new_store "$DSTORE"
make_issue "$DSTORE/IM/IM-007.md" "IM-007"
make_issue "$DSTORE/IM/IM-0007.md" "IM-0007"
(cd "$DSTORE" && git add IM && git commit -q -m "create dup-padding")

RC=0
# COVERS: cli.id-canonicalization
STDERR=$(ISSUE_STORE_ROOT="$DSTORE" "$ISSUE_SHOW" IM-7 --no-body 2>&1 >/dev/null) || RC=$?
assert_eq "1" "$RC" "IM-7 with both IM-007.md and IM-0007.md exits 1"
assert_contains "IM-007.md" "$STDERR" "D2 error names IM-007.md"
assert_contains "IM-0007.md" "$STDERR" "D2 error names IM-0007.md"

# ---------------------------------------------------------------------------
# Section E: valid shape, no file -> not-found, no crash
# ---------------------------------------------------------------------------
echo
echo "[E] Valid shape, no file: exact miss -> glob miss -> not-found"

RC=0
# COVERS: cli.id-canonicalization
STDERR=$("$ISSUE_SHOW" IM-999999 --no-body 2>&1 >/dev/null) || RC=$?
assert_eq "1" "$RC" "IM-999999 (no file) exits 1"
assert_contains "not found" "$STDERR" "IM-999999 reports issue file not found (no crash)"

# ---------------------------------------------------------------------------
# Section F: source guard -- lock paths are built only in idresolve
# ---------------------------------------------------------------------------
echo
echo "[F] Source guard: no verb hand-builds a .locks lock path"

# Every '.locks/ ... .lock' path build must live in lib/idresolve.py (the
# single canonical builder, IM-205) or lib/concurrency.py (the low-level
# lock-path helpers keyed by an already-canonical id). Any '.locks' path
# construction elsewhere under lib/cli/ would risk a raw-id phantom lock.
OFFENDERS=$(grep -rn '\.locks' "$PROJECT_ROOT/lib/cli/" 2>/dev/null \
    | grep -F '.lock"' \
    | grep -v 'admin_init' || true)
if [ -z "$OFFENDERS" ]; then
    pass "no verb module hand-builds a per-issue .locks/<id>.lock path"
else
    fail "verb module builds a .locks lock path outside idresolve:"
    echo "$OFFENDERS"
fi

# ---------------------------------------------------------------------------
# Section G: help/version
# ---------------------------------------------------------------------------
echo
echo "[G] Help and version"
RC=0; "$ISSUE_SHOW" --help >/dev/null 2>&1 || RC=$?
assert_eq "0" "$RC" "issue show --help exits 0"

echo
echo "==================================================================="
echo "IM-205 id-canonicalization: $PASS passed, $FAIL failed"
echo "==================================================================="
[ "$FAIL" -eq 0 ]
