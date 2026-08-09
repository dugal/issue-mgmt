#!/usr/bin/env bash
# migrate-to-store.sh -- pre-merge verification
#
# Exercises every acceptance criterion against the dev checkout's
# scripts/migrate-to-store. Builds throwaway projects + a synthetic
# store in a temp directory, prints PASS:/FAIL: per check, cleans up
# on exit, and exits non-zero on any FAIL.
#
# Acceptance criteria:
#   - Dry-run prints planned operations without modifying anything
#   - Byte-identical sha256 between source and destination
#   - Validation error aborts; nothing committed in the store
#   - .migrated-to-store sentinel is the single source of truth
#   - Re-running against an already-migrated project is a no-op
#     with "already migrated; sentinel at <path>"
#   - Cross-reference integrity: every XX-NNN token resolves to a
#     file under the store, or surfaces as a warning
#
# Run from the project root:
#   tests/verification/migrate-to-store.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
SCRIPT="$(resolve_cli migrate-to-store)"

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t migrate-to-store.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

# Need a clean HOME so any user config file does not leak into tests.
export HOME="$WORKDIR/home"
mkdir -p "$HOME"

# Git identity for the apply-path commits the tool drives.
export GIT_AUTHOR_NAME="Test"
export GIT_AUTHOR_EMAIL="t@t"
export GIT_COMMITTER_NAME="Test"
export GIT_COMMITTER_EMAIL="t@t"

mk_project() {
    # mk_project <name> <prefix> <count> [extra-md-body]
    # Initializes a git repo with one commit (the tool requires source HEAD).
    local name="$1" prefix="$2" count="$3" extra="${4:-}"
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
        local body="Issue $id body."
        if [[ $i -eq 1 && -n "$extra" ]]; then
            body="Issue $id body. $extra"
        fi
        cat > "$dir/.issues/$id.md" <<EOF
---
id: $id
title: "Issue $id"
type: feature
status: open
priority: high
created: 2026-05-15
updated: 2026-05-15
branch: ""
tags: []
---

## Summary

$body
EOF
    done
    echo "$dir"
}

mk_store() {
    # Initializes a git repo with one commit (the tool requires a HEAD for
    # the path-restricted migration commits).
    local dir="$WORKDIR/$1"
    mkdir -p "$dir"
    git init -q -b main "$dir"
    echo "store init" > "$dir/README.md"
    git -C "$dir" add README.md
    git -C "$dir" commit -q -m "store init"
    echo "$dir"
}

# --- Test 1: --help ---
"$SCRIPT" --help >/dev/null 2>&1 && pass "--help exits 0" \
    || fail "--help exits 0"

# --- Test 2: --version ---
v=$("$SCRIPT" --version 2>&1)
[[ "$v" =~ ^v[0-9] ]] && pass "--version prints v<digits> ($v)" \
    || fail "--version (got: $v)"

# --- Test 3: missing --project exits 2 ---
"$SCRIPT" >/dev/null 2>&1
rc=$?
[[ $rc -eq 2 ]] && pass "missing --project exits 2 (got $rc)" \
    || fail "missing --project (rc=$rc)"

# --- Test 4: no store resolvable exits 1 ---
P1="$(mk_project p1 MP 2)"
S1="$(mk_store s1)"
ISSUE_STORE_ROOT="" "$SCRIPT" --project "$P1" >/dev/null 2>&1
rc=$?
[[ $rc -eq 1 ]] && pass "no store resolvable exits 1 (got $rc)" \
    || fail "no store resolvable (rc=$rc)"

# --- Test 5: dry-run writes nothing ---
out=$("$SCRIPT" --project "$P1" --store "$S1" --dry-run 2>&1)
rc=$?
[[ $rc -eq 0 ]] && pass "dry-run exits 0" || fail "dry-run exit (rc=$rc)"
[[ "$out" == *"dry-run"* || "$out" == *"Plan (dry-run)"* ]] \
    && pass "dry-run output labels the plan" \
    || fail "dry-run label (out=$out)"
[[ ! -d "$S1/MP" ]] && pass "dry-run did not create store/MP/" \
    || fail "dry-run created store/MP/"
[[ ! -f "$P1/.issues/.migrated-to-store" ]] \
    && pass "dry-run did not write sentinel" \
    || fail "dry-run wrote sentinel"

