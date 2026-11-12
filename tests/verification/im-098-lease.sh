#!/usr/bin/env bash
# Verification script for IM-098 (TTL lease primitive) and the CLI
# loser-side contract, contiguous 3-7 exit codes).
#
# Exercises the acceptance criteria end-to-end against a temp store:
#   - transition-issue --claim writes claim/lease_until/claim_generation,
#     flips status open -> in-progress, bumps the era counter
#   - --json claim output carries claim_generation (W0a F4)
#   - Idempotent same-actor re-claim (exit 0, no bump)
#   - Concurrent-claim loser exits 4 with error=lost-race + contender
#   - Stale --claim-generation exits 5 with error=stale-local-view
#   - --renew extends lease_until only; --release clears claim +
#     lease_until and preserves claim_generation
#   - --lease-ttl overrides the 3600s binary default
#   - issue-reap-expired-claims: --dry-run, real reap preserves
#     generation, appends reaper note, never touches valid leases
#   - reaper --prefix case-folds through the shared resolver (IM-277):
#     `--prefix im` resolves IM, announces on stderr, keeps --json stdout
#     a single document; an absent prefix stays exit 2 naming the known set
#   - claim on blocked issue exits 7 (error=not-claimable)
#   - claim on missing issue exits 7 (reason=not-found)
#   - edit-issue --claim-generation guard (exit 5 mismatch / exit 0 match)
#   - exit-3 renumber: lock contention surfaces exit 3 with
#     error=concurrent-edit (was 17 through v2.4.1)
#   - --json error emission is a parseable one-line JSON object
#
# Run from the project root:
#     tests/verification/im-098-lease.sh

set -u

# Unset any inherited ISSUE_STORE_ROOT so the caller's real store can
# never win the resolution ladder over this script's temp fixtures.
unset ISSUE_STORE_ROOT

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
TRANSITION="$(resolve_cli transition-issue)"
EDIT="$(resolve_cli edit-issue)"
REAP="$(resolve_cli issue-reap-expired-claims)"
CONCURRENCY_PY="$PROJECT_ROOT/lib/concurrency.py"  # Tier 2: stack-coupled test adversary; see docs/design/concurrency-test-hook.md

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
        fail "$3: '$1' not found in '$2'"
    fi
}

assert_file_contains() {
    if grep -qF -- "$1" "$2"; then
        pass "$3 (file contains '$1')"
    else
        fail "$3: '$1' not in $2:"
        sed -n '1,40p' "$2"
    fi
}

assert_file_not_contains() {
    if grep -qF -- "$1" "$2"; then
        fail "$3: '$1' unexpectedly present in $2"
    else
        pass "$3 ('$1' absent)"
    fi
}

assert_no_claim_block() {
    # assert_no_claim_block <file> <label> -- the frontmatter claim block
    # (a bare `claim:` line + indented children) must be absent. The audit
    # notes legitimately contain the substring "claim:", so match the
    # exact line shape, not the substring.
    if grep -qE '^claim:[[:space:]]*$' "$1" || grep -q '^  assignee:' "$1"; then
        fail "$2: claim block still present in $1"
    else
        pass "$2 (claim block absent)"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im098-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

TODAY=$(date +%Y-%m-%d)

make_issue() {
    # make_issue <path> <id> <status>
    cat > "$1" <<EOF
---
id: $2
title: "Lease fixture"
type: feature
status: $3
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: [a]
changelog:
---

## Summary

Fixture body.

## Implementation Notes
EOF
}

mk_store() {
    # mk_store <path> -- git-init'd store with an IM prefix dir
    mkdir -p "$1/IM"
    (
        cd "$1"
        git init -q
        git config user.email "$FIXTURE_EMAIL"
        git config user.name test
        git commit -q --allow-empty -m "init store"
    )
}

json_field() {
    # json_field <json-string> <dotted-path>
    python3 -c '
import json, sys
obj = json.loads(sys.argv[1])
for part in sys.argv[2].split("."):
    obj = obj[part]
print(obj)
' "$1" "$2"
}

STORE="$TMPDIR_ROOT/store"
mk_store "$STORE"

# ---------------------------------------------------------------------------
echo
echo "[A] --claim acquires the lease (fields, status, generation, note)"
# ---------------------------------------------------------------------------
make_issue "$STORE/IM/IM-201.md" "IM-201" "open"

OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --claim carol --no-reindex --json)
RC=$?
assert_eq "0" "$RC" "first claim exits 0"
# COVERS: concurrency.primitives
# COVERS: workflow.ttl-lease
assert_file_contains "claim:" "$STORE/IM/IM-201.md" "claim: block written"
assert_file_contains "  assignee: carol" "$STORE/IM/IM-201.md" "assignee written"
assert_file_contains "  claimed_at: " "$STORE/IM/IM-201.md" "claimed_at written"
assert_file_contains "lease_until: " "$STORE/IM/IM-201.md" "top-level lease_until written"
assert_file_contains "claim_generation: 1" "$STORE/IM/IM-201.md" \
    "first-ever claim sets claim_generation 1 (was absent = 0)"
