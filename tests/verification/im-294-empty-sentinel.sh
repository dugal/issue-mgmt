#!/usr/bin/env bash
# im-294-empty-sentinel.sh -- pre-merge verification for IM-294.
#
# An empty (or whitespace-only) .issues/.migrated-to-store sentinel used to
# make the shared resolver return Path('') == Path('.'), silently rerouting
# the store root to whatever directory the verb ran from. A present-but-empty
# sentinel is corrupt routing state: every reader now fails loud with the
# branchable `reason=corrupt-sentinel` payload naming the sentinel path --
# the shared 4-step chain (through the dispatcher backstop), `admin
# snapshot`'s walk-up, and `admin init-project`'s local read -- and
# `admin init-project --store ROOT` repairs the empty file instead of
# refusing it as divergent.
#
# Run from the project root:
#   tests/verification/im-294-empty-sentinel.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
ISSUE_SHOW="$(resolve_cli issue-show)"
SEARCH="$(resolve_cli search-issues)"
SNAPSHOT="$(resolve_cli issue-snapshot)"
INIT_STORE="$(resolve_cli init-issue-store)"
INIT_PROJECT="$(resolve_cli init-issue-project)"

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t im-294.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

export HOME="$WORKDIR/home"
mkdir -p "$HOME"
unset ISSUE_STORE_ROOT
unset ISSUE_MGMT_NO_AUTOCOMMIT

export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="t@t"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="t@t"

PROJ="$WORKDIR/proj"
mkdir -p "$PROJ/.issues"
git init -q -b main "$PROJ" 1>&2
git -C "$PROJ" commit -q --allow-empty -m "project init" 1>&2
: > "$PROJ/.issues/.migrated-to-store"
SENTINEL="$PROJ/.issues/.migrated-to-store"
# The resolver reports the symlink-resolved path (macOS /var -> /private/var),
# so assertions compare against the physical form.
SENTINEL_PHYS="$(cd "$PROJ" && pwd -P)/.issues/.migrated-to-store"

cd "$PROJ"

echo "== [A] shared chain: a read verb refuses the empty sentinel =="

OUT=$("$ISSUE_SHOW" XX-001 2>"$WORKDIR/a-err.txt"); rc=$?
# COVERS: substrate.store-root-resolution
[ "$rc" -eq 1 ] \
    && pass "show exits 1 on an empty sentinel" \
    || fail "show exited $rc (want 1); stderr: $(cat "$WORKDIR/a-err.txt")"
grep -q 'reason=corrupt-sentinel' "$WORKDIR/a-err.txt" \
    && pass "stderr carries reason=corrupt-sentinel" \
    || fail "stderr lacks reason=corrupt-sentinel: $(cat "$WORKDIR/a-err.txt")"
grep -q "sentinel=$SENTINEL_PHYS" "$WORKDIR/a-err.txt" \
    && pass "stderr names the sentinel path" \
    || fail "stderr does not name the sentinel path"
grep -qi 'traceback' "$WORKDIR/a-err.txt" \
    && fail "stderr contains a traceback" \
    || pass "no traceback on stderr"
[ -z "$OUT" ] \
    && pass "stdout is empty on the refusal" \
    || fail "stdout not empty: $OUT"

echo "== [B] --json emits the one-line JSON shape on stderr =="

OUT=$("$SEARCH" anything --json 2>"$WORKDIR/b-err.txt"); rc=$?
[ "$rc" -eq 1 ] \
    && pass "search --json exits 1 on an empty sentinel" \
    || fail "search --json exited $rc (want 1)"
python3 - "$WORKDIR/b-err.txt" <<'PY' \
    && pass "stderr parses as one JSON object with reason=corrupt-sentinel" \
    || fail "stderr is not the documented JSON error object"
import json, sys
with open(sys.argv[1]) as fh:
    obj = json.loads(fh.read().strip())
assert obj["error"] == "precondition-failed", obj
assert obj["reason"] == "corrupt-sentinel", obj
assert obj["exit"] == 1, obj
assert obj["sentinel"].endswith(".migrated-to-store"), obj
PY

echo "== [C] env and flag legs still short-circuit a corrupt sentinel =="

STORE="$WORKDIR/store"
"$INIT_STORE" --store "$STORE" --non-interactive 1>&2 \
    || { echo "FAIL: init-store failed"; exit 1; }
OUT=$(ISSUE_STORE_ROOT="$STORE" "$ISSUE_SHOW" XX-001 2>&1); rc=$?
# The env leg outranks the sentinel, so the corrupt sentinel is never read:
# the failure is the ordinary not-found, not corrupt-sentinel.
case "$OUT" in
    *corrupt-sentinel*) fail "env leg did not short-circuit the sentinel" ;;
    *) pass "env leg short-circuits the corrupt sentinel (exit $rc, not corrupt-sentinel)" ;;
esac

echo "== [D] admin snapshot's walk-up refuses the empty sentinel =="

OUT=$("$SNAPSHOT" 2>"$WORKDIR/d-err.txt"); rc=$?
[ "$rc" -eq 1 ] && grep -q 'reason=corrupt-sentinel' "$WORKDIR/d-err.txt" \
    && pass "snapshot exits 1 with reason=corrupt-sentinel" \
    || fail "snapshot: exit $rc, stderr: $(cat "$WORKDIR/d-err.txt")"

echo "== [E] admin init-project refuses without --store, repairs with it =="

OUT=$("$INIT_PROJECT" --prefix VT --non-interactive 2>"$WORKDIR/e-err.txt"); rc=$?
[ "$rc" -eq 1 ] && grep -q 'reason=corrupt-sentinel' "$WORKDIR/e-err.txt" \
    && pass "init-project without --store exits 1 with reason=corrupt-sentinel" \
    || fail "init-project without --store: exit $rc, stderr: $(cat "$WORKDIR/e-err.txt")"

"$INIT_PROJECT" --store "$STORE" --prefix VT --non-interactive 1>&2; rc=$?
PAYLOAD=$(cat "$SENTINEL")
[ "$rc" -eq 0 ] && [ -n "$PAYLOAD" ] \
    && pass "init-project --store repairs the empty sentinel (payload: $PAYLOAD)" \
    || fail "repair failed: exit $rc, sentinel payload: [$PAYLOAD]"

"$INIT_PROJECT" --store "$STORE" --prefix VT --non-interactive 1>&2; rc=$?
[ "$rc" -eq 0 ] \
    && pass "re-run after repair is idempotent (exit 0)" \
    || fail "re-run after repair exited $rc"

echo
if [ "$fail_count" -eq 0 ]; then
    echo "im-294-empty-sentinel: all checks passed."
    exit 0
else
    echo "im-294-empty-sentinel: $fail_count check(s) FAILED."
    exit 1
fi
