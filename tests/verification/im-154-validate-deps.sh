#!/usr/bin/env bash
# Verification script for IM-154 validate-issue depends_on shape lint.
#
# A non-list `depends_on` reads as "unmanaged", so a malformed dependency
# block does not fail loudly on the authoring path. This lint is the
# out-of-band structural backstop. Each malformed shape must exit 1 with a
# diagnostic naming the depends_on field; each well-formed shape (including
# an empty list and an absent key) must exit 0.
#
# Fixtures use the canonical block-sequence form (not inline flow) so the
# lint runs off the real block-aware read path
# (frontmatter.parse_frontmatter_lines), not the scalar schema parser.
#
# Run from the project root:
#     tests/verification/im-154-validate-deps.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
CMD="$(resolve_cli validate-issue)"

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

# assert_exit <expected_rc> <file> <label>
assert_exit() {
    local expected="$1" file="$2" label="$3" out rc
    out="$("$CMD" "$file" 2>&1)"
    rc=$?
    if [ "$rc" -eq "$expected" ]; then
        pass "$label (exit $rc)"
    else
        fail "$label: expected exit $expected, got $rc"
        printf '%s\n' "$out" | head -10
    fi
}

# assert_exit_names_field <expected_rc> <file> <label>
# Like assert_exit but also requires the diagnostic to name depends_on.
assert_exit_names_field() {
    local expected="$1" file="$2" label="$3" out rc
    out="$("$CMD" "$file" 2>&1)"
    rc=$?
    if [ "$rc" -ne "$expected" ]; then
        fail "$label: expected exit $expected, got $rc"
        printf '%s\n' "$out" | head -10
        return
    fi
    if printf '%s' "$out" | grep -qF -- "depends_on"; then
        pass "$label (exit $rc; diagnostic names depends_on)"
    else
        fail "$label: exit $rc correct but diagnostic did not name depends_on:"
        printf '%s\n' "$out" | head -10
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im154-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT
WORK="$TMPDIR_ROOT/IM"
mkdir -p "$WORK"

# Shared valid header; each fixture appends its own depends_on shape (or none).
write_issue() {
    # write_issue <ID> <depends_on-block-or-empty>
    local id="$1" dep="$2" f="$WORK/$1.md"
    {
        echo "---"
        echo "id: $id"
        echo 'title: "dep shape fixture"'
        echo "type: bug"
        echo "status: open"
        echo "priority: high"
        echo "created: 2026-07-11"
        echo "updated: 2026-07-11"
        printf '%s' "$dep"
        echo "---"
        echo ""
        echo "## Summary"
        echo "body"
    } > "$f"
    printf '%s\n' "$f"
}

echo "=== Well-formed shapes must pass (exit 0) ==="

# COVERS: validation.depends-on-shape
f=$(write_issue IM-201 $'depends_on:\n  - to: IM-001\n    type: blocks\n')
assert_exit 0 "$f" "well-formed block sequence passes"

# COVERS: validation.depends-on-shape
f=$(write_issue IM-202 $'depends_on: []\n')
assert_exit 0 "$f" "empty list depends_on: [] passes"

# COVERS: validation.depends-on-shape
f=$(write_issue IM-203 "")
assert_exit 0 "$f" "absent depends_on key passes"

# COVERS: validation.depends-on-shape
f=$(write_issue IM-204 $'depends_on:\n  - to: RM-107\n    type: blocks\n')
assert_exit 0 "$f" "cross-prefix target (RM-107) in an IM issue passes"

# COVERS: validation.depends-on-shape
f=$(write_issue IM-205 $'depends_on:\n  - to: IM-001\n    type: parent-child\n  - to: RM-107\n    type: related\n')
assert_exit 0 "$f" "multi-edge well-formed block passes"

echo ""
echo "=== Malformed shapes must fail (exit 1) and name depends_on ==="

# COVERS: validation.depends-on-shape
f=$(write_issue IM-210 $'depends_on: RM-107\n')
assert_exit_names_field 1 "$f" "scalar depends_on rejected"

# COVERS: validation.depends-on-shape
f=$(write_issue IM-211 $'depends_on:\n  - type: blocks\n')
assert_exit_names_field 1 "$f" "entry missing to rejected"

# COVERS: validation.depends-on-shape
f=$(write_issue IM-212 $'depends_on:\n  - to: IM-001\n    type: nonsense\n')
assert_exit_names_field 1 "$f" "type outside four IM kinds rejected"

# COVERS: validation.depends-on-shape
f=$(write_issue IM-213 $'depends_on:\n  - to: not-an-id\n    type: blocks\n')
assert_exit_names_field 1 "$f" "malformed to id rejected"

echo ""
echo "============================================================"
echo "PASSED: $PASS    FAILED: $FAIL"
echo "============================================================"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