# --- Test 6: apply happy path ---
"$SCRIPT" --project "$P1" --store "$S1" >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 ]] && pass "apply exits 0" || fail "apply exit (rc=$rc)"
# COVERS: migration.tool
[[ -f "$S1/MP/MP-001.md" && -f "$S1/MP/MP-002.md" ]] \
    && pass "apply copied MP-001.md and MP-002.md" \
    || fail "apply copied issues"
[[ -f "$S1/MP/.prefix" ]] && pass "apply copied .prefix companion" \
    || fail "apply copied .prefix"
[[ -f "$P1/.issues/.migrated-to-store" ]] \
    && pass "apply wrote sentinel" || fail "apply wrote sentinel"

# --- Test 7: sentinel contains store path ---
sentinel_content="$(cat "$P1/.issues/.migrated-to-store" 2>/dev/null)"
# Path comparison must allow macOS /var -> /private/var realpath rewrites,
# so use a Python equivalence check.
match=$(python3 -c "
import os, sys
a = os.path.realpath(sys.argv[1])
b = os.path.realpath(sys.argv[2])
print('yes' if a == b else 'no')
" "$sentinel_content" "$S1")
[[ "$match" == "yes" ]] \
    && pass "sentinel content matches store path (real path equal)" \
    || fail "sentinel content '$sentinel_content' vs '$S1'"

# --- Test 7b: sentinel preserves symlink form of --store ---
# When --store is a symlink, the sentinel must contain the symlink path,
# NOT the realpath. The routing primitive should be self-sufficient when
# the underlying mount disappears or maps differently on another machine.
P7B=$(mk_project p7b PSB 2)
S7B_REAL="$WORKDIR/store-7b-real"
S7B_LINK="$WORKDIR/store-7b-link"
mkdir -p "$S7B_REAL"
git init -q -b main "$S7B_REAL" && git -C "$S7B_REAL" commit -q --allow-empty -m init
ln -s "$S7B_REAL" "$S7B_LINK"
"$SCRIPT" --project "$P7B" --store "$S7B_LINK" >/dev/null 2>&1
rc=$?
sentinel_content="$(cat "$P7B/.issues/.migrated-to-store" 2>/dev/null)"
realpath_form="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$S7B_LINK")"
# COVERS: migration.sentinel-symlink
[[ $rc -eq 0 ]] && [[ "$sentinel_content" == "$S7B_LINK" ]] && [[ "$sentinel_content" != "$realpath_form" ]] \
    && pass "sentinel preserves symlink form of --store" \
    || fail "sentinel was '$sentinel_content'; expected '$S7B_LINK' (not realpath '$realpath_form')"

# --- Test 8: byte-identical sha256 ---
src_sha="$(shasum -a 256 "$P1/.issues/MP-001.md" | awk '{print $1}')"
dst_sha="$(shasum -a 256 "$S1/MP/MP-001.md" | awk '{print $1}')"
[[ "$src_sha" == "$dst_sha" ]] \
    && pass "byte-identical sha256 src vs dest" \
    || fail "sha256 mismatch ($src_sha vs $dst_sha)"

# --- Test 9: idempotent re-run exits 0 with 'already migrated' ---
out=$("$SCRIPT" --project "$P1" --store "$S1" 2>&1)
rc=$?
[[ $rc -eq 0 ]] && [[ "$out" == *"already migrated"* ]] \
    && pass "idempotent re-run says 'already migrated' (rc=$rc)" \
    || fail "idempotent re-run (rc=$rc out=$out)"

# --- Test 10: per-issue lock files exist after apply ---
[[ -f "$S1/MP/.locks/MP-001.lock" && -f "$S1/MP/.locks/MP-002.lock" ]] \
    && pass "per-issue lock files written to store/MP/.locks/" \
    || fail "per-issue lock files"

# --- Test 11: validation error aborts; nothing in store ---
P2="$(mk_project p2 ZQ 2)"
# Corrupt an issue: set status to a value not in ALLOWED_STATUSES.
python3 - "$P2/.issues/ZQ-002.md" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read().replace("status: open", "status: bogus")
open(path, "w").write(text)
PYEOF
S2="$(mk_store s2)"
out=$("$SCRIPT" --project "$P2" --store "$S2" 2>&1)
rc=$?
[[ $rc -eq 1 ]] && pass "validation error exits 1 (got $rc)" \
    || fail "validation error exit (rc=$rc)"