assert_file_contains "status: in-progress" "$STORE/IM/IM-201.md" \
    "claim flips status open -> in-progress"
assert_file_contains "status: open -> in-progress -- claimed by carol" \
    "$STORE/IM/IM-201.md" "transition note names the claimant"

assert_eq "1" "$(json_field "$OUT" claim_generation)" \
    "--json claim output carries claim_generation (W0a F4)"
assert_eq "carol" "$(json_field "$OUT" claim.assignee)" \
    "--json claim output carries claim.assignee"
assert_eq "in-progress" "$(json_field "$OUT" status)" \
    "--json claim output carries status"

# TTL default: lease_until - claimed_at == 3600s
DELTA=$(python3 -c "
import json, sys, datetime
o = json.loads(sys.argv[1])
def p(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
print(int((p(o['claim']['lease_until']) - p(o['claim']['claimed_at'])).total_seconds()))
" "$OUT")
assert_eq "3600" "$DELTA" "binary default lease TTL is 3600s"

# Store auto-commit landed with the stable message
LASTMSG=$(git -C "$STORE" log -1 --format=%s)
assert_eq "claim IM-201" "$LASTMSG" "store commit message is 'claim <id>'"

# ---------------------------------------------------------------------------
echo
echo "[B] Idempotent same-actor re-claim (exit 0, no bump)"
# ---------------------------------------------------------------------------
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --claim carol --no-reindex --json)
RC=$?
assert_eq "0" "$RC" "same-actor re-claim exits 0"
assert_eq "1" "$(json_field "$OUT" claim_generation)" \
    "no generation bump on idempotent re-claim"
assert_eq "True" "$(json_field "$OUT" no_op)" "re-claim reported as no-op"

# ---------------------------------------------------------------------------
echo
echo "[C] Concurrent-claim loser exits 4 (lost-race)"
# ---------------------------------------------------------------------------
ERR_FILE="$TMPDIR_ROOT/lost-race.err"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --claim claude --no-reindex \
    2>"$ERR_FILE"
RC=$?
# COVERS: concurrency.exit-17
assert_eq "4" "$RC" "claim loser exits 4 (lost-race)"
assert_file_contains "error=lost-race" "$ERR_FILE" "stderr names error=lost-race"
assert_file_contains "contender=carol" "$ERR_FILE" "stderr names the contender"
assert_file_contains "lease_until=" "$ERR_FILE" "stderr carries lease_until"

# --json error shape is one parseable JSON object
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --claim claude --no-reindex \
    --json 2>"$ERR_FILE"
RC=$?
assert_eq "4" "$RC" "claim loser exits 4 under --json"
JSON_ERR=$(cat "$ERR_FILE")
assert_eq "4" "$(json_field "$JSON_ERR" exit)" "JSON stderr carries exit=4"
assert_eq "lost-race" "$(json_field "$JSON_ERR" class)" \
    "JSON stderr carries class=lost-race"
assert_eq "carol" "$(json_field "$JSON_ERR" contender)" \
    "JSON stderr carries contender"

# ---------------------------------------------------------------------------
echo
echo "[D] Stale --claim-generation exits 5 (stale-local-view)"
# ---------------------------------------------------------------------------
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --claim carol \
    --claim-generation 0 --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "5" "$RC" "stale generation on claim exits 5"
assert_file_contains "error=stale-local-view" "$ERR_FILE" \
    "stderr names error=stale-local-view"
assert_file_contains "current_generation=1" "$ERR_FILE" \
    "stderr carries current_generation"
assert_file_contains "caller_generation=0" "$ERR_FILE" \
    "stderr carries caller_generation"

# Plain transition with stale generation also exits 5
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 done \
    --claim-generation 0 --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "5" "$RC" "plain transition with stale generation exits 5"
assert_file_contains "status: in-progress" "$STORE/IM/IM-201.md" \
    "guarded transition did not write"

# Matching generation proceeds
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --renew \
    --claim-generation 1 --json >/dev/null 2>&1
assert_eq "0" "$?" "matching generation passes the guard (renew)"

# ---------------------------------------------------------------------------
echo
echo "[E] --renew extends lease_until only; --lease-ttl override"
# ---------------------------------------------------------------------------
BEFORE_UNTIL=$(grep '^lease_until:' "$STORE/IM/IM-201.md")
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --renew --lease-ttl 7200 --json)
RC=$?
assert_eq "0" "$RC" "renew exits 0"
assert_eq "1" "$(json_field "$OUT" claim_generation)" \
    "renew preserves claim_generation"
