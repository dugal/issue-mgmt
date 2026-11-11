#!/usr/bin/env bash
# tests/verification/lib.sh -- shared harness bootstrap.
#
# Sourced (never executed) by every verification script:
#
#     . "$(dirname "$0")/lib.sh"
#
# Defines:
#   PROJECT_ROOT    -- repo root. Set only if the caller has not already
#                      set it, so scripts that compute their own root
#                      (or use a different variable name) are unaffected.
#   ISSUE_MGMT_BIN  -- directory holding the CLIs under test. Defaults
#                      to $PROJECT_ROOT/scripts; override to aim the
#                      whole suite at an alternate implementation
#                      (cross-stack conformance):
#
#                          ISSUE_MGMT_BIN=/path/to/alt/bin \
#                              tests/verification/edit-issue.sh
#
#   resolve_cli NAME -- print the path of CLI NAME under ISSUE_MGMT_BIN,
#                      preferring the extensionless name and falling back
#                      to NAME.sh (the legacy bash entry points
#                      new-issue.sh / reindex-issues.sh keep their
#                      extension in scripts/; an alternate implementation
#                      ships extensionless binaries). Fails loudly with a
#                      diagnostic on stderr when neither exists.
#
#   ISSUE_MGMT_CLI_STYLE -- selects the CLI surface resolve_cli targets
#                      (v3.0.0 consolidation):
#                        consolidated  (DEFAULT) -- the
#                                         single `issue <verb>` dispatcher.
#                                         resolve_cli maps each flat NAME to
#                                         its `issue <verb>` form and returns
#                                         a generated shim that execs
#                                         `issue <verb> "$@"`, so call sites
#                                         of the shape
#                                             CLI="$(resolve_cli new-issue)"
#                                             "$CLI" ...
#                                         drive the dispatcher unchanged.
#                        flat          -- the 21 flat binaries. DEFUNCT on
#                                         this tree post-WP7: the flat names
#                                         are now tombstone stubs (redirect +
#                                         exit 2), so flat mode resolves to a
#                                         stub. Retained only for an alternate
#                                         implementation supplied via
#                                         ISSUE_MGMT_BIN that still ships flat
#                                         binaries, or for running the suite
#                                         against a pre-v3.0.0 tag.
#                      The default flipped from `flat` to `consolidated` in
#                      WP7: once the flat binaries became tombstones
#                      the dispatcher is the only working surface, so a bare
#                      `tests/verification/foo.sh` run must target it. The
#                      full flat->verb mapping lives here so the whole suite
#                      flips with one env var and NO fan-out WP has to touch a
#                      verification script or this file.
#
#   shipped_skill_names / shipped_skill_count
#                   -- the ground-truth shipped skill set, derived from
#                      .claude/skills/<name>-dev/ (IM-264). Every link in
#                      the skill chain reads these instead of restating a
#                      literal; see the definitions at the end of this file.
#
#   FIXTURE_EMAIL   -- the one throwaway git identity for the whole suite
#                      (IM-268). A script that needs `git commit` to not
#                      refuse uses this; it must NOT invent its own address.
#                      Every distinct address needs its own hand-written
#                      .publish.yaml `leaks.allow` entry (the publish gate's
#                      email detector suppresses a hit only on a full-address
#                      match), and three of four authors forgot. Derived here
#                      from tests/fixture_identity.py, which is the single
#                      source of truth that scripts/check-fixture-emails
#                      enforces against the whole public view.
#
# Deliberately out of scope here: env handling (ISSUE_STORE_ROOT
# hygiene stays per-script), assert helpers, temp-dir management. This
# file owns binary resolution plus the ground-truth derivations the whole
# suite shares (the shipped skill set, the fixture git identity) -- anything
# a second script would otherwise restate as a literal. The Makefile
# coverage-black-box loop
# skips this file by name -- it is a library, not a test.
#
# macOS bash 3.2 compatible: no arrays beyond BASH_SOURCE, no locals
# required, no GNU-isms.

_VERIFY_LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [ -z "${PROJECT_ROOT:-}" ]; then
    PROJECT_ROOT=$(cd "$_VERIFY_LIB_DIR/../.." && pwd)
fi
ISSUE_MGMT_BIN="${ISSUE_MGMT_BIN:-$PROJECT_ROOT/scripts}"

# The suite's one throwaway git identity, DERIVED from its single source of
# truth rather than restated here -- two agreeing declarations is the shape
# IM-261 catalogued, and a second copy is a second thing to forget.
#
# Fails the sourcing script loudly rather than leaving FIXTURE_EMAIL empty:
# `git config user.email ""` succeeds, so an empty value would surface much
# later as an unrelated commit failure.
FIXTURE_EMAIL=$(sed -n 's/^FIXTURE_EMAIL = "\([^"]*\)".*$/\1/p' \
    "$PROJECT_ROOT/tests/fixture_identity.py" 2>/dev/null | head -1)
if [ -z "$FIXTURE_EMAIL" ]; then
    echo "tests/verification/lib.sh: cannot read FIXTURE_EMAIL from" \
         "$PROJECT_ROOT/tests/fixture_identity.py -- refusing to run with" \
         "no fixture git identity" >&2
    exit 1
