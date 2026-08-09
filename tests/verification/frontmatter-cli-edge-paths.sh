#!/usr/bin/env bash
# Verification script for lib/frontmatter.py CLI edge-path coverage.
#
# Lifts CLI coverage of lib/frontmatter.py from 77% to ~90%+ by exercising
# the lib-layer error and edge branches that scripts/edit-issue dispatches
# into. Complements IM-114 (which covered scripts/edit-issue's argparse
# layer error paths); this script targets the deeper lib-layer surface
# behind those flags.
#
# Three buckets exercised:
#   1. Structural-section errors -- add_section / rename_section /
#      remove_section / move_section ValueError branches reached via
#      --add-section / --rename-section / --remove-section /
#      --move-section. Each error path bubbles through edit-issue main()
#      catch-all at line 1005 to exit 1.
#   2. YAML quoting / parsing -- _quote_value_like + _single_quote +
#      _split_value_comment + _needs_quoting reserved-word branches.
#      Exercised via round-trips of `edit-issue --set FIELD=VALUE`
#      against tailored seed values; the resulting file is read back to
#      assert byte-level preservation of quote style and inline
#      comments.
#   3. Malformed-frontmatter fail-fast -- parse_issue ValueError
#      branches ("no opening frontmatter fence", "no closing
#      frontmatter fence") raised when edit-issue tries to read a file
#      that has no opening `---` or no closing `---`. The error
#      diagnostic names the file path.
#
# Bucket 4 (CRLF line endings) deferred to follow-up
# `## Out of scope`.
#
# Run from the project root:
#     tests/verification/frontmatter-cli-edge-paths.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
EDIT_ISSUE="$(resolve_cli edit-issue)"
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
    # assert_contains <needle> <haystack> <label>
    if echo "$2" | grep -qF -- "$1"; then
        pass "$3 (contains '$1')"
    else
        fail "$3: '$1' not found in '$2'"
    fi
}

assert_file_contains() {
    if grep -qF -- "$1" "$2"; then
        pass "$3 (file contains '$1')"
    else
        fail "$3: '$1' not in $2:"
        sed -n '1,40p' "$2"
    fi
}

