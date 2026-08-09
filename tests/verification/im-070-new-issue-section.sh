#!/usr/bin/env bash
# im-070-new-issue-section.sh -- pre-merge verification for IM-070.
#
# Exercises every IM-070 acceptance criterion against the dev checkout's
# scripts/new-issue.sh. Uses isolated temp git stores + projects; this
# repo's own .issues/ is never touched. PASS:/FAIL: per check, cleans up,
# exits non-zero on any FAIL.
#
# Run from the project root:
#   tests/verification/im-070-new-issue-section.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
NEW_ISSUE="$(resolve_cli new-issue)"
VALIDATE="$(resolve_cli validate-issue)"
# Resolve init-issue-project through the same seam (it maps to
# `issue admin init-project` in consolidated mode; the flat name is a
# tombstone). resolve_cli returns an absolute path, safe to call after cd.
INIT_ISSUE_PROJECT="$(resolve_cli init-issue-project)"
TEMPLATE_SRC="$REPO_ROOT/ISSUE-TEMPLATE.md"

[[ -x "$NEW_ISSUE" ]] || { echo "FAIL: $NEW_ISSUE not executable"; exit 1; }
[[ -x "$VALIDATE" ]] || { echo "FAIL: $VALIDATE not executable"; exit 1; }
[[ -f "$TEMPLATE_SRC" ]] || { echo "FAIL: $TEMPLATE_SRC missing"; exit 1; }

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t im-070.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

export HOME="$WORKDIR/home"
mkdir -p "$HOME"
unset ISSUE_STORE_ROOT
unset ISSUE_MGMT_NO_AUTOCOMMIT

export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="t@t"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="t@t"

mk_store() {
    local dir="$WORKDIR/$1"
    mkdir -p "$dir"
    git init -q -b main "$dir" 1>&2
    git -C "$dir" commit -q --allow-empty -m "store init" 1>&2
    echo "$dir"
}

mk_project() {
    local name="$1" prefix="$2" store="$3"
    local dir="$WORKDIR/$name"
    mkdir -p "$dir/.issues"
    git init -q -b main "$dir" 1>&2
    git -C "$dir" commit -q --allow-empty -m "project init" 1>&2
    ( cd "$dir" && "$INIT_ISSUE_PROJECT" \
        --repo "$name" --prefix "$prefix" \
        --store "$store" --non-interactive ) 1>&2 \
        || { echo "mk_project: init-issue-project failed for $name:$prefix" 1>&2; exit 1; }
    echo "$store" > "$dir/.issues/.migrated-to-store"
    echo "$dir"
}

STORE="$(mk_store store)"
PROJ="$(mk_project proj PA "$STORE")"

# --- Test 1: --section replaces an existing template section ---------
cd "$PROJ"
"$NEW_ISSUE" --section "Summary=Replaced summary body." \
    "Section replace" feature low >/dev/null 2>&1
ID1="PA-001"
F1="$STORE/PA/$ID1.md"
# COVERS: cli.create-seeded-body
if grep -qF "Replaced summary body." "$F1"; then
    pass "Test 1: --section replaces existing template section body"
else
    fail "Test 1: replaced summary body not found in $F1"
fi
# Confirm template's other section headers survived
if grep -qE '^## Implementation Notes' "$F1"; then
    pass "Test 1: template's other section headers preserved"
else
    fail "Test 1: ## Implementation Notes missing from $F1"
fi

# --- Test 2: --section appends an unknown section --------------------
"$NEW_ISSUE" --section "Design=Multi line\nblock" \
    "Section append" feature low >/dev/null 2>&1
ID2="PA-002"
F2="$STORE/PA/$ID2.md"
# Append: the new section heading and body must be present
if grep -qE '^## Design$' "$F2" && grep -qF 'Multi line\nblock' "$F2"; then
    pass "Test 2: --section appends an unknown section with body"
else
    fail "Test 2: Design section or body missing in $F2"
fi

# --- Test 3: multiple --section flags ----------------------------------
"$NEW_ISSUE" --section "Summary=S body" --section "Acceptance Criteria=A body" \
    "Multi sections" feature low >/dev/null 2>&1
ID3="PA-003"
F3="$STORE/PA/$ID3.md"
if grep -qF "S body" "$F3" && grep -qE '^## Acceptance Criteria$' "$F3" \
        && grep -qF "A body" "$F3"; then
    pass "Test 3: multiple --section flags compose"
else
    fail "Test 3: not all sections populated in $F3"
fi

# --- Test 4: --from-file replaces body verbatim ------------------------
FIXTURE="$WORKDIR/fixture.md"
cat > "$FIXTURE" <<'EOF'

## Verbatim Heading

A line that did not appear in the template at all.

## Another

Second section.
EOF
"$NEW_ISSUE" --from-file "$FIXTURE" "From file" feature low >/dev/null 2>&1
ID4="PA-004"
F4="$STORE/PA/$ID4.md"
# Body region is everything after the second `---` fence
BODY4=$(awk 'BEGIN{seen=0} /^---$/{seen++; next} seen>=2{print}' "$F4")
EXPECTED4=$(cat "$FIXTURE")
if [[ "$BODY4" == "$EXPECTED4" ]]; then
    pass "Test 4: --from-file replaces body verbatim"
else
    fail "Test 4: body mismatch in $F4"
fi

# --- Test 5: --from-file - reads stdin ---------------------------------
STDIN_BODY=$'\n## Stdin Section\n\nFrom stdin.\n'
echo -n "$STDIN_BODY" | "$NEW_ISSUE" --from-file - \
    "From stdin" feature low >/dev/null 2>&1
