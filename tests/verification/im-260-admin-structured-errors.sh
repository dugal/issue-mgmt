#!/usr/bin/env bash
# im-260-admin-structured-errors.sh -- C3: the nine `issue admin` subverbs
# carry the branchable payload on every failure path (IM-260).
#
# Each check drives one converted failure path and asserts the exit code
# is unchanged and the stderr line carries the `error=` / `reason=` keys
# (or, under --json, the one-line JSON payload). Store safety: every
# invocation pins ISSUE_STORE_ROOT (and HOME) to throwaway paths, so no
# induction can reach the operator's store, config, or installed skills.
#
# Run from the project root:
#   tests/verification/im-260-admin-structured-errors.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"

ISSUE="$ISSUE_MGMT_BIN/issue"
if [ ! -x "$ISSUE" ]; then
    echo "im-260-admin-structured-errors.sh: no dispatcher at $ISSUE" >&2
    exit 1
fi

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then pass "$3 ($2)"; else fail "$3: expected '$1', got '$2'"; fi
}
assert_contains() {
    if echo "$2" | grep -qF -- "$1"; then pass "$3 (contains '$1')"; else
        fail "$3: '$1' not found in:"; echo "$2" | sed 's/^/  | /'; fi
}

WORKDIR="$(mktemp -d -t im-260-c3.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT
mkdir -p "$WORKDIR/home" "$WORKDIR/nonempty" && touch "$WORKDIR/nonempty/junk"
ABSENT="$WORKDIR/no-such-store"

run() {  # run <args...> with pinned env; stderr -> $ERR, rc -> $RC
    ERR=$(env HOME="$WORKDIR/home" ISSUE_STORE_ROOT="$ABSENT" \
          "$ISSUE" "$@" </dev/null 2>&1 >/dev/null)
    RC=$?
}

# --- admin validate ---------------------------------------------------------
# COVERS: surface.structured-errors
run admin validate
assert_eq 2 "$RC" "validate: no files exits 2"
assert_contains "reason=missing-operand" "$ERR" "validate: missing operand payload"
run admin validate "$WORKDIR/nope.md"
assert_eq 2 "$RC" "validate: missing file exits 2"
assert_contains "reason=file-not-found" "$ERR" "validate: file-not-found payload"

# --- admin reindex ----------------------------------------------------------
# COVERS: surface.structured-errors
run admin reindex "$WORKDIR/x" --store "$WORKDIR/x"
assert_eq 2 "$RC" "reindex: store+positional exits 2"
assert_contains "reason=conflicting-flags" "$ERR" "reindex: conflict payload"
run admin reindex "$WORKDIR/no-such-dir"
assert_eq 1 "$RC" "reindex: missing legacy dir exits 1"
assert_contains "error=not-found" "$ERR" "reindex: not-found payload"

# --- admin snapshot ---------------------------------------------------------
# COVERS: surface.structured-errors
run admin snapshot
assert_eq 1 "$RC" "snapshot: unresolvable store exits 1"
assert_contains "reason=store-unresolvable" "$ERR" "snapshot: store payload"
run admin snapshot --json
assert_eq 1 "$RC" "snapshot: unresolvable store under --json exits 1"
assert_contains '"reason":"store-unresolvable"' "$ERR" \
    "snapshot: one-line JSON payload under --json"

# --- admin reap -------------------------------------------------------------
# COVERS: surface.structured-errors
run admin reap
assert_eq 1 "$RC" "reap: unresolvable store exits 1"
assert_contains "reason=store-unresolvable" "$ERR" "reap: store payload"

# --- admin install-skills ---------------------------------------------------
# COVERS: surface.structured-errors
ERR=$(env HOME="$WORKDIR/home" ISSUE_STORE_ROOT="$ABSENT" PATH=/usr/bin:/bin \
      "$ISSUE" admin install-skills </dev/null 2>&1 >/dev/null)
RC=$?
assert_eq 1 "$RC" "install-skills: no brew exits 1"
assert_contains "reason=missing-dependency" "$ERR" \
    "install-skills: missing-dependency payload"
assert_contains "dependency=brew" "$ERR" "install-skills: names the dependency"

# --- admin init-store -------------------------------------------------------
# COVERS: surface.structured-errors
run admin init-store --store "$WORKDIR/nonempty"
assert_eq 1 "$RC" "init-store: non-empty non-git target exits 1"
assert_contains "reason=non-empty-target" "$ERR" "init-store: refusal payload"
run admin init-store
assert_eq 2 "$RC" "init-store: missing --store exits 2"
assert_contains "reason=bad-arguments" "$ERR" \
    "init-store: argparse refusal exits through the payload"

# --- admin init-project -----------------------------------------------------
# COVERS: surface.structured-errors
PROJ="$WORKDIR/proj"; mkdir -p "$PROJ"
( cd "$PROJ" && git init -q . )
ERR=$(cd "$PROJ" && env HOME="$WORKDIR/home" ISSUE_STORE_ROOT="$ABSENT" \
      "$ISSUE" admin init-project --prefix ZZQ --non-interactive \
      </dev/null 2>&1 >/dev/null)
RC=$?
assert_eq 1 "$RC" "init-project: absent store exits 1"
assert_contains "reason=store-unresolvable" "$ERR" "init-project: store payload"
mkdir -p "$WORKDIR/store2" && ( cd "$WORKDIR/store2" && git init -q . )
ERR=$(cd "$PROJ" && env HOME="$WORKDIR/home" ISSUE_STORE_ROOT="$WORKDIR/store2" \
      "$ISSUE" admin init-project --prefix bad-prefix --non-interactive \
      </dev/null 2>&1 >/dev/null)
RC=$?
assert_eq 1 "$RC" "init-project: invalid prefix exits 1"
assert_contains "reason=invalid-prefix" "$ERR" "init-project: prefix payload"

# --- admin migrate ----------------------------------------------------------
# COVERS: surface.structured-errors
run admin migrate --project "$WORKDIR/no-such-proj"
assert_eq 1 "$RC" "migrate: bad project path exits 1"
assert_contains "reason=not-a-directory" "$ERR" "migrate: path payload"
run admin migrate --project "$WORKDIR" --bogus-flag
assert_eq 2 "$RC" "migrate: unknown flag exits 2"
assert_contains "reason=bad-arguments" "$ERR" "migrate: argparse payload"

# --- admin selftest ---------------------------------------------------------
# selftest's pass path is covered by im-165-selftest.sh; its failure path
# needs an induced in-smoke failure, which no flag reaches -- the payload
# emission is pinned by code review and the unit suite. Assert here only
# that the argparse refusal is structured, like every other subverb.
# COVERS: surface.structured-errors
run admin selftest --bogus-flag
assert_eq 2 "$RC" "selftest: unknown flag exits 2"
assert_contains "reason=bad-arguments" "$ERR" "selftest: argparse payload"

echo
echo "----- im-260 admin structured-errors summary -----"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