assert_eq "carol" "$(json_field "$OUT" claim.assignee)" \
    "renew preserves the assignee"
AFTER_UNTIL=$(grep '^lease_until:' "$STORE/IM/IM-201.md")
if [ "$BEFORE_UNTIL" != "$AFTER_UNTIL" ]; then
    pass "renew rewrote lease_until"
else
    fail "renew did not change lease_until"
fi
LASTMSG=$(git -C "$STORE" log -1 --format=%s)
assert_eq "renew IM-201" "$LASTMSG" "store commit message is 'renew <id>'"

# ---------------------------------------------------------------------------
echo
echo "[F] --release clears claim + lease_until, preserves generation"
# ---------------------------------------------------------------------------
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --release >/dev/null
RC=$?
assert_eq "0" "$RC" "release exits 0"
assert_no_claim_block "$STORE/IM/IM-201.md" "claim block cleared"
assert_file_not_contains "lease_until:" "$STORE/IM/IM-201.md" "lease_until cleared"
assert_file_contains "claim_generation: 1" "$STORE/IM/IM-201.md" \
    "release preserves claim_generation"
assert_file_contains "claim: released (was carol" "$STORE/IM/IM-201.md" \
    "release appends an audit note"
assert_file_contains "status: in-progress" "$STORE/IM/IM-201.md" \
    "release leaves status untouched"

# Idempotent release of an unclaimed issue
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --release >/dev/null
assert_eq "0" "$?" "release of unclaimed issue is an exit-0 no-op"

# Re-claim after release bumps to era 2
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-201 --claim claude --no-reindex --json)
assert_eq "2" "$(json_field "$OUT" claim_generation)" \
    "re-claim after release starts era 2"

# ---------------------------------------------------------------------------
echo
echo "[G] issue-reap-expired-claims"
# ---------------------------------------------------------------------------
# expired lease (claim with --lease-ttl 1, then backdate lease_until)
make_issue "$STORE/IM/IM-202.md" "IM-202" "open"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-202 --claim carol --no-reindex >/dev/null
python3 - "$STORE/IM/IM-202.md" <<'PYEOF'
import re, sys
path = sys.argv[1]
text = open(path).read()
text = re.sub(r"(?m)^lease_until: .*$",
              "lease_until: 2020-01-01T00:00:00Z", text)
open(path, "w").write(text)
PYEOF
git -C "$STORE" add -A
git -C "$STORE" commit -q -m "test: backdate IM-202 lease"

# valid lease (must NOT be reaped)
make_issue "$STORE/IM/IM-203.md" "IM-203" "open"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-203 --claim claude --no-reindex >/dev/null