assert_file_line_eq() {
    # assert_file_line_eq <expected exact line> <file> <label>
    # Uses fixed-string + line-regex match to verify byte-exact line presence
    # without regex escaping headaches.
    if grep -qxF -- "$1" "$2"; then
        pass "$3 (exact line '$1')"
    else
        fail "$3: exact line '$1' not in $2:"
        sed -n '1,20p' "$2"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im119-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# Put the CLI bin dir on PATH so edit-issue's auto-reindex can
# find reindex-issues.
export PATH="$ISSUE_MGMT_BIN:$PATH"

# Hard-isolate from any caller-set ISSUE_STORE_ROOT; resolution order is
# env > sentinel, and our tests use per-project anchors.
unset ISSUE_STORE_ROOT

LEGACY="$TMPDIR_ROOT/legacy-project"
mkdir -p "$LEGACY/.issues"
echo "IM" > "$LEGACY/.issues/.prefix"

# Standard fixture: three named sections (Summary, Implementation Notes,
# Archived Plan Content) plus stock frontmatter. The frontmatter mix is
# tuned to exercise quoting variants: title is double-quoted, branch is
# quoted-empty, tags is unquoted flow, changelog carries an inline
# comment.
make_issue() {
    # make_issue <path> <id>
    cat > "$1" <<EOF
---
id: $2
title: "Sample issue"
type: feature
status: open
priority: high
created: 2026-05-12
updated: 2026-05-12
branch: ""
tags: [a, b]
changelog:    # set by /update-changelog -- do not edit manually
---

## Summary

Original summary text.

## Implementation Notes

- item one
- item two

## Archived Plan Content
EOF
}

# ---------------------------------------------------------------------------
# Bucket 1: structural-section error paths
#
# Each assertion exercises one ValueError raise site inside
# lib/frontmatter.py's structural helpers. ValueError from these helpers
# is caught at scripts/edit-issue line 1005 and mapped to exit 1; the
# stderr diagnostic is the raise message verbatim, prefixed with
# "edit-issue: ".
# ---------------------------------------------------------------------------
echo
echo "[1] Structural-section error paths"

# 1.1 -- rename_section: source section not found
#   lib/frontmatter.py:296 / :300 -- "section not found: 'OLD'"
make_issue "$LEGACY/.issues/IM-101.md" "IM-101"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-101 --rename-section "Bogus Section" "New Name" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.1 rename-section MISSING exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section not found: 'Bogus Section'" "$STDERR" "1.1 stderr names missing source"

# 1.2 -- rename_section: destination already exists
#   lib/frontmatter.py:302 -- "section already exists: 'NEW'"
make_issue "$LEGACY/.issues/IM-102.md" "IM-102"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-102 --rename-section "Summary" "Implementation Notes" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.2 rename-section to existing name exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section already exists: 'Implementation Notes'" "$STDERR" \
    "1.2 stderr names duplicate destination"

# 1.3 -- remove_section: section not found
#   lib/frontmatter.py:328 -- "section not found: 'NAME'"
make_issue "$LEGACY/.issues/IM-103.md" "IM-103"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-103 --remove-section "Bogus Section" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.3 remove-section MISSING exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section not found: 'Bogus Section'" "$STDERR" "1.3 stderr names missing section"

# 1.4 -- move_section: --after anchor missing
#   lib/frontmatter.py:388 -- "section not found: 'AFTER'" (in
#   move_section's remaining_text scan after the moved section is
#   extracted). The moved section itself ("Summary") exists.
make_issue "$LEGACY/.issues/IM-104.md" "IM-104"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-104 --move-section "Summary" --after "Bogus Section" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.4 move-section after MISSING exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section not found: 'Bogus Section'" "$STDERR" "1.4 stderr names missing anchor"

# 1.5 -- move_section: --to-position out of range
#   lib/frontmatter.py:375 -- "position out of range: N (have M sections after extraction)"
make_issue "$LEGACY/.issues/IM-105.md" "IM-105"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-105 --move-section "Summary" --to-position 99 --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.5 move-section to-position 99 exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "position out of range: 99" "$STDERR" "1.5 stderr names out-of-range position"

# 1.6 -- move_section: destination anchor IS the moved section
#   lib/frontmatter.py:360 -- "destination anchor cannot be the moved section itself: 'NAME'"
make_issue "$LEGACY/.issues/IM-106.md" "IM-106"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-106 --move-section "Summary" --after "Summary" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.6 move-section self-anchor exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "destination anchor cannot be the moved section itself" "$STDERR" \
    "1.6 stderr names self-anchor rule"

# 1.7 -- add_section: --to-position out of range
#   lib/frontmatter.py:261 -- "position out of range: N (have M sections)"
make_issue "$LEGACY/.issues/IM-107.md" "IM-107"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-107 --add-section "New" --to-position 99 --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.7 add-section to-position 99 exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "position out of range: 99" "$STDERR" "1.7 stderr names out-of-range position"

# 1.8 -- add_section: name already exists
#   lib/frontmatter.py:239 -- "section already exists: 'NAME'"
make_issue "$LEGACY/.issues/IM-108.md" "IM-108"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-108 --add-section "Summary" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.8 add-section existing name exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section already exists: 'Summary'" "$STDERR" "1.8 stderr names duplicate"

# 1.9 -- add_section: --before anchor missing
#   lib/frontmatter.py:256 -- "section not found: 'BEFORE'"
make_issue "$LEGACY/.issues/IM-109.md" "IM-109"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-109 --add-section "New" --before "Bogus Section" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.9 add-section before MISSING exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section not found: 'Bogus Section'" "$STDERR" "1.9 stderr names missing anchor"

# 1.10 -- move_section: --before anchor missing (after-extraction sweep
# raises section-not-found for the destination, not the moved section).
#   lib/frontmatter.py:393 -- "section not found: 'BEFORE'"
make_issue "$LEGACY/.issues/IM-110.md" "IM-110"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-110 --move-section "Summary" --before "Bogus Section" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.10 move-section before MISSING exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section not found: 'Bogus Section'" "$STDERR" "1.10 stderr names missing anchor"

# 1.11 -- move_section: moved section itself does not exist
#   lib/frontmatter.py:359 -- "section not found: 'NAME'"
make_issue "$LEGACY/.issues/IM-111.md" "IM-111"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-111 --move-section "Bogus" --to-position 0 --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.11 move-section MISSING moved section exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section not found: 'Bogus'" "$STDERR" "1.11 stderr names missing source"

# 1.12 -- rename_section: rename to the same name returns the body
# unchanged. Exercises the early-return path at lib/frontmatter.py:293-297
# (rename_section returns body_text verbatim when old_name == new_name and
# the section exists). edit-issue still bumps `updated:` because a
# section action was recorded, so the file is not byte-identical -- the
# lib-layer guarantee is that the body section heading and contents are
# unchanged.
make_issue "$LEGACY/.issues/IM-112.md" "IM-112"
BEFORE_BODY=$(awk '/^---$/{c++; next} c>=2' "$LEGACY/.issues/IM-112.md")
RC=0
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-112 --rename-section "Summary" "Summary" --no-reindex
) || RC=$?
AFTER_BODY=$(awk '/^---$/{c++; next} c>=2' "$LEGACY/.issues/IM-112.md")
# COVERS: cli.edit-multi-section-atomic
assert_eq "0" "$RC" "1.12 rename-section to same name exits 0 (no-op)"
# COVERS: cli.frontmatter-byte-preservation
assert_eq "$BEFORE_BODY" "$AFTER_BODY" "1.12 rename to same name preserves body bytes"

