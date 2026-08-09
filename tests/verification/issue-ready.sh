#!/usr/bin/env bash
# Verification script for issue-ready -- the frontmatter-native readiness
# filter (the bd read path was retired in v2.11.0).
#
# Exercises the real issue-ready CLI against a temp store fixture built
# entirely from markdown frontmatter (no bd, no .beads/):
#   - open issue with no deps -> ready
#   - blocks dep done / cancelled -> ready
#   - blocks dep open / in-progress -> filtered out with reason
#   - same-prefix blocks dep open -> filtered out (uniform with
#     cross-prefix; bd hid these)
#   - non-blocks dep (related) -> ignored
#   - blocks dep with no store file -> filtered (status=unknown)
#   - --actor filtering against store claim frontmatter
#   - --repo prefix restriction (blocker status still resolved store-wide)
#   - deterministic ready ordering (priority, then created, then id)
#   - --text rendering
#   - regression: >100 store-wide ready rows never truncate the
#     ready set; --limit is post-filter, never silent (stderr notice),
#     0 = unlimited, negative = usage error
#   - no bd binary on PATH and no .beads/ present
#   - exit 1 + error=no-ready-work on an empty ready set
#     (key=value default, one-line JSON stderr under --json)
#   - --help / --version / usage errors
#
# Runs WITHOUT bd -- no skip. Hermetic unit coverage lives in
# tests/test_issue_ready.py.
#
# Run from the project root:
#     tests/verification/issue-ready.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE_READY="$(resolve_cli issue-ready)"

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

assert_not_contains() {
    if echo "$2" | grep -qF -- "$1"; then
        fail "$3: unexpected '$1' found in: $2"
    else
        pass "$3 (no '$1')"
    fi
}

WORKDIR="$(mktemp -d -t issue-ready.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

# Hard-isolate from the caller's real store.
unset ISSUE_STORE_ROOT

STORE="$WORKDIR/store"

# write_issue <id> <status> <title> [priority] [created] [deps_block] [claim_block]
write_issue() {
    local id="$1" status="$2" title="$3"
    local priority="${4:-medium}" created="${5:-2026-07-01}"
    local deps="${6:-}" claim="${7:-}"
    local prefix="${id%%-*}"
    mkdir -p "$STORE/$prefix"
    {
        echo "---"
        echo "id: $id"
        echo "title: \"$title\""
        echo "type: chore"
        echo "status: $status"
        echo "priority: $priority"
        echo "created: $created"
        echo "updated: 2026-07-01"
        [ -n "$deps" ] && printf '%s\n' "$deps"
        [ -n "$claim" ] && printf '%s\n' "$claim"
        echo "---"
        echo ""
        echo "## Summary"
        echo ""
        echo "Fixture body for $id."
    } > "$STORE/$prefix/$id.md"
}

blocks_dep() { printf 'depends_on:\n  - to: %s\n    type: blocks\n' "$1"; }

CLAIM_ALICE="claim:
  assignee: alice
  claimed_at: 2026-07-08T00:00:00Z
lease_until: 2099-01-01T00:00:00Z
claim_generation: 1"

# --- Fixture ---------------------------------------------------------
# Cross-prefix blocker targets.
write_issue DOUG-301 done      "Closed elsewhere"
write_issue DOUG-302 open      "Still open elsewhere"
write_issue DOUG-303 cancelled "Cancelled elsewhere"

# IM candidates.
write_issue IM-301 open "No deps"
write_issue IM-302 open "Cross dep done"      medium 2026-07-01 "$(blocks_dep DOUG-301)"
write_issue IM-303 open "Cross dep open"      medium 2026-07-01 "$(blocks_dep DOUG-302)"
write_issue IM-304 open "Mixed deps"          medium 2026-07-01 "$(printf 'depends_on:\n  - to: DOUG-301\n    type: blocks\n  - to: DOUG-302\n    type: blocks\n')"
write_issue IM-305 open "Same-prefix blocked" medium 2026-07-01 "$(blocks_dep IM-301)"
write_issue IM-306 open "Related cross dep"   medium 2026-07-01 "$(printf 'depends_on:\n  - to: DOUG-302\n    type: related\n')"
write_issue IM-307 open "Claimed by alice"    medium 2026-07-01 "" "$CLAIM_ALICE"
write_issue IM-308 open "Cross dep cancelled" medium 2026-07-01 "$(blocks_dep DOUG-303)"

