#!/usr/bin/env bash
# Verification for the shipped CLI cheat sheet (IM-267).
#
# `share/issue-mgmt/CHEATSHEET.md` ships in the formula and installs next to
# the skills, so a session reads it as current fact about the CLI. Before
# IM-267 nothing read its text: every test touching it checked only that it
# gets INSTALLED (the formula carries the install line, a sentinel string
# reaches ~/.claude/skills/, `--json` reports the path). Its inventories were
# correct, but by luck rather than by construction -- and its one statement
# about argument POSITION was wrong for four of the five flags it named,
# which is the worst case for this file: a session that learns flag placement
# here writes commands that exit 2.
#
# Two halves, deliberately split by what each can honestly prove:
#
#   [A] Flag placement, by EXECUTION. Reading the sentence cannot establish
#       whether the dispatcher accepts a flag before the verb; only running
#       it can. Every placement the sheet asserts is run against the real
#       CLI and must exit 0.
#   [B] Inventory + exit-code guidance, via scripts/check-cheatsheet, which
#       diffs the sheet against docs/cli-surface.json and the skills dir.
#
# Exit-CODE references in the sheet are covered by skill-exit-codes.sh, whose
# glob IM-265 widened over all shipped markdown.
#
# Run from the project root:
#     tests/verification/cheatsheet.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE="$ISSUE_MGMT_BIN/issue"
SHEET="$PROJECT_ROOT/share/issue-mgmt/CHEATSHEET.md"

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

# The operator's store is never touched: the only verbs run here are read-only
# or --help/--version probes, and ISSUE_STORE_ROOT is pinned to a temp dir.
TMPDIR_ROOT=$(mktemp -d -t im-cheatsheet-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT
export ISSUE_STORE_ROOT="$TMPDIR_ROOT/store"
mkdir -p "$ISSUE_STORE_ROOT"

# ---------------------------------------------------------------------------
echo
echo "[A] flag-placement claims hold when executed"

# The sheet must not tell a caller that a flag works before the verb unless
# it does. IM-267: it listed five, and `--store`, `--json`, `--lock-timeout`
# and `--no-reindex` were all rejected pre-verb with exit 2 -- the dispatcher
# attaches them PER VERB. Only `--version` worked.
#
# Each probe runs the placement and records the real exit code. `--help` and
# `--version` are the dispatcher's own flags and must work pre-verb; the
# per-verb flags must NOT, and the sheet must not claim they do.

probe_rc() {  # probe_rc <args...> -> echoes the exit code
    "$ISSUE" "$@" >/dev/null 2>&1
    echo $?
}

RC=$(probe_rc --version)
# COVERS: dist.cheatsheet
[ "$RC" = "0" ] && pass "dispatcher accepts --version before any verb (rc=0)" \
    || fail "dispatcher rejected --version before the verb (rc=$RC)"

RC=$(probe_rc --help)
# COVERS: dist.cheatsheet
[ "$RC" = "0" ] && pass "dispatcher accepts --help before any verb (rc=0)" \
    || fail "dispatcher rejected --help before the verb (rc=$RC)"

# Now the four the sheet used to claim. Each must be rejected pre-verb, AND
# accepted post-verb -- that pair is what makes "flags go after the verb"
# a checked statement rather than a reworded guess.
check_per_verb_flag() {  # <flag> <pre-verb argv...> -- <post-verb argv...>
    flag=$1; shift
    pre=""
    while [ "$1" != "--" ]; do pre="$pre $1"; shift; done
    shift
    PRE_RC=$(probe_rc $pre)
    POST_RC=$(probe_rc "$@")
    # COVERS: dist.cheatsheet
    if [ "$PRE_RC" = "2" ]; then
        pass "$flag is rejected BEFORE the verb (rc=2), as the sheet says"
    else
        fail "$flag before the verb gave rc=$PRE_RC, expected 2 -- the sheet's placement rule is wrong"
    fi
    # COVERS: dist.cheatsheet
    if [ "$POST_RC" != "2" ]; then
        pass "$flag is accepted AFTER the verb (rc=$POST_RC)"
    else
        fail "$flag after the verb gave rc=2 -- the sheet names a flag that verb does not take"
    fi
}

check_per_verb_flag "--store" --store "$ISSUE_STORE_ROOT" list -- list --store "$ISSUE_STORE_ROOT"
check_per_verb_flag "--json" --json list -- list --json

# And the sheet must not still be asserting the refuted claim.
# COVERS: dist.cheatsheet
if grep -q 'Global flags accepted before the verb args' "$SHEET"; then
    fail "the sheet still claims these flags are accepted before the verb args"
else
    pass "the sheet no longer claims per-verb flags work before the verb"
fi
# COVERS: dist.cheatsheet
if grep -q 'Flags go AFTER the verb' "$SHEET"; then
    pass "the sheet states the correct placement rule"
else
    fail "the sheet does not state where flags go"
fi

# ---------------------------------------------------------------------------
echo
echo "[B] inventory + exit-code guidance match the CLI"

if OUT=$("$PROJECT_ROOT/scripts/check-cheatsheet" 2>&1); then
    # COVERS: dist.cheatsheet
    pass "cheat sheet inventories and exit guidance match the CLI"
    echo "$OUT" | sed 's/^/    /'
else
    fail "cheat sheet disagrees with the CLI:"
    echo "$OUT" | sed 's/^/    /'
fi

# ---------------------------------------------------------------------------
echo
echo "=============================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=============================================="
[ "$FAIL" -eq 0 ]
