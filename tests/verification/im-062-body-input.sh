#!/usr/bin/env bash
# Verification script for IM-062 -- edit-issue body input flags
# (--append-file / --prepend-file / --replace-file). PATH=='-' reads
# stdin to EOF, matching the POSIX convention and `new-issue
# --from-file -`.
#
# Covers:
#   - Each new flag's happy path with a real file PATH.
#   - PATH=='-' as the stdin sentinel for append, prepend, and replace.
#   - Backtick/apostrophe/$() content that the inline form chokes on
#     (the field repro shape).
#   - Empty file and PATH=='-' with empty stdin -> empty body.
#   - Missing file -> exit 1; mutex conflicts -> exit 2.
#   - --append-file without --section -> exit 2.
#   - --replace-file composes with --if-unchanged-since SHA:
#     stale SHA -> exit 3; current SHA -> exit 0.
#
# Run from the project root:
#     tests/verification/im-062-body-input.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
EDIT_ISSUE="$(resolve_cli edit-issue)"

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
        fail "$3: '$1' not found in '$2'"
    fi
}

assert_file_contains() {
    if grep -qF -- "$1" "$2"; then
        pass "$3 (file contains '$1')"
    else
        fail "$3: '$1' not in $2"
    fi
}

assert_file_not_contains() {
    if grep -qF -- "$1" "$2"; then
        fail "$3: '$1' unexpectedly present in $2"
    else
        pass "$3 (file does not contain '$1')"
    fi
}

TMPDIR_ROOT=$(mktemp -d -t im062-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

export PATH="$ISSUE_MGMT_BIN:$PATH"
# Hard-isolate from the caller's real store.
unset ISSUE_STORE_ROOT

make_issue() {
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
tags: []
changelog:    # set by /update-changelog -- do not edit manually
---

## Summary

Original summary.

## Implementation Notes

Seed line.

## Archived Plan Content
EOF
}

# ---------------------------------------------------------------------------
# Store fixture
# ---------------------------------------------------------------------------
STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM"
(
    cd "$STORE"
    git init -q
    git config user.email "$FIXTURE_EMAIL"
    git config user.name test
    printf 'fixture-proj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
make_issue "$STORE/IM/IM-200.md" "IM-200"
make_issue "$STORE/IM/IM-201.md" "IM-201"
make_issue "$STORE/IM/IM-202.md" "IM-202"
make_issue "$STORE/IM/IM-203.md" "IM-203"
(
    cd "$STORE"
    git add IM/
    git commit -q -m "seed fixtures"
)

PROJ="$TMPDIR_ROOT/fixture-proj"
mkdir -p "$PROJ/.issues"
echo "IM" > "$PROJ/.issues/.prefix"
echo "$STORE" > "$PROJ/.issues/.migrated-to-store"

# ---------------------------------------------------------------------------
# [1] --append-file: happy path with shell-hostile content
# ---------------------------------------------------------------------------
echo
echo "[1] --append-file with shell-hostile content"

PAYLOAD="$TMPDIR_ROOT/payload-append.md"
# Backticks, code fence, apostrophes, $() and ${} -- the exact shapes
# that broke inline --append in the earlier repros.
cat > "$PAYLOAD" <<'EOF'

Appended line with `inline code`, an apostrophe in stamp-version's name,
and a shell-style expansion `$(date +%Y)` plus `${HOME}`.

```bash
echo "fenced block"
```
EOF

RC=0
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-200 --section "Implementation Notes" \
        --append-file "$PAYLOAD"
) >/dev/null || RC=$?

# COVERS: cli.edit-body-file-input
assert_eq "0" "$RC" "--append-file happy path -> exit 0"
assert_file_contains "inline code" "$STORE/IM/IM-200.md" \
    "appended content lands"
assert_file_contains "stamp-version's name" "$STORE/IM/IM-200.md" \
    "apostrophe preserved verbatim"
assert_file_contains '$(date +%Y)' "$STORE/IM/IM-200.md" \
    "shell-style expansion preserved verbatim"
assert_file_contains "fenced block" "$STORE/IM/IM-200.md" \
    "code fence content lands"
assert_file_contains "Seed line." "$STORE/IM/IM-200.md" \
    "pre-existing section content preserved"

# ---------------------------------------------------------------------------
# [2] --prepend-file: happy path
# ---------------------------------------------------------------------------
echo
echo "[2] --prepend-file"

PAYLOAD2="$TMPDIR_ROOT/payload-prepend.md"
printf '**Prepended header.**\n\n' > "$PAYLOAD2"

RC=0
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-201 --section "Implementation Notes" \
        --prepend-file "$PAYLOAD2"
) >/dev/null || RC=$?