# 1.12b -- rename_section: rename to the same name when the section does
# NOT exist still raises (the early-return at lib/frontmatter.py:293
# guards with a find_section check and raises ValueError before the
# no-op branch).
make_issue "$LEGACY/.issues/IM-115.md" "IM-115"
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-115 --rename-section "Bogus" "Bogus" --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.edit-multi-section-atomic
assert_eq "1" "$RC" "1.12b rename MISSING to same name exits 1"
# COVERS: cli.edit-multi-section-atomic
assert_contains "section not found: 'Bogus'" "$STDERR" "1.12b stderr names missing source"

# 1.13 -- add_section: --to-position == len(sections) appends at end
# (success path, exercises lib/frontmatter.py:266 + 274/277-280 EOF branch).
make_issue "$LEGACY/.issues/IM-113.md" "IM-113"
# 3 existing sections (Summary, Implementation Notes, Archived Plan Content);
# --to-position 3 appends after the last.
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-113 --add-section "Appended" --to-position 3 --no-reindex
)
# COVERS: cli.edit-multi-section-atomic
assert_file_contains "## Appended" "$LEGACY/.issues/IM-113.md" \
    "1.13 add-section to-position N==len appends at EOF"

# 1.14 -- move_section: --to-position == len(remaining) appends at end
# (success path, exercises lib/frontmatter.py:380 + at_eof branch 399-402).
make_issue "$LEGACY/.issues/IM-114.md" "IM-114"
# Move Summary to position 2 (after extraction there are 2 sections, so
# position 2 means append at EOF -- the at_eof branch).
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-114 --move-section "Summary" --to-position 2 --no-reindex
)
# After the move, "## Summary" should still be present (verifies the
# move succeeded; the at_eof branch executed).
# COVERS: cli.edit-multi-section-atomic
assert_file_contains "## Summary" "$LEGACY/.issues/IM-114.md" \
    "1.14 move-section to-position N==len(remaining) lands at EOF"
# And it should now appear AFTER "## Archived Plan Content" -- assert by
# line order.
SUMMARY_LINE=$(grep -n "^## Summary" "$LEGACY/.issues/IM-114.md" | head -1 | cut -d: -f1)
ARCHIVED_LINE=$(grep -n "^## Archived Plan Content" "$LEGACY/.issues/IM-114.md" | head -1 | cut -d: -f1)
if [ -n "$SUMMARY_LINE" ] && [ -n "$ARCHIVED_LINE" ] && [ "$SUMMARY_LINE" -gt "$ARCHIVED_LINE" ]; then
    # COVERS: cli.edit-multi-section-atomic
    pass "1.14 moved Summary now follows Archived Plan Content"
else
    fail "1.14 move ordering: summary=$SUMMARY_LINE archived=$ARCHIVED_LINE"
fi

