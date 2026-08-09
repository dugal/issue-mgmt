#!/usr/bin/env bash
# migrations-record.sh -- pre-merge verification for IM-054.
#
# Exercises the new behavior: migrate-to-store drives
# two store-side git commits and appends a TSV line to
# <store>/.migrations capturing the source repo's canonical path,
# pre-migration HEAD sha, the migration commit sha, and an ISO 8601
# timestamp.
#
# Run from the project root:
#   tests/verification/migrations-record.sh
#
# Acceptance criteria covered:
#   - .migrations line written for every successful new migration
#   - Source-repo sha captured before any in-source write
#   - Canonical path used (mount-form / symlink paths normalize)
#   - Idempotent: re-run does not duplicate the line
#   - .migrations committed in a tightly-coupled follow-up commit
#   - Partial-prior-migration crash window is detected on re-run

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
SCRIPT="$(resolve_cli migrate-to-store)"

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t migrations-record.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

# Clean HOME so user config does not leak into tests.
export HOME="$WORKDIR/home"
mkdir -p "$HOME"

# Inline git identity for the commits the tool will drive.
export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="t@t"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="t@t"

mk_source_project() {
    # mk_source_project <name> <prefix> <count>
    # Initializes a real git repo with one initial commit, then writes
    # the issues. The HEAD sha at the moment migrate-to-store runs is
    # what should appear in .migrations.
    local name="$1" prefix="$2" count="$3"
    local dir="$WORKDIR/$name"
    mkdir -p "$dir"
    git init -q -b main "$dir"
    echo "init" > "$dir/README.md"
    git -C "$dir" add README.md
    git -C "$dir" commit -q -m init
    mkdir -p "$dir/.issues"
    echo "$prefix" > "$dir/.issues/.prefix"
    local i
    for ((i=1; i<=count; i++)); do
        local id
        id="$(printf '%s-%03d' "$prefix" "$i")"
        cat > "$dir/.issues/$id.md" <<EOF
---
id: $id
title: "Issue $id"
type: feature
status: open
priority: high
created: 2026-05-19
updated: 2026-05-19
branch: ""
tags: []
---

## Summary

Body for $id.
EOF
    done
    # Issues are intentionally NOT committed to the source repo -- the
    # tool captures HEAD as the pre-migration boundary, which precedes
    # these working-tree files.
    echo "$dir"
}

mk_store() {
    local dir="$WORKDIR/$1"
    mkdir -p "$dir"
    git init -q -b main "$dir"
    echo "store init" > "$dir/README.md"
    git -C "$dir" add README.md
    git -C "$dir" commit -q -m "store init"
    echo "$dir"
}

# --- Test 1: happy path writes one .migrations line ---
P1="$(mk_source_project p1 MP 2)"
S1="$(mk_store s1)"
SRC_PRE_SHA="$(git -C "$P1" rev-parse HEAD)"

"$SCRIPT" --project "$P1" --store "$S1" >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 ]] && pass "apply exits 0" || fail "apply exit (rc=$rc)"

# COVERS: migration.migrations-record
[[ -f "$S1/.migrations" ]] \
    && pass ".migrations file exists" \
    || fail ".migrations file missing"

line_count=$(wc -l < "$S1/.migrations" | tr -d ' ')
[[ "$line_count" == "1" ]] \
    && pass ".migrations has exactly one line (got $line_count)" \
    || fail ".migrations line count ($line_count)"

# --- Test 2: TSV shape ---
LINE="$(cat "$S1/.migrations")"
field_count=$(awk -F'\t' '{print NF}' <<<"$LINE")
[[ "$field_count" == "5" ]] \
    && pass ".migrations line has 5 tab-separated fields (got $field_count)" \
    || fail ".migrations line field count ($field_count)"

FIELD_PREFIX=$(awk -F'\t' '{print $1}' <<<"$LINE")
FIELD_PATH=$(awk -F'\t' '{print $2}' <<<"$LINE")
FIELD_SRC_SHA=$(awk -F'\t' '{print $3}' <<<"$LINE")
FIELD_STORE_SHA=$(awk -F'\t' '{print $4}' <<<"$LINE")
FIELD_WHEN=$(awk -F'\t' '{print $5}' <<<"$LINE")

[[ "$FIELD_PREFIX" == "MP" ]] \
    && pass "field 1 is the prefix (MP)" \
    || fail "field 1 prefix (got '$FIELD_PREFIX')"

# Source path must be the realpath of the project (canonical form).
expected_canonical="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$P1")"
[[ "$FIELD_PATH" == "$expected_canonical" ]] \
    && pass "field 2 is the canonical source path" \
    || fail "field 2 path (got '$FIELD_PATH', expected '$expected_canonical')"