[[ "$out" == *"invalid-status"* ]] \
    && pass "validation stderr names invalid-status" \
    || fail "validation stderr (out=$out)"
[[ ! -d "$S2/ZQ" ]] \
    && pass "validation error: store/ZQ/ not created" \
    || fail "validation error: store/ZQ/ exists"
[[ ! -f "$P2/.issues/.migrated-to-store" ]] \
    && pass "validation error: sentinel not written" \
    || fail "validation error: sentinel exists"

# --- Test 12: cross-reference unresolved -> warning, not error ---
# Embed a reference to a DOUG-NNN that does not exist in the store.
P3="$(mk_project p3 PA 1 "See DOUG-099 for context.")"
S3="$(mk_store s3)"
out=$("$SCRIPT" --project "$P3" --store "$S3" 2>&1)
rc=$?
[[ $rc -eq 0 ]] \
    && pass "unresolved cross-ref does not error (rc=$rc)" \
    || fail "unresolved cross-ref errored (rc=$rc)"
# COVERS: migration.xref-warnings
[[ "$out" == *"DOUG-099"* && "$out" == *"warning"* ]] \
    && pass "unresolved cross-ref surfaces DOUG-099 warning" \
    || fail "unresolved cross-ref warning (out=$out)"

# --- Test 13: cross-reference resolved against existing store ---
# Pre-populate store with a DOUG-099 from another (fake) project.
S4="$(mk_store s4)"
mkdir -p "$S4/DOUG"
cat > "$S4/DOUG/DOUG-099.md" <<'EOF'
---
id: DOUG-099
title: "Other"
type: feature
status: done
priority: low
created: 2026-01-01
updated: 2026-01-01
---
EOF
P4="$(mk_project p4 PB 1 "See DOUG-099 for context.")"
out=$("$SCRIPT" --project "$P4" --store "$S4" 2>&1)
rc=$?
[[ $rc -eq 0 ]] && pass "resolved cross-ref apply succeeds (rc=$rc)" \
    || fail "resolved cross-ref (rc=$rc)"
[[ "$out" != *"warning"* ]] \
    && pass "resolved cross-ref produces no warning" \
    || fail "resolved cross-ref unexpected warning (out=$out)"

# --- Test 14: --prefix override + .prefix mismatch errors ---
P5="$(mk_project p5 PC 1)"
S5="$(mk_store s5)"
out=$("$SCRIPT" --project "$P5" --store "$S5" --prefix ZZ 2>&1)
rc=$?
[[ $rc -eq 1 && "$out" == *"mismatch"* ]] \
    && pass "--prefix vs .prefix mismatch errors (rc=$rc)" \
    || fail "--prefix mismatch (rc=$rc out=$out)"

# --- Test 15: --json shape (success path) ---
P6="$(mk_project p6 PD 1)"
S6="$(mk_store s6)"
json_out=$("$SCRIPT" --project "$P6" --store "$S6" --json 2>&1)
rc=$?
[[ $rc -eq 0 ]] && pass "--json success exits 0" || fail "--json exit (rc=$rc)"
python3 -c "
import json, sys
d = json.loads(sys.argv[1])
assert d['executed'] is True
assert d['sentinel_written'] is True
assert d['plan']['prefix'] == 'PD'
assert len(d['copies_succeeded']) >= 1
print('OK')
" "$json_out" >/dev/null 2>&1 \
    && pass "--json output is valid JSON with expected keys" \
    || fail "--json shape (out=$json_out)"

# --- Test 16: --json shape (already-migrated path) ---
json_out=$("$SCRIPT" --project "$P6" --store "$S6" --json 2>&1)
rc=$?
[[ $rc -eq 0 ]] && pass "--json already-migrated exits 0" \
    || fail "--json already-migrated exit (rc=$rc)"
python3 -c "
import json, sys
d = json.loads(sys.argv[1])
assert d['already_migrated'] is True
assert 'sentinel_path' in d
print('OK')
" "$json_out" >/dev/null 2>&1 \
    && pass "--json already-migrated has expected keys" \
    || fail "--json already-migrated shape (out=$json_out)"

# --- Test 17: no .issues/ dir errors ---
P7="$WORKDIR/no-issues"
mkdir -p "$P7"
out=$("$SCRIPT" --project "$P7" --store "$S1" 2>&1)
rc=$?
[[ $rc -eq 1 && "$out" == *".issues"* ]] \
    && pass "missing .issues/ errors (rc=$rc)" \
    || fail "missing .issues/ (rc=$rc out=$out)"

