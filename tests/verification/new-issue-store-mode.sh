#!/usr/bin/env bash
# new-issue-store-mode.sh -- pre-merge verification
#
# Exercises every acceptance criterion against the dev checkout's
# scripts/new-issue.sh. Uses isolated temp git repos and temp stores;
# this repo's own .issues/ is never touched. Prints PASS:/FAIL: per
# check, cleans up, exits non-zero on any FAIL.
#
# Run from the project root:
#   tests/verification/new-issue-store-mode.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
SCRIPT="$(resolve_cli new-issue)"
TEMPLATE_SRC="$REPO_ROOT/ISSUE-TEMPLATE.md"

[[ -x "$SCRIPT" ]] || { echo "FAIL: $SCRIPT not executable"; exit 1; }
[[ -f "$TEMPLATE_SRC" ]] || { echo "FAIL: $TEMPLATE_SRC missing"; exit 1; }

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t new-issue-store.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

# Make the dev template addressable so $SCRIPT's "PREFIX/ISSUE-TEMPLATE.md"
# fallback works regardless of anchor.

mk_repo() {
    local name="$1"
    local path="$WORKDIR/$name"
    mkdir -p "$path/.issues"
    git -C "$path" init -q
    git -C "$path" config user.email test@example.com
    git -C "$path" config user.name test
    echo "$path"
}

mk_store() {
    local name="$1"
    local path="$WORKDIR/$name"
    mkdir -p "$path"
    git -C "$path" init -q
    git -C "$path" config user.email test@example.com
    git -C "$path" config user.name test
    git -C "$path" commit -q --allow-empty -m "init store"
    echo "$path"
}

# Build a fresh project + store and pre-register issue-mgmt:IM in the
# store. Returns the project path on stdout.
mk_registered_repo() {
    local repo_name="$1" store_path="$2" prefix="$3"
    local path
    path="$(mk_repo "$repo_name")"
    echo "$prefix" > "$path/.issues/.prefix"
    printf '%s:%s\n' "$repo_name" "$prefix" > "$store_path/.repos"
    echo "$path"
}

