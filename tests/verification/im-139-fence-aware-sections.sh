#!/usr/bin/env bash
# im-139-fence-aware-sections.sh -- pre-merge verification for IM-139.
#
# The section-boundary parser is fence-aware: a `## ` line inside a
# fenced code block (``` or ~~~, longer fences, up to 3 spaces of
# indentation) is content, not a section boundary. IM-139's observed
# defect was an automated status-transition note and an edit-time
# append both landing INSIDE a fenced block mid-section instead of at
# the true section end (MAD-129, 2026-07-08). This script exercises the
# shared find_section fix end-to-end through every consumer: the
# transition note writer, issue note, edit-time appends, show's
# section fetch, and the validator -- against an isolated temp store.
#
# Run from the project root:
#   tests/verification/im-139-fence-aware-sections.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
NEW_ISSUE="$(resolve_cli new-issue)"
EDIT_ISSUE="$(resolve_cli edit-issue)"
TRANSITION="$(resolve_cli transition-issue)"
ISSUE_NOTE="$(resolve_cli issue-note)"
ISSUE_SHOW="$(resolve_cli issue-show)"
VALIDATE="$(resolve_cli validate-issue)"
INIT_ISSUE_PROJECT="$(resolve_cli init-issue-project)"

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t im-139.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

export HOME="$WORKDIR/home"
mkdir -p "$HOME"
unset ISSUE_STORE_ROOT
unset ISSUE_MGMT_NO_AUTOCOMMIT

export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="t@t"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="t@t"

STORE="$WORKDIR/store"
mkdir -p "$STORE"
git init -q -b main "$STORE" 1>&2
git -C "$STORE" commit -q --allow-empty -m "store init" 1>&2

PROJ="$WORKDIR/proj"
mkdir -p "$PROJ/.issues"
git init -q -b main "$PROJ" 1>&2
git -C "$PROJ" commit -q --allow-empty -m "project init" 1>&2
( cd "$PROJ" && "$INIT_ISSUE_PROJECT" \
    --repo proj --prefix FA --store "$STORE" --non-interactive ) 1>&2 \
    || { echo "FAIL: init-issue-project failed"; exit 1; }

cd "$PROJ"

# --- Fixture: an issue whose Summary embeds a fenced ## line ----------
# Seeded via --from-file (verbatim by design) to reproduce the MAD-129
# shape: fenced markdown example mid-section, real prose after it.
FIXTURE="$WORKDIR/body.md"
cat > "$FIXTURE" <<'EOF'

## Summary

intro paragraph

```markdown
## fenced heading
fenced content
```

tail paragraph after the fence

## Implementation Notes

EOF
"$NEW_ISSUE" --from-file "$FIXTURE" "Fence fixture" bug low >/dev/null 2>&1
ID="FA-001"
F="$STORE/FA/$ID.md"
[[ -f "$F" ]] || { echo "FAIL: $F was not created"; exit 1; }

# --- 1. show --section returns the WHOLE section past the fence -------
OUT1=$("$ISSUE_SHOW" "$ID" --section "Summary" 2>/dev/null)
if echo "$OUT1" | grep -qF "tail paragraph after the fence"; then
    pass "1. show --section Summary extends past the fenced ## line"
else
    fail "1. show --section Summary truncated at the fenced heading"
fi
if echo "$OUT1" | grep -qF "## fenced heading"; then
    pass "1. fenced heading rendered as section content"
else
    fail "1. fenced heading missing from the section body"
fi

# --- 2. edit --append lands at the true end of the section ------------
"$EDIT_ISSUE" "$ID" --section "Summary" --append "APPENDED LINE" \
    >/dev/null 2>&1 || fail "2. edit --append exited nonzero"
if grep -qF $'tail paragraph after the fence\n\nAPPENDED LINE' <(cat "$F"); then
    pass "2. edit --append landed after the tail paragraph (true section end)"
else
    fail "2. edit --append did not land at the true section end"
fi
if awk '/^```markdown$/{f=1} /^```$/{f=0} f && /APPENDED LINE/{exit 1}' "$F"; then
    pass "2. appended line is not inside the fence"
else
    fail "2. appended line landed inside the fenced block (the IM-139 defect)"
fi

# --- 3. transition audit line lands in Implementation Notes -----------
# The exact observed defect: the automated status-transition line landed
# inside the fence. Add a fenced block to Implementation Notes first.
"$EDIT_ISSUE" "$ID" --section "Implementation Notes" \
    --append $'```markdown\n## notes example\n```' >/dev/null 2>&1 \
    || fail "3. priming append exited nonzero"
"$TRANSITION" "$ID" in-progress >/dev/null 2>&1 \
    || fail "3. transition exited nonzero"
AUDIT=$(awk '/^## notes example$/{seen=1} seen && /status: open -> in-progress/{print "ordered"}' "$F")
if [[ "$AUDIT" == "ordered" ]]; then
    pass "3. transition audit line landed after the fenced block"
else
    fail "3. transition audit line did not land at the true section end"
fi

# --- 4. issue note lands at the true end too ---------------------------
"$ISSUE_NOTE" "$ID" "a dated note" >/dev/null 2>&1 \
    || fail "4. issue note exited nonzero"
NOTE=$(awk '/status: open -> in-progress/{seen=1} seen && /a dated note/{print "ordered"}' "$F")
if [[ "$NOTE" == "ordered" ]]; then
    pass "4. issue note landed after the audit line (true section end)"
else
    fail "4. issue note did not land at the true section end"
fi

# --- 5. the fenced heading never became a section ----------------------
SECTIONS=$(python3 - "$F" "$REPO_ROOT/lib" <<'PYS'
import sys
sys.path.insert(0, sys.argv[2])
import frontmatter
_, body = frontmatter.read_issue(sys.argv[1])
print("\n".join(frontmatter.list_section_names(body)))
PYS
)
if echo "$SECTIONS" | grep -qx "fenced heading" \
        || echo "$SECTIONS" | grep -qx "notes example"; then
    fail "5. a fenced heading leaked into the section list: [$SECTIONS]"
else
    pass "5. fenced headings are not sections (list: $(echo $SECTIONS | tr '\n' ' '))"
fi

# --- 6. validate passes on the final file ------------------------------
set +e
ISSUE_STORE_ROOT="$STORE" "$VALIDATE" "$F" >/dev/null 2>&1
rc6=$?
set -e
if [[ "$rc6" -eq 0 ]]; then
    pass "6. validate-issue passes on the fence-bearing issue"
else
    fail "6. validate-issue rc=$rc6"
fi

# --- Summary ---------------------------------------------------------
echo
if [[ "$fail_count" -gt 0 ]]; then
    echo "FAIL: $fail_count check(s) failed"
    exit 1
fi
echo "PASS: all IM-139 checks passed"
