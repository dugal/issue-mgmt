#!/usr/bin/env bash
# Verification script for IM-153: first-class dependency-edge authoring.
#
#   edit-issue --add-dep KIND:TARGET / --remove-dep TARGET engine +
#   thin issue-dep add/remove/list binary + /issue-dep-dev skill.
#
# Frontmatter-native checks: canonical block bytes, idempotency,
# cross-prefix, retract-to-empty, commit message, --json, exit codes, and
# the authoring-time blocks-cycle guard.
#
# Run from the project root:
#     tests/verification/im-153-issue-dep.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
DEP="$(resolve_cli issue-dep)"
EDIT="$(resolve_cli edit-issue)"
REINDEX="$(resolve_cli reindex-issues)"

PASS=0
FAIL=0
SKIP=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
skip() { echo "SKIP: $*"; SKIP=$((SKIP+1)); }

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
        fail "$3: '$1' not found in: $2"
    fi
}

WORKDIR="$(mktemp -d -t im-153.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

export PATH="$ISSUE_MGMT_BIN:$PATH"
unset ISSUE_STORE_ROOT
unset ISSUE_MGMT_BD_BIN
unset ISSUE_MGMT_NO_BD_BRIDGE
unset ISSUE_MGMT_NO_AUTOCOMMIT

mk_store() {
    local path="$WORKDIR/$1"
    mkdir -p "$path"
    git -C "$path" init -q
    git -C "$path" config user.email "$FIXTURE_EMAIL"
    git -C "$path" config user.name test
    git -C "$path" commit -q --allow-empty -m "init store"
    echo "$path"
}

mk_registered_repo() {
    # mk_registered_repo <repo-name> <store-path> <prefix>
    local path="$WORKDIR/$1"
    mkdir -p "$path/.issues"
    git -C "$path" init -q
    git -C "$path" config user.email "$FIXTURE_EMAIL"
    git -C "$path" config user.name test
    echo "$3" > "$path/.issues/.prefix"
    echo "$2" > "$path/.issues/.migrated-to-store"
    printf '%s:%s\n' "$1" "$3" >> "$2/.repos"
    echo "$path"
}

write_issue() {
    # write_issue <store> <prefix> <id>
    local dir="$1/$2"
    mkdir -p "$dir"
    cat > "$dir/$3.md" <<EOF
---
id: $3
title: "Fixture $3"
type: chore
status: open
priority: medium
created: 2026-07-01
updated: 2026-07-01
branch: ""
tags: []
---

## Summary

Fixture body for $3.

## Implementation Notes
EOF
}

# =====================================================================
# Section A: frontmatter-side authoring (plain store, no bd)
# =====================================================================
STORE_A="$(mk_store store-a)"
REPO_A="$(mk_registered_repo repo-a "$STORE_A" DOUG)"
write_issue "$STORE_A" DOUG DOUG-064
git -C "$STORE_A" add -A >/dev/null 2>&1
git -C "$STORE_A" commit -q -m "seed fixtures"

# --- A1: issue-dep add produces canonical block bytes ----------------
out=$( cd "$REPO_A" && "$DEP" add DOUG-064 blocks:RM-107 2>&1 )
rc=$?
# COVERS: cli.dep-authoring
assert_eq 0 "$rc" "A1: issue-dep add exits 0 (cross-prefix DOUG -> RM)"

FILE_A="$STORE_A/DOUG/DOUG-064.md"
# Byte-exact canonical shape: 2-space list marker, to-before-type,
# 4-space continuation (matches bridge-bd.sh A8 fixture).
block=$( grep -E '^depends_on:$|^  - to: |^    type: ' "$FILE_A" )
expected=$'depends_on:\n  - to: RM-107\n    type: blocks'
# COVERS: cli.dep-authoring
assert_eq "$expected" "$block" "A1: canonical block bytes (2/4-space, to-before-type)"