# --- Test 1: legacy mode (no env, no sentinel) -----------------------
REPO1="$(mk_repo legacy-proj)"
echo "IM" > "$REPO1/.issues/.prefix"
out=$( cd "$REPO1" && unset ISSUE_STORE_ROOT; "$SCRIPT" "Legacy smoke" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && ls "$REPO1/.issues/"IM-001.md >/dev/null 2>&1; then
    pass "legacy: file created at <anchor>/IM-001.md"
else
    fail "legacy: rc=$rc out=$out"
fi

# regression: the auto-reindex must place the just-created ID
# in the anchor's INDEX.md. Prior to the mutation pipeline the INDEX could be
# regenerated stale -- file existed, new ID missing.
if [[ -f "$REPO1/.issues/INDEX.md" ]] && grep -qF "| IM-001 " "$REPO1/.issues/INDEX.md"; then
    # COVERS: cli.auto-reindex
    pass "regression: legacy auto-reindex includes new ID in INDEX.md"
else
    fail "regression: IM-001 not in legacy INDEX.md after new-issue"
fi

# --- Test 2: store mode via env var, registered repo -----------------
STORE2="$(mk_store store-env)"
REPO2="$(mk_registered_repo issue-mgmt "$STORE2" IM)"
out=$( cd "$REPO2" && ISSUE_STORE_ROOT="$STORE2" "$SCRIPT" "Env test" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && ls "$STORE2/IM/"IM-001.md >/dev/null 2>&1; then
    # COVERS: substrate.store-relocation
    # COVERS: substrate.store-root-resolution
    # COVERS: substrate.id-stability
    pass "store/env: file lands under <store>/IM/"
else
    fail "store/env: rc=$rc out=$out (files: $(ls "$STORE2/IM/" 2>&1))"
fi
# Confirm no file leaked to the project anchor
if [[ ! -e "$REPO2/.issues/IM-001.md" ]]; then
    pass "store/env: no file leaked to <anchor>/.issues/"
else
    fail "store/env: file leaked to <anchor>/.issues/"
fi

# --- Test 3: store mode, unregistered repo --------------------------
STORE3="$(mk_store store-empty)"
REPO3="$(mk_repo unknown-proj)"
out=$( cd "$REPO3" && ISSUE_STORE_ROOT="$STORE3" "$SCRIPT" "Should fail" chore low 2>&1 )
rc=$?
if [[ $rc -ne 0 ]] && [[ "$out" == *"init-issue-project"* ]] \
   && [[ ! -e "$STORE3/UNKNOWN" ]] && [[ -z "$(ls -A "$STORE3" 2>/dev/null | grep -vE '^(\.repos|\.git)$')" ]]; then
    # COVERS: registry.unregistered-fail
    pass "store/env: unregistered repo fails pointing at init-issue-project"
else
    fail "store/env: unregistered repo (rc=$rc out=$out store=$(ls -A "$STORE3" 2>/dev/null))"
fi

# --- Test 4: store mode via sentinel ---------------------------------
STORE4="$(mk_store store-sentinel)"
REPO4="$(mk_registered_repo issue-mgmt "$STORE4" IM)"
echo "$STORE4" > "$REPO4/.issues/.migrated-to-store"
out=$( cd "$REPO4" && unset ISSUE_STORE_ROOT; "$SCRIPT" "Sentinel test" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && ls "$STORE4/IM/"IM-001.md >/dev/null 2>&1; then
    pass "store/sentinel: file lands under <store>/IM/"
else
    fail "store/sentinel: rc=$rc out=$out"
fi

# --- Test 5: cross-validation mismatch -------------------------------
STORE5="$(mk_store store-mismatch)"
REPO5="$(mk_repo issue-mgmt-mm)"
echo "XX" > "$REPO5/.issues/.prefix"          # local says XX
printf 'issue-mgmt-mm:IM\n' > "$STORE5/.repos" # registry says IM
out=$( cd "$REPO5" && ISSUE_STORE_ROOT="$STORE5" "$SCRIPT" "Mismatch" chore low 2>&1 )
rc=$?
if [[ $rc -ne 0 ]] && [[ "$out" == *"IM"* ]] && [[ "$out" == *"XX"* ]]; then
    # COVERS: registry.prefix-cross-validate
    pass "store: prefix mismatch fails naming both values (IM, XX)"
else
    fail "store: prefix mismatch (rc=$rc out=$out)"
fi

# --- Test 6: --repo NAME override -----------------------------------
STORE6="$(mk_store store-repo-override)"
REPO6="$(mk_repo some-other-basename)"
printf 'issue-mgmt:IM\n' > "$STORE6/.repos"
out=$( cd "$REPO6" && ISSUE_STORE_ROOT="$STORE6" "$SCRIPT" --repo issue-mgmt "Override" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && ls "$STORE6/IM/"IM-001.md >/dev/null 2>&1; then
    pass "--repo override: resolves via registry under <store>/IM/"
else
    fail "--repo override: rc=$rc out=$out"
fi

# --- Test 7: --help documents new surface ----------------------------
help_out=$("$SCRIPT" --help 2>&1)
missing=()
for term in ISSUE_STORE_ROOT ".repos" "--repo" "init-issue-project" "migrated-to-store"; do
    [[ "$help_out" == *"$term"* ]] || missing+=("$term")
done
if [[ ${#missing[@]} -eq 0 ]]; then
    pass "--help documents env var, registry, --repo, init-issue-project, sentinel"
else
    fail "--help missing terms: ${missing[*]}"
fi

# --- Test 8: --help and --version still exit 0 -----------------------
"$SCRIPT" --help >/dev/null 2>&1 && pass "--help exits 0" || fail "--help exits 0"
v=$("$SCRIPT" --version 2>&1)
[[ "$v" =~ ^v[0-9] ]] && pass "--version prints v<digits> ($v)" \
    || fail "--version prints v<digits> (got: $v)"

# --- Test 9: store mode, non-git cwd without --repo fails ------------
STORE9="$(mk_store store-nogit)"
NONGIT="$WORKDIR/nongit-cwd"
mkdir -p "$NONGIT"
printf 'issue-mgmt:IM\n' > "$STORE9/.repos"
out=$( cd "$NONGIT" && ISSUE_STORE_ROOT="$STORE9" "$SCRIPT" "Should fail" chore low 2>&1 )
rc=$?
if [[ $rc -ne 0 ]] && [[ "$out" == *"git"* || "$out" == *"--repo"* ]]; then
    pass "store: non-git cwd without --repo fails clearly"
else
    fail "store: non-git cwd without --repo (rc=$rc out=$out)"
fi

# --- Test 10: store mode resolves <store>/<PREFIX>/ISSUE-TEMPLATE.md ---
# Per-prefix override is the first step in the new resolution order; the
# new issue must pick up sections unique to that template body.
STORE10="$(mk_store store-prefix-tmpl)"
REPO10="$(mk_registered_repo issue-mgmt "$STORE10" IM)"
mkdir -p "$STORE10/IM"
cat > "$STORE10/IM/ISSUE-TEMPLATE.md" <<'EOF'
---
id: PFX-NNN
title: "Title"
type: bug
status: open
priority: high
created: 2026-01-01
updated: 2026-01-01
---

## PerPrefixSentinel
EOF
out=$( cd "$REPO10" && ISSUE_STORE_ROOT="$STORE10" "$SCRIPT" "Per-prefix" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && grep -q '^## PerPrefixSentinel$' "$STORE10/IM/IM-001.md"; then
    # COVERS: substrate.template-shipping
    pass "per-prefix template <store>/IM/ISSUE-TEMPLATE.md wins"
else
    fail "per-prefix template not used (rc=$rc out=$out)"
fi

# --- Test 11: store-root template wins when no per-prefix override ---
STORE11="$(mk_store store-root-tmpl)"
REPO11="$(mk_registered_repo issue-mgmt "$STORE11" IM)"
cat > "$STORE11/ISSUE-TEMPLATE.md" <<'EOF'
---
id: PFX-NNN
title: "Title"
type: bug
status: open
priority: high
created: 2026-01-01
updated: 2026-01-01
---

## StoreRootSentinel
EOF
out=$( cd "$REPO11" && ISSUE_STORE_ROOT="$STORE11" "$SCRIPT" "Store root" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && grep -q '^## StoreRootSentinel$' "$STORE11/IM/IM-001.md"; then
    pass "store-root template <store>/ISSUE-TEMPLATE.md wins when no per-prefix override"
else
    fail "store-root template not used (rc=$rc out=$out)"
fi

# --- Test 12: no store-side template falls through to dev/Homebrew ---
# With neither <store>/<PREFIX>/ nor <store>/ providing a template, the
# script must fall through to PREFIX/share/issue-mgmt/ISSUE-TEMPLATE.md
# (Homebrew) or PREFIX/ISSUE-TEMPLATE.md (dev). In this checkout the dev
# template has a known "## Summary" section.
STORE12="$(mk_store store-no-tmpl)"
REPO12="$(mk_registered_repo issue-mgmt "$STORE12" IM)"
out=$( cd "$REPO12" && ISSUE_STORE_ROOT="$STORE12" "$SCRIPT" "Fallthrough" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && grep -q '^## Summary$' "$STORE12/IM/IM-001.md"; then
    pass "store mode falls through to Homebrew/dev template"
else
    fail "store mode fallthrough (rc=$rc out=$out)"
fi

# --- Test 13: per-prefix override beats store-root override ---
STORE13="$(mk_store store-both-tmpl)"
REPO13="$(mk_registered_repo issue-mgmt "$STORE13" IM)"
cat > "$STORE13/ISSUE-TEMPLATE.md" <<'EOF'
---
id: PFX-NNN
title: "Title"
type: bug
status: open
priority: high
created: 2026-01-01
updated: 2026-01-01
---

## StoreRootSentinel
EOF
mkdir -p "$STORE13/IM"
cat > "$STORE13/IM/ISSUE-TEMPLATE.md" <<'EOF'
---
id: PFX-NNN
title: "Title"
type: bug
status: open
priority: high
created: 2026-01-01
updated: 2026-01-01
---

## PerPrefixSentinel
EOF
out=$( cd "$REPO13" && ISSUE_STORE_ROOT="$STORE13" "$SCRIPT" "Override priority" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] \
   && grep -q '^## PerPrefixSentinel$' "$STORE13/IM/IM-001.md" \
   && ! grep -q '^## StoreRootSentinel$' "$STORE13/IM/IM-001.md"; then
    pass "per-prefix override beats store-root override"
else
    fail "override priority (rc=$rc out=$out)"
fi

# --- Test 14: legacy mode does NOT consult anchor template -----
# Previously the anchor's ISSUE-TEMPLATE.md won; v2.0.0 dropped that step.
# In a legacy-mode project (no store), an anchor template is ignored and
# the script falls through to the dev template.
REPO14="$(mk_repo legacy-tmpl)"
echo "IM" > "$REPO14/.issues/.prefix"
cat > "$REPO14/.issues/ISSUE-TEMPLATE.md" <<'EOF'
---
id: PFX-NNN
title: "Title"
type: bug
status: open
priority: high
created: 2026-01-01
updated: 2026-01-01
---

## AnchorOverrideSentinel
EOF
out=$( cd "$REPO14" && unset ISSUE_STORE_ROOT; "$SCRIPT" "Legacy anchor" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] \
   && ! grep -q '^## AnchorOverrideSentinel$' "$REPO14/.issues/IM-001.md" \
   && grep -q '^## Summary$' "$REPO14/.issues/IM-001.md"; then
    pass "legacy mode no longer reads <anchor>/ISSUE-TEMPLATE.md (dropped step)"
else
    fail "legacy anchor template was still read (rc=$rc out=$out)"
fi

# --- Test 15: store-mode auto-reindex includes new ID in -----
# both <store>/<PREFIX>/INDEX.md and <store>/INDEX.md.
# Uses a git-managed store so the mutation pipeline can commit.
STORE15="$(mk_store store-autoreindex)"
mkdir -p "$STORE15/IM"
(
    cd "$STORE15"
    git init -q
    git config user.email test@example.com
    git config user.name test
    printf 'autoreindex-proj:IM\n' > .repos
    git add .repos
    git commit -q -m "init store"
)
REPO15="$(mk_repo autoreindex-proj)"
echo "IM" > "$REPO15/.issues/.prefix"
echo "$STORE15" > "$REPO15/.issues/.migrated-to-store"
out=$( cd "$REPO15" && unset ISSUE_STORE_ROOT; "$SCRIPT" "Auto-reindex smoke" chore low 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && [[ -f "$STORE15/IM/IM-001.md" ]]; then
    pass "IM-049 setup: store-mode new-issue created the file"
else
    fail "IM-049 setup: store-mode new-issue rc=$rc out=$out"
fi
if [[ -f "$STORE15/IM/INDEX.md" ]] && grep -qF "| IM-001 " "$STORE15/IM/INDEX.md"; then
    pass "regression: store auto-reindex includes new ID in per-prefix INDEX"
else
    fail "regression: IM-001 not in <store>/IM/INDEX.md after new-issue"
    [[ -f "$STORE15/IM/INDEX.md" ]] && {
        echo "--- per-prefix INDEX content ---"
        cat "$STORE15/IM/INDEX.md"
        echo "--------------------------------"
    }
fi
if [[ -f "$STORE15/INDEX.md" ]] && grep -qF "| IM-001 " "$STORE15/INDEX.md"; then
    pass "regression: store auto-reindex includes new ID in root INDEX aggregate"
else
    fail "regression: IM-001 not in <store>/INDEX.md aggregate after new-issue"
fi

# --- Summary ---------------------------------------------------------
echo "---"
if [[ $fail_count -eq 0 ]]; then
    echo "ALL CHECKS PASSED"
    exit 0
else
    echo "$fail_count CHECK(S) FAILED"
    exit 1
fi