run_ready() {
    ISSUE_STORE_ROOT="$STORE" "$ISSUE_READY" "$@"
}

ready_ids() {
    python3 -c "import json,sys; d=json.load(sys.stdin); print(' '.join(sorted(r['id'] for r in d['ready'])))"
}

filtered_ids() {
    python3 -c "import json,sys; d=json.load(sys.stdin); print(' '.join(sorted(f['id'] for f in d['filtered_out'])))"
}

echo "== default JSON output, --repo IM =="
OUT=$(run_ready --repo IM)
RC=$?
assert_eq 0 "$RC" "exit 0 with ready issues"
# COVERS: workflow.readiness-filter
assert_eq "IM-301 IM-302 IM-306 IM-307 IM-308" "$(echo "$OUT" | ready_ids)" \
    "ready set: passthrough + closed-dep + related-dep + cancelled-dep"
# COVERS: workflow.readiness-filter
assert_eq "IM-303 IM-304 IM-305" "$(echo "$OUT" | filtered_ids)" \
    "filtered set: open-cross-dep + mixed + same-prefix-blocked"
assert_contains '"reason": "blocked by DOUG-302 (open)"' "$OUT" \
    "filtered reason names the open blocker"
assert_contains '"reason": "blocked by IM-301 (open)"' "$OUT" \
    "same-prefix blocked candidate now filtered with reason"

echo "== ready entry shape =="
PREFIX_OF_301=$(echo "$OUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print([r['prefix'] for r in d['ready'] if r['id']=='IM-301'][0])")
assert_eq "IM" "$PREFIX_OF_301" "ready entries carry the prefix field"

echo "== no --repo: other-prefix candidates included, closed excluded =="
OUT=$(run_ready)
assert_contains "DOUG-302" "$(echo "$OUT" | ready_ids)" \
    "open DOUG record is a candidate across prefixes"
assert_not_contains "DOUG-301" "$(echo "$OUT" | ready_ids)" \
    "done DOUG record not in ready set"
assert_not_contains "DOUG-303" "$(echo "$OUT" | ready_ids)" \
    "cancelled DOUG record not in ready set"

echo "== deterministic ordering (priority, then created, then id) =="
ORD_STORE="$WORKDIR/ord"; STORE_SAVE="$STORE"; STORE="$ORD_STORE"
write_issue IM-050 open "low"    low    2026-01-01
write_issue IM-010 open "hi-may" high   2026-05-01
write_issue IM-020 open "hi-mar" high   2026-03-01
write_issue IM-030 open "med"    medium 2026-02-01
ORDER=$(ISSUE_STORE_ROOT="$ORD_STORE" "$ISSUE_READY" --repo IM | \
    python3 -c "import json,sys; print(' '.join(r['id'] for r in json.load(sys.stdin)['ready']))")
assert_eq "IM-020 IM-010 IM-030 IM-050" "$ORDER" \
    "ready ordered high(by created) > medium > low"
STORE="$STORE_SAVE"

echo "== --actor filtering =="
OUT=$(run_ready --repo IM --actor bob)
assert_eq "IM-301 IM-302 IM-306 IM-308" "$(echo "$OUT" | ready_ids)" \
    "actor bob: claimed-by-alice candidate dropped"
assert_contains '"reason": "claim held by alice, not bob"' "$OUT" \
    "actor filter reason names the claim holder"
OUT=$(run_ready --repo IM --actor alice)
assert_contains "IM-307" "$(echo "$OUT" | ready_ids)" \
    "actor alice keeps her own claim"

echo "== --text rendering =="
OUT=$(run_ready --repo IM --text)
assert_contains "Ready:" "$OUT" "text mode has Ready: header"
assert_contains "IM-301" "$OUT" "text mode lists ready id"
assert_contains "No deps" "$OUT" "text mode lists ready title"
assert_contains "Filtered out (not ready):" "$OUT" \
    "text mode has filtered header"
assert_contains "IM-303  blocked by DOUG-302 (open)" "$OUT" \
    "text mode filtered line"

echo "== no bd binary on PATH and no .beads/ present =="
if [ -d "$STORE/.beads" ]; then
    fail "unexpected .beads/ in fixture store"
else
    pass "fixture store has no .beads/ index"
