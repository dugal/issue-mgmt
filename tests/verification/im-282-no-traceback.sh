#!/usr/bin/env bash
# im-282-no-traceback.sh -- failure-matrix no-traceback gate (IM-282).
#
# `surface.no-traceback` promises that no failure path on any verb
# terminates in an unhandled exception. Through v3.6.x nothing measured
# that -- the clause stayed [planned] on "no KNOWN violation", which is
# weaker evidence than a measured clean matrix (the v3.4.1 owner decision).
# This script is the measurement the `unenforced` table's entry names in
# `enforced_by`: it drives EVERY verb on the surface through generic
# failure inductions and asserts no Python traceback reaches the caller.
#
# The verb list is derived from docs/cli-surface.json rather than restated
# here, so a newly added verb enters the matrix automatically -- a hand
# list would be a second declaration of the surface (the IM-261 class).
#
# Inductions, per verb (every invocation runs with stdin exhausted via
# </dev/null, so the non-TTY leg of surface.non-interactive rides along --
# a prompt loop that crashes or a blocking read dies here):
#   [A] unknown flag            <verb> --no-such-flag-xyz   (usage path)
#   [B] bare invocation         <verb>                      (missing
#       operands, or an interactive path meeting exhausted stdin)
#   [C] unresolvable issue id   <verb> ZZ-999               (missing file /
#       unknown prefix / unexpected operand, depending on the verb)
#   [D] unresolvable store      <verb> ZZ-999 with ISSUE_STORE_ROOT
#       pointing at a path that does not exist
# plus targeted malformed-operand inductions ([T] section) for the grammars
# the generic matrix cannot reach (bad dependency kind, unknown state, bad
# int, guarded field, unknown --json field, empty --prefix).
#
# Store safety: every invocation pins ISSUE_STORE_ROOT to a throwaway
# mktemp store (or a deliberately absent path) and HOME to a throwaway
# home, so no induction -- including the bare mutating verbs -- can reach
# the operator's store, config, or installed skills.
#
# Exit 0 when every induction is traceback-free; non-zero otherwise.
#
# Run from the project root:
#   tests/verification/im-282-no-traceback.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"

ISSUE="$ISSUE_MGMT_BIN/issue"
if [ ! -x "$ISSUE" ]; then
    echo "im-282-no-traceback.sh: no dispatcher at $ISSUE" >&2
    exit 1
fi

fail_count=0
pass_count=0
pass() { echo "PASS: $1"; pass_count=$((pass_count + 1)); }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t im-282.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

export HOME="$WORKDIR/home"
mkdir -p "$HOME"
unset ISSUE_STORE_ROOT
unset ISSUE_MGMT_NO_AUTOCOMMIT

export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="$FIXTURE_EMAIL"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="$FIXTURE_EMAIL"

# A neutral cwd: no .issues/ anchor, not a git repo, so nothing resolves
# through the sentinel leg and only the per-invocation env decides.
CWD="$WORKDIR/cwd"
mkdir -p "$CWD"

# The resolvable fixture store (throwaway, scaffolded by the branch CLI).
STORE="$WORKDIR/store"
if ! (cd "$CWD" && "$ISSUE" admin init-store --store "$STORE" \
        --non-interactive >"$WORKDIR/scaffold.out" 2>&1 </dev/null); then
    echo "im-282-no-traceback.sh: could not scaffold the fixture store:" >&2
    cat "$WORKDIR/scaffold.out" >&2
    exit 1
fi

# The deliberately absent store for induction [D].
ABSENT="$WORKDIR/absent-store"

MARKER="Traceback (most recent call last)"