# --dry-run reports but does not write
OUT=$(ISSUE_STORE_ROOT="$STORE" "$REAP" --dry-run --json)
RC=$?
assert_eq "0" "$RC" "reaper --dry-run exits 0"
assert_contains '"id": "IM-202"' "$(echo "$OUT" | python3 -m json.tool)" \
    "dry-run lists the expired claim"
assert_eq "True" "$(json_field "$OUT" dry_run)" "dry-run flagged in JSON"
assert_file_contains "claim:" "$STORE/IM/IM-202.md" \
    "dry-run did not clear the claim"

# Real reap
OUT=$(ISSUE_STORE_ROOT="$STORE" "$REAP" --json)
RC=$?
assert_eq "0" "$RC" "reaper exits 0"
assert_no_claim_block "$STORE/IM/IM-202.md" "expired claim cleared"
assert_file_not_contains "lease_until:" "$STORE/IM/IM-202.md" \
    "expired lease_until cleared"
assert_file_contains "claim_generation: 1" "$STORE/IM/IM-202.md" \
    "reaper preserves claim_generation (W0a F1)"
assert_file_contains "expired lease for carol reaped" "$STORE/IM/IM-202.md" \
    "reaper note appended to Implementation Notes"
assert_file_contains "  assignee: claude" "$STORE/IM/IM-203.md" \
    "valid lease untouched by the reaper"
LASTMSG=$(git -C "$STORE" log -1 --format=%s)
assert_eq "reap IM-202" "$LASTMSG" "store commit message is 'reap <id>'"

# Idempotent second run: nothing to reap
OUT=$(ISSUE_STORE_ROOT="$STORE" "$REAP")
assert_eq "0" "$?" "second reap run exits 0"
assert_contains "No expired claims." "$OUT" "second run reports nothing to reap"

# IM-277: --prefix case-folds through the one shared resolver, so the reaper
# is the seventh scoping surface converged with search / list / index /
# ready / history search / history activity. Before the fix a resolvable
# lowercase prefix died at an upper-case-only shape gate with exit 2.
OUT=$(ISSUE_STORE_ROOT="$STORE" "$REAP" --prefix im --dry-run 2>/dev/null)
RC=$?
# COVERS: cli.id-canonicalization
assert_eq "0" "$RC" "reaper --prefix im (lowercase) resolves and exits 0"

ERR=$(ISSUE_STORE_ROOT="$STORE" "$REAP" --prefix im --dry-run 2>&1 >/dev/null)
# COVERS: cli.id-canonicalization
assert_contains "resolved im -> IM" "$ERR" \
    "reaper announces the prefix canonicalization on stderr"

# Already-canonical input is silent (the sibling surfaces' contract).
ERR=$(ISSUE_STORE_ROOT="$STORE" "$REAP" --prefix IM --dry-run 2>&1 >/dev/null)
assert_eq "" "$ERR" "reaper --prefix IM emits no canonicalization note"

# The note is on stderr, so --json stdout stays exactly one document.
OUT=$(ISSUE_STORE_ROOT="$STORE" "$REAP" --prefix im --dry-run --json 2>/dev/null)
JCHK=$(printf '%s' "$OUT" | python3 -c \
    'import json,sys; json.load(sys.stdin); print("ok")' 2>/dev/null)
# COVERS: output.json-stable
assert_eq "ok" "$JCHK" \
    "reaper --prefix im --json stdout is one clean JSON document"

# A genuinely absent prefix is still an argument error naming the known set.
ERR=$(ISSUE_STORE_ROOT="$STORE" "$REAP" --prefix ZZZZ --dry-run 2>&1 >/dev/null)
RC=$?
# COVERS: output.exit-codes
assert_eq "2" "$RC" "reaper --prefix ZZZZ (absent) -> exit 2"
assert_contains "known=IM" "$ERR" \
    "reaper absent-prefix error names the known set"

# Reaper is privileged: after the reap, a paused actor presenting the old
# generation gets exit 5 from the CLI (the reaper itself never checked one).
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-202 --claim intruder \
    --no-reindex >/dev/null   # era 2
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-202 done \
    --claim-generation 1 --no-reindex 2>"$ERR_FILE"
assert_eq "5" "$?" "paused actor with pre-reap generation exits 5"

