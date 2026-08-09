#!/usr/bin/env bash
# Verification script for IM-105 (W6 skill rewires). Skills are prompt
# files, so this script exercises the exact command sequences and
# python3 snippets the six rewired workflow-side SKILL.md files
# document, against the real CLIs in a temp store:
#
#   (a) the /issue-start-dev claim sequence: opportunistic reap ->
#       transition-issue --claim --json -> parse claim_generation ->
#       persist session state -> guarded branch edit
#   (b) lost-race: actor A holds, actor B's claim exits 4 with the
#       contender named in the one-line JSON stderr object
#   (c) stale-view: wrong --claim-generation exits 5 with
#       current_generation/caller_generation in the JSON stderr
#   (d) session-state write/read round trip using the documented
#       snippets verbatim
#   (e) reaper invocation path (expired lease reaped, generation
#       preserved)
#   (f) issue-ready suggestion path (frontmatter-native; no bd)
#   (g) generation-guard degradation: missing state file -> empty GEN,
#       one-line note, unguarded call still succeeds
#
# Run from the project root:
#     tests/verification/im-105-skill-rewires.sh

set -u

# Hard-isolate from the caller's real store.
unset ISSUE_STORE_ROOT
unset ISSUE_MGMT_BD_BIN

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
TRANSITION="$(resolve_cli transition-issue)"
EDIT="$(resolve_cli edit-issue)"
SHOW="$(resolve_cli issue-show)"
REAP="$(resolve_cli issue-reap-expired-claims)"
READY="$(resolve_cli issue-ready)"

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
        fail "$3: '$1' not found in: $2"
    fi
}

WORKDIR="$(mktemp -d -t im-105-skills.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

STORE="$WORKDIR/store"
mkdir -p "$STORE/TS"

write_issue_file() {
    # write_issue_file <path> <id> <status>
    cat > "$1" <<EOF
---
id: $2
title: "Fixture $2"
type: chore
status: $3
priority: medium
created: 2026-07-08
updated: 2026-07-08
branch: ""
tags: []
---

## Summary

Fixture body for $2.

## Implementation Notes

EOF
}

write_issue_file "$STORE/TS/TS-001.md" TS-001 open
write_issue_file "$STORE/TS/TS-002.md" TS-002 open
write_issue_file "$STORE/TS/TS-003.md" TS-003 open
( cd "$STORE" && git init -q . && git add -A && git commit -qm "fixture" )
export ISSUE_STORE_ROOT="$STORE"

# The skills key session state by the Claude main-loop PID via bash
# $PPID. In this script any stable numeric key exercises the same
# round trip; the script's own $PPID plays that role.
SESSION_DIR="$WORKDIR/.claude/issue-mgmt-sessions"
SESSION_STATE="$SESSION_DIR/${PPID}.json"
ACTOR="claude-code-${PPID}"

json_field() {
    # json_field <json> <field...>  -- print nested field or nothing
    python3 - "$@" <<'PY'
import json, sys
try:
    obj = json.loads(sys.argv[1])
    for key in sys.argv[2:]:
        obj = obj[key]
    print(obj)
except (ValueError, KeyError, TypeError):
    pass
PY
}

echo "== (a) /issue-start-dev documented claim sequence =="

# Step 2 -- opportunistic reap (clean store: exit 0, nothing reaped).
OUT=$("$REAP" --repo TS --json 2>/dev/null)
RC=$?
assert_eq 0 "$RC" "(a) opportunistic reap exits 0 on a clean prefix"
assert_contains '"reaped": []' "$OUT" "(a) clean prefix reaps nothing"

# Step 3 -- claim.
CLAIM_JSON=$("$TRANSITION" TS-001 --claim "$ACTOR" --json --no-reindex)
RC=$?
assert_eq 0 "$RC" "(a) transition-issue --claim exits 0"
GEN_ACQ=$(json_field "$CLAIM_JSON" claim_generation)
assert_eq 1 "$GEN_ACQ" "(a) acquired claim_generation parses from stdout JSON"
assert_eq "$ACTOR" "$(json_field "$CLAIM_JSON" claim assignee)" \
    "(a) claim JSON names this actor as assignee"
assert_eq in-progress "$(json_field "$CLAIM_JSON" status)" \
    "(a) claim flips status to in-progress"

# Step 4 -- persist session state (documented write snippet, verbatim
# except SESSION_STATE resolves into the fixture, not a git toplevel).
mkdir -p "$(dirname "$SESSION_STATE")"
python3 - "$SESSION_STATE" "TS-001" "$CLAIM_JSON" <<'PY'
import json, os, sys
path, issue_id, claim = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
try:
    with open(path) as f:
        state = json.load(f)
except (OSError, ValueError):
    state = {}