fi

# flat binary NAME -> `issue <verb>` words (v3.0.0; design section 2a).
# `transition-issue` maps to the `issue transition` primitive, which
# re-hosts the flat binary's whole arg surface (state positional AND the
# --claim/--renew/--release lease ops), so this stays a clean 1:1 mapping.
_consolidated_verb_for() {
    case "$1" in
        new-issue)                 echo "create" ;;
        transition-issue)          echo "transition" ;;
        edit-issue)                echo "edit" ;;
        issue-note)                echo "note" ;;
        issue-dep)                 echo "dep" ;;
        search-issues)             echo "search" ;;
        issue-show)                echo "show" ;;
        issue-ready)               echo "ready" ;;
        issue-history)             echo "history show" ;;
        search-history)            echo "history search" ;;
        show-issue-at)             echo "history at" ;;
        activity-issues)           echo "history activity" ;;
        reindex-issues)            echo "admin reindex" ;;
        validate-issue)            echo "admin validate" ;;
        issue-snapshot)            echo "admin snapshot" ;;
        issue-reap-expired-claims) echo "admin reap" ;;
        init-issue-store)          echo "admin init-store" ;;
        init-issue-project)        echo "admin init-project" ;;
        migrate-to-store)          echo "admin migrate" ;;
        issue-mgmt-selftest)       echo "admin selftest" ;;
        issue-mgmt-install-skills) echo "admin install-skills" ;;
        *)                         return 1 ;;
    esac
}

# Per-sourcing shim directory (one small dir per verification-script
# process when consolidated mode is used). Not trap-cleaned on purpose:
# lib.sh is sourced, and a cleanup trap here would clobber the caller's
# own EXIT trap. The dirs are tiny and land in TMPDIR.
_CONSOLIDATED_SHIM_DIR=""

_resolve_cli_consolidated() {
    _verb="$(_consolidated_verb_for "$1")" || {
        echo "tests/verification/lib.sh: no consolidated mapping for CLI '$1'" \
            "(ISSUE_MGMT_CLI_STYLE=consolidated)" >&2
        return 1
    }
    _issue_bin="$ISSUE_MGMT_BIN/issue"
    if [ ! -x "$_issue_bin" ]; then
        echo "tests/verification/lib.sh: consolidated 'issue' binary not found" \
            "at $_issue_bin" >&2
        return 1
    fi
    if [ -z "$_CONSOLIDATED_SHIM_DIR" ]; then
        _CONSOLIDATED_SHIM_DIR=$(mktemp -d "${TMPDIR:-/tmp}/im-consolidated-shims.XXXXXX") \
            || return 1
    fi
    _shim="$_CONSOLIDATED_SHIM_DIR/$1"
    # Regenerate each call so a changed ISSUE_MGMT_BIN is honored.
    {
        printf '#!/usr/bin/env bash\n'
        printf 'exec "%s" %s "$@"\n' "$_issue_bin" "$_verb"
    } > "$_shim" || return 1
    chmod +x "$_shim" || return 1
    printf '%s\n' "$_shim"
}

resolve_cli() {
    if [ "${ISSUE_MGMT_CLI_STYLE:-consolidated}" = "consolidated" ]; then
        _resolve_cli_consolidated "$1"
        return
    fi
    if [ -x "$ISSUE_MGMT_BIN/$1" ]; then
        printf '%s\n' "$ISSUE_MGMT_BIN/$1"
    elif [ -x "$ISSUE_MGMT_BIN/$1.sh" ]; then
        printf '%s\n' "$ISSUE_MGMT_BIN/$1.sh"
    else
        echo "tests/verification/lib.sh: cannot resolve CLI '$1' under ISSUE_MGMT_BIN=$ISSUE_MGMT_BIN" >&2
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Ground-truth shipped skill set (IM-264).
#
# The canonical (non-dev) skill names, derived from the only place the
# project actually declares them: .claude/skills/<name>-dev/. That is what
# the Makefile stages into the tarball as skills/<name>.md and what the
# formula installs into <pkgshare>/skills/.
#
# Every link in the chain derives from this instead of restating it. A hand
# literal here drifted silently for two releases: im-029-homebrew.sh's
# EXPECTED_SKILLS declared 13 names from v2.0.0 while the project shipped 15,
# and it was the guard for gen-formula's own hand list -- two hand lists
# compared to each other, neither to ground truth.
#
# Fails loudly rather than printing nothing: an empty derivation would make
# every "exactly this set" assertion downstream vacuous, which is the failure
# mode this function exists to prevent.
shipped_skill_names() {
    _ssn=$(ls -d "$PROJECT_ROOT"/.claude/skills/*-dev 2>/dev/null \
            | xargs -n1 basename | sed 's/-dev$//' | sort)
    if [ -z "$_ssn" ]; then
        echo "tests/verification/lib.sh: no skills found under" \
             "$PROJECT_ROOT/.claude/skills/*-dev -- refusing to report an" \
             "empty skill set" >&2
        return 1
    fi
    printf '%s\n' "$_ssn"
}

shipped_skill_count() {
    shipped_skill_names | wc -l | tr -d '[:space:]'
}