ID5="PA-005"
F5="$STORE/PA/$ID5.md"
if grep -qE '^## Stdin Section$' "$F5" && grep -qF "From stdin." "$F5"; then
    pass "Test 5: --from-file - reads from stdin"
else
    fail "Test 5: stdin body not present in $F5"
fi

# --- Test 6: --section + --from-file -> exit 2 ------------------------
set +e
out6=$("$NEW_ISSUE" --section "Summary=X" --from-file "$FIXTURE" \
    "Both flags" feature low 2>&1)
rc6=$?
set -e
if [[ "$rc6" -eq 2 ]] && echo "$out6" | grep -qi "mutually exclusive"; then
    pass "Test 6: --section + --from-file exits 2 with mutual-exclusion message"
else
    fail "Test 6: rc=$rc6 out=$out6"
fi

# --- Test 7: validate-issue passes for every issue created ------------
set +e
ISSUE_STORE_ROOT="$STORE" "$VALIDATE" "$STORE/PA"/PA-*.md >/dev/null 2>&1
rc7=$?
set -e
if [[ "$rc7" -eq 0 ]]; then
    pass "Test 7: validate-issue passes for all created issues"
else
    fail "Test 7: validate-issue rc=$rc7"
fi

# --- Test 8: --json carries sections_applied and from_file -----------
JSON8=$("$NEW_ISSUE" --json --section "Summary=J body" --section "Design=Other" \
    "Json sections" feature low 2>/dev/null)
ID8="PA-006"
F8="$STORE/PA/$ID8.md"
parsed=$(python3 - "$JSON8" "$F8" <<'PYJ' 2>&1
import json, os, sys
obj = json.loads(sys.argv[1])
want_id = "PA-006"
want_sections = ["Summary", "Design"]
if obj.get("id") != want_id:
    print(f"id mismatch: {obj.get('id')!r}"); sys.exit(1)
if obj.get("sections_applied") != want_sections:
    print(f"sections_applied mismatch: {obj.get('sections_applied')!r}"); sys.exit(1)
if obj.get("from_file") is not None:
    print(f"from_file should be null: {obj.get('from_file')!r}"); sys.exit(1)
if obj.get("file") != os.path.abspath(sys.argv[2]):
    print(f"file path mismatch: {obj.get('file')!r}"); sys.exit(1)
print("ok")
PYJ
)
if [[ "$parsed" == "ok" ]]; then
    pass "Test 8: --json shape carries sections_applied and from_file=null"
else
    fail "Test 8: $parsed (raw: $JSON8)"
fi

# --- Test 9: --json with --from-file carries abs from_file path ------
JSON9=$("$NEW_ISSUE" --json --from-file "$FIXTURE" \
    "Json from-file" feature low 2>/dev/null)
parsed9=$(python3 - "$JSON9" "$FIXTURE" <<'PYJ' 2>&1
import json, os, sys
obj = json.loads(sys.argv[1])
if obj.get("from_file") != os.path.abspath(sys.argv[2]):
    print(f"from_file mismatch: {obj.get('from_file')!r}"); sys.exit(1)
if obj.get("sections_applied") != []:
    print(f"sections_applied should be []: {obj.get('sections_applied')!r}"); sys.exit(1)
print("ok")
PYJ
)
if [[ "$parsed9" == "ok" ]]; then
    pass "Test 9: --json with --from-file records abs path and empty sections_applied"
else
    fail "Test 9: $parsed9 (raw: $JSON9)"
fi

# --- Test 10: --section with empty TEXT yields empty body ------------
"$NEW_ISSUE" --section "Summary=" "Empty section" feature low >/dev/null 2>&1
ID10="PA-008"
F10="$STORE/PA/$ID10.md"
# Summary body should be present but empty (heading kept, no body text)
if grep -qE '^## Summary$' "$F10"; then
    # Look at the section body: the heading line should be followed by
    # a blank line, then another `## ` heading (no content between).
    awk '/^## Summary$/{found=1; next} found && /^## /{exit} found && NF{has_content=1} END{exit has_content}' "$F10"
    if [[ $? -eq 0 ]]; then
        pass "Test 10: --section NAME= produces empty Summary body"
    else
        fail "Test 10: Summary body should be empty in $F10"
    fi
else
    fail "Test 10: ## Summary heading missing in $F10"
fi

# --- Test 11: bad --section format (no =) exits 2 --------------------
set +e
out11=$("$NEW_ISSUE" --section "NoEqualsHere" "Bad section" feature low 2>&1)
rc11=$?
set -e
if [[ "$rc11" -eq 2 ]] && echo "$out11" | grep -qi "NAME=TEXT"; then
    pass "Test 11: --section without = exits 2"
else
    fail "Test 11: rc=$rc11 out=$out11"
fi

# --- Test 12: missing --from-file path exits 1 -----------------------
set +e
out12=$("$NEW_ISSUE" --from-file "$WORKDIR/does-not-exist.md" \
    "Missing path" feature low 2>&1)
rc12=$?
set -e
if [[ "$rc12" -eq 1 ]] && echo "$out12" | grep -qi "not readable"; then
    pass "Test 12: --from-file with missing path exits 1"
else
    fail "Test 12: rc=$rc12 out=$out12"
fi

# --- Summary ---------------------------------------------------------
echo
if [[ "$fail_count" -gt 0 ]]; then
    echo "FAIL: $fail_count check(s) failed"
    exit 1
fi
echo "PASS: all IM-070 checks passed"
