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
SKILL_GLOB="$PROJECT_ROOT"/.claude/skills/*-dev/SKILL.md
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

echo "[1] no SKILL.md presents a retired/out-of-contract exit code as current (valid = 0..$MAXVALID)"

# A code reference is treated as CURRENT unless the line carries a historical
# qualifier -- a skill may accurately note "exit 3 was 17 through v2.4.1".
HIST='was |through v|renumber|formerly|prior to|used to'

# Shape A -- prose: "exit N" / "exit code N" / "exits N" where N is a single
# digit >= 8 or any two-digit number (retired 17/18, or anything above the
# single-digit contract ceiling).
badA=$(grep -rnEi 'exit(s| code)? +([89]|[1-9][0-9])\b' $SKILL_GLOB 2>/dev/null \
         | grep -viE "$HIST" || true)

# Shape B -- list-item error tables: "- `N` --" where N is outside 0..MAXVALID.
badB=$(grep -rnE '^- +`[0-9]+`' $SKILL_GLOB 2>/dev/null \
         | grep -viE "$HIST" \
         | awk -v max="$MAXVALID" -F'`' '($2 + 0) > max { print }' || true)

if [ -z "$badA" ] && [ -z "$badB" ]; then
    # COVERS: dist.skill-exit-code-parity
    pass "all SKILL.md exit-code references are within the current 0..$MAXVALID contract"
else
    fail "SKILL.md files reference an exit code outside the current contract:"
    [ -n "$badA" ] && echo "$badA" | sed 's/^/    [prose] /'
    [ -n "$badB" ] && echo "$badB" | sed 's/^/    [table] /'
    echo "    -> the CLI returns codes 0..$MAXVALID (see lib/errors.py);"
    echo "       fix the skill, or add a historical qualifier if the mention is deliberate history."
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
