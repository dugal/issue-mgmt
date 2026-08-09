#!/usr/bin/env bash
# Verification script for validate-issue.
#
# Covers the acceptance criteria end-to-end via subprocesses:
#   - Exit codes: 0 (clean), 1 (violations), 2 (tool error)
#   - --json structured output shape
#   - Diagnostic per violation type: missing field, invalid status,
#     id-filename mismatch, malformed frontmatter
#   - Smoke test against the live workspace corpus
#
# Run from the project root:
#     tests/verification/validate-issue.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
CMD="$(resolve_cli validate-issue)"

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    # assert_eq <expected> <actual> <label>
    if [ "$1" = "$2" ]; then
        pass "$3 (=$2)"
    else
        fail "$3: expected '$1', got '$2'"
    fi
}

assert_contains() {
    # assert_contains <needle> <haystack> <label>
    case "$2" in
        *"$1"*) pass "$3 (matched '$1')" ;;
        *) fail "$3: '$1' not found in: $2" ;;
    esac
}

TMPDIR_ROOT=$(mktemp -d -t im026-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

WORK="$TMPDIR_ROOT/issues"
mkdir -p "$WORK"

# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

cat > "$WORK/IM-100.md" <<'EOF'
---
id: IM-100
title: "Valid example"
type: feature
status: open
priority: high
created: 2026-05-13
updated: 2026-05-13
branch: ""
tags: []
changelog:
---

## Summary

Body.
EOF

cat > "$WORK/IM-101.md" <<'EOF'
---
id: IM-101
type: feature
status: open
priority: high
created: 2026-05-13
updated: 2026-05-13
---
EOF

cat > "$WORK/IM-102.md" <<'EOF'
---
id: IM-102
title: "Underscore status"
type: feature
status: in_progress
priority: high
created: 2026-05-13
updated: 2026-05-13
---
EOF

# id-filename mismatch: id says RM-103 but filename is IM-103.md
cat > "$WORK/IM-103.md" <<'EOF'
---
id: RM-103
title: "Wrong filename"
type: feature
status: open
priority: high
created: 2026-05-13
updated: 2026-05-13
---
EOF

# No frontmatter at all
cat > "$WORK/IM-104.md" <<'EOF'
This file has no YAML frontmatter at all.
EOF

# ---------------------------------------------------------------------------
# Check 1: valid file -> exit 0, no output
# ---------------------------------------------------------------------------
echo ""
echo "### Check 1: valid file, exit 0"
out=$("$CMD" "$WORK/IM-100.md" 2>&1)
rc=$?
# COVERS: validation.linter
assert_eq "0" "$rc" "exit code for valid file"
assert_eq "" "$out" "no diagnostics emitted for valid file"

# ---------------------------------------------------------------------------
# Check 2: missing required field -> exit 1, missing-field diagnostic
# ---------------------------------------------------------------------------
echo ""
echo "### Check 2: missing required field"
out=$("$CMD" "$WORK/IM-101.md" 2>&1)
rc=$?
# COVERS: substrate.frontmatter-schema
assert_eq "1" "$rc" "exit code for missing field"
assert_contains "missing-field" "$out" "diagnostic code is missing-field"
assert_contains "'title'" "$out" "diagnostic names the field"

# ---------------------------------------------------------------------------
# Check 3: illegal status value -> exit 1, invalid-status diagnostic
# ---------------------------------------------------------------------------
echo ""
echo "### Check 3: illegal status value (in_progress underscore)"
out=$("$CMD" "$WORK/IM-102.md" 2>&1)
rc=$?
assert_eq "1" "$rc" "exit code for invalid status"
assert_contains "invalid-status" "$out" "diagnostic code is invalid-status"
assert_contains "in_progress" "$out" "diagnostic includes the rejected value"

# ---------------------------------------------------------------------------
# Check 4: id-filename mismatch -> exit 1, id-filename-mismatch diagnostic
# ---------------------------------------------------------------------------
echo ""
echo "### Check 4: id-filename mismatch"
out=$("$CMD" "$WORK/IM-103.md" 2>&1)
rc=$?
assert_eq "1" "$rc" "exit code for id-filename mismatch"
assert_contains "id-filename-mismatch" "$out" "diagnostic code is id-filename-mismatch"

# ---------------------------------------------------------------------------
# Check 5: malformed frontmatter -> exit 1, malformed-frontmatter diagnostic
# ---------------------------------------------------------------------------
echo ""
echo "### Check 5: malformed frontmatter"
out=$("$CMD" "$WORK/IM-104.md" 2>&1)
rc=$?
assert_eq "1" "$rc" "exit code for malformed frontmatter"
assert_contains "malformed-frontmatter" "$out" "diagnostic code is malformed-frontmatter"

# ---------------------------------------------------------------------------
# Check 6: file not found -> exit 2 (tool error)
# ---------------------------------------------------------------------------
echo ""
echo "### Check 6: file not found"
out=$("$CMD" "$WORK/does-not-exist.md" 2>&1)
rc=$?
assert_eq "2" "$rc" "exit code for missing file"
assert_contains "file not found" "$out" "stderr message names the failure"

# ---------------------------------------------------------------------------
# Check 7: no files -> exit 2 (tool error)
# ---------------------------------------------------------------------------
echo ""
echo "### Check 7: no positional arguments"
out=$("$CMD" 2>&1)
rc=$?
assert_eq "2" "$rc" "exit code when no files given"

# ---------------------------------------------------------------------------
# Check 8: --json shape
# ---------------------------------------------------------------------------
echo ""
echo "### Check 8: --json output shape"
json=$("$CMD" --json "$WORK/IM-100.md" "$WORK/IM-102.md" 2>/dev/null)
rc=$?
assert_eq "1" "$rc" "exit code 1 (one valid, one invalid)"
# Parse JSON and assert structure via python.
shape_ok=$(python3 - <<EOF "$json"
import json, sys
data = json.loads(sys.argv[1])
assert isinstance(data, list), "top-level is array"
assert len(data) == 2
assert data[0]["valid"] is True
assert data[1]["valid"] is False
assert any(v["code"] == "invalid-status" for v in data[1]["violations"])
print("ok")
EOF
)
assert_eq "ok" "$shape_ok" "JSON contains top-level array; per-file objects have valid+violations"

# ---------------------------------------------------------------------------
# Check 9: multiple files, mix of ok/violations -> exit 1
# ---------------------------------------------------------------------------
echo ""
echo "### Check 9: mixed batch"
out=$("$CMD" "$WORK/IM-100.md" "$WORK/IM-101.md" 2>&1)
rc=$?
assert_eq "1" "$rc" "exit 1 when at least one file has violations"
assert_contains "IM-101.md" "$out" "diagnostic lines name the offending file"

# ---------------------------------------------------------------------------
# Check 10: --version exits 0
# ---------------------------------------------------------------------------
echo ""
echo "### Check 10: --version and --help"
out=$("$CMD" --version 2>&1)
rc=$?
assert_eq "0" "$rc" "exit 0 for --version"
case "$out" in
    v*) pass "--version prints a version string (got '$out')" ;;
    *) fail "--version output unexpected: '$out'" ;;