# --- Test 18: --project not a directory errors ---
out=$("$SCRIPT" --project "$WORKDIR/no-such-path" --store "$S1" 2>&1)
rc=$?
[[ $rc -eq 1 ]] && pass "missing project dir errors (rc=$rc)" \
    || fail "missing project dir (rc=$rc)"

# --- Test 19: registry-driven prefix when .prefix absent ---
P8="$WORKDIR/p8"
mkdir -p "$P8"
git init -q -b main "$P8" && echo init > "$P8/README.md" \
    && git -C "$P8" add README.md && git -C "$P8" commit -q -m init
mkdir -p "$P8/.issues"
cat > "$P8/.issues/RG-001.md" <<'EOF'
---
id: RG-001
title: "Reg-driven"
type: feature
status: open
priority: high
created: 2026-05-15
updated: 2026-05-15
branch: ""
tags: []
---

## Summary

Body.
EOF
S8="$(mk_store s8)"
echo "p8:RG" > "$S8/.repos"
"$SCRIPT" --project "$P8" --store "$S8" >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 && -f "$S8/RG/RG-001.md" ]] \
    && pass "registry-driven prefix (no .prefix) succeeds" \
    || fail "registry-driven prefix (rc=$rc)"

# --- Test 20: ISSUE_STORE_ROOT env resolution ---
P9="$(mk_project p9 PE 1)"
S9="$(mk_store s9)"
ISSUE_STORE_ROOT="$S9" "$SCRIPT" --project "$P9" >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 && -f "$S9/PE/PE-001.md" ]] \
    && pass "ISSUE_STORE_ROOT env resolves store" \
    || fail "env-resolved store (rc=$rc)"

# --- Test 21: extraneous file skipped, not copied ---
P10="$(mk_project p10 PF 1)"
echo "# notes" > "$P10/.issues/NOTES.md"
S10="$(mk_store s10)"
out=$("$SCRIPT" --project "$P10" --store "$S10" --dry-run 2>&1)
[[ "$out" == *"extraneous"* && "$out" == *"NOTES.md"* ]] \
    && pass "extraneous NOTES.md listed in dry-run plan" \
    || fail "extraneous list (out=$out)"
"$SCRIPT" --project "$P10" --store "$S10" >/dev/null 2>&1
[[ ! -f "$S10/PF/NOTES.md" ]] \
    && pass "extraneous NOTES.md not copied to store" \
    || fail "extraneous NOTES.md was copied"

# --- Test 22: ISSUE-TEMPLATE.md de-dup -- divergent local copied ---
# "# template" trivially differs from the canonical, so it counts as an
# override and lands in the store.
P11="$(mk_project p11 PG 1)"
echo "# template" > "$P11/.issues/ISSUE-TEMPLATE.md"
S11="$(mk_store s11)"
out=$("$SCRIPT" --project "$P11" --store "$S11" 2>&1)
[[ -f "$S11/PG/ISSUE-TEMPLATE.md" ]] \
    && pass "divergent template copied to store/<PFX>/" \
    || fail "divergent template not copied"
[[ "$out" == *"copied as override"* ]] \
    && pass "human output names template action 'copied as override'" \
    || fail "copy-action message (out=$out)"

# --- Test 22b: template matching canonical is skipped ---
# The canonical resolved by the CLI is <repo>/ISSUE-TEMPLATE.md (dev) when
# the Homebrew share path is not present; copy that file into the project
# anchor so sha256 matches and de-dup fires.
P11b="$(mk_project p11b PGB 1)"
cp "$REPO_ROOT/ISSUE-TEMPLATE.md" "$P11b/.issues/ISSUE-TEMPLATE.md"
S11b="$(mk_store s11b)"
out=$("$SCRIPT" --project "$P11b" --store "$S11b" 2>&1)
rc=$?
[[ $rc -eq 0 ]] && pass "skip-canonical exits 0 (rc=$rc)" \
    || fail "skip-canonical exit (rc=$rc)"
[[ ! -f "$S11b/PGB/ISSUE-TEMPLATE.md" ]] \
    && pass "matching template NOT copied to store/<PFX>/" \
    || fail "matching template was copied"
[[ "$out" == *"skipped -- matches canonical"* ]] \
    && pass "human output names template action 'skipped'" \
    || fail "skip-action message (out=$out)"

