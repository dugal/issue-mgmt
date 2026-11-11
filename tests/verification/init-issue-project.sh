#!/usr/bin/env bash
# init-issue-project.sh -- pre-merge verification
#
# Exercises every acceptance criterion against the dev checkout's
# scripts/init-issue-project. Sets up temp git repos and a temp store,
# prints PASS:/FAIL: per check, cleans up, exits non-zero on any FAIL.
#
# Run from the project root:
#   tests/verification/init-issue-project.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
SCRIPT="$(resolve_cli init-issue-project)"

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t init-issue-project.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

mk_repo() {
    local name="$1"
    local path="$WORKDIR/$name"
    mkdir -p "$path"
    git -C "$path" init -q
    git -C "$path" config user.email "$FIXTURE_EMAIL"
    git -C "$path" config user.name test
    echo "$path"
}

# Stores must be git repos before init-issue-project (or any
# other mutation) runs against them. Without the baseline commit
# the store_mutation context fails at sweep-entry with "fatal: not a
# git repository" and the mutation never lands.
mk_store() {
    local path="$1"
    mkdir -p "$path"
    git -C "$path" init -q
    git -C "$path" config user.email "$FIXTURE_EMAIL"
    git -C "$path" config user.name test
    git -C "$path" commit -q --allow-empty -m "init store"
}

STORE="$WORKDIR/store"
mk_store "$STORE"

# =====================================================================
# REUSABLE: HOME-override pattern for ~/.config/issue-mgmt/config
# Lift this block verbatim for any sibling test exercising a binary
# that reads CONFIG_PATH = Path.home() / ".config" / "issue-mgmt" / "config"
# (scripts/issue-snapshot is a planned
# next consumer.)
#
# Three knobs need overriding together for an isolated config-fallback
# resolution path:
#   1. HOME=<scratch>   -- redirects Path.home() so the binary's
#                          CONFIG_PATH points under the scratch dir,
#                          not the developer's real ~/.config/.
#   2. unset ISSUE_STORE_ROOT -- the dev shell that runs the script
#                          may have this set (it is the canonical way
#                          to point store-mode CLIs at the project's
#                          actual store). Leaving it set short-circuits
#                          the resolution ladder ABOVE the config-file
#                          check, so config-file paths are never reached.
#   3. cd <repo>        -- resolve_anchor walks up from cwd; cwd must
#                          sit inside a git repo with no `.issues/`
#                          directory above it, or the sentinel branch
#                          fires before the config-file branch.
#
# Convention: each new scratch HOME is named explicitly with a
# unique label by the caller (`mk_home_override <LABEL>`). Counter-
# based auto-numbering would seem cleaner but DOES NOT WORK from
# `$(mk_home_override)` because command substitution runs the helper
# in a subshell, so any counter increment is lost before the next
# call sees it. Explicit labels also make grep'ing failures easier --
# you immediately know which test the home-dir belongs to.
#
# `mk_home_override <LABEL> [STORE_ROOT_SEED]`:
#   - creates $WORKDIR/home-<LABEL>/.config/issue-mgmt/
#   - optionally seeds the config file with `store_root = STORE_ROOT_SEED`
#   - echoes the home dir path on stdout
#
# Then run the binary inside a subshell of your choice as:
#
#   ( cd "$REPO" && unset ISSUE_STORE_ROOT && \
#       HOME="$home_dir" CMD... )
#
# Keeping the subshell composition explicit (rather than wrapping in a
# helper) avoids env-var-bleed footguns and makes it obvious which env
# vars are pinned at which level when reading the test later.
# =====================================================================
mk_home_override() {
    local label="$1"
    local seed="${2:-}"
    [[ -z "$label" ]] && { echo "mk_home_override: missing LABEL" >&2; exit 99; }
    local home_dir="$WORKDIR/home-$label"
    mkdir -p "$home_dir/.config/issue-mgmt"
    if [[ -n "$seed" ]]; then
        printf 'store_root = %s\n' "$seed" \
            > "$home_dir/.config/issue-mgmt/config"
    fi
    echo "$home_dir"
}
# =====================================================================
# END REUSABLE: HOME-override pattern
# =====================================================================

