#!/usr/bin/env bash
# Shipped-skill naming guard (IM-276).
#
# A shipped skill is the program the model runs. Through v3.4.0 every one of
# the 15 shipped bodies named `/issue-*-dev` commands and declared a `-dev`
# frontmatter `name:`, because staging renamed the FILE and never rewrote the
# BODY -- so on any machine without this project checked out the installed
# skill told the agent to invoke commands that do not resolve. Four guards
# missed it, each because it derived both sides of its comparison from
# `.claude/skills/*-dev`.
#
# The fix inverts the direction: `share/issue-mgmt/skills/<name>.md` is
# authored canonically (and IS the shipped artifact, staged verbatim), and the
# dev-alias copies are generated from it. This script is the end-to-end
# verdict over both halves:
#
#   [1] the committed dev copies match what the canonical sources generate,
#       so a hand edit to a generated file fails loudly instead of being
#       discarded on the next regeneration;
#   [2] the STAGED bodies -- built by the same staging step `make dist` runs --
#       declare their own installed name and name only commands that exist off
#       this checkout;
#   [3] the gate bites: a `-dev` reference reintroduced into a staged body
#       fails check [2], while the dev tree it was derived from stays clean.
#
# Hermetic: reads the source tree, stages into a temp dir, never touches the
# operator's store, HOME, or the Homebrew tap.

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

TMPROOT=$(mktemp -d -t im276-shipped.XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

echo "[1] the project-local dev skill copies are current"

if OUT=$("$PROJECT_ROOT/scripts/gen-dev-skills" --check 2>&1); then
    pass "every .claude/skills/<name>-dev/SKILL.md matches its canonical source"
    echo "$OUT" | sed 's/^/    /'
else
    fail "a dev skill copy diverged from its canonical source:"
    echo "$OUT" | sed 's/^/    /'
fi

echo
echo "[2] the shipped skill bodies name only commands that resolve"

STAGE="$TMPROOT/skills"
if ! "$PROJECT_ROOT/scripts/stage-skills" "$STAGE" >/dev/null 2>&1; then
    fail "scripts/stage-skills could not stage the shipped skills"
else
    # The guard reads the STAGED artifact and derives its expectation from it
    # (the installed-command set is the staged tree's own basenames) plus the
    # dispatcher's VERB/TIER declarations. Nothing on either side comes from
    # .claude/skills, which is what the four pre-existing guards got wrong.
    if OUT=$("$PROJECT_ROOT/scripts/check-shipped-skills" \
                 --from-dir "$STAGE" 2>&1); then
        # COVERS: surface.self-naming
        # COVERS: dist.skills-install-binary
        pass "every staged skill declares its installed name and names only installed skills and real verbs"
        echo "$OUT" | sed 's/^/    /'
    else
        fail "a staged skill body names a command that does not resolve:"
        echo "$OUT" | sed 's/^/    /'
    fi

    # And the shipped bodies carry no `-dev` token at all -- the measurement
    # IM-276 was filed on (129 occurrences across all 15 files in v3.4.0),
    # asserted directly rather than inferred from the checks above.
    DEV_HITS=$(grep -c -- '-dev' "$STAGE"/*.md 2>/dev/null \
                 | awk -F: '{ total += $2 } END { print total + 0 }')
    if [ "$DEV_HITS" -eq 0 ]; then
        # COVERS: surface.self-naming
        pass "staged skill bodies carry 0 \`-dev\` references"
    else
        fail "staged skill bodies carry $DEV_HITS \`-dev\` reference(s)"
        grep -n -- '-dev' "$STAGE"/*.md | sed 's/^/    /'
    fi
fi

echo
echo "[3] gate bites: a -dev reference reintroduced into a staged body fails"

BITE="$TMPROOT/bite"
"$PROJECT_ROOT/scripts/stage-skills" "$BITE" >/dev/null 2>&1
BITE_FILE="$BITE/issue-complete.md"
# APPEND the bite rather than rewriting an existing sentence. A fixture
# anchored in live prose dies when that prose is legitimately reworded --
# the IM-273 shape, and it fired here immediately: the first draft rewrote
# the `/issue-edit` reference that IM-127 then removed in the same branch.
printf '\n- Then run `/issue-edit-dev <id>`.\n' >> "$BITE_FILE"
if grep -q -- '/issue-edit-dev' "$BITE_FILE"; then
    if "$PROJECT_ROOT/scripts/check-shipped-skills" \
           --from-dir "$BITE" >/dev/null 2>&1; then
        fail "the guard PASSED a staged body naming /issue-edit-dev -- it does not bite"
    else
        # COVERS: surface.self-naming
        pass "the guard fails on a staged body naming /issue-edit-dev"
    fi
    # The same tree, read the old way, is perfectly consistent: the defect
    # exists only in the artifact that ships. This is the difference between
    # this guard and the four that missed IM-276.
    if "$PROJECT_ROOT/scripts/gen-dev-skills" --check >/dev/null 2>&1; then
        pass "the dev-tree gate is blind to the staged-only defect (as designed)"
    else
        fail "the dev-tree gate reported a problem it cannot see -- fixture leak"
    fi
else
    fail "could not inject the bite into $BITE_FILE"
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