[[ "$FIELD_SRC_SHA" == "$SRC_PRE_SHA" ]] \
    && pass "field 3 is the source pre-migration HEAD sha" \
    || fail "field 3 src sha (got '$FIELD_SRC_SHA', expected '$SRC_PRE_SHA')"

# Store sha is the migration commit's sha -- 40 hex chars.
if [[ "$FIELD_STORE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    pass "field 4 is a 40-char hex sha"
else
    fail "field 4 sha shape (got '$FIELD_STORE_SHA')"
fi

# ISO 8601: must contain T and a timezone marker (Z or +/-HH:MM).
if [[ "$FIELD_WHEN" == *"T"* ]] && [[ "$FIELD_WHEN" =~ (Z|[+-][0-9][0-9]:?[0-9][0-9])$ ]]; then
    pass "field 5 is ISO 8601 with timezone"
else
    fail "field 5 timestamp shape (got '$FIELD_WHEN')"
fi

# --- Test 3: store has two new commits from the migration ---
store_log=$(git -C "$S1" log --format="%H %s")
n_lines=$(echo "$store_log" | wc -l | tr -d ' ')
# Initial "store init" + migration commit + .migrations commit = 3.
[[ "$n_lines" == "3" ]] \
    && pass "store git log has 3 commits (init + migration + record)" \
    || fail "store git log line count ($n_lines)"

newest_subject=$(git -C "$S1" log -1 --format="%s")
[[ "$newest_subject" == "chore: record .migrations entry for MP" ]] \
    && pass "newest commit subject is the .migrations record commit" \
    || fail "newest commit subject (got '$newest_subject')"

migration_subject=$(git -C "$S1" log -1 --skip=1 --format="%s")
[[ "$migration_subject" == "feat: migrate p1 to store (MP-001..MP-002)" ]] \
    && pass "second-newest commit subject is the migration commit" \
    || fail "migration commit subject (got '$migration_subject')"

# The migration commit's sha must equal the FIELD_STORE_SHA value.
migration_sha=$(git -C "$S1" rev-parse HEAD~1)
[[ "$migration_sha" == "$FIELD_STORE_SHA" ]] \
    && pass "field 4 sha matches the migration commit sha" \
    || fail "field 4 != migration commit sha ($FIELD_STORE_SHA vs $migration_sha)"

# --- Test 4: source sentinel written last ---
[[ -f "$P1/.issues/.migrated-to-store" ]] \
    && pass "source sentinel written" \
    || fail "source sentinel missing"

# --- Test 5: idempotent re-run (sentinel present) is a no-op ---
out=$("$SCRIPT" --project "$P1" --store "$S1" 2>&1)
rc=$?
[[ $rc -eq 0 && "$out" == *"already migrated"* ]] \
    && pass "re-run with sentinel says 'already migrated' (rc=$rc)" \
    || fail "re-run with sentinel (rc=$rc out=$out)"

new_line_count=$(wc -l < "$S1/.migrations" | tr -d ' ')
[[ "$new_line_count" == "1" ]] \
    && pass "re-run with sentinel does not duplicate .migrations line" \
    || fail ".migrations line count after re-run ($new_line_count)"

# --- Test 6: partial-prior-migration crash window is detected ---
# Simulate the crash signature: dest issue files present but source
# sentinel missing.
rm "$P1/.issues/.migrated-to-store"
out=$("$SCRIPT" --project "$P1" --store "$S1" 2>&1)
rc=$?
[[ $rc -eq 1 && "$out" == *"partial prior migration"* ]] \
    && pass "partial-prior-migration detected (rc=$rc)" \
    || fail "partial-prior detection (rc=$rc out=$out)"

# Re-write the sentinel so subsequent tests don't trip the same path.
echo "$S1" > "$P1/.issues/.migrated-to-store"

# --- Test 7: canonical path resolves through a symlink ---
# Migrate a different project via a symlinked path. The recorded path
# must be the realpath of the source, not the symlink form.
P2_REAL="$(mk_source_project p2 SY 1)"
P2_LINK="$WORKDIR/p2-via-link"
ln -s "$P2_REAL" "$P2_LINK"
S2="$(mk_store s2)"

"$SCRIPT" --project "$P2_LINK" --store "$S2" >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 ]] && pass "symlinked project migration exits 0" \
    || fail "symlinked project migration (rc=$rc)"

recorded_path=$(awk -F'\t' '{print $2}' "$S2/.migrations")
expected_real="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$P2_LINK")"
[[ "$recorded_path" == "$expected_real" ]] \
    && pass "symlinked path normalized to realpath in .migrations" \
    || fail "symlinked path normalization (got '$recorded_path', expected '$expected_real')"