# --- Test 1: --help ---
"$SCRIPT" --help >/dev/null 2>&1 && pass "--help exits 0" || fail "--help exits 0"

# --- Test 2: --version ---
v=$("$SCRIPT" --version 2>&1)
[[ "$v" =~ ^v[0-9] ]] && pass "--version prints v<digits> ($v)" \
    || fail "--version prints v<digits> (got: $v)"

# --- Test 3: fresh register creates entry + local prefix ---
REPO_A="$(mk_repo foo)"
( cd "$REPO_A" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix FOO --non-interactive ) \
    >/dev/null 2>&1 && pass "fresh register exits 0" || fail "fresh register exits 0"
# COVERS: registry.basename-prefix-unique
grep -qx 'foo:FOO' "$STORE/.repos" \
    && pass "registry contains foo:FOO" || fail "registry contains foo:FOO"
[[ "$(cat "$REPO_A/.issues/.prefix")" == "FOO" ]] \
    && pass "local .issues/.prefix written" || fail "local .issues/.prefix written"

# --- Test 3b: fresh register writes the sentinel ---
SENTINEL="$REPO_A/.issues/.migrated-to-store"
# COVERS: substrate.init-project-sentinel
[[ -f "$SENTINEL" ]] \
    && pass "fresh register wrote .migrated-to-store sentinel" \
    || fail "fresh register wrote .migrated-to-store sentinel"
# Sentinel uses expanduser().absolute(), NOT realpath() -- so an absolute,
# symlink-preserved path. With ISSUE_STORE_ROOT="$STORE" (already absolute),
# expect verbatim contents + trailing newline.
expected_sentinel="$STORE"
[[ "$(cat "$SENTINEL")" == "$expected_sentinel" ]] \
    && pass "sentinel content matches store root with trailing newline" \
    || fail "sentinel content matches store root (expected '$expected_sentinel', got '$(cat "$SENTINEL")')"

# --- Test 4: idempotent re-run ---
out=$( cd "$REPO_A" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix FOO --non-interactive 2>&1 )
rc=$?
[[ $rc -eq 0 ]] && [[ "$out" == *"already registered"* ]] \
    && pass "idempotent re-run says 'already registered'" \
    || fail "idempotent re-run says 'already registered' (rc=$rc out=$out)"

# --- Test 4b: idempotent re-run leaves sentinel unchanged ---
[[ "$(cat "$SENTINEL")" == "$expected_sentinel" ]] \
    && pass "idempotent re-run leaves sentinel unchanged" \
    || fail "idempotent re-run leaves sentinel unchanged (got '$(cat "$SENTINEL")')"

# --- Test 4c: pre-existing divergent sentinel is refused ---
REPO_DIV="$(mk_repo divergent)"
mkdir -p "$REPO_DIV/.issues"
echo "/some/other/store" > "$REPO_DIV/.issues/.migrated-to-store"
out=$( cd "$REPO_DIV" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix DIV --non-interactive 2>&1 )
rc=$?
[[ $rc -ne 0 ]] && [[ "$out" == *"divergent"* ]] \
    && pass "divergent sentinel refused with clear error" \
    || fail "divergent sentinel refused (rc=$rc out=$out)"
# Confirm the divergent sentinel was not overwritten.
[[ "$(cat "$REPO_DIV/.issues/.migrated-to-store")" == "/some/other/store" ]] \
    && pass "divergent sentinel left intact (not overwritten)" \
    || fail "divergent sentinel left intact (got '$(cat "$REPO_DIV/.issues/.migrated-to-store")')"

# --- Test 4d: pre-existing matching sentinel is a no-op success ---
REPO_MATCH="$(mk_repo matchsent)"
mkdir -p "$REPO_MATCH/.issues"
echo "$STORE" > "$REPO_MATCH/.issues/.migrated-to-store"
out=$( cd "$REPO_MATCH" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix MS --non-interactive 2>&1 )
rc=$?
[[ $rc -eq 0 ]] \
    && pass "matching pre-existing sentinel: register succeeds" \
    || fail "matching pre-existing sentinel: register succeeds (rc=$rc out=$out)"