assert_eq "0" "$RC" "--prepend-file happy path -> exit 0"
assert_file_contains "Prepended header." "$STORE/IM/IM-201.md" \
    "prepended content lands"
# Confirm order: header appears before the seed line in the file.
PREPEND_LINE=$(grep -n "Prepended header" "$STORE/IM/IM-201.md" \
    | head -1 | cut -d: -f1)
SEED_LINE=$(grep -n "Seed line" "$STORE/IM/IM-201.md" \
    | head -1 | cut -d: -f1)
if [ -n "$PREPEND_LINE" ] && [ -n "$SEED_LINE" ] \
        && [ "$PREPEND_LINE" -lt "$SEED_LINE" ]; then
    pass "prepend ordering correct (header L$PREPEND_LINE before seed L$SEED_LINE)"
else
    fail "prepend ordering: header L$PREPEND_LINE, seed L$SEED_LINE"
fi

# ---------------------------------------------------------------------------
# [3] --replace-file: happy path
# ---------------------------------------------------------------------------
echo
echo "[3] --replace-file"

PAYLOAD3="$TMPDIR_ROOT/payload-replace.md"
cat > "$PAYLOAD3" <<'EOF'

Replacement summary with backticks (`x`) and apostrophes (it's).
EOF

RC=0
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-202 --section Summary --replace-file "$PAYLOAD3"
) >/dev/null || RC=$?

assert_eq "0" "$RC" "--replace-file happy path -> exit 0"
assert_file_contains "Replacement summary" "$STORE/IM/IM-202.md" \
    "replacement content lands"
assert_file_not_contains "Original summary." "$STORE/IM/IM-202.md" \
    "prior summary text wiped"

# ---------------------------------------------------------------------------
# [4] PATH=='-' reads stdin: --append-file -
# ---------------------------------------------------------------------------
echo
echo "[4] --append-file - (stdin sentinel)"

RC=0
(
    cd "$PROJ"
    printf 'Stdin-appended line with `tick` and apostrophe (it'\''s).\n' \
        | "$EDIT_ISSUE" IM-200 --section Summary --append-file -
) >/dev/null || RC=$?

assert_eq "0" "$RC" "--append-file - happy path -> exit 0"
assert_file_contains "Stdin-appended line" "$STORE/IM/IM-200.md" \
    "stdin content lands"

# ---------------------------------------------------------------------------
# [5] PATH=='-' reads stdin: --replace-file -
# ---------------------------------------------------------------------------
echo
echo "[5] --replace-file - (stdin sentinel)"

RC=0
(
    cd "$PROJ"
    printf 'Stdin-replacement.\n' \
        | "$EDIT_ISSUE" IM-201 --section Summary --replace-file -
) >/dev/null || RC=$?

assert_eq "0" "$RC" "--replace-file - happy path -> exit 0"
assert_file_contains "Stdin-replacement." "$STORE/IM/IM-201.md" \
    "stdin replacement lands"
assert_file_not_contains "Original summary." "$STORE/IM/IM-201.md" \
    "prior summary wiped via stdin"

# ---------------------------------------------------------------------------
# [6] Missing file -> exit 1
# ---------------------------------------------------------------------------
echo
echo "[6] Missing file -> exit 1"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-200 --section Summary \
        --append-file "$TMPDIR_ROOT/does-not-exist.md" 2>&1 >/dev/null
) || RC=$?

assert_eq "1" "$RC" "missing file -> exit 1"
assert_contains "does-not-exist.md" "$STDERR" "stderr names the missing path"

# ---------------------------------------------------------------------------
# [7] Unreadable file -> exit 1
# ---------------------------------------------------------------------------
echo
echo "[7] Unreadable file -> exit 1"

NOREAD="$TMPDIR_ROOT/noread.md"
printf 'unreadable\n' > "$NOREAD"
chmod 000 "$NOREAD"

# Skip the chmod-based check if running as root (chmod 000 doesn't bite root).
if [ "$(id -u)" -eq 0 ]; then
    echo "SKIP: running as root, chmod 000 does not enforce"
else
    RC=0
    STDERR=$(
        cd "$PROJ"
        "$EDIT_ISSUE" IM-200 --section Summary \
            --append-file "$NOREAD" 2>&1 >/dev/null
    ) || RC=$?
    assert_eq "1" "$RC" "unreadable file -> exit 1"
    assert_contains "noread.md" "$STDERR" "stderr names the unreadable path"
fi
chmod 600 "$NOREAD"  # restore so trap cleanup can rm

# ---------------------------------------------------------------------------
# [8] Empty file -> empty replacement allowed
# ---------------------------------------------------------------------------
echo
echo "[8] Empty file allowed (empty body)"