# ---------------------------------------------------------------------------
# Bucket 2: YAML quoting / parsing edge paths
#
# Each assertion seeds an issue with a specific quote style or inline
# comment, runs `edit-issue --set FIELD=VALUE`, then reads the file
# back to assert the lib-layer chose the right quoting branch. The
# branches under test:
#   _split_value_comment   -- inline comment past quoted/unquoted value
#   _quote_value_like      -- preserve double/single outer quoting
#   _single_quote          -- escape embedded apostrophe via doubling
#   _needs_quoting         -- reserved-word + special-character branches
# ---------------------------------------------------------------------------
echo
echo "[2] YAML quoting / parsing edge paths"

# 2.1 -- Double-quoted field stays double-quoted after --set
#   _quote_value_like:525 -- "if old_value[0] == '\"' and old_value[-1] == '\"'"
#   Seed `title: "Sample issue"`; set new value; result must remain
#   double-quoted (assert exact line presence).
make_issue "$LEGACY/.issues/IM-201.md" "IM-201"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-201 --set title="New title" --no-reindex
)
# COVERS: cli.frontmatter-byte-preservation
assert_file_line_eq 'title: "New title"' "$LEGACY/.issues/IM-201.md" \
    "2.1 double-quote style preserved across --set"

# 2.2 -- Single-quoted field stays single-quoted after --set
#   _quote_value_like:527 -- "if old_value[0] == \"'\" and old_value[-1] == \"'\""
#   _single_quote:539-541
#   Seed a single-quoted field by hand-editing the file before the edit,
#   then update via --set. The new value must round-trip with single
#   quotes (not double-quote-promoted).
make_issue "$LEGACY/.issues/IM-202.md" "IM-202"
# Replace the title line with a single-quoted variant.
python3 - <<PYEOF
import pathlib
p = pathlib.Path("$LEGACY/.issues/IM-202.md")
text = p.read_text()
text = text.replace('title: "Sample issue"', "title: 'Sample issue'")
p.write_text(text)
PYEOF
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-202 --set title="Brand new" --no-reindex
)
# COVERS: cli.frontmatter-byte-preservation
assert_file_line_eq "title: 'Brand new'" "$LEGACY/.issues/IM-202.md" \
    "2.2 single-quote style preserved across --set"

# 2.3 -- Single-quoted value with embedded apostrophe is escaped via
# doubling per YAML single-quoted scalar rules.
#   _single_quote:540 -- "value.replace(\"'\", \"''\")"
make_issue "$LEGACY/.issues/IM-203.md" "IM-203"
python3 - <<PYEOF
import pathlib
p = pathlib.Path("$LEGACY/.issues/IM-203.md")
text = p.read_text()
text = text.replace('title: "Sample issue"', "title: 'plain'")
p.write_text(text)
PYEOF
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-203 --set "title=can't stop" --no-reindex
)
# COVERS: cli.frontmatter-byte-preservation
assert_file_line_eq "title: 'can''t stop'" "$LEGACY/.issues/IM-203.md" \
    "2.3 single-quote escapes embedded apostrophe by doubling"

# 2.4 -- Reserved-word value triggers default quoting on a previously
# unquoted field.
#   _needs_quoting:555 -- "value.lower() in {'null', 'true', ...}"
#   _default_quote:545 -- delegates to _double_quote
#   Seed: type is unquoted (`type: feature`). Set type=null. After write,
#   the value must be quoted (any quoting form) so YAML does not parse
#   it as the null token. Our default is double-quote.
make_issue "$LEGACY/.issues/IM-204.md" "IM-204"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-204 --set type=null --no-reindex
)
# COVERS: cli.frontmatter-byte-preservation
assert_file_line_eq 'type: "null"' "$LEGACY/.issues/IM-204.md" \
    "2.4 reserved-word 'null' quoted on previously-unquoted field"

# 2.5 -- Special-character value (containing ':') triggers default
# quoting on a previously unquoted field.
#   _needs_quoting:553 -- "any(ch in value for ch in ':#...')"
make_issue "$LEGACY/.issues/IM-205.md" "IM-205"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-205 --set "type=a: b" --no-reindex
)
# COVERS: cli.frontmatter-byte-preservation
assert_file_line_eq 'type: "a: b"' "$LEGACY/.issues/IM-205.md" \
    "2.5 ':' special-character forces quoting on previously-unquoted field"