# --- Test 8: source not a git repo errors with a clear message ---
P3="$WORKDIR/p3-no-git"
mkdir -p "$P3/.issues"
echo "NG" > "$P3/.issues/.prefix"
cat > "$P3/.issues/NG-001.md" <<'EOF'
---
id: NG-001
title: "no git"
type: feature
status: open
priority: high
created: 2026-05-19
updated: 2026-05-19
branch: ""
tags: []
---

## Summary

Body.
EOF
S3="$(mk_store s3)"
out=$("$SCRIPT" --project "$P3" --store "$S3" 2>&1)
rc=$?
[[ $rc -eq 1 && "$out" == *"not a git repository"* ]] \
    && pass "source-not-a-git-repo errors with clear message (rc=$rc)" \
    || fail "no-git error (rc=$rc out=$out)"

# --- Test 9: --message override changes the migration commit subject ---
P4="$(mk_source_project p4 OV 1)"
S4="$(mk_store s4)"
"$SCRIPT" --project "$P4" --store "$S4" \
    --message "feat: migrate p4 -- DOUG-039 Stage X" >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 ]] && pass "--message apply exits 0" \
    || fail "--message apply (rc=$rc)"

migration_subject=$(git -C "$S4" log -1 --skip=1 --format="%s")
[[ "$migration_subject" == *"DOUG-039 Stage X"* ]] \
    && pass "--message override appears in migration commit subject" \
    || fail "--message override (got '$migration_subject')"

# .migrations second commit subject is fixed, not overridable.
record_subject=$(git -C "$S4" log -1 --format="%s")
[[ "$record_subject" == "chore: record .migrations entry for OV" ]] \
    && pass ".migrations record subject is unaffected by --message" \
    || fail "record subject (got '$record_subject')"

# --- Test 10: unrelated dirty state in store is preserved ---
P5="$(mk_source_project p5 UR 1)"
S5="$(mk_store s5)"
echo "unrelated work" > "$S5/stray.txt"
"$SCRIPT" --project "$P5" --store "$S5" >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 ]] && pass "apply with dirty store working tree exits 0" \
    || fail "apply with dirty store (rc=$rc)"

[[ -f "$S5/stray.txt" ]] \
    && pass "unrelated working-tree file still on disk after migration" \
    || fail "unrelated file gone after migration"

tracked=$(git -C "$S5" ls-files -- stray.txt)
[[ -z "$tracked" ]] \
    && pass "unrelated file not swept into a commit" \
    || fail "unrelated file got committed (tracked: $tracked)"

# --- Test 11: dry-run writes no .migrations line and no commits ---
P6="$(mk_source_project p6 DR 1)"
S6="$(mk_store s6)"
log_before=$(git -C "$S6" log --format="%H" | wc -l | tr -d ' ')
"$SCRIPT" --project "$P6" --store "$S6" --dry-run >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 ]] && pass "--dry-run exits 0" || fail "--dry-run (rc=$rc)"
[[ ! -f "$S6/.migrations" ]] \
    && pass "--dry-run did not write .migrations" \
    || fail "--dry-run wrote .migrations"
log_after=$(git -C "$S6" log --format="%H" | wc -l | tr -d ' ')
[[ "$log_before" == "$log_after" ]] \
    && pass "--dry-run produced no new store commits" \
    || fail "--dry-run added commits ($log_before -> $log_after)"

# --- Test 12: --json output exposes new fields ---
P7="$(mk_source_project p7 JS 1)"
S7="$(mk_store s7)"
json_out=$("$SCRIPT" --project "$P7" --store "$S7" --json 2>&1)
rc=$?
[[ $rc -eq 0 ]] && pass "--json apply exits 0" \
    || fail "--json apply (rc=$rc)"

python3 -c "
import json, sys
d = json.loads(sys.argv[1])
keys = ['source_pre_migration_sha', 'migration_commit_sha',
        'migrations_commit_sha', 'migrations_line', 'canonical_source_path']
for k in keys:
    assert d.get(k), f'missing or empty key: {k}'
assert d['canonical_source_path'].endswith('/p7'), d['canonical_source_path']
assert len(d['migration_commit_sha']) == 40
assert len(d['source_pre_migration_sha']) == 40
print('OK')
" "$json_out" >/dev/null 2>&1 \
    && pass "--json carries IM-054 fields (shas, line, canonical path)" \
    || fail "--json IM-054 fields (out=$json_out)"

# --- Summary ---
echo "---"
if [[ $fail_count -eq 0 ]]; then
    echo "ALL CHECKS PASSED"
    exit 0
else
    echo "$fail_count CHECK(S) FAILED"
    exit 1
fi
