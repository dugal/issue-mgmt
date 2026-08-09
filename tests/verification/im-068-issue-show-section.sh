#!/usr/bin/env bash
# Verification script for IM-068 (--section / --frontmatter / --no-body / --json)
# and IM-069 (multi-id batch mode) on the new `issue-show` binary.
#
# Run from the project root:
#     tests/verification/im-068-issue-show-section.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE_SHOW="$(resolve_cli issue-show)"

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then
        pass "$3 ($2)"
    else
        fail "$3: expected '$1', got '$2'"
    fi
}

assert_contains() {
    if echo "$2" | grep -qF -- "$1"; then
        pass "$3 (contains '$1')"
    else
        fail "$3: '$1' not found in:"
        echo "$2" | sed 's/^/  | /'
    fi
}

assert_not_contains() {
    if echo "$2" | grep -qF -- "$1"; then
        fail "$3: '$1' SHOULD NOT appear in output"
        echo "$2" | sed 's/^/  | /'
    else
        pass "$3 (does not contain '$1')"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im068-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# ---------------------------------------------------------------------------
# Build a synthetic store with three issues, each with multiple sections.
# ---------------------------------------------------------------------------
STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM" "$STORE/DOUG"

write_issue() {
    local id="$1" prefix="${1%%-*}"
    cat > "$STORE/$prefix/$id.md"
}

write_issue IM-001 <<'EOF'
---
id: IM-001
title: "fixture one"
type: feature
status: open
priority: high
created: 2026-06-01
updated: 2026-06-01
branch: ""
tags: []
---

## Summary

Summary body for IM-001 line 1.
Summary body for IM-001 line 2.

## Acceptance Criteria

- one
- two

## Implementation Notes

2026-06-01: created IM-001 fixture.
EOF

write_issue IM-002 <<'EOF'
---
id: IM-002
title: "fixture two"
type: feature
status: open
priority: medium
created: 2026-06-01
updated: 2026-06-01
branch: ""
tags: []
---

## Summary

Summary body for IM-002.

## Implementation Notes

2026-06-01: created IM-002 fixture.
EOF

write_issue DOUG-001 <<'EOF'
---
id: DOUG-001
title: "fixture carol"
type: chore
status: open
priority: low
created: 2026-06-01
updated: 2026-06-01
branch: ""
tags: []
---

## Summary

Summary body for DOUG-001.
EOF

export ISSUE_STORE_ROOT="$STORE"

# ---------------------------------------------------------------------------
# Section A: --help / --version
# ---------------------------------------------------------------------------
echo
echo "[A] Help and version"

# Banner check uses the verb token `show` (substring of both the flat
# `issue-show` usage line and the consolidated `issue show` usage line) so
# the assertion holds under either ISSUE_MGMT_CLI_STYLE.
HELP=$("$ISSUE_SHOW" --help 2>&1)
assert_contains "show" "$HELP" "--help prints tool banner"
assert_contains "--section" "$HELP" "--help documents --section"
assert_contains "--frontmatter" "$HELP" "--help documents --frontmatter"
assert_contains "--no-body" "$HELP" "--help documents --no-body"
assert_contains "--json" "$HELP" "--help documents --json"
assert_contains "Multi-id batch mode" "$HELP" "--help documents multi-id batch mode"
assert_contains "Examples:" "$HELP" "--help has an Examples block"

VOUT=$("$ISSUE_SHOW" --version)
assert_contains "v" "$VOUT" "--version prints a version string"

# ---------------------------------------------------------------------------
# Section B: Default behavior (no flags) is byte-for-byte
# ---------------------------------------------------------------------------
echo
echo "[B] Default behavior: byte-for-byte"

DIRECT=$(cat "$STORE/IM/IM-001.md")
VIA_TOOL=$("$ISSUE_SHOW" IM-001)
# COVERS: cli.issue-show
assert_eq "$DIRECT" "$VIA_TOOL" "default output equals cat IM-001.md"

DIRECT_BYTES=$(wc -c < "$STORE/IM/IM-001.md")
VIA_BYTES=$("$ISSUE_SHOW" IM-001 | wc -c)
assert_eq "$DIRECT_BYTES" "$VIA_BYTES" "byte counts match"

# ---------------------------------------------------------------------------
# Section C: --section semantics (single, multiple, order, atomicity)
# ---------------------------------------------------------------------------
echo
echo "[C] --section semantics"

OUT=$("$ISSUE_SHOW" IM-001 --section "Summary")
assert_contains "## Summary" "$OUT" "single --section prints heading"
assert_contains "Summary body for IM-001 line 1." "$OUT" "single --section prints body"
assert_not_contains "## Acceptance Criteria" "$OUT" "single --section omits other sections"
assert_not_contains "## Implementation Notes" "$OUT" "single --section omits Implementation Notes"

OUT=$("$ISSUE_SHOW" IM-001 --section "Acceptance Criteria" --section "Summary")
# Caller-chosen order: AC first, then Summary
LINE_AC=$(echo "$OUT" | grep -n "## Acceptance Criteria" | head -1 | cut -d: -f1)
LINE_SUM=$(echo "$OUT" | grep -n "## Summary" | head -1 | cut -d: -f1)
if [ -n "$LINE_AC" ] && [ -n "$LINE_SUM" ] && [ "$LINE_AC" -lt "$LINE_SUM" ]; then
    pass "multi --section preserves caller-specified order (AC before Summary)"
else
    fail "multi --section order wrong: AC@$LINE_AC Sum@$LINE_SUM"
fi

# Case-sensitive: "summary" (lowercase) should NOT match "## Summary"
OUT_ERR=$("$ISSUE_SHOW" IM-001 --section "summary" 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "case-mismatched section name -> exit 1"
assert_contains "summary" "$OUT_ERR" "case-mismatch error names the requested name"
assert_contains "IM-001" "$OUT_ERR" "case-mismatch error names the issue id"

# Missing section -> exit 1, stderr names section and id, stdout empty
"$ISSUE_SHOW" IM-001 --section "DoesNotExist" >"$TMPDIR_ROOT/out.txt" 2>"$TMPDIR_ROOT/err.txt"
RC=$?
assert_eq "1" "$RC" "missing section -> exit 1"
OUT_STDOUT=$(cat "$TMPDIR_ROOT/out.txt")
OUT_STDERR=$(cat "$TMPDIR_ROOT/err.txt")
assert_eq "" "$OUT_STDOUT" "missing section: stdout is empty"
assert_contains "DoesNotExist" "$OUT_STDERR" "missing section: stderr names section"
assert_contains "IM-001" "$OUT_STDERR" "missing section: stderr names issue id"

# Atomic: existing + missing produces NO partial stdout
"$ISSUE_SHOW" IM-001 --section "Summary" --section "DoesNotExist" \
    >"$TMPDIR_ROOT/out.txt" 2>"$TMPDIR_ROOT/err.txt"
RC=$?
assert_eq "1" "$RC" "existing+missing -> exit 1"
OUT_STDOUT=$(cat "$TMPDIR_ROOT/out.txt")
assert_eq "" "$OUT_STDOUT" "existing+missing: stdout suppressed (atomic per invocation)"

# ---------------------------------------------------------------------------
# Section D: --frontmatter and --no-body
# ---------------------------------------------------------------------------
echo
echo "[D] --frontmatter and --no-body"

OUT=$("$ISSUE_SHOW" IM-001 --no-body)
assert_contains '---' "$OUT" "--no-body prints frontmatter fences"
assert_contains 'id: IM-001' "$OUT" "--no-body prints frontmatter body"
assert_not_contains "## Summary" "$OUT" "--no-body omits body sections"

OUT=$("$ISSUE_SHOW" IM-001 --frontmatter --section "Summary")
assert_contains 'id: IM-001' "$OUT" "--frontmatter + --section includes frontmatter"
assert_contains "## Summary" "$OUT" "--frontmatter + --section includes the section"
assert_not_contains "## Acceptance Criteria" "$OUT" \
    "--frontmatter + --section omits non-requested sections"

# --no-body x --section rejected at parse time (exit 2)
"$ISSUE_SHOW" IM-001 --no-body --section "Summary" \
    >"$TMPDIR_ROOT/out.txt" 2>"$TMPDIR_ROOT/err.txt"
RC=$?
assert_eq "2" "$RC" "--no-body + --section rejected at parse time -> exit 2"
OUT_STDERR=$(cat "$TMPDIR_ROOT/err.txt")
assert_contains "mutually exclusive" "$OUT_STDERR" \
    "--no-body + --section error names the conflict"

# ---------------------------------------------------------------------------
# Section E: --json shape (single id)
# ---------------------------------------------------------------------------
echo
echo "[E] --json shape (single id)"

JSON=$("$ISSUE_SHOW" IM-001 --json)
echo "$JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['id'] == 'IM-001', d
assert d['file'].endswith('IM-001.md'), d
assert d['store'].endswith('store'), d
assert 'frontmatter' in d and d['frontmatter']['id'] == 'IM-001', d
assert d['full_text'] is not None and 'Summary body for IM-001' in d['full_text'], d
assert 'sections' not in d, d
print('json-default-ok')
" >"$TMPDIR_ROOT/jc1.out" 2>&1
if grep -q "json-default-ok" "$TMPDIR_ROOT/jc1.out"; then
    pass "--json default shape carries id/file/store/frontmatter/full_text"
else
    fail "--json default shape wrong: $(cat "$TMPDIR_ROOT/jc1.out")"
fi

JSON=$("$ISSUE_SHOW" IM-001 --section "Summary" --section "Implementation Notes" --json)
echo "$JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['sections_requested'] == ['Summary', 'Implementation Notes'], d
assert set(d['sections'].keys()) == {'Summary', 'Implementation Notes'}, d
assert '## Summary' in d['sections']['Summary'], d
assert '## Implementation Notes' in d['sections']['Implementation Notes'], d
assert d['full_text'] is None, d
print('json-section-ok')
" >"$TMPDIR_ROOT/jc2.out" 2>&1
if grep -q "json-section-ok" "$TMPDIR_ROOT/jc2.out"; then
    pass "--json with --section carries sections_requested/sections, full_text null"
else
    fail "--json section shape wrong: $(cat "$TMPDIR_ROOT/jc2.out")"
fi

JSON=$("$ISSUE_SHOW" IM-001 --no-body --json)
echo "$JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['full_text'] is None, d
assert 'sections' not in d, d
assert d['frontmatter']['id'] == 'IM-001', d
print('json-nobody-ok')
" >"$TMPDIR_ROOT/jc3.out" 2>&1
if grep -q "json-nobody-ok" "$TMPDIR_ROOT/jc3.out"; then
    pass "--json --no-body: full_text null, sections absent, frontmatter present"
else
    fail "--json --no-body shape wrong: $(cat "$TMPDIR_ROOT/jc3.out")"
fi

# ---------------------------------------------------------------------------
# Section F: Argument validation
# ---------------------------------------------------------------------------
echo
echo "[F] Argument validation"

OUT_ERR=$("$ISSUE_SHOW" 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "missing ID -> exit 2"
assert_contains "missing ID" "$OUT_ERR" "missing-ID error is explicit"

# `im-001` (lowercase) case-folds to IM-001 and is no longer
# malformed. Only genuinely malformed shapes remain exit-2 here.
for bad in "I-001" "IM-" "IM001" "foo"; do
    OUT_ERR=$("$ISSUE_SHOW" "$bad" 2>&1 >/dev/null)
    RC=$?
    assert_eq "2" "$RC" "bad ID '$bad' -> exit 2"
    assert_contains "invalid ID" "$OUT_ERR" "bad ID '$bad' error names 'invalid ID'"
done

# Missing file -> exit 1
OUT_ERR=$("$ISSUE_SHOW" IM-999 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "missing issue file -> exit 1"
assert_contains "not found" "$OUT_ERR" "missing-file error names 'not found'"

# ---------------------------------------------------------------------------
# Section G: Multi-id text mode
# ---------------------------------------------------------------------------
echo
echo "[G] Multi-id text mode"

OUT=$("$ISSUE_SHOW" IM-001 IM-002 DOUG-001)
assert_contains "=== IM-001 ===" "$OUT" "header for IM-001 present"
assert_contains "=== IM-002 ===" "$OUT" "header for IM-002 present"
assert_contains "=== DOUG-001 ===" "$OUT" "header for DOUG-001 present"

# Order is positional
P1=$(echo "$OUT" | grep -n "=== IM-001 ===" | head -1 | cut -d: -f1)
P2=$(echo "$OUT" | grep -n "=== IM-002 ===" | head -1 | cut -d: -f1)
P3=$(echo "$OUT" | grep -n "=== DOUG-001 ===" | head -1 | cut -d: -f1)
if [ "$P1" -lt "$P2" ] && [ "$P2" -lt "$P3" ]; then
    pass "multi-id text headers appear in positional order"
else
    fail "multi-id order wrong: $P1/$P2/$P3"
fi

# All --section flags apply uniformly to all ids
OUT=$("$ISSUE_SHOW" IM-001 IM-002 --section "Summary")
HEAD1=$(echo "$OUT" | grep -c "## Summary")
assert_eq "2" "$HEAD1" "--section Summary applies to each id (two headings)"
assert_not_contains "## Acceptance Criteria" "$OUT" \
    "--section limits each id to the requested section"

# Duplicate ids: render twice in positional order
OUT=$("$ISSUE_SHOW" IM-001 IM-001 --section "Summary")
HCOUNT=$(echo "$OUT" | grep -c "=== IM-001 ===")
assert_eq "2" "$HCOUNT" "duplicate ids render once each (no de-dup)"

# Mixed-success: continue past failure, exit 1 at end
"$ISSUE_SHOW" IM-001 IM-999 IM-002 --section "Summary" \
    >"$TMPDIR_ROOT/out.txt" 2>"$TMPDIR_ROOT/err.txt"
RC=$?
assert_eq "1" "$RC" "mixed-success multi-id -> exit 1"
OUT_STDOUT=$(cat "$TMPDIR_ROOT/out.txt")
OUT_STDERR=$(cat "$TMPDIR_ROOT/err.txt")
assert_contains "=== IM-001 ===" "$OUT_STDOUT" "mixed-success: IM-001 rendered to stdout"
assert_contains "=== IM-002 ===" "$OUT_STDOUT" "mixed-success: IM-002 rendered to stdout"
assert_not_contains "=== IM-999 ===" "$OUT_STDOUT" \
    "mixed-success: failed id does NOT appear in stdout"
assert_contains "=== IM-999 ===" "$OUT_STDERR" \
    "mixed-success: failed id appears in stderr"
assert_contains "[error:" "$OUT_STDERR" \
    "mixed-success: stderr line carries [error: ...]"

# ---------------------------------------------------------------------------
# Section H: Multi-id JSON mode
# ---------------------------------------------------------------------------
echo
echo "[H] Multi-id JSON mode"

JSON=$("$ISSUE_SHOW" IM-001 IM-002 DOUG-001 --json)
echo "$JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert isinstance(d, list), type(d)
assert len(d) == 3, len(d)
assert [x['id'] for x in d] == ['IM-001', 'IM-002', 'DOUG-001'], d
for entry in d:
    assert 'error' not in entry, entry
    assert entry['frontmatter']['id'] == entry['id'], entry
print('json-array-ok')
" >"$TMPDIR_ROOT/jc4.out" 2>&1
if grep -q "json-array-ok" "$TMPDIR_ROOT/jc4.out"; then
    pass "--json multi-id: top-level array, positional order, single-id shape per entry"
else
    fail "--json multi-id shape wrong: $(cat "$TMPDIR_ROOT/jc4.out")"
fi

JSON=$("$ISSUE_SHOW" IM-001 IM-999 IM-002 --json)
echo "$JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert len(d) == 3, len(d)
assert d[0]['id'] == 'IM-001' and 'error' not in d[0], d[0]
assert d[1]['id'] == 'IM-999' and 'error' in d[1], d[1]
assert d[2]['id'] == 'IM-002' and 'error' not in d[2], d[2]
print('json-mixed-ok')
" >"$TMPDIR_ROOT/jc5.out" 2>&1
if grep -q "json-mixed-ok" "$TMPDIR_ROOT/jc5.out"; then
    pass "--json multi-id mixed-success: failure entry carries error field, order preserved"
else
    fail "--json mixed-success wrong: $(cat "$TMPDIR_ROOT/jc5.out")"
fi

"$ISSUE_SHOW" IM-001 IM-999 IM-002 --json >/dev/null 2>&1
assert_eq "1" "$?" "--json mixed-success exits 1"

# ---------------------------------------------------------------------------
# Section I: Sentinel-based store-mode detection
# ---------------------------------------------------------------------------
echo
echo "[I] Sentinel-based store-mode detection"

SENTINEL_DIR="$TMPDIR_ROOT/sentinel-proj/.issues"
mkdir -p "$SENTINEL_DIR"
printf "%s" "$STORE" > "$SENTINEL_DIR/.migrated-to-store"

(
    cd "$TMPDIR_ROOT/sentinel-proj"
    unset ISSUE_STORE_ROOT
    "$ISSUE_SHOW" IM-001 --section Summary >"$TMPDIR_ROOT/s.out" 2>"$TMPDIR_ROOT/s.err"
    echo $? > "$TMPDIR_ROOT/s.rc"
)
assert_eq "0" "$(cat "$TMPDIR_ROOT/s.rc")" "sentinel mode -> exit 0"
assert_contains "Summary body for IM-001" "$(cat "$TMPDIR_ROOT/s.out")" \
    "sentinel mode returns the same content as env mode"

# ---------------------------------------------------------------------------
# Section J: Legacy mode (no store, walk to nearest .issues/)
# ---------------------------------------------------------------------------
echo
echo "[J] Legacy mode resolution"

LEGACY_ROOT="$TMPDIR_ROOT/legacy-proj"
mkdir -p "$LEGACY_ROOT/.issues"
cat > "$LEGACY_ROOT/.issues/IM-500.md" <<'EOF'
---
id: IM-500
title: "legacy fixture"
type: chore
status: open
priority: low
created: 2026-06-01
updated: 2026-06-01
branch: ""
tags: []
---

## Summary

legacy body content.
EOF

(
    cd "$LEGACY_ROOT"
    unset ISSUE_STORE_ROOT
    "$ISSUE_SHOW" IM-500 --section Summary >"$TMPDIR_ROOT/l.out" 2>"$TMPDIR_ROOT/l.err"
    echo $? > "$TMPDIR_ROOT/l.rc"
)
assert_eq "0" "$(cat "$TMPDIR_ROOT/l.rc")" "legacy mode -> exit 0"
assert_contains "legacy body content." "$(cat "$TMPDIR_ROOT/l.out")" \
    "legacy mode returns the requested section body"

# ---------------------------------------------------------------------------
# Section K: --store ROOT override
# ---------------------------------------------------------------------------
echo
echo "[K] --store ROOT override"

ALT_STORE="$TMPDIR_ROOT/alt-store"
mkdir -p "$ALT_STORE/IM"
cat > "$ALT_STORE/IM/IM-777.md" <<'EOF'
---
id: IM-777
title: "alt store"
type: chore
status: open
priority: low
created: 2026-06-01
updated: 2026-06-01
branch: ""
tags: []
---

## Summary

alternate-store content.
EOF

(
    unset ISSUE_STORE_ROOT
    OUT=$("$ISSUE_SHOW" IM-777 --store "$ALT_STORE" --section Summary)
    echo "$OUT" | grep -q "alternate-store content."
    echo "exit=$?" > "$TMPDIR_ROOT/k.rc"
)
assert_contains "exit=0" "$(cat "$TMPDIR_ROOT/k.rc")" \
    "--store overrides env and finds the issue in the alternate store"

# ---------------------------------------------------------------------------
# Section L: Read-only -- no .locks/ entries created
# ---------------------------------------------------------------------------
echo
echo "[L] Read-only: no locks acquired"

"$ISSUE_SHOW" IM-001 >/dev/null 2>&1
"$ISSUE_SHOW" IM-001 --section Summary >/dev/null 2>&1
"$ISSUE_SHOW" IM-001 IM-002 DOUG-001 --json >/dev/null 2>&1
"$ISSUE_SHOW" IM-001 IM-999 --section Summary >/dev/null 2>&1 || true

if find "$STORE" -type d -name ".locks" 2>/dev/null | grep -q .; then
    fail ".locks/ directory was created by issue-show (should not happen)"
else
    pass "no .locks/ directory created by issue-show"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "----- issue-show verification summary -----"
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
