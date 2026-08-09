#!/usr/bin/env bash
# Verification script for the show-issue-at subcommand.
#
# Exercises the acceptance criteria that benefit from a real
# filesystem, a real git repo, and a real subprocess:
#   - Returns issue body at the given ref (SHA / tag / branch / HEAD)
#   - Date resolution works for both absolute and relative forms
#   - Distinct historical states return distinct contents
#   - Clear error if the issue did not exist at that ref (rather
#     than empty output)
#   - --json output is parseable, carries id/file/ref/resolved_sha/content
#   - Errors clearly when ID is malformed (exit 2)
#   - Errors clearly when --at is missing (exit 2)
#   - Errors clearly when ref does not resolve (exit 1)
#   - Errors clearly when date predates all commits (exit 1)
#   - Garbage --at value (looks like an issue ID, not a ref or date)
#     errors instead of silently returning HEAD
#   - Legacy-mode error includes the exact `git show` fallback command
#   - Sentinel-based store-mode detection works
#   - --help / --version
#   - Does not acquire any locks (read-only: no .locks/ entries)
#
# Run from the project root:
#     tests/verification/show-issue-at.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
SHOW_ISSUE_AT="$(resolve_cli show-issue-at)"

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

TMPDIR_ROOT=$(mktemp -d -t im032-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# ---------------------------------------------------------------------------
# Build a synthetic issue store as a real git repo with backdated commits
# ---------------------------------------------------------------------------
STORE="$TMPDIR_ROOT/store"
mkdir -p "$STORE/IM" "$STORE/DOUG"

(
    cd "$STORE"
    git init --quiet --initial-branch=main
    git config user.email "verify@example.com"
    git config user.name "Verify User"
    git config commit.gpgsign false
)

# Three commits to IM-100.md with distinct content + backdates, so we
# can ask for state at each by SHA, tag, branch, and date.
write_and_commit() {
    local content="$1" message="$2" date="$3"
    echo "$content" > "$STORE/IM/IM-100.md"
    (
        cd "$STORE"
        git add IM/IM-100.md
        GIT_AUTHOR_DATE="$date" GIT_COMMITTER_DATE="$date" \
            git commit --quiet -m "$message"
    )
}

write_and_commit "v1 body" "feat: add IM-100"        "2026-01-15T12:00:00+0000"
SHA1=$(git -C "$STORE" rev-parse HEAD)
(cd "$STORE" && git tag v1)

write_and_commit "v2 body" "chore: update IM-100"    "2026-02-15T12:00:00+0000"
SHA2=$(git -C "$STORE" rev-parse HEAD)

write_and_commit "v3 body" "fix: correct IM-100"     "2026-03-15T12:00:00+0000"
SHA3=$(git -C "$STORE" rev-parse HEAD)

# IM-200 only added in the last commit -- exercises "did not exist at
# earlier ref"
echo "carol body" > "$STORE/IM/IM-200.md"
(
    cd "$STORE"
    git add IM/IM-200.md
    GIT_AUTHOR_DATE="2026-03-15T12:00:00+0000" GIT_COMMITTER_DATE="2026-03-15T12:00:00+0000" \
        git commit --quiet -m "feat: add IM-200"
)
SHA4=$(git -C "$STORE" rev-parse HEAD)

export ISSUE_STORE_ROOT="$STORE"

# ---------------------------------------------------------------------------
# Section A: --help / --version
# ---------------------------------------------------------------------------
echo
echo "[A] Help and version"

HELP_OUTPUT=$("$SHOW_ISSUE_AT" --help 2>&1)
assert_contains "show-issue-at" "$HELP_OUTPUT" "--help prints tool banner"
assert_contains "--at" "$HELP_OUTPUT" "--help documents --at flag"
assert_contains "--json" "$HELP_OUTPUT" "--help documents --json flag"
assert_contains "Examples:" "$HELP_OUTPUT" "--help has an Examples block"
assert_contains "ISSUE_STORE_ROOT" "$HELP_OUTPUT" "--help documents store mode"
assert_contains "out of scope for this surface" "$HELP_OUTPUT" "--help states the legacy-mode boundary"
assert_contains "git show" "$HELP_OUTPUT" "--help shows the legacy fallback command"

VERSION_OUTPUT=$("$SHOW_ISSUE_AT" --version)
assert_contains "v" "$VERSION_OUTPUT" "--version prints a version string"

# ---------------------------------------------------------------------------
# Section B: Resolve by SHA, branch, tag, HEAD
# ---------------------------------------------------------------------------
echo
echo "[B] Resolve by ref (SHA / branch / tag / HEAD)"

OUT=$("$SHOW_ISSUE_AT" IM-100 --at HEAD)
RC=$?
assert_eq "0" "$RC" "exit 0 for --at HEAD"
assert_contains "v3 body" "$OUT" "--at HEAD returns latest content"

OUT=$("$SHOW_ISSUE_AT" IM-100 --at main)
assert_contains "v3 body" "$OUT" "--at main returns latest content"

OUT=$("$SHOW_ISSUE_AT" IM-100 --at "$SHA1")
RC=$?
assert_eq "0" "$RC" "exit 0 for --at <full-sha>"
assert_contains "v1 body" "$OUT" "--at <SHA1> returns earliest content"

OUT=$("$SHOW_ISSUE_AT" IM-100 --at "${SHA2:0:8}")
assert_contains "v2 body" "$OUT" "--at <short-sha> returns middle content"

OUT=$("$SHOW_ISSUE_AT" IM-100 --at v1)
assert_contains "v1 body" "$OUT" "--at <tag> returns tagged content"

# ---------------------------------------------------------------------------
# Section C: Resolve by date (absolute and relative)
# ---------------------------------------------------------------------------
echo
echo "[C] Resolve by date"

# Absolute date between commits: 2026-02-01 is before SHA2 (2026-02-15)
# so the most recent commit at-or-before is SHA1.
OUT=$("$SHOW_ISSUE_AT" IM-100 --at 2026-02-01)
RC=$?
assert_eq "0" "$RC" "exit 0 for absolute date"
assert_contains "v1 body" "$OUT" \
    "--at 2026-02-01 returns SHA1 content (date is before SHA2)"

# Absolute date after middle commit: 2026-02-20 is after SHA2 but
# before SHA3, so should return SHA2 content.
OUT=$("$SHOW_ISSUE_AT" IM-100 --at 2026-02-20)
assert_contains "v2 body" "$OUT" "--at 2026-02-20 returns SHA2 content"

# Absolute date with time component
OUT=$("$SHOW_ISSUE_AT" IM-100 --at 2026-04-01T12:00:00)
assert_contains "v3 body" "$OUT" "--at <ISO datetime> returns SHA3 content"

# Relative form: "yesterday" -- all commits are well in the past
# (oldest is 2026-01-15), so yesterday should resolve to SHA4 (or the
# latest commit). For the IM-100 file, that means v3 body.
OUT=$("$SHOW_ISSUE_AT" IM-100 --at yesterday)
RC=$?
assert_eq "0" "$RC" "exit 0 for --at yesterday"
assert_contains "v3 body" "$OUT" "--at yesterday returns latest content"

# Relative form with .ago
OUT=$("$SHOW_ISSUE_AT" IM-100 --at 1.day.ago)
assert_contains "v3 body" "$OUT" "--at 1.day.ago returns latest content"

# ---------------------------------------------------------------------------
# Section D: File did not exist at ref -- clear error, not empty output
# ---------------------------------------------------------------------------
echo
echo "[D] File did not exist at ref"

# IM-200.md added in SHA4, ask for it at SHA1
OUT_STDOUT=$("$SHOW_ISSUE_AT" IM-200 --at "$SHA1" 2>/dev/null)
RC=$?
OUT_STDERR=$("$SHOW_ISSUE_AT" IM-200 --at "$SHA1" 2>&1 >/dev/null)
assert_eq "1" "$RC" "exit 1 when file did not exist at ref"
assert_eq "" "$OUT_STDOUT" "stdout is empty when file did not exist"
assert_contains "did not exist" "$OUT_STDERR" "stderr names 'did not exist'"
assert_contains "IM/IM-200.md" "$OUT_STDERR" "stderr names the file path"

# Same with date: 2026-02-01 predates IM-200's introduction
OUT_STDERR=$("$SHOW_ISSUE_AT" IM-200 --at 2026-02-01 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "exit 1 when file did not exist at date"
assert_contains "did not exist" "$OUT_STDERR" \
    "date-based 'did not exist' error is identical"

# ---------------------------------------------------------------------------
# Section E: --json shape
# ---------------------------------------------------------------------------
echo
echo "[E] --json shape"

JSON_OUT=$("$SHOW_ISSUE_AT" IM-100 --at "$SHA2" --json)
RC=$?
assert_eq "0" "$RC" "--json exits 0"
echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['id'] == 'IM-100', d
assert d['file'] == 'IM/IM-100.md', d
assert d['ref'] == '$SHA2', d
assert len(d['resolved_sha']) == 40, d
assert d['resolved_sha'] == '$SHA2', d
assert 'v2 body' in d['content'], d
print('json-shape-ok')
" >"$TMPDIR_ROOT/jsoncheck.out" 2>&1
if grep -q "json-shape-ok" "$TMPDIR_ROOT/jsoncheck.out"; then
    pass "--json carries id/file/ref/resolved_sha/content"
else
    fail "--json shape wrong: $(cat "$TMPDIR_ROOT/jsoncheck.out")"
fi

# --json + date: resolved_sha is the actual SHA the date pointed at,
# not the literal date string
JSON_OUT=$("$SHOW_ISSUE_AT" IM-100 --at 2026-02-20 --json)
echo "$JSON_OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d['ref'] == '2026-02-20', d
assert d['resolved_sha'] == '$SHA2', d  # date resolves to SHA2
print('json-date-ok')
" >"$TMPDIR_ROOT/jsoncheck2.out" 2>&1
if grep -q "json-date-ok" "$TMPDIR_ROOT/jsoncheck2.out"; then
    pass "--json with date stores ref verbatim, resolved_sha as actual SHA"
else
    fail "--json date shape wrong: $(cat "$TMPDIR_ROOT/jsoncheck2.out")"
fi

# ---------------------------------------------------------------------------
# Section F: Argument validation
# ---------------------------------------------------------------------------
echo
echo "[F] Argument validation"

# Missing ID
OUT_ERR=$("$SHOW_ISSUE_AT" 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "missing ID -> exit 2"
assert_contains "missing ID" "$OUT_ERR" "missing-ID error is explicit"

# Missing --at
OUT_ERR=$("$SHOW_ISSUE_AT" IM-100 2>&1 >/dev/null)
RC=$?
assert_eq "2" "$RC" "missing --at -> exit 2"
assert_contains "--at" "$OUT_ERR" "missing-at error mentions --at"

# Bad ID
# `im-030` (lowercase) case-folds to IM-030 and is no longer
# malformed. Only genuinely malformed shapes remain exit-2 here.
for bad_id in "I-030" "IM-" "IM030" "IM-03a" "foo"; do
    OUT_ERR=$("$SHOW_ISSUE_AT" "$bad_id" --at HEAD 2>&1 >/dev/null)
    RC=$?
    assert_eq "2" "$RC" "bad ID '$bad_id' -> exit 2"
    assert_contains "invalid ID" "$OUT_ERR" "bad ID '$bad_id' error names 'invalid ID'"
done

# ---------------------------------------------------------------------------
# Section G: Unresolvable refs and dates
# ---------------------------------------------------------------------------
echo
echo "[G] Unresolvable refs and dates"

# Unknown branch / ref
OUT_ERR=$("$SHOW_ISSUE_AT" IM-100 --at no-such-branch-xyz 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "unknown ref -> exit 1"
assert_contains "could not resolve" "$OUT_ERR" "unknown-ref error is explicit"

# Date predates all commits
OUT_ERR=$("$SHOW_ISSUE_AT" IM-100 --at 1990-01-01 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "date predating all commits -> exit 1"
assert_contains "no commit found" "$OUT_ERR" \
    "predating-date error names 'no commit found'"

# Critical guarantee: garbage that looks like an issue ID does NOT
# silently fall back to HEAD via approxidate. This is the load-bearing
# behavior that keeps `--at IM-030` (typo for `IM-030.md`?) from
# returning the wrong issue's HEAD content.
OUT_STDOUT=$("$SHOW_ISSUE_AT" IM-100 --at IM-030 2>/dev/null)
RC=$?
OUT_STDERR=$("$SHOW_ISSUE_AT" IM-100 --at IM-030 2>&1 >/dev/null)
assert_eq "1" "$RC" "garbage --at value -> exit 1 (NOT silent fallback to HEAD)"
assert_eq "" "$OUT_STDOUT" "garbage --at value -> empty stdout"
assert_contains "could not resolve" "$OUT_STDERR" \
    "garbage --at value error is explicit"

# ---------------------------------------------------------------------------
# Section H: Legacy mode error
# ---------------------------------------------------------------------------
echo
echo "[H] Legacy-mode error carries the git show fallback"

LEGACY_DIR="$TMPDIR_ROOT/legacy-cwd"
mkdir -p "$LEGACY_DIR"
(
    cd "$LEGACY_DIR"
    unset ISSUE_STORE_ROOT
    "$SHOW_ISSUE_AT" IM-030 --at v1.5.0 \
        2>"$TMPDIR_ROOT/legacy.err" \
        >"$TMPDIR_ROOT/legacy.out"
    echo $? > "$TMPDIR_ROOT/legacy.rc"
)
LEGACY_RC=$(cat "$TMPDIR_ROOT/legacy.rc")
LEGACY_ERR=$(cat "$TMPDIR_ROOT/legacy.err")
LEGACY_OUT=$(cat "$TMPDIR_ROOT/legacy.out")

assert_eq "1" "$LEGACY_RC" "legacy mode -> exit 1"
assert_eq "" "$LEGACY_OUT" "legacy mode keeps stdout empty"
assert_contains "store mode not active" "$LEGACY_ERR" \
    "legacy error names the precondition"
assert_contains "git show v1.5.0:.issues/IM-030.md" "$LEGACY_ERR" \
    "legacy error carries the exact fallback command (with the requested ID and ref)"
assert_contains "operates only on the issue store" "$LEGACY_ERR" \
    "legacy error names the store-only boundary"

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
    "$SHOW_ISSUE_AT" IM-100 --at HEAD >"$TMPDIR_ROOT/sentinel.out" 2>"$TMPDIR_ROOT/sentinel.err"
    echo $? > "$TMPDIR_ROOT/sentinel.rc"
)
SENTINEL_RC=$(cat "$TMPDIR_ROOT/sentinel.rc")
SENTINEL_OUT=$(cat "$TMPDIR_ROOT/sentinel.out")
assert_eq "0" "$SENTINEL_RC" "sentinel-mode happy-path -> exit 0"
assert_contains "v3 body" "$SENTINEL_OUT" \
    "sentinel-mode returns the same content as env-mode"

# ---------------------------------------------------------------------------
# Section J: Read-only -- no .locks/ entries created
# ---------------------------------------------------------------------------
echo
echo "[J] Read-only: no locks acquired"

"$SHOW_ISSUE_AT" IM-100 --at HEAD >/dev/null 2>&1
"$SHOW_ISSUE_AT" IM-100 --at "$SHA1" >/dev/null 2>&1
"$SHOW_ISSUE_AT" IM-100 --at 2026-02-01 --json >/dev/null 2>&1
"$SHOW_ISSUE_AT" IM-200 --at "$SHA1" >/dev/null 2>&1 || true

if find "$STORE" -type d -name ".locks" 2>/dev/null | grep -q .; then
    fail ".locks/ directory was created by show-issue-at (should not happen)"
else
    pass "no .locks/ directory created by show-issue-at"
fi

# ---------------------------------------------------------------------------
# Section K: Missing prefix dir / missing file at HEAD
# ---------------------------------------------------------------------------
echo
echo "[K] Missing prefix dir and missing file at HEAD"

# Prefix dir does not exist anywhere in history
OUT_ERR=$("$SHOW_ISSUE_AT" ZZZ-001 --at HEAD 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "missing prefix dir at HEAD -> exit 1"
assert_contains "did not exist" "$OUT_ERR" \
    "missing prefix at HEAD reports 'did not exist'"

# Prefix exists but file does not, at HEAD
OUT_ERR=$("$SHOW_ISSUE_AT" IM-999 --at HEAD 2>&1 >/dev/null)
RC=$?
assert_eq "1" "$RC" "missing issue file at HEAD -> exit 1"
assert_contains "did not exist" "$OUT_ERR" \
    "missing-file at HEAD reports 'did not exist'"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "----- show-issue-at verification summary -----"
echo "PASS: $PASS"
echo "FAIL: $FAIL"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