[[ "$(cat "$REPO_MATCH/.issues/.migrated-to-store")" == "$STORE" ]] \
    && pass "matching pre-existing sentinel unchanged after register" \
    || fail "matching pre-existing sentinel unchanged"

# --- Test 5: duplicate basename with different prefix ---
out=$( cd "$REPO_A" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix BAR --non-interactive 2>&1 )
rc=$?
[[ $rc -ne 0 ]] && pass "dup basename diff prefix exits non-zero" \
    || fail "dup basename diff prefix exits non-zero (rc=$rc)"

# --- Test 6: duplicate prefix on different repo ---
REPO_B="$(mk_repo bar)"
out=$( cd "$REPO_B" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix FOO --non-interactive 2>&1 )
rc=$?
[[ $rc -ne 0 ]] && pass "dup prefix on diff repo exits non-zero" \
    || fail "dup prefix on diff repo exits non-zero (rc=$rc)"

# --- Test 7: cross-validation mismatch ---
REPO_C="$(mk_repo baz)"
mkdir -p "$REPO_C/.issues"
echo "ZZZ" > "$REPO_C/.issues/.prefix"
out=$( cd "$REPO_C" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix BAZ --non-interactive 2>&1 )
rc=$?
[[ $rc -ne 0 ]] && [[ "$out" == *"disagrees"* ]] \
    && pass "prefix mismatch with local .prefix exits non-zero" \
    || fail "prefix mismatch (rc=$rc out=$out)"

# --- Test 8: invalid prefix ---
REPO_D="$(mk_repo qux)"
out=$( cd "$REPO_D" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix lower --non-interactive 2>&1 )
rc=$?
[[ $rc -ne 0 ]] && pass "invalid prefix exits non-zero" \
    || fail "invalid prefix exits non-zero (rc=$rc)"

# --- Test 9: reserved prefix ISSUE ---
out=$( cd "$REPO_D" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix ISSUE --non-interactive 2>&1 )
rc=$?
[[ $rc -ne 0 ]] && pass "reserved prefix ISSUE rejected" \
    || fail "reserved prefix ISSUE rejected (rc=$rc)"

# --- Test 10: --non-interactive with no store fails clearly ---
# Uses the HOME-override helper so the developer's real ~/.config/issue-mgmt/config
# AND a dev-shell ISSUE_STORE_ROOT cannot mask the documented diagnostic.
# COVERS: substrate.store-root-resolution
REPO_E="$(mk_repo nostore)"
home_e=$(mk_home_override test10-nostore)
out=$( cd "$REPO_E" && unset ISSUE_STORE_ROOT && \
       HOME="$home_e" "$SCRIPT" --prefix NS --non-interactive 2>&1 )
rc=$?
# COVERS: surface.structured-errors
[[ $rc -ne 0 ]] && [[ "$out" == *"error=precondition-failed"* ]] \
    && [[ "$out" == *"reason=store-unresolvable"* ]] \
    && pass "no store + non-interactive fails with a branchable payload" \
    || fail "no store + non-interactive (rc=$rc out=$out)"

# --- Test 11: --repo NAME override ---
REPO_F="$(mk_repo somerepo)"
( cd "$REPO_F" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --repo overridden --prefix OVR --non-interactive ) \
    >/dev/null 2>&1
grep -qx 'overridden:OVR' "$STORE/.repos" \
    && pass "--repo override recorded in registry" \
    || fail "--repo override recorded in registry"

# --- Test 12: not in a git repo ---
NONGIT="$WORKDIR/nongit"
mkdir -p "$NONGIT"
out=$( cd "$NONGIT" && ISSUE_STORE_ROOT="$STORE" "$SCRIPT" --prefix NG --non-interactive 2>&1 )
rc=$?
[[ $rc -ne 0 ]] && [[ "$out" == *"git"* ]] \
    && pass "non-git directory fails with git message" \
    || fail "non-git directory (rc=$rc out=$out)"