fi
# COVERS: workflow.readiness-filter
# Run with a scrubbed PATH that contains no bd binary. Invoke the CLI
# directly (its shebang, or the consolidated shim) rather than via an
# explicit python3 -- under ISSUE_MGMT_CLI_STYLE=consolidated $ISSUE_READY
# resolves to a bash shim that execs `issue ready`, which python3 cannot run.
OUT=$(PATH="/usr/bin:/bin" ISSUE_STORE_ROOT="$STORE" "$ISSUE_READY" --repo IM 2>/dev/null | ready_ids)
assert_contains "IM-301" "$OUT" "issue-ready works with bd absent from PATH"

echo "== IM-175: >100 store-wide ready rows never truncate the ready set =="
for i in $(seq 401 510); do
    write_issue "PAD-$i" open "Flood $i"
done
write_issue IM-399 open "Low-priority sentinel" low
TOTAL_READY=$(run_ready | python3 -c "import json,sys; print(len(json.load(sys.stdin)['ready']))")
if [ "$TOTAL_READY" -gt 100 ]; then
    pass "no-repo ready set exceeds 100 rows ($TOTAL_READY rows)"
else
    fail "no-repo ready set unexpectedly small ($TOTAL_READY rows; fixture broken?)"
fi
OUT=$(run_ready --repo IM 2>"$WORKDIR/stderr.txt")
# COVERS: workflow.readiness-filter
assert_contains "IM-399" "$(echo "$OUT" | ready_ids)" \
    "low-priority sentinel visible under --repo IM despite >100 store-wide ready"
assert_eq "" "$(cat "$WORKDIR/stderr.txt")" \
    "no truncation notice when no --limit is passed"

echo "== IM-175: --limit is post-filter and never silent =="
OUT=$(run_ready --repo IM --limit 2 2>"$WORKDIR/stderr.txt")
assert_eq 0 "$?" "--limit exits 0 when capped set is non-empty"
LIMITED=$(echo "$OUT" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['ready']))")
assert_eq 2 "$LIMITED" "--limit 2 caps the ready list at 2"
assert_contains "more ready issue(s) not shown" "$(cat "$WORKDIR/stderr.txt")" \
    "suppressed rows announced on stderr"
OUT=$(run_ready --repo IM --limit 0 2>"$WORKDIR/stderr.txt")
assert_contains "IM-399" "$(echo "$OUT" | ready_ids)" \
    "--limit 0 is unlimited"
assert_eq "" "$(cat "$WORKDIR/stderr.txt")" \
    "no notice under --limit 0"
run_ready --limit -3 >/dev/null 2>&1
assert_eq 2 "$?" "negative --limit is a usage error"

echo "== empty ready set: exit 1 + error=no-ready-work =="
OUT=$(run_ready --repo ZZ 2>"$WORKDIR/stderr.txt")
RC=$?
assert_eq 1 "$RC" "exit 1 when ready set is empty"
assert_contains "error=no-ready-work" "$(cat "$WORKDIR/stderr.txt")" \
    "key=value stderr by default"
assert_eq "" "$(echo "$OUT" | ready_ids)" "stdout shape still emitted (empty ready)"

OUT=$(run_ready --repo ZZ --json 2>"$WORKDIR/stderr.txt")
RC=$?
assert_eq 1 "$RC" "exit 1 under explicit --json"
assert_contains '"class":"no-ready-work"' "$(cat "$WORKDIR/stderr.txt")" \
    "one-line JSON stderr with class field under --json"
assert_contains '"exit":1' "$(cat "$WORKDIR/stderr.txt")" \
    "JSON stderr carries exit field"

echo "== usage and preconditions =="
run_ready --json --text >/dev/null 2>&1
assert_eq 2 "$?" "--json with --text is a usage error"
run_ready --repo lowercase >/dev/null 2>&1
assert_eq 2 "$?" "malformed --repo prefix is a usage error"
ISSUE_STORE_ROOT="$WORKDIR/does-not-exist" "$ISSUE_READY" >/dev/null 2>"$WORKDIR/stderr.txt"
assert_eq 1 "$?" "unresolvable store is a precondition failure"
assert_contains "reason=store-unresolvable" "$(cat "$WORKDIR/stderr.txt")" \
    "precondition stderr names the unresolvable store"

echo "== --help / --version =="
OUT=$(run_ready --help)
assert_eq 0 "$?" "--help exits 0"
assert_contains "Examples:" "$OUT" "--help includes example invocation"
OUT=$(run_ready --version)
assert_eq 0 "$?" "--version exits 0"
assert_contains "v" "$OUT" "--version prints a version string"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