state.setdefault("issues", {})[issue_id] = {
    "claim_generation": claim["claim_generation"],
    "claimed_at": claim["claim"]["claimed_at"],
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
PY
if [ -f "$SESSION_STATE" ]; then
    pass "(a) session-state file written"
else
    fail "(a) session-state file missing at $SESSION_STATE"
fi

# Step 5 -- guarded branch edit with the acquired generation.
"$EDIT" TS-001 --set branch="pilot-c2/w6-smoke" --claim-generation "$GEN_ACQ" --no-reindex
assert_eq 0 "$?" "(a) guarded branch edit with matching generation exits 0"
FM=$("$SHOW" TS-001 --frontmatter --json)
assert_eq "pilot-c2/w6-smoke" "$(json_field "$FM" frontmatter branch)" \
    "(a) branch recorded on the issue"

echo "== (b) lost-race: exit 4, contender named in JSON stderr =="

"$TRANSITION" TS-002 --claim actor-a --json --no-reindex >/dev/null 2>&1
ERR=$("$TRANSITION" TS-002 --claim actor-b --json --no-reindex 2>&1 >/dev/null)
RC=$?
assert_eq 4 "$RC" "(b) second claimant exits 4"
assert_eq lost-race "$(json_field "$ERR" class)" "(b) stderr JSON class=lost-race"
assert_eq actor-a "$(json_field "$ERR" contender)" "(b) stderr JSON names the contender"
assert_eq 4 "$(json_field "$ERR" exit)" "(b) stderr JSON carries exit=4"
LEASE_UNTIL=$(json_field "$ERR" lease_until)
if [ -n "$LEASE_UNTIL" ]; then
    pass "(b) stderr JSON carries lease_until ($LEASE_UNTIL)"
else
    fail "(b) stderr JSON missing lease_until: $ERR"
fi

echo "== (c) stale-view: wrong --claim-generation exits 5 =="

ERR=$("$TRANSITION" TS-001 done --claim-generation 99 --json --no-reindex 2>&1 >/dev/null)
RC=$?
assert_eq 5 "$RC" "(c) stale generation on transition exits 5"
assert_eq stale-local-view "$(json_field "$ERR" class)" "(c) stderr JSON class=stale-local-view"
assert_eq 1 "$(json_field "$ERR" current_generation)" "(c) stderr JSON current_generation"
assert_eq 99 "$(json_field "$ERR" caller_generation)" "(c) stderr JSON caller_generation"

ERR=$("$EDIT" TS-001 --set priority=low --claim-generation 99 --json --no-reindex 2>&1 >/dev/null)
assert_eq 5 "$?" "(c) stale generation on edit-issue exits 5"
assert_eq stale-local-view "$(json_field "$ERR" class)" "(c) edit-issue stderr JSON class=stale-local-view"

echo "== (d) session-state read round trip (documented read snippet) =="

GEN=$(python3 - "$SESSION_STATE" "TS-001" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f)["issues"][sys.argv[2]]["claim_generation"])
except (OSError, ValueError, KeyError, TypeError):
    pass
PY
)
assert_eq "$GEN_ACQ" "$GEN" "(d) read snippet returns the acquired generation"

# The recorded generation drives a guarded terminal transition.
"$TRANSITION" TS-001 done --claim-generation "$GEN" --json --no-reindex >/dev/null 2>&1
assert_eq 0 "$?" "(d) guarded done with recorded generation exits 0"

echo "== (e) reaper invocation path (expired lease) =="

"$TRANSITION" TS-003 --claim actor-sleepy --lease-ttl 1 --json --no-reindex >/dev/null 2>&1
assert_eq 0 "$?" "(e) short-TTL claim acquired"
sleep 2
OUT=$("$REAP" --repo TS --json 2>/dev/null)
RC=$?
assert_eq 0 "$RC" "(e) reaper exits 0"
assert_contains '"id": "TS-003"' "$OUT" "(e) expired TS-003 claim reaped"
FM=$("$SHOW" TS-003 --frontmatter --json)
assert_eq 1 "$(json_field "$FM" frontmatter claim_generation)" \
    "(e) claim_generation preserved after reap"

echo "== (f) issue-ready suggestion path (frontmatter-native) =="

# issue-ready computes readiness directly from markdown frontmatter,
# so an open, unblocked issue file is all the fixture needs -- no bd.
write_issue_file "$STORE/TS/TS-004.md" TS-004 open
OUT=$("$READY" --repo TS 2>/dev/null)
RC=$?
assert_eq 0 "$RC" "(f) issue-ready exits 0 with a ready candidate"
assert_contains '"id": "TS-004"' "$OUT" "(f) ready set contains the open candidate"
OUT=$("$READY" --repo TS --actor "$ACTOR" 2>/dev/null)
assert_eq 0 "$?" "(f) issue-ready --actor exits 0 (candidate unclaimed)"

echo "== (g) generation-guard degradation (missing state file) =="

MISSING_STATE="$SESSION_DIR/999999.json"
GEN=$(python3 - "$MISSING_STATE" "TS-002" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f)["issues"][sys.argv[2]]["claim_generation"])
except (OSError, ValueError, KeyError, TypeError):
    pass
PY
)
if [ -n "$GEN" ]; then
    GUARD="--claim-generation $GEN"
else
    GUARD=""
    NOTE=$(echo "note: no recorded generation for TS-002 in this session; proceeding without the --claim-generation guard")
fi
assert_eq "" "$GEN" "(g) missing state file yields empty GEN"
assert_contains "proceeding without the --claim-generation guard" "${NOTE:-}" \
    "(g) degradation note emitted"
# Unguarded call proceeds (same-state no-op on the claimed TS-002).
"$TRANSITION" TS-002 in-progress $GUARD --no-reindex >/dev/null 2>&1
assert_eq 0 "$?" "(g) unguarded transition proceeds (exit 0)"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