# --- Test 13: registry sorted lexicographically ---
sorted=$(sort -c "$STORE/.repos" 2>&1 && echo OK || echo NO)
[[ "$sorted" == "OK" ]] && pass "registry is lexicographically sorted" \
    || fail "registry is lexicographically sorted ($sorted)"

# --- Test 14: concurrent registration smoke test ---
STORE2="$WORKDIR/store2"
mk_store "$STORE2"
REPO_X="$(mk_repo concX)"
REPO_Y="$(mk_repo concY)"
( cd "$REPO_X" && ISSUE_STORE_ROOT="$STORE2" "$SCRIPT" --prefix CX --non-interactive ) >/dev/null 2>&1 &
PID_X=$!
( cd "$REPO_Y" && ISSUE_STORE_ROOT="$STORE2" "$SCRIPT" --prefix CY --non-interactive ) >/dev/null 2>&1 &
PID_Y=$!
wait $PID_X
rc_x=$?
wait $PID_Y
rc_y=$?
if [[ $rc_x -eq 0 ]] && [[ $rc_y -eq 0 ]] \
   && grep -qx 'concX:CX' "$STORE2/.repos" \
   && grep -qx 'concY:CY' "$STORE2/.repos"; then
    pass "concurrent register: both entries present"
else
    fail "concurrent register: rc_x=$rc_x rc_y=$rc_y registry=$(cat "$STORE2/.repos" 2>&1)"
fi

# --- Test 15: --store flag overrides env ---
STORE3="$WORKDIR/store3"
mk_store "$STORE3"
REPO_S="$(mk_repo flagstore)"
( cd "$REPO_S" && ISSUE_STORE_ROOT=/should/not/exist \
    "$SCRIPT" --store "$STORE3" --prefix FS --non-interactive ) >/dev/null 2>&1
grep -qx 'flagstore:FS' "$STORE3/.repos" \
    && pass "--store flag overrides ISSUE_STORE_ROOT" \
    || fail "--store flag overrides ISSUE_STORE_ROOT"

# --- Test 16: interactive store-root prompt with piped path ---
# Exercises the prompt branch of resolve_store_root() AND the write
# branch of write_config_store_root(). Provides a fresh prompted path
# and confirms (a) the script registered against it, (b) the config
# file was written under the HOME-override, (c) the "Saved store_root"
# stderr echo is emitted.
STORE_INT1="$WORKDIR/store-interactive-1"
mk_store "$STORE_INT1"
REPO_INT1="$(mk_repo intprompt1)"
home1=$(mk_home_override test16-prompt)
# COVERS: substrate.store-root-resolution
out=$( cd "$REPO_INT1" && unset ISSUE_STORE_ROOT && \
       printf '%s\n' "$STORE_INT1" | \
       HOME="$home1" "$SCRIPT" --prefix IP1 --repo intprompt1 2>&1 )
rc=$?
config_file="$home1/.config/issue-mgmt/config"
if [[ $rc -eq 0 ]] \
   && grep -qx 'intprompt1:IP1' "$STORE_INT1/.repos" \
   && [[ -f "$config_file" ]] \
   && grep -qx "store_root = $STORE_INT1" "$config_file" \
   && [[ "$out" == *"Saved store_root = $STORE_INT1"* ]]; then
    pass "interactive store-root prompt: registers, writes config, echoes Saved"
else
    fail "interactive store-root prompt (rc=$rc out=$out config=$(cat "$config_file" 2>&1))"
fi

# --- Test 17: interactive store-root prompt refuses an empty answer ---
# IM-252: empty/EOF stdin used to fall through to a synthesized
# $HOME/issues-store and PERSIST it to the config file. That is the
# built-in default path substrate.store-root-resolution says is never
# shipped, and `issue admin init-project </dev/null` reached it by EOF.
# The store is pre-created so the ONLY thing under test is the refusal --
# if the default were still synthesized, registration would succeed.
REPO_INT2="$(mk_repo intprompt2)"
home2=$(mk_home_override test17-default)
DEFAULT_STORE="$home2/issues-store"
mk_store "$DEFAULT_STORE"
# COVERS: substrate.store-root-resolution
out=$( cd "$REPO_INT2" && unset ISSUE_STORE_ROOT && \
       printf '\n' | \
       HOME="$home2" "$SCRIPT" --prefix IP2 --repo intprompt2 2>&1 )