# --- A2: commit message is `dep <id>` --------------------------------
msg=$( git -C "$STORE_A" log -1 --format='%s' )
# COVERS: cli.dep-authoring
assert_eq "dep DOUG-064" "$msg" "A2: per-tool commit message"

# --- A3: idempotent re-add is a no-op --------------------------------
( cd "$REPO_A" && "$DEP" add DOUG-064 blocks:RM-107 >/dev/null 2>&1 )
rc=$?
n=$( grep -c 'to: RM-107' "$FILE_A" )
# COVERS: cli.dep-authoring
assert_eq 0 "$rc" "A3: re-add exits 0"
# COVERS: cli.dep-authoring
assert_eq 1 "$n" "A3: re-add did not duplicate the edge"

# --- A4: a second distinct edge appends ------------------------------
( cd "$REPO_A" && "$DEP" add DOUG-064 related:IM-102 >/dev/null 2>&1 )
n=$( grep -c '^  - to:' "$FILE_A" )
# COVERS: cli.dep-authoring
assert_eq 2 "$n" "A4: distinct edge appended (2 edges)"

# --- A5: list renders both edges (no lock) ---------------------------
out=$( cd "$REPO_A" && "$DEP" list DOUG-064 2>/dev/null )
# COVERS: cli.dep-authoring
assert_contains "blocks:RM-107" "$out" "A5: list shows the blocks edge"
# COVERS: cli.dep-authoring
assert_contains "related:IM-102" "$out" "A5: list shows the related edge"

# --- A6: --json list is a stable JSON array --------------------------
jout=$( cd "$REPO_A" && "$DEP" list DOUG-064 --json 2>/dev/null )
depcount=$( echo "$jout" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["depends_on"]))' )
# COVERS: cli.dep-authoring
assert_eq 2 "$depcount" "A6: --json list carries both edges"

# --- A7: remove one edge by --kind; the other survives ---------------
( cd "$REPO_A" && "$DEP" remove DOUG-064 RM-107 --kind blocks >/dev/null 2>&1 )
n=$( grep -c '^  - to:' "$FILE_A" )
# COVERS: cli.dep-authoring
assert_eq 1 "$n" "A7: --kind-scoped remove drops exactly one edge"

# --- A8: removing the last edge writes depends_on: [] ----------------
( cd "$REPO_A" && "$DEP" remove DOUG-064 IM-102 >/dev/null 2>&1 )
depline=$( grep '^depends_on' "$FILE_A" )
# COVERS: cli.dep-authoring
assert_eq "depends_on: []" "$depline" "A8: last-edge removal yields depends_on: [] (key not deleted)"
haskey=$( grep -c '^depends_on' "$FILE_A" )
# COVERS: cli.dep-authoring
assert_eq 1 "$haskey" "A8: the depends_on key is still present"

# --- A9: remove of an absent edge is an idempotent no-op -------------
( cd "$REPO_A" && "$DEP" remove DOUG-064 IM-999 >/dev/null 2>&1 )
rc=$?
# COVERS: cli.dep-authoring
assert_eq 0 "$rc" "A9: remove of absent edge exits 0"

