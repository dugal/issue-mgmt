#!/usr/bin/env bash
# Skill exit-code parity guard.
#
# Shipped SKILL.md files are executable behavior, not documentation: a
# model-invoked skill keys its retry and error handling on the exit codes it
# names. If a skill documents a code the CLI no longer returns, that handling
# silently never fires -- e.g. a mutating skill telling the agent to retry on a
# retired `concurrent-edit` code means the retry never triggers under real lock
# contention.
#
# This guard fails when any SKILL.md presents a retired or out-of-contract exit
# code as a CURRENT code, so the skills cannot drift from the lib/errors.py
# loser-side exit-code contract again. It derives the valid ceiling from
# errors.py, so a future contract change moves the guard with it.
#
# The broader class -- coupling shipped agent-facing surfaces to the tests that
# prove their contract -- is tracked separately for the onboarding path.

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
# Every SHIPPED markdown surface that names an exit code, not just the
# skills. IM-265: the glob was `.claude/skills/*-dev/SKILL.md` only, which
# left the installed cheat sheet and the two store-template files -- both
# copied into every new store by `issue admin init-store`, and both carrying
# exit codes -- outside the one guard that checks them. IM-228 had to correct
# the store templates by hand for exactly this reason.
SKILL_GLOB="$PROJECT_ROOT"/.claude/skills/*-dev/SKILL.md
SHIPPED_GLOB="$PROJECT_ROOT/share/issue-mgmt/CHEATSHEET.md $PROJECT_ROOT/share/store-template/LAYOUT.md $PROJECT_ROOT/share/store-template/README.md"
ALL_GLOB="$SKILL_GLOB $SHIPPED_GLOB"
ERRORS="$PROJECT_ROOT/lib/errors.py"
PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

# Highest valid exit code in the current contract (contiguous 1..N, read from
# the EXIT_* constants that own it -- not hard-coded here).
MAXVALID=$(grep -oE 'EXIT_[A-Z_]+ *= *[0-9]+' "$ERRORS" \
             | grep -oE '[0-9]+$' | sort -n | tail -1)
if [ -z "$MAXVALID" ]; then
    echo "FAIL: could not read the exit-code contract from $ERRORS"
    exit 1
fi

echo "[1] no shipped markdown presents a retired/out-of-contract exit code as current (valid = 0..$MAXVALID)"

# A code reference is treated as CURRENT unless the line carries a historical
# qualifier -- a skill may accurately note "exit 3 was 17 through v2.4.1".
HIST='was |through v|renumber|formerly|prior to|used to'

# Shape A -- prose: "exit N" / "exit code N" / "exits N" where N is a single
# digit >= 8 or any two-digit number (retired 17/18, or anything above the
# single-digit contract ceiling).
badA=$(grep -rnEi 'exit(s| code)? +([89]|[1-9][0-9])\b' $ALL_GLOB 2>/dev/null \
         | grep -viE "$HIST" || true)

# Shape B -- list-item error tables: "- `N` --" where N is outside 0..MAXVALID.
badB=$(grep -rnE '^- +`[0-9]+`' $ALL_GLOB 2>/dev/null \
         | grep -viE "$HIST" \
         | awk -v max="$MAXVALID" -F'`' '($2 + 0) > max { print }' || true)

if [ -z "$badA" ] && [ -z "$badB" ]; then
    # The same assertion covers two requirements. dist.skill-exit-code-parity
    # is the shipped-skill half: no skill may present an out-of-contract code
    # as current. surface.exit-code-space is the closed-space half: the set of
    # codes treated as valid here is DERIVED from the EXIT_* constants (see
    # MAXVALID above) rather than written down, so the closed space cannot be
    # widened in a shipped surface without moving lib/errors.py first. Exit
    # codes are not part of the argument surface, so docs/cli-surface.json
    # cannot carry them -- this is the authority the vocabulary registry's
    # `unenforced` table names for the clause.
    # COVERS: dist.skill-exit-code-parity
    # COVERS: surface.exit-code-space
    pass "all shipped-markdown exit-code references are within the current 0..$MAXVALID contract"
else
    fail "shipped markdown references an exit code outside the current contract:"
    [ -n "$badA" ] && echo "$badA" | sed 's/^/    [prose] /'
    [ -n "$badB" ] && echo "$badB" | sed 's/^/    [table] /'
    echo "    -> the CLI returns codes 0..$MAXVALID (see lib/errors.py);"
    echo "       fix the skill, or add a historical qualifier if the mention is deliberate history."
fi

echo
echo "[2] each skill's documented conditions match the CLI it drives"

# The ceiling check above is necessary and NOT sufficient: it never checks
# WHICH code a skill maps to a condition. IM-238 (this same phase) flipped the
# enumeration reads to exit 1 on a zero result and two shipped skills kept
# telling the agent 0 -- with the guard green, because 0 is inside the
# contract. check-skill-contract adds the condition-level checks, each driven
# by ground truth: the zero-result code per verb CLASS from
# docs/cli-surface.json, `concurrent-edit` membership on every mutating verb
# from lib/errors.py, and documented flags against the verb they are passed to.
if CONTRACT_OUT=$("$PROJECT_ROOT/scripts/check-skill-contract" 2>&1); then
    # COVERS: dist.skill-exit-code-parity
    pass "every shipped skill agrees with the CLI it drives"
    echo "$CONTRACT_OUT" | sed 's/^/    /'
else
    fail "shipped skills disagree with the CLI they drive:"
    echo "$CONTRACT_OUT" | sed 's/^/    /'
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