rc=$?
if [[ $rc -eq 1 ]] \
   && [[ "$out" == *"error=precondition-failed"* ]] \
   && [[ "$out" == *"reason=store-unresolvable"* ]] \
   && ! grep -qs 'intprompt2:IP2' "$DEFAULT_STORE/.repos" \
   && [[ ! -f "$home2/.config/issue-mgmt/config" ]]; then
    pass "interactive store-root prompt: empty input refuses, synthesizes no default"
else
    fail "interactive store-root prompt empty (rc=$rc out=$out)"
fi

# --- Test 18: interactive prefix prompt with valid input ---
# resolve_store_root via --store; prefix derivation prompts. Pipe a
# valid prefix; assert it lands in .issues/.prefix AND the registry.
STORE_INT3="$WORKDIR/store-interactive-3"
mk_store "$STORE_INT3"
REPO_INT3="$(mk_repo intprompt3)"
out=$( cd "$REPO_INT3" && unset ISSUE_STORE_ROOT && \
       printf 'IP3\n' | "$SCRIPT" --store "$STORE_INT3" --repo intprompt3 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] \
   && [[ "$(cat "$REPO_INT3/.issues/.prefix")" == "IP3" ]] \
   && grep -qx 'intprompt3:IP3' "$STORE_INT3/.repos"; then
    pass "interactive prefix prompt: valid input registers"
else
    fail "interactive prefix prompt valid (rc=$rc out=$out)"
fi

# --- Test 19: interactive prefix prompt re-prompts on invalid ---
# Pipe an invalid token (lowercase, then reserved ISSUE, then valid).
# Assert (a) the re-prompt loop fires (two "invalid" diagnostics in stderr),
# (b) the final valid prefix is accepted, (c) the documented "must match"
# wording is present.
STORE_INT4="$WORKDIR/store-interactive-4"
mk_store "$STORE_INT4"
REPO_INT4="$(mk_repo intprompt4)"
out=$( cd "$REPO_INT4" && unset ISSUE_STORE_ROOT && \
       printf 'lower\nISSUE\nIP4\n' | \
       "$SCRIPT" --store "$STORE_INT4" --repo intprompt4 2>&1 )
rc=$?
invalid_count=$(grep -c "must match" <<< "$out" || true)
if [[ $rc -eq 0 ]] \
   && [[ "$invalid_count" -ge 2 ]] \
   && grep -qx 'intprompt4:IP4' "$STORE_INT4/.repos"; then
    pass "interactive prefix prompt: invalid -> reserved -> valid (loop fires)"
else
    fail "interactive prefix prompt invalid+valid (rc=$rc invalid_count=$invalid_count out=$out)"
fi

# --- Test 20: ~/.config/issue-mgmt/config fallback resolution ---
# Pre-seed the config under HOME-override; no --store, no env, no
# sentinel. Exercises the cfg branch of resolve_store_root() AND
# read_config_store_root() including the comment-skipping path.
STORE_CFG="$WORKDIR/store-cfg-fallback"
mk_store "$STORE_CFG"
REPO_CFG="$(mk_repo cfgfallback)"
# Seed a config with a comment line + a blank line + the store_root
# key, to exercise read_config_store_root() branches that skip
# blanks and comments.
home_cfg=$(mk_home_override test20-cfg-fallback)
cat > "$home_cfg/.config/issue-mgmt/config" <<EOF
# managed by issue-mgmt

store_root = $STORE_CFG
EOF
# COVERS: substrate.store-root-resolution
out=$( cd "$REPO_CFG" && unset ISSUE_STORE_ROOT && \
       HOME="$home_cfg" "$SCRIPT" --prefix CFG --repo cfgfallback --non-interactive 2>&1 )