# --- A10: bad kind and malformed target exit 2 -----------------------
( cd "$REPO_A" && "$DEP" add DOUG-064 nope:RM-107 >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "A10: bad kind exits 2"
( cd "$REPO_A" && "$DEP" add DOUG-064 blocks:notanid >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "A10: malformed target exits 2"

# --- A11: the edit-issue engine authors the same edge ----------------
write_issue "$STORE_A" DOUG DOUG-065
git -C "$STORE_A" add -A >/dev/null 2>&1
git -C "$STORE_A" commit -q -m "seed DOUG-065"
( cd "$REPO_A" && "$EDIT" DOUG-065 --add-dep parent-child:DOUG-064 >/dev/null 2>&1 )
rc=$?
FILE_A65="$STORE_A/DOUG/DOUG-065.md"
block65=$( grep -E '^  - to:|^    type:' "$FILE_A65" )
# COVERS: cli.dep-authoring
assert_eq 0 "$rc" "A11: edit-issue --add-dep exits 0 (hyphenated kind)"
# COVERS: cli.dep-authoring
assert_contains "type: parent-child" "$block65" "A11: hyphenated kind split on first colon"

# --- A12: authoring-time blocks-cycle guard (bd-independent) --
# A `blocks` add that would close a cycle is refused BEFORE any write, so
# the markdown source of truth never holds a cycle bd cannot mirror. This
# store has no .beads, proving the guard is bridge-independent.
STORE_CY="$(mk_store store-cycle)"
REPO_CY="$(mk_registered_repo repo-cycle "$STORE_CY" DOUG)"
for n in DOUG-070 DOUG-071 DOUG-072; do write_issue "$STORE_CY" DOUG "$n"; done
git -C "$STORE_CY" add -A >/dev/null 2>&1
git -C "$STORE_CY" commit -q -m "seed cycle fixtures"
( cd "$REPO_CY" && "$EDIT" DOUG-070 --add-dep blocks:DOUG-071 >/dev/null 2>&1 )
( cd "$REPO_CY" && "$EDIT" DOUG-071 --add-dep blocks:DOUG-072 >/dev/null 2>&1 )
# Closing edge DOUG-072 -> DOUG-070 forms a 3-cycle: must be refused.
out=$( cd "$REPO_CY" && "$EDIT" DOUG-072 --add-dep blocks:DOUG-070 2>&1 )
rc=$?
# COVERS: workflow.authoring-cycle-guard
assert_eq 1 "$rc" "A12: closing blocks edge refused with exit 1"
# COVERS: workflow.authoring-cycle-guard
assert_contains "reason=dependency-cycle" "$out" "A12: structured dependency-cycle error"
# COVERS: workflow.authoring-cycle-guard
assert_contains "DOUG-072 -> DOUG-070 -> DOUG-071 -> DOUG-072" "$out" "A12: cycle path named"
# The refused write left NO edge on disk (source of truth untouched).
FILE_CY72="$STORE_CY/DOUG/DOUG-072.md"
# COVERS: workflow.authoring-cycle-guard
assert_eq 0 "$( grep -c 'to: DOUG-070' "$FILE_CY72" )" \
    "A12: refused edge not written to markdown"
# No issue file is dirty/untracked after the refusal -- the mutation aborted
# before any write (the runtime .locks/ dir is the only untracked artifact).
# COVERS: workflow.authoring-cycle-guard
assert_eq "" "$( git -C "$STORE_CY" status --porcelain -- '*.md' )" \
    "A12: no issue-file write after refusal (no orphan write)"
# A self-loop is likewise refused.
out=$( cd "$REPO_CY" && "$DEP" add DOUG-070 blocks:DOUG-070 2>&1 )
rc=$?
# COVERS: workflow.authoring-cycle-guard
assert_eq 1 "$rc" "A12: self-loop blocks edge refused (via issue-dep wrapper)"
# A non-blocks back-edge closing the same loop is ALLOWED (not ordering).
out=$( cd "$REPO_CY" && "$EDIT" DOUG-072 --add-dep discovered-from:DOUG-070 2>&1 )
rc=$?
# COVERS: workflow.authoring-cycle-guard
assert_eq 0 "$rc" "A12: discovered-from back-edge allowed (provenance is not ordering)"

# =====================================================================
# Section B: authored edges round-trip through the markdown source of truth
# =====================================================================
STORE_B="$(mk_store store-b)"
REPO_B="$(mk_registered_repo repo-b "$STORE_B" IM)"
write_issue "$STORE_B" IM IM-101
write_issue "$STORE_B" IM IM-102

# Author an edge through the verb; it lands in the depends_on block.
( cd "$REPO_B" && "$DEP" add IM-102 blocks:IM-101 >/dev/null 2>&1 )
listing=$( cd "$REPO_B" && "$DEP" list IM-102 2>/dev/null )
assert_contains "blocks:IM-101" "$listing" "B1: issue-dep add records the IM-102 -> IM-101 edge"

# Retract; the last edge collapses to the authoritative empty list.
( cd "$REPO_B" && "$DEP" remove IM-102 IM-101 >/dev/null 2>&1 )
listing=$( cd "$REPO_B" && "$DEP" list IM-102 2>/dev/null )
assert_eq "" "$listing" "B2: last-edge removal empties the edge list"
grep -q "^depends_on: \[\]" "$STORE_B/IM/IM-102.md" \
    && pass "B3: retract writes the authoritative depends_on: [] (never deletes the key)" \
    || fail "B3: retract did not write depends_on: []"

# =====================================================================
# Section C: depcore list reader -- edge and error paths
# =====================================================================
# The lock-free `dep list` reader (lib/depcore.py) has render and error
# branches the happy-path sections above never reach: an issue with no
# depends_on key, a legacy string-form (quoted "KIND:TARGET") edge, a
# missing issue, and a malformed id.
STORE_C="$(mk_store store-c)"
REPO_C="$(mk_registered_repo repo-c "$STORE_C" IM)"
write_issue "$STORE_C" IM IM-300           # written with no depends_on key
git -C "$STORE_C" add -A >/dev/null 2>&1
git -C "$STORE_C" commit -q -m "seed section C"

# --- C1: list on an issue with no depends_on key -> empty, exit 0 ----
out=$( cd "$REPO_C" && "$DEP" list IM-300 2>/dev/null )
rc=$?
# COVERS: cli.dep-authoring
assert_eq 0 "$rc" "C1: dep list on a depless issue exits 0"
# COVERS: cli.dep-authoring
assert_eq "" "$out" "C1: dep list on a depless issue prints nothing"

# --- C2: --json on a depless issue is an empty edge array ------------
jout=$( cd "$REPO_C" && "$DEP" list IM-300 --json 2>/dev/null )
depcount=$( echo "$jout" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["depends_on"]))' )
# COVERS: cli.dep-authoring
assert_eq 0 "$depcount" "C2: --json list on a depless issue is an empty array"

# --- C3: list renders a legacy string-form edge verbatim ------------
# A quoted list entry parses to a plain string (not a to/type mapping),
# so the reader prints it through the non-dict render branch as-is.
cat > "$STORE_C/IM/IM-301.md" <<'EOF'
---
id: IM-301
title: "String-form deps"
type: chore
status: open
priority: medium
created: 2026-07-01
updated: 2026-07-01
branch: ""
depends_on:
  - "blocks:IM-300"
---

## Summary

Body.

## Implementation Notes
EOF
git -C "$STORE_C" add -A >/dev/null 2>&1
git -C "$STORE_C" commit -q -m "seed string-form dep"
out=$( cd "$REPO_C" && "$DEP" list IM-301 2>/dev/null )
# COVERS: cli.dep-authoring
assert_contains "blocks:IM-300" "$out" "C3: string-form edge rendered verbatim"

# --- C4: list on a missing issue exits 1 ----------------------------
( cd "$REPO_C" && "$DEP" list IM-999 >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 1 "$?" "C4: dep list on a missing issue exits 1"

# --- C5: list with a malformed id exits 2 ---------------------------
( cd "$REPO_C" && "$DEP" list notanid >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "C5: dep list with a malformed id exits 2"

# =====================================================================
# Section D: issue-dep subcommand routing + argument-shape errors
# =====================================================================
# The dep verb's dispatcher (run/_cmd_add/_cmd_remove) has argument-shape
# branches the authoring sections above never reach: a missing/unknown
# subcommand, missing operands, a colon-less add operand, and the --json
# mutation output shape.
write_issue "$STORE_A" DOUG DOUG-080
git -C "$STORE_A" add -A >/dev/null 2>&1
git -C "$STORE_A" commit -q -m "seed section D"

# --- D1: missing subcommand exits 2 ----------------------------------
( cd "$REPO_A" && "$DEP" >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "D1: dep with no subcommand exits 2"

# --- D2: unknown subcommand exits 2 ----------------------------------
( cd "$REPO_A" && "$DEP" frobnicate DOUG-080 >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "D2: dep with an unknown subcommand exits 2"

# --- D3: add with a colon-less operand exits 2 -----------------------
( cd "$REPO_A" && "$DEP" add DOUG-080 blocksRM107 >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "D3: dep add without a colon exits 2"

# --- D4: add with no operand exits 2 ---------------------------------
( cd "$REPO_A" && "$DEP" add DOUG-080 >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "D4: dep add with no KIND:TARGET exits 2"

# --- D5: remove with no target exits 2 -------------------------------
( cd "$REPO_A" && "$DEP" remove DOUG-080 >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "D5: dep remove with no TARGET exits 2"

# --- D6: remove with an invalid --kind exits 2 -----------------------
( cd "$REPO_A" && "$DEP" remove DOUG-080 RM-107 --kind nope >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 2 "$?" "D6: dep remove with an invalid --kind exits 2"

# --- D7: add --json emits a parseable mutation summary ---------------
jout=$( cd "$REPO_A" && "$DEP" add DOUG-080 blocks:RM-107 --json 2>/dev/null )
echo "$jout" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null \
    && pass "D7: dep add --json stdout is one parseable JSON document" \
    || fail "D7: dep add --json not parseable: $jout"
# COVERS: cli.dep-authoring
assert_contains "\"id\": \"DOUG-080\"" "$jout" "D7: add --json carries the issue id"

# --- D8: add on a missing issue exits 1 ------------------------------
( cd "$REPO_A" && "$DEP" add DOUG-999 blocks:RM-107 >/dev/null 2>&1 )
# COVERS: cli.dep-authoring
assert_eq 1 "$?" "D8: dep add on a missing issue exits 1"

# --- D9: the retired --type / --note spellings are unknown arguments -
# IM-270 retired both. --type is the issue type (list / search / create)
# and --note the closing note (transition / complete); each meant the
# dependency kind and the dependency note on this verb through v3.4.x, and
# a retired spelling exits 2 rather than resolving.
( cd "$REPO_A" && "$DEP" remove DOUG-080 RM-107 --type blocks >/dev/null 2>&1 )
# COVERS: surface.stability
assert_eq 2 "$?" "D9: retired --type is an unknown argument (exit 2)"
( cd "$REPO_A" && "$DEP" add DOUG-080 blocks:RM-107 --note x >/dev/null 2>&1 )
# COVERS: surface.stability
assert_eq 2 "$?" "D9: retired --note is an unknown argument (exit 2)"
# `edit` spelled the same dependency-kind concept --dep-type, retired with
# them, so one concept now has exactly one spelling on both verbs.
( cd "$REPO_A" && "$EDIT" DOUG-080 --remove-dep RM-107 --dep-type blocks \
    >/dev/null 2>&1 )
# COVERS: surface.stability
assert_eq 2 "$?" "D9: retired --dep-type is an unknown argument (exit 2)"

# --- E: one grammar for the (kind, target) pair on both ops (IM-243) --
# `add` fused the pair into KIND:TARGET while `remove` split it across
# TARGET --kind KIND through v3.4.x, so an agent that learned the add form
# and tried `dep remove ID blocks:RM-107` got a failed lookup for a target
# literally named "blocks:RM-107". Both ops now read either spelling.
write_issue "$STORE_A" DOUG DOUG-081
git -C "$STORE_A" add -A >/dev/null 2>&1
git -C "$STORE_A" commit -q -m "seed section E"
FILE_A81="$STORE_A/DOUG/DOUG-081.md"

( cd "$REPO_A" && "$DEP" add DOUG-081 blocks:RM-107 >/dev/null 2>&1 )
( cd "$REPO_A" && "$DEP" add DOUG-081 related:RM-107 >/dev/null 2>&1 )
( cd "$REPO_A" && "$DEP" remove DOUG-081 blocks:RM-107 >/dev/null 2>&1 )
rc=$?
# COVERS: cli.dep-authoring
assert_eq 0 "$rc" "E1: dep remove accepts the fused KIND:TARGET operand"
# COVERS: surface.one-meaning-per-flag
assert_eq 1 "$( grep -c '^  - to:' "$FILE_A81" )" \
    "E1: the fused operand scoped the removal to the blocks edge"
# COVERS: cli.dep-authoring
assert_contains "type: related" "$( grep -E '^    type:' "$FILE_A81" )" \
    "E1: the related edge to the same target survived"

# The split spelling is unchanged, and `add` now reads it too -- the
# mirror-image of the reported failure (an agent that learned remove's
# split form and tried it on add).
( cd "$REPO_A" && "$DEP" add DOUG-081 RM-108 --kind blocks >/dev/null 2>&1 )
rc=$?
# COVERS: surface.one-meaning-per-flag
assert_eq 0 "$rc" "E2: dep add accepts the split TARGET --kind KIND spelling"
# COVERS: cli.dep-authoring
assert_contains "to: RM-108" "$( grep -E '^  - to:' "$FILE_A81" )" \
    "E2: the split spelling authored the same edge"

# A fused kind plus a CONFLICTING --kind is a usage error; the caller said
# two different things and the CLI does not pick one.
out=$( cd "$REPO_A" && "$DEP" remove DOUG-081 blocks:RM-108 --kind related 2>&1 )
rc=$?
# COVERS: surface.one-meaning-per-flag
assert_eq 2 "$rc" "E3: fused kind conflicting with --kind exits 2"
# COVERS: surface.one-meaning-per-flag
assert_contains "conflicting dependency kind" "$out" \
    "E3: the diagnostic names the conflict"
# COVERS: cli.dep-authoring
assert_contains "to: RM-108" "$( grep -E '^  - to:' "$FILE_A81" )" \
    "E3: the refused removal wrote nothing"

# A fused kind AGREEING with --kind is accepted -- one thing said twice.
( cd "$REPO_A" && "$DEP" remove DOUG-081 blocks:RM-108 --kind blocks \
    >/dev/null 2>&1 )
# COVERS: surface.one-meaning-per-flag
assert_eq 0 "$?" "E4: fused kind agreeing with --kind is accepted"

# A colon-headed operand whose head is not a dependency kind stays an
# error naming the four kinds -- never a target silently containing a colon.
out=$( cd "$REPO_A" && "$DEP" remove DOUG-081 nope:RM-107 2>&1 )
rc=$?
# COVERS: cli.dep-authoring
assert_eq 2 "$rc" "E5: an unknown fused head exits 2"
# COVERS: cli.dep-authoring
assert_contains "discovered-from" "$out" "E5: the diagnostic names the four kinds"

# Both spellings reach the `edit` engine too -- `dep` is a thin wrapper
# over it, so the grammar is one surface, not two.
( cd "$REPO_A" && "$EDIT" DOUG-081 --remove-dep related:RM-107 >/dev/null 2>&1 )
rc=$?
# COVERS: surface.one-meaning-per-flag
assert_eq 0 "$rc" "E6: edit --remove-dep accepts the fused operand"
# COVERS: cli.dep-authoring
assert_eq "depends_on: []" "$( grep '^depends_on' "$FILE_A81" )" \
    "E6: the fused removal retracted the last edge"
( cd "$REPO_A" && "$EDIT" DOUG-081 --add-dep RM-109 --kind blocks \
    >/dev/null 2>&1 )
# COVERS: surface.one-meaning-per-flag
assert_eq 0 "$?" "E6: edit --add-dep accepts the split spelling"
out=$( cd "$REPO_A" && "$EDIT" DOUG-081 --add-dep blocks:RM-109 --kind related 2>&1 )
rc=$?
# COVERS: surface.one-meaning-per-flag
assert_eq 2 "$rc" "E6: edit refuses a conflicting kind (exit 2)"

echo
echo "im-153-issue-dep: $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -eq 0 ]