# --- Test 22c: template absent ---
P11c="$(mk_project p11c PGC 1)"
S11c="$(mk_store s11c)"
out=$("$SCRIPT" --project "$P11c" --store "$S11c" 2>&1)
[[ ! -f "$S11c/PGC/ISSUE-TEMPLATE.md" ]] \
    && pass "absent template -> nothing written" \
    || fail "absent template wrote something"
[[ "$out" == *"absent in source"* ]] \
    && pass "human output names template action 'absent'" \
    || fail "absent-action message (out=$out)"

# --- Test 22d: --json plan exposes template section ---
P11d="$(mk_project p11d PGD 1)"
cp "$REPO_ROOT/ISSUE-TEMPLATE.md" "$P11d/.issues/ISSUE-TEMPLATE.md"
S11d="$(mk_store s11d)"
json_out=$("$SCRIPT" --project "$P11d" --store "$S11d" --json --dry-run 2>&1)
rc=$?
[[ $rc -eq 0 ]] && pass "--json --dry-run exits 0" \
    || fail "--json --dry-run exit (rc=$rc)"
python3 -c "
import json, sys
d = json.loads(sys.argv[1])
t = d['plan']['template']
assert t['action'] == 'skip-canonical', t['action']
assert t['local_path'] is not None
assert t['canonical_path'] is not None
assert t['local_sha'] == t['canonical_sha']
print('OK')
" "$json_out" >/dev/null 2>&1 \
    && pass "--json template section has action/local_sha/canonical_sha" \
    || fail "--json template shape (out=$json_out)"

# --- Test 22e: store-root override wins over Homebrew canonical ---
# When <store>/ISSUE-TEMPLATE.md exists, it is the de-dup comparand; the
# local must match it (not Homebrew) for the skip path to fire.
P11e="$(mk_project p11e PGE 1)"
S11e="$(mk_store s11e)"
cat > "$S11e/ISSUE-TEMPLATE.md" <<'EOF'
---
id: PFX-NNN
title: "Custom store-wide template"
type: bug
status: open
priority: high
created: 2026-01-01
updated: 2026-01-01
---
EOF
# Local copy is byte-identical to <store>/ISSUE-TEMPLATE.md.
cp "$S11e/ISSUE-TEMPLATE.md" "$P11e/.issues/ISSUE-TEMPLATE.md"
out=$("$SCRIPT" --project "$P11e" --store "$S11e" 2>&1)
[[ "$out" == *"$S11e/ISSUE-TEMPLATE.md"* ]] \
    && pass "store-root template named as canonical" \
    || fail "store-root canonical not surfaced (out=$out)"
[[ ! -f "$S11e/PGE/ISSUE-TEMPLATE.md" ]] \
    && pass "skip fires against store-root canonical" \
    || fail "store-root match still wrote per-prefix copy"

# --- Test 23: --repo override changes registry lookup ---
P12="$WORKDIR/p12"
mkdir -p "$P12"
git init -q -b main "$P12" && echo init > "$P12/README.md" \
    && git -C "$P12" add README.md && git -C "$P12" commit -q -m init
mkdir -p "$P12/.issues"
cat > "$P12/.issues/RR-001.md" <<'EOF'
---
id: RR-001
title: "Repo override"
type: feature
status: open
priority: high
created: 2026-05-15
updated: 2026-05-15
branch: ""
tags: []
---
EOF
S12="$(mk_store s12)"
echo "different-name:RR" > "$S12/.repos"
"$SCRIPT" --project "$P12" --store "$S12" --repo different-name >/dev/null 2>&1
rc=$?
[[ $rc -eq 0 && -f "$S12/RR/RR-001.md" ]] \
    && pass "--repo override uses registry NAME (got rc=$rc)" \
    || fail "--repo override (rc=$rc)"

# --- Test 24: registry-absent warning surfaces in human output ---
P13="$(mk_project p13 PH 1)"
S13="$(mk_store s13)"
out=$("$SCRIPT" --project "$P13" --store "$S13" --dry-run 2>&1)
[[ "$out" == *"registry"*"ABSENT"* ]] \
    && pass "registry-absent surfaced in human output" \
    || fail "registry-absent line (out=$out)"

# --- Summary ---
echo "---"
if [[ $fail_count -eq 0 ]]; then
    echo "ALL CHECKS PASSED"
    exit 0
else
    echo "$fail_count CHECK(S) FAILED"
    exit 1
fi