esac

out=$("$CMD" --help 2>&1 | head -1)
rc=${PIPESTATUS[0]}
assert_eq "0" "$rc" "exit 0 for --help"
# "validate" is common to both the flat `validate-issue` usage line and the
# consolidated `issue admin validate` usage line, so the assertion holds under
# either ISSUE_MGMT_CLI_STYLE.
assert_contains "validate" "$out" "--help prints program banner"

# ---------------------------------------------------------------------------
# Check 11: corpus smoke test (live workspace)
# ---------------------------------------------------------------------------
echo ""
echo "### Check 11: live workspace corpus"
WORKSPACE="${ISSUE_MGMT_ROOT:-$HOME/projects}"
if [ ! -d "$WORKSPACE" ]; then
    # Non-hermetic check: needs the developer's real workspace corpus. On a
    # clean box / CI / container it is absent -- SKIP rather than FAIL so the
    # full suite stays green.
    echo "SKIP: workspace not found: $WORKSPACE -- cannot smoke-test corpus"
else
    # Collect every issue file, excluding INDEX and template stubs.
    files=$(find "$WORKSPACE" -name '*.md' -path '*/.issues/*' \
            -not -name 'INDEX.md' -not -name 'ISSUE-TEMPLATE.md' 2>/dev/null)
    count=$(printf "%s\n" "$files" | sed '/^$/d' | wc -l | tr -d ' ')
    if [ "$count" -eq 0 ]; then
        fail "no issue files found under $WORKSPACE"
    else
        # shellcheck disable=SC2086  -- intentional word-splitting
        out=$("$CMD" $files 2>&1)
        rc=$?
        if [ "$rc" -eq 0 ]; then
            pass "all $count corpus files validate clean"
        else
            fail "corpus validation failed (exit=$rc, $(printf "%s" "$out" | wc -l | tr -d ' ') diagnostic line(s)):"
            printf "%s\n" "$out" | head -20
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "PASSED: $PASS    FAILED: $FAIL"
echo "============================================================"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