# 2.6a -- Setting a brand-new field (key not in frontmatter) appends a new
# line via set_field's append branch.
#   lib/frontmatter.py:127-128 -- "fm_lines.append(...)"
# The seed has no `assignee` field; setting it should add a new line. A
# bare alphanumeric value does not need quoting, so the result is
# verbatim `assignee: alice`.
make_issue "$LEGACY/.issues/IM-260.md" "IM-260"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-260 --set assignee=alice --no-reindex
)
# COVERS: cli.frontmatter-byte-preservation
assert_file_line_eq "assignee: alice" "$LEGACY/.issues/IM-260.md" \
    "2.6a brand-new field appended unquoted when value is YAML-safe"

# 2.6b -- Setting a brand-new field whose value needs quoting exercises
# the _needs_quoting=True branch inside set_field's append path
# (lib/frontmatter.py:127, ternary's true side -> _default_quote).
make_issue "$LEGACY/.issues/IM-261.md" "IM-261"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-261 --set assignee=yes --no-reindex
)
# COVERS: cli.frontmatter-byte-preservation
assert_file_line_eq 'assignee: "yes"' "$LEGACY/.issues/IM-261.md" \
    "2.6b brand-new field with reserved-word value appended quoted"

# 2.6 -- Inline trailing comment is preserved when the value is changed.
# Already covered by edit-issue.sh on `changelog: ...`, but repeating
# here threads `_split_value_comment` through this script so the
# coverage report attributes the line to a frontmatter-CLI suite that
# actually targets it. The seed has `changelog:    # set by ...`; the
# comment text must survive verbatim across --set.
make_issue "$LEGACY/.issues/IM-206.md" "IM-206"
(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-206 --set changelog=skip --no-reindex
)
# COVERS: cli.frontmatter-byte-preservation
assert_file_contains "changelog: skip   # set by /update-changelog -- do not edit manually" \
    "$LEGACY/.issues/IM-206.md" \
    "2.6 inline comment preserved across --set on a previously-empty value"

# ---------------------------------------------------------------------------
# Bucket 3: malformed-frontmatter fail-fast
#
# Two hand-crafted fixtures that exercise the two parse_issue ValueError
# branches:
#   - lib/frontmatter.py:80 -- "no opening frontmatter fence"
#   - lib/frontmatter.py:87 -- "no closing frontmatter fence"
# Both reach the CLI via edit-issue's read_issue call inside apply_edits
# (scripts/edit-issue:789) and bubble to the line-1005 ValueError catch,
# exit 1. The diagnostic must name the file path so the operator can find
# the broken file.
# ---------------------------------------------------------------------------
echo
echo "[3] Malformed-frontmatter fail-fast"

# 3.1 -- No opening fence
#   File starts with a heading, no `---` at line 1.
cat > "$LEGACY/.issues/IM-301.md" <<'EOF'
title: hand-broken (no opening fence)
type: chore
---

## Summary

This file lacks the opening fence.
EOF
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-301 --set priority=low --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.frontmatter-byte-preservation
assert_eq "1" "$RC" "3.1 no-opening-fence exits 1"
# COVERS: cli.frontmatter-byte-preservation
assert_contains "no opening frontmatter fence" "$STDERR" "3.1 stderr names parser error"
# COVERS: cli.frontmatter-byte-preservation
assert_contains "IM-301.md" "$STDERR" "3.1 stderr names the broken file path"

# 3.2 -- No closing fence
#   File has an opening fence but no matching closing fence.
cat > "$LEGACY/.issues/IM-302.md" <<'EOF'
---
title: hand-broken (no closing fence)
type: chore
status: open
priority: high

## Summary

This file's frontmatter never closes.
EOF
RC=0
STDERR=$(
    cd "$LEGACY"
    "$EDIT_ISSUE" IM-302 --set priority=low --no-reindex 2>&1 >/dev/null
) || RC=$?
# COVERS: cli.frontmatter-byte-preservation
assert_eq "1" "$RC" "3.2 no-closing-fence exits 1"
# COVERS: cli.frontmatter-byte-preservation
assert_contains "no closing frontmatter fence" "$STDERR" "3.2 stderr names parser error"
# COVERS: cli.frontmatter-byte-preservation
assert_contains "IM-302.md" "$STDERR" "3.2 stderr names the broken file path"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "============================================="
echo "PASS: $PASS    FAIL: $FAIL"
echo "============================================="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