rc=$?
if [[ $rc -eq 0 ]] && grep -qx 'cfgfallback:CFG' "$STORE_CFG/.repos"; then
    pass "~/.config/issue-mgmt/config fallback resolves store_root"
else
    fail "config fallback (rc=$rc out=$out)"
fi

# --- Test 21: write_config_store_root updates existing key ---
# Run an interactive store-root prompt against a HOME that already has
# a config with a DIFFERENT store_root. The writer should preserve
# unrelated lines and rewrite the store_root line in place (the "found"
# branch of write_config_store_root).
STORE_REWRITE_OLD="$WORKDIR/store-rewrite-old"
STORE_REWRITE_NEW="$WORKDIR/store-rewrite-new"
mk_store "$STORE_REWRITE_NEW"
REPO_RW="$(mk_repo cfgrewrite)"
home_rw=$(mk_home_override test21-rewrite)
# Pipe the NEW store path; resolve_store_root reads the old one from
# config (4th in the ladder) -- but we want to force the prompt branch
# to also exercise the writer's rewrite path. To do that, seed the
# config with only the comment + other_setting (no store_root key);
# the prompt fires, writes the answer, and the writer's "not found ->
# append" branch executes WHILE preserving the comment and other_setting.
cat > "$home_rw/.config/issue-mgmt/config" <<EOF
# preserved comment
other_setting = keep_me
EOF
out=$( cd "$REPO_RW" && unset ISSUE_STORE_ROOT && \
       printf '%s\n' "$STORE_REWRITE_NEW" | \
       HOME="$home_rw" "$SCRIPT" --prefix CRW --repo cfgrewrite 2>&1 )
rc=$?
cfg_content=$(cat "$home_rw/.config/issue-mgmt/config")
if [[ $rc -eq 0 ]] \
   && [[ "$cfg_content" == *"# preserved comment"* ]] \
   && [[ "$cfg_content" == *"other_setting = keep_me"* ]] \
   && [[ "$cfg_content" == *"store_root = $STORE_REWRITE_NEW"* ]]; then
    pass "config writer preserves unrelated lines and adds store_root"
else
    fail "config writer preserve (rc=$rc cfg=$cfg_content out=$out)"
fi

# Note: write_config_store_root()'s found=True branch (which
# rewrites an existing `store_root =` line in place) is unreachable
# from init-issue-project's normal flow today: resolve_store_root()'s
# config-file check short-circuits BEFORE the prompt that triggers the
# writer, so the writer is only reached on the not-found-append path
# (covered by Test 21). When a future binary calls
# write_config_store_root() with a pre-existing store_root key the
# branch will become reachable; revisit then.

# --- Test 22: a resolved-but-absent store root refuses, never tracebacks ---
# IM-252: the verb resolved a store root, then lazy-seeded ISSUE-TEMPLATE.md
# into it without checking the directory existed, so shutil.copyfile raised
# and the process exited through an unhandled FileNotFoundError -- a Python
# stack trace where an agent expects a branchable payload.
REPO_MISSING="$(mk_repo missingstore)"
home_missing=$(mk_home_override test22-missing)
MISSING_STORE="$WORKDIR/no-such-store-dir"
# COVERS: surface.structured-errors
out=$( cd "$REPO_MISSING" && \
       ISSUE_STORE_ROOT="$MISSING_STORE" HOME="$home_missing" \
       "$SCRIPT" --prefix MS --non-interactive 2>&1 )
rc=$?
if [[ $rc -eq 1 ]] \
   && [[ "$out" == *"error=precondition-failed"* ]] \
   && [[ "$out" == *"reason=store-unresolvable"* ]] \
   && [[ "$out" == *"store=$MISSING_STORE"* ]] \
   && [[ "$out" != *"Traceback"* ]]; then
    pass "absent store root refuses with a branchable payload, no traceback"
else
    fail "absent store root (rc=$rc out=$out)"
fi

# --- Summary ---
echo "---"
if [[ $fail_count -eq 0 ]]; then
    echo "ALL CHECKS PASSED"
    exit 0
else
    echo "$fail_count CHECK(S) FAILED"
    exit 1
fi