# ---------------------------------------------------------------------------
echo
echo "[H] not-claimable paths exit 7"
# ---------------------------------------------------------------------------
make_issue "$STORE/IM/IM-204.md" "IM-204" "open"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-204 blocked --reason wait \
    --no-reindex >/dev/null
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-204 --claim carol \
    --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "7" "$RC" "claim on blocked issue exits 7"
assert_file_contains "error=not-claimable" "$ERR_FILE" \
    "stderr names error=not-claimable"
assert_file_contains "status=blocked" "$ERR_FILE" "stderr carries status"

ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-999 --claim carol \
    --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "7" "$RC" "claim on missing issue exits 7"
assert_file_contains "reason=not-found" "$ERR_FILE" "stderr carries reason=not-found"

ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-204 --renew --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "7" "$RC" "renew without a claim exits 7"
assert_file_contains "reason=no-claim" "$ERR_FILE" "stderr carries reason=no-claim"

# ---------------------------------------------------------------------------
echo
echo "[I] edit-issue --claim-generation guard"
# ---------------------------------------------------------------------------
make_issue "$STORE/IM/IM-205.md" "IM-205" "open"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-205 --claim carol --no-reindex >/dev/null

ISSUE_STORE_ROOT="$STORE" "$EDIT" IM-205 --set priority=low \
    --claim-generation 1 --no-reindex >/dev/null
assert_eq "0" "$?" "edit with matching generation exits 0"
assert_file_contains "priority: low" "$STORE/IM/IM-205.md" "guarded edit landed"

ISSUE_STORE_ROOT="$STORE" "$EDIT" IM-205 --set priority=high \
    --claim-generation 0 --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "5" "$RC" "edit with stale generation exits 5"
assert_file_contains "error=stale-local-view" "$ERR_FILE" \
    "edit-issue stderr names error=stale-local-view"
assert_file_contains "priority: low" "$STORE/IM/IM-205.md" \
    "stale-guard edit did not write"

# Generation matches but lease released -> exit 7 (lease-expired class)
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-205 --release >/dev/null
ISSUE_STORE_ROOT="$STORE" "$EDIT" IM-205 --set priority=high \
    --claim-generation 1 --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "7" "$RC" "guarded edit after release exits 7"
assert_file_contains "reason=lease-expired" "$ERR_FILE" \
    "stderr carries reason=lease-expired"

# ---------------------------------------------------------------------------
echo
echo "[J] exit-3 renumber: lock contention is concurrent-edit at exit 3"
# ---------------------------------------------------------------------------
make_issue "$STORE/IM/IM-206.md" "IM-206" "open"
LOCK="$STORE/IM/.locks/IM-206.lock"
mkdir -p "$STORE/IM/.locks"

python3 "$CONCURRENCY_PY" flock-run "$LOCK" -- sleep 3 &
HOLDER=$!
sleep 0.5

ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-206 --claim carol \
    --lock-timeout 0.5 --no-reindex 2>"$ERR_FILE"
RC=$?
# COVERS: concurrency.exit-17
assert_eq "3" "$RC" "lock contention on claim exits 3 (was 17)"
assert_file_contains "error=concurrent-edit" "$ERR_FILE" \
    "stderr names error=concurrent-edit"

wait "$HOLDER" 2>/dev/null