# run_case DESC STORE_PATH CMD... -- run one induction with stdin
# exhausted and the store pinned, and assert no traceback reaches the
# caller on either stream. The exit code itself is not asserted (each
# induction is a failure path by construction and the exit-code space has
# its own authority); it is reported so a surprising code is visible.
run_case() {
    _desc="$1"; _store="$2"; shift 2
    ( cd "$CWD" && ISSUE_STORE_ROOT="$_store" "$@" \
        >"$WORKDIR/case.out" 2>"$WORKDIR/case.err" </dev/null )
    _rc=$?
    if grep -Fq "$MARKER" "$WORKDIR/case.err" "$WORKDIR/case.out" \
            2>/dev/null; then
        fail "$_desc: traceback reached the caller (exit $_rc)"
        sed -n '1,6p' "$WORKDIR/case.err" | sed 's/^/    /'
    else
        pass "$_desc (exit $_rc)"
    fi
}

# The verb list, derived from the committed surface manifest.
VERBS_FILE="$WORKDIR/verbs.txt"
python3 - "$REPO_ROOT/docs/cli-surface.json" >"$VERBS_FILE" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1]))
for verb in manifest["verbs"]:
    path = verb["path"]
    assert path.startswith("issue "), path
    print(path[len("issue "):])
PY
VERB_COUNT=$(wc -l <"$VERBS_FILE" | tr -d '[:space:]')
if [ "$VERB_COUNT" -lt 20 ]; then
    echo "im-282-no-traceback.sh: only $VERB_COUNT verbs derived from the" \
         "manifest -- refusing to run a hollow matrix" >&2
    exit 1
fi
pass "verb list derived from docs/cli-surface.json ($VERB_COUNT verbs)"

echo "== generic failure matrix over every verb =="

# COVERS: surface.no-traceback
while IFS= read -r verb; do
    # Word-splitting on $verb is intended: "history show" is two argv words.
    run_case "[A] $verb --no-such-flag-xyz" "$STORE" \
        "$ISSUE" $verb --no-such-flag-xyz
    run_case "[B] $verb (bare, exhausted stdin)" "$STORE" \
        "$ISSUE" $verb
    run_case "[C] $verb ZZ-999" "$STORE" \
        "$ISSUE" $verb ZZ-999
    run_case "[D] $verb ZZ-999 (absent store)" "$ABSENT" \
        "$ISSUE" $verb ZZ-999
done <"$VERBS_FILE"

echo "== targeted malformed-operand inductions =="

# COVERS: surface.no-traceback
run_case "[T] dep add with a bad dependency kind" "$STORE" \
    "$ISSUE" dep add ZZ-001 badkind:ZZ-002
run_case "[T] transition to an unknown state" "$STORE" \
    "$ISSUE" transition ZZ-999 not-a-state
run_case "[T] edit body qualifier without a --section anchor" "$STORE" \
    "$ISSUE" edit ZZ-999 --append "text"
run_case "[T] edit of a guarded frontmatter field" "$STORE" \
    "$ISSUE" edit ZZ-999 --set status=done
run_case "[T] show with an unknown --json field" "$STORE" \
    "$ISSUE" show ZZ-999 --json no-such-field
run_case "[T] index with a non-integer --limit" "$STORE" \
    "$ISSUE" index --limit abc
run_case "[T] list with an empty --prefix" "$STORE" \
    "$ISSUE" list --prefix ""
run_case "[T] history at with a dangling --at" "$STORE" \
    "$ISSUE" history at ZZ-999 --at
run_case "[T] note without its text operand" "$STORE" \
    "$ISSUE" note ZZ-999
run_case "[T] create with a bad type and priority" "$STORE" \
    "$ISSUE" create "fixture title" badtype badpriority
run_case "[T] admin validate on a file that does not exist" "$STORE" \
    "$ISSUE" admin validate "$WORKDIR/no-such-file.md"
run_case "[T] admin migrate on a project that does not exist" "$STORE" \
    "$ISSUE" admin migrate --project "$WORKDIR/no-such-project"

echo
echo "im-282-no-traceback.sh: $pass_count passed, $fail_count failed"
[ "$fail_count" -eq 0 ] || exit 1
exit 0