EMPTYFILE="$TMPDIR_ROOT/empty.md"
: > "$EMPTYFILE"

RC=0
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-203 --section Summary --replace-file "$EMPTYFILE"
) >/dev/null || RC=$?

assert_eq "0" "$RC" "empty file --replace-file -> exit 0"
assert_file_not_contains "Original summary." "$STORE/IM/IM-203.md" \
    "Summary section emptied"

# ---------------------------------------------------------------------------
# [9] PATH=='-' with empty stdin allowed (empty body)
# ---------------------------------------------------------------------------
echo
echo "[9] --replace-file - with empty stdin allowed"

RC=0
(
    cd "$PROJ"
    : | "$EDIT_ISSUE" IM-203 --section "Implementation Notes" --replace-file -
) >/dev/null || RC=$?

assert_eq "0" "$RC" "empty stdin via --replace-file - -> exit 0"

# ---------------------------------------------------------------------------
# [10] Mutex: any two body-input flags -> exit 2
# ---------------------------------------------------------------------------
echo
echo "[10] Mutex conflicts -> exit 2"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-200 --section Summary --append "x" \
        --append-file "$PAYLOAD" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--append + --append-file -> exit 2"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-200 --section Summary \
        --replace-file "$PAYLOAD3" --append-file "$PAYLOAD" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--replace-file + --append-file -> exit 2"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-200 --section Summary --prepend "y" \
        --replace-file "$PAYLOAD3" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--prepend + --replace-file -> exit 2"

# ---------------------------------------------------------------------------
# [11] --append-file without --section -> exit 2
# ---------------------------------------------------------------------------
echo
echo "[11] --append-file without --section -> exit 2"

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-200 --append-file "$PAYLOAD" 2>&1 >/dev/null
) || RC=$?
assert_eq "2" "$RC" "--append-file without --section -> exit 2"
assert_contains "--section" "$STDERR" "stderr names --section requirement"

# ---------------------------------------------------------------------------
# [12] --replace-file composes with --if-unchanged-since:
#      stale SHA -> exit 3; current SHA -> exit 0.
# ---------------------------------------------------------------------------
echo
echo "[12] --replace-file + --if-unchanged-since composition"

# Reset the Implementation Notes section of IM-203 with a known body
# we can race against.
RESET="$TMPDIR_ROOT/reset.md"
cat > "$RESET" <<'EOF'

- [ ] item A
- [ ] item B
EOF
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-203 --section "Implementation Notes" \
        --replace-file "$RESET"
) >/dev/null

SHA_STALE=$(git -C "$STORE" rev-parse HEAD)

# Sibling lands an --append between SHA_STALE and the caller's submit.
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-203 --section "Implementation Notes" \
        --append "- [x] item A (sibling)"
) >/dev/null

# Caller's replace-file built off SHA_STALE -- should be rejected.
STALE_REPLACE="$TMPDIR_ROOT/stale.md"
cat > "$STALE_REPLACE" <<'EOF'

- [ ] item A
- [ ] item B
- [x] item C (drafted)
EOF

RC=0
STDERR=$(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-203 --section "Implementation Notes" \
        --replace-file "$STALE_REPLACE" \
        --if-unchanged-since "$SHA_STALE" 2>&1 >/dev/null
) || RC=$?

assert_eq "3" "$RC" "stale SHA + --replace-file -> exit 3"
assert_contains "concurrent-edit" "$STDERR" "stderr names concurrent-edit"
assert_file_contains "item A (sibling)" "$STORE/IM/IM-203.md" \
    "sibling append survives blocked replace-file"
assert_file_not_contains "item C (drafted)" "$STORE/IM/IM-203.md" \
    "stale replace-file body did not land"

# Current SHA -> replacement lands.
SHA_NOW=$(git -C "$STORE" rev-parse HEAD)
FRESH_REPLACE="$TMPDIR_ROOT/fresh.md"
cat > "$FRESH_REPLACE" <<'EOF'

- [x] item A (after retry)
- [ ] item B
EOF

RC=0
(
    cd "$PROJ"
    "$EDIT_ISSUE" IM-203 --section "Implementation Notes" \
        --replace-file "$FRESH_REPLACE" \
        --if-unchanged-since "$SHA_NOW"
) >/dev/null || RC=$?

assert_eq "0" "$RC" "current SHA + --replace-file -> exit 0"
assert_file_contains "item A (after retry)" "$STORE/IM/IM-203.md" \
    "fresh replace-file body lands"
assert_file_not_contains "item A (sibling)" "$STORE/IM/IM-203.md" \
    "fresh replace-file correctly overwrites prior content"

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
exit 0