# ---------------------------------------------------------------------------
echo
echo "[K] usage errors exit 2"
# ---------------------------------------------------------------------------
"$TRANSITION" IM-201 done --claim carol 2>/dev/null
assert_eq "2" "$?" "positional state + --claim exits 2"
"$TRANSITION" IM-201 --claim carol --renew 2>/dev/null
assert_eq "2" "$?" "--claim + --renew exits 2"
"$TRANSITION" IM-201 --claim 'bad claimant!' 2>/dev/null
assert_eq "2" "$?" "invalid claimant exits 2"
"$TRANSITION" IM-201 --claim carol --lease-ttl 0 2>/dev/null
assert_eq "2" "$?" "--lease-ttl 0 exits 2"
"$TRANSITION" IM-201 in-progress --lease-ttl 60 2>/dev/null
assert_eq "2" "$?" "--lease-ttl on a plain transition exits 2"
# IM-277: `--prefix im` now case-folds to IM (section [G]); only a shape
# invalid even upper-cased -- here 9 characters -- stays exit 2.
"$REAP" --prefix lowercase 2>/dev/null
assert_eq "2" "$?" "reaper invalid --prefix exits 2"
# Additional transition-primitive argument-shape errors (each fails before
# any store access):
"$TRANSITION" 2>/dev/null
assert_eq "2" "$?" "transition with no issue id exits 2"
"$TRANSITION" IM-201 2>/dev/null
assert_eq "2" "$?" "transition with an id but no target state exits 2"
"$TRANSITION" IM-201 done --claim-generation -1 2>/dev/null
assert_eq "2" "$?" "transition with a negative --claim-generation exits 2"
"$TRANSITION" IM-201 --claim carol --branch foo 2>/dev/null
assert_eq "2" "$?" "--claim with --branch exits 2 (lease op takes no branch)"
"$TRANSITION" IM-201 --release --lease-ttl 60 2>/dev/null
assert_eq "2" "$?" "--release with --lease-ttl exits 2"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" notanid --claim carol 2>/dev/null
assert_eq "2" "$?" "lease op on a malformed id exits 2"

# ---------------------------------------------------------------------------
echo
echo "[K2] human-readable (non-JSON) lease output"
# ---------------------------------------------------------------------------
# Every lease check above used --json; exercise the plain-text formatter
# branches (claim no-op, renew, release) so their human strings are covered.
make_issue "$STORE/IM/IM-208.md" "IM-208" "open"
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-208 --claim carol --no-reindex)
assert_contains "Claim acquired on IM-208 for carol" "$OUT" \
    "claim prints a human-readable acquisition line"
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-208 --claim carol --no-reindex)
assert_contains "Claim already held on IM-208 by carol" "$OUT" \
    "same-actor re-claim prints the human no-op line"
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-208 --renew --no-reindex)
assert_contains "Lease renewed on IM-208 for carol" "$OUT" \
    "renew prints a human-readable renewal line"
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-208 --release --no-reindex)
assert_contains "Claim released on IM-208" "$OUT" \
    "release prints a human-readable release line"

# ---------------------------------------------------------------------------
echo
echo "[L] --help / --version"
# ---------------------------------------------------------------------------
"$TRANSITION" --help | grep -q -- "--claim CLAIMANT" \
    && pass "transition-issue --help documents --claim" \
    || fail "transition-issue --help missing --claim"
"$EDIT" --help | grep -q -- "--claim-generation" \
    && pass "edit-issue --help documents --claim-generation" \
    || fail "edit-issue --help missing --claim-generation"
"$REAP" --help >/dev/null && pass "reaper --help exits 0" \
    || fail "reaper --help failed"
VER=$("$REAP" --version)
[ -n "$VER" ] && pass "reaper --version prints ($VER)" \
    || fail "reaper --version empty"

# ---------------------------------------------------------------------------
echo
echo "[M] --json stdout purity with auto-reindex enabled"
# ---------------------------------------------------------------------------
# Every earlier --json check passed --no-reindex; the bug was the
# auto-reindex notice landing on stdout ahead of the JSON object, which
# breaks the skill snippets' whole-stdout json.loads. Exercise
# the default (reindexing) path and require machine-pure stdout.
make_issue "$STORE/IM/IM-209.md" "IM-209" "open"

M_ERR="$TMPDIR_ROOT/m-stderr.txt"
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-209 --claim carol --json 2>"$M_ERR")
RC=$?
assert_eq "0" "$RC" "claim with auto-reindex exits 0"
# COVERS: output.json-stable
echo "$OUT" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null \
    && pass "claim --json stdout is exactly one parseable JSON document" \
    || fail "claim --json stdout is not pure JSON: $OUT"
case "$OUT" in
    *Reindexed*) fail "reindex notice leaked onto --json stdout" ;;
    *) pass "reindex notice absent from --json stdout" ;;
esac
grep -q "Reindexed" "$M_ERR" \
    && pass "reindex notice routed to stderr under --json" \
    || fail "reindex notice missing from stderr under --json ($(cat "$M_ERR"))"

OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-209 done --claim-generation 1 --json 2>"$M_ERR")
# COVERS: output.json-stable
echo "$OUT" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null \
    && pass "transition done --json stdout is pure with auto-reindex" \
    || fail "transition done --json stdout is not pure JSON: $OUT"

OUT=$(ISSUE_STORE_ROOT="$STORE" "$EDIT" IM-209 --set priority=high --json 2>"$M_ERR")
# COVERS: output.json-stable
echo "$OUT" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null \
    && pass "edit-issue --json stdout is pure with auto-reindex" \
    || fail "edit-issue --json stdout is not pure JSON: $OUT"

# ---------------------------------------------------------------------------
echo
echo "[N] claim on an already in-progress (unclaimed) issue records the claim"
# ---------------------------------------------------------------------------
# A plain transition to in-progress leaves the issue unclaimed; a
# subsequent --claim must acquire the lease WITHOUT synthesizing a bogus
# open -> in-progress transition note (the status is already in-progress),
# recording a plain "claim: acquired" audit line instead.
make_issue "$STORE/IM/IM-210.md" "IM-210" "open"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-210 in-progress --no-reindex >/dev/null
OUT=$(ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-210 --claim carol --no-reindex --json)
RC=$?
# COVERS: workflow.ttl-lease
assert_eq "0" "$RC" "claim on an in-progress issue exits 0"
assert_file_contains "  assignee: carol" "$STORE/IM/IM-210.md" \
    "claim on in-progress records the assignee"
assert_file_contains "status: in-progress" "$STORE/IM/IM-210.md" \
    "claim on in-progress leaves status in-progress"
# COVERS: workflow.ttl-lease
assert_file_contains "claim: acquired by carol" "$STORE/IM/IM-210.md" \
    "claim on in-progress writes a plain claim-acquired note (no synthetic transition)"
assert_eq "1" "$(json_field "$OUT" claim_generation)" \
    "claim on in-progress starts generation 1"

# ---------------------------------------------------------------------------
echo
echo "[O] --renew on an expired lease is refused (exit 7, lease-expired)"
# ---------------------------------------------------------------------------
make_issue "$STORE/IM/IM-211.md" "IM-211" "open"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-211 --claim carol --no-reindex >/dev/null
# Backdate the lease so it is expired, then attempt a renew.
python3 - "$STORE/IM/IM-211.md" <<'PYEOF'
import re, sys
path = sys.argv[1]
text = open(path).read()
text = re.sub(r"(?m)^lease_until: .*$",
              "lease_until: 2020-01-01T00:00:00Z", text)
open(path, "w").write(text)
PYEOF
git -C "$STORE" add -A
git -C "$STORE" commit -q -m "test: backdate IM-211 lease"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-211 --renew --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "7" "$RC" "renew on an expired lease exits 7"
assert_file_contains "reason=lease-expired" "$ERR_FILE" \
    "renew-expired stderr carries reason=lease-expired"

# ---------------------------------------------------------------------------
echo
echo "[P] transition guarded by a matching generation but a released lease -> exit 7"
# ---------------------------------------------------------------------------
# The transition primitive's caller-side fencing guard (require_active):
# the generation matches the caller's read, but the lease was released, so
# a paused actor cannot write through -- exit 7 (lease-expired), no write.
make_issue "$STORE/IM/IM-212.md" "IM-212" "open"
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-212 --claim carol --no-reindex >/dev/null
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-212 --release >/dev/null
ISSUE_STORE_ROOT="$STORE" "$TRANSITION" IM-212 done \
    --claim-generation 1 --no-reindex 2>"$ERR_FILE"
RC=$?
assert_eq "7" "$RC" "matching-generation transition after release exits 7"
assert_file_contains "reason=lease-expired" "$ERR_FILE" \
    "transition guard stderr carries reason=lease-expired"
assert_file_contains "status: in-progress" "$STORE/IM/IM-212.md" \
    "guarded transition after release did not write"

# ---------------------------------------------------------------------------
echo
echo "=============================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=============================="
[ "$FAIL" -eq 0 ]
