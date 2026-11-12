#!/usr/bin/env bash
# Verification script for IM-029 Homebrew formula + issue-mgmt-install-skills.
#
# Covers IM-029 acceptance criteria that can run without performing a real
# `brew install`. The packaged surface is the single `issue` dispatcher.
#   - The `issue` dispatcher exists, is executable, and answers --help /
#     --version
#   - gen-formula emits a formula that installs `issue`, the lib/ directory
#     (incl the lib/cli/ verb package), ISSUE-TEMPLATE.md, the skills
#     DIRECTORY, and LICENSE / CHANGELOG / README; the caveats block points
#     at `issue admin install-skills`; the test block exercises `issue`
#   - install-skills, given a synthetic pkgshare and an
#     HOME-pointed-at-a-tmp-dir, populates ~/.claude/skills/<name>/SKILL.md
#     for every shipped skill with byte-identical content
#   - install-skills exits non-zero when the pkgshare is missing
#     or empty
#
# Every skill name and count below is DERIVED from the SHIPPED sources
# share/issue-mgmt/skills/*.md via lib.sh's shipped_skill_names (IM-264;
# re-pointed off the derived .claude/skills/*-dev copies in IM-276, which is
# the defect this script could not see while both sides of its comparison
# came from the dev tree). A hand-maintained EXPECTED_SKILLS
# literal lived here from v2.0.0 and declared 13 of the 15 shipped skills,
# while being the only guard for gen-formula's own hand list -- two hand
# lists compared to each other, neither to ground truth.
#
# The final post-publish step (real `make install` + the user's own
# Claude Code restart) is a separate manual gate -- this script does NOT
# touch the real ~/.claude/skills/, the Homebrew tap, or the dist/ dir.
#
# Run from the project root:
#     tests/verification/im-029-homebrew.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
GEN_FORMULA="$PROJECT_ROOT/scripts/gen-formula"
INSTALL_SKILLS="$(resolve_cli issue-mgmt-install-skills)"

PASS=0
FAIL=0

pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

assert_eq() {
    if [ "$1" = "$2" ]; then
        pass "$3 (=$2)"
    else
        fail "$3: expected '$1', got '$2'"
    fi
}

assert_contains() {
    case "$2" in
        *"$1"*) pass "$3 (matched '$1')" ;;
        *) fail "$3: '$1' not found in haystack" ;;
    esac
}

assert_not_contains() {
    case "$2" in
        *"$1"*) fail "$3: unexpectedly contained '$1'" ;;
        *) pass "$3 (no '$1')" ;;
    esac
}

TMPDIR_ROOT=$(mktemp -d -t im029-verify.XXXXXX)
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

# The single `issue` dispatcher is the only installed binary; the formula
# bin.install-s just `issue`.
DISPATCHER=issue
EXPECTED_BINS=("$DISPATCHER")

# Ground truth, derived (lib.sh). Newline-delimited; bash 3.2 has no
# process substitution into an array without a temp file, and a space-safe
# skill name is guaranteed by the directory-name grammar, so word splitting
# over the list is safe here.
SHIPPED_SKILLS=$(shipped_skill_names) || exit 1
SHIPPED_SKILL_COUNT=$(shipped_skill_count)

# ---------------------------------------------------------------------------
# Block 1: the `issue` dispatcher is present and self-executing -- it answers
#          --help and --version cleanly.
# ---------------------------------------------------------------------------
echo "== Block 1: bin sources are present and self-executing =="

ISSUE_BIN="$ISSUE_MGMT_BIN/issue"
if [ ! -x "$ISSUE_BIN" ]; then
    fail "issue dispatcher missing or not executable: $ISSUE_BIN"
else
    # COVERS: general.executability
    "$ISSUE_BIN" --help >/dev/null 2>&1 && pass "issue --help exits 0" \
        || fail "issue --help failed"
    "$ISSUE_BIN" --version >/dev/null 2>&1 && pass "issue --version exits 0" \
        || fail "issue --version failed"
fi

# ---------------------------------------------------------------------------
# Block 2: gen-formula emits a formula with the expected install block,
#          caveats, and test block. Uses a stub tarball so no real dist build
#          is required.
# ---------------------------------------------------------------------------
echo "== Block 2: gen-formula emits the right install block =="

STUB_TARBALL="$TMPDIR_ROOT/issue-mgmt-v1.5.1.tar.gz"
: > "$STUB_TARBALL"

FORMULA_OUT="$TMPDIR_ROOT/issue-mgmt.rb"
if "$GEN_FORMULA" "$STUB_TARBALL" > "$FORMULA_OUT" 2>"$TMPDIR_ROOT/gen-formula.stderr"; then
    pass "gen-formula exits 0 against a stub tarball"
else
    fail "gen-formula failed: $(cat "$TMPDIR_ROOT/gen-formula.stderr")"
fi

FORMULA_BODY=$(cat "$FORMULA_OUT")

# COVERS: principles.p1
# COVERS: dist.formula-bundling
for bin in "${EXPECTED_BINS[@]}"; do
    assert_contains "bin.install \"$bin\"" "$FORMULA_BODY" "formula installs $bin"
done

assert_contains "prefix.install \"lib\"" "$FORMULA_BODY" "formula installs lib/"
assert_contains "(share/\"issue-mgmt\").install \"ISSUE-TEMPLATE.md\"" "$FORMULA_BODY" "formula installs ISSUE-TEMPLATE.md"
# COVERS: dist.cheatsheet
assert_contains "(share/\"issue-mgmt\").install \"CHEATSHEET.md\"" "$FORMULA_BODY" "formula installs CHEATSHEET.md"
assert_contains "doc.install \"CHANGELOG.md\"" "$FORMULA_BODY" "formula installs CHANGELOG.md"
assert_contains "doc.install \"README.md\"" "$FORMULA_BODY" "formula installs README.md"

# The formula installs the skills DIRECTORY, so it names no skill and cannot
# drift from the shipped set. These two assertions pin that property: the
# directive is present, and no per-skill filename survives anywhere in the
# formula. A declaration that does not exist cannot go stale -- which is the
# IM-264 fix, as distinct from correcting a stale list.
# COVERS: dist.skills-one-per-cli
assert_contains '(share/"issue-mgmt").install "skills"' "$FORMULA_BODY" \
    "formula installs the skills directory"
# COVERS: dist.skills-one-per-cli
if printf '%s\n' "$FORMULA_BODY" | grep -q 'skills/[a-z-]*\.md'; then
    fail "formula enumerates individual skill files (re-introduces the IM-264 hand list)"
else
    pass "formula enumerates no individual skill file"
fi

# The three independent declarations of the pkgshare skills path must agree:
# the Makefile stages into $(STAGE)/skills, the formula installs "skills"
# under share/issue-mgmt, and install-skills reads
# <brew-prefix>/share/issue-mgmt/skills. A directory install only ships the
# skills if all three spell the same path; nothing else checks that.
# COVERS: dist.skills-install-binary
grep -q '\$(STAGE)/skills' "$PROJECT_ROOT/Makefile" \
    && pass "Makefile stages skills into \$(STAGE)/skills" \
    || fail "Makefile no longer stages skills into \$(STAGE)/skills"
# COVERS: dist.skills-install-binary
grep -q '"share" / "issue-mgmt" / "skills"' "$PROJECT_ROOT/lib/cli/admin_install_skills.py" \
    && pass "install-skills reads <brew-prefix>/share/issue-mgmt/skills" \
    || fail "install-skills pkgshare path no longer matches the formula install target"

# LICENSE is tracked and the formula declares license "MIT", but it reached
# no packaging list from v3.1.0 until IM-264, so a brew-installed
# issue-mgmt carried no license text.
# COVERS: dist.formula-bundling
assert_contains 'doc.install "LICENSE"' "$FORMULA_BODY" "formula installs LICENSE"

assert_contains "def caveats" "$FORMULA_BODY" "formula has caveats block"
assert_contains "issue admin install-skills" "$FORMULA_BODY" "caveats reference 'issue admin install-skills'"
assert_contains "test do" "$FORMULA_BODY" "formula has a test block"
# COVERS: dist.formula-bundling
assert_contains "bin.install \"issue\"" "$FORMULA_BODY" "formula installs the issue dispatcher"
assert_contains "#{bin}/issue" "$FORMULA_BODY" "formula test block exercises the issue dispatcher"
# The single dispatcher is the only installed binary -- no flat-name stubs.
assert_not_contains "bin.install \"new-issue\"" "$FORMULA_BODY" "formula does not install a new-issue stub"
assert_not_contains "bin.install \"transition-issue\"" "$FORMULA_BODY" "formula does not install a transition-issue stub"

# ---------------------------------------------------------------------------
# Block 3: issue-mgmt-install-skills, given a synthetic pkgshare and an
#          isolated HOME, populates ~/.claude/skills/<name>/SKILL.md for
#          every shipped skill with byte-identical content.
# ---------------------------------------------------------------------------
echo "== Block 3: issue-mgmt-install-skills works against a synthetic pkgshare =="

FAKE_BREW_PREFIX="$TMPDIR_ROOT/brew-prefix"
FAKE_HOME="$TMPDIR_ROOT/home"
mkdir -p "$FAKE_BREW_PREFIX/bin" "$FAKE_BREW_PREFIX/share/issue-mgmt/skills" "$FAKE_HOME"

# Drop a stub `brew` on PATH that always points the installer at our fake
# prefix. The installer relies on `brew --prefix`; we shadow it.
cat > "$FAKE_BREW_PREFIX/bin/brew" <<EOF
#!/usr/bin/env bash
[ "\$1" = "--prefix" ] && echo "$FAKE_BREW_PREFIX" && exit 0
exit 1
EOF
chmod +x "$FAKE_BREW_PREFIX/bin/brew"

# Populate the synthetic pkgshare from ground truth, each file carrying a
# unique sentinel line so we can confirm an exact copy.
for name in $SHIPPED_SKILLS; do
    printf 'sentinel-for-%s\n' "$name" > "$FAKE_BREW_PREFIX/share/issue-mgmt/skills/$name.md"
done

# The cheat sheet ships one level up from the skills dir. Drop a sentinel copy
# so we can confirm install-skills surfaces it next to the installed skills.
printf 'cheatsheet-sentinel\n' > "$FAKE_BREW_PREFIX/share/issue-mgmt/CHEATSHEET.md"

HOME="$FAKE_HOME" PATH="$FAKE_BREW_PREFIX/bin:$PATH" \
    "$INSTALL_SKILLS" > "$TMPDIR_ROOT/install.out" 2> "$TMPDIR_ROOT/install.err"
RC=$?
# COVERS: dist.skills-install-binary
assert_eq "0" "$RC" "issue-mgmt-install-skills exit code"

for name in $SHIPPED_SKILLS; do
    dest="$FAKE_HOME/.claude/skills/$name/SKILL.md"
    if [ ! -f "$dest" ]; then
        fail "skill not installed: $name (expected $dest)"
        continue
    fi
    expected="sentinel-for-$name"
    actual=$(cat "$dest")
    assert_eq "$expected" "$actual" "skill $name installed with correct contents"
done

# The "Done. N skill(s) installed." footer is what a developer sees and is
# the simplest health signal. The count is interpolated from ground truth;
# it read a hard-coded 13 from v2.0.0 through v3.3.1.
assert_contains "$SHIPPED_SKILL_COUNT skill(s) installed" "$(cat "$TMPDIR_ROOT/install.out")" \
    "installer summary reports $SHIPPED_SKILL_COUNT skills"

# The installed set must be EXACTLY the shipped set, not merely a superset:
# an installer that silently skipped a file would pass every per-name check
# above, since each only asserts its own name is present.
INSTALLED_SKILLS=$(ls -d "$FAKE_HOME"/.claude/skills/*/ 2>/dev/null \
                   | xargs -n1 basename | sort)
# COVERS: dist.skills-install-binary
assert_eq "$SHIPPED_SKILLS" "$INSTALLED_SKILLS" \
    "installed skill set is exactly the shipped set"

# The cheat sheet is copied next to the installed skills, and its install
# path is printed so an agent/operator can find it on disk.
CHEAT_DEST="$FAKE_HOME/.claude/skills/issue-mgmt-CHEATSHEET.md"
# COVERS: dist.cheatsheet
if [ -f "$CHEAT_DEST" ]; then
    pass "cheat sheet installed next to skills ($CHEAT_DEST)"
    # COVERS: dist.cheatsheet
    assert_eq "cheatsheet-sentinel" "$(cat "$CHEAT_DEST")" "cheat sheet copied with correct contents"
else
    fail "cheat sheet not installed (expected $CHEAT_DEST)"
fi
# COVERS: dist.cheatsheet
assert_contains "issue-mgmt-CHEATSHEET.md" "$(cat "$TMPDIR_ROOT/install.out")" "installer prints the cheat-sheet install path"

# ---------------------------------------------------------------------------
# Block 4: issue-mgmt-install-skills failure modes.
# ---------------------------------------------------------------------------
echo "== Block 4: install-skills error paths =="

# Missing pkgshare directory.
EMPTY_PREFIX="$TMPDIR_ROOT/empty-prefix"
mkdir -p "$EMPTY_PREFIX/bin"
cat > "$EMPTY_PREFIX/bin/brew" <<EOF
#!/usr/bin/env bash
[ "\$1" = "--prefix" ] && echo "$EMPTY_PREFIX" && exit 0
exit 1
EOF
chmod +x "$EMPTY_PREFIX/bin/brew"

HOME="$TMPDIR_ROOT/home2" PATH="$EMPTY_PREFIX/bin:$PATH" \
    "$INSTALL_SKILLS" > "$TMPDIR_ROOT/missing.out" 2> "$TMPDIR_ROOT/missing.err"
assert_eq "1" "$?" "install-skills exits 1 when pkgshare missing"
assert_contains "skills directory not found" "$(cat "$TMPDIR_ROOT/missing.err")" "stderr names the missing directory"

# pkgshare present but empty -> exit 1, warning.
EMPTY_PKGSHARE="$TMPDIR_ROOT/empty-pkgshare"
mkdir -p "$EMPTY_PKGSHARE/bin" "$EMPTY_PKGSHARE/share/issue-mgmt/skills"
cat > "$EMPTY_PKGSHARE/bin/brew" <<EOF
#!/usr/bin/env bash
[ "\$1" = "--prefix" ] && echo "$EMPTY_PKGSHARE" && exit 0
exit 1
EOF
chmod +x "$EMPTY_PKGSHARE/bin/brew"

HOME="$TMPDIR_ROOT/home3" PATH="$EMPTY_PKGSHARE/bin:$PATH" \
    "$INSTALL_SKILLS" > "$TMPDIR_ROOT/empty.out" 2> "$TMPDIR_ROOT/empty.err"
assert_eq "1" "$?" "install-skills exits 1 when pkgshare empty"
assert_contains "no skill files found" "$(cat "$TMPDIR_ROOT/empty.err")" "stderr names the empty-pkgshare condition"

# Unknown argument -> exit 2.
HOME="$TMPDIR_ROOT/home4" PATH="$FAKE_BREW_PREFIX/bin:$PATH" \
    "$INSTALL_SKILLS" --bogus > "$TMPDIR_ROOT/bogus.out" 2> "$TMPDIR_ROOT/bogus.err"
assert_eq "2" "$?" "install-skills exits 2 on unknown argument"
assert_contains "Unknown argument" "$(cat "$TMPDIR_ROOT/bogus.err")" "stderr names the unknown argument"

# ---------------------------------------------------------------------------
# Block 5: installed-layout mutation refreshes INDEX.md in-process
#          (supersedes the earlier sibling-subprocess test).
#
# Pre-v3.0.0 the mutating flat binaries regenerated INDEX.md by shelling out
# to a sibling `reindex-issues[.sh]`, and a name mismatch across the .sh
# boundary silently skipped it. The consolidated `issue` dispatcher
# regenerates IN-PROCESS (reindex.reindex_after_mutation), so that failure
# class cannot occur -- there is no sibling name to resolve. This block builds
# a synthetic INSTALLED layout (bin/issue + lib/ incl the lib/cli/ verb
# package + share/) and asserts a mutating verb refreshes both INDEX.md files.
# ---------------------------------------------------------------------------
echo "== Block 5: installed-layout mutation refreshes INDEX.md in-process (WP7) =="

# This block copies lib/ into a throwaway install prefix and runs the
# dispatcher out of it. Under `make coverage-black-box` the armed python
# children record coverage against those ephemeral `*/lib/*.py` paths (they
# match the .coveragerc include glob), and the temp tree is deleted on exit
# -- so the source is gone by `coverage json` time. coverage-model's
# `[measures.black-box] ignore_errors = true` skips those phantom
# entries at report time instead of aborting the whole measure.

if ! command -v git >/dev/null 2>&1; then
    fail "git not found on PATH (required for the installed-layout store)"
else
    PKG="$TMPDIR_ROOT/pkg"
    mkdir -p "$PKG/bin" "$PKG/lib/cli" "$PKG/share/issue-mgmt/store-template"

    # Mirror the Makefile staging + formula bin.install: the `issue`
    # dispatcher plus the whole lib/ tree, including the lib/cli/ verb
    # package the dispatcher discovers at runtime.
    cp "$PROJECT_ROOT/scripts/issue" "$PKG/bin/issue"
    chmod +x "$PKG/bin/issue"
    cp "$PROJECT_ROOT"/lib/*.py "$PROJECT_ROOT"/lib/*.sh "$PKG/lib/"
    cp "$PROJECT_ROOT"/lib/cli/*.py "$PKG/lib/cli/"
    cp "$PROJECT_ROOT/ISSUE-TEMPLATE.md"           "$PKG/share/issue-mgmt/"
    cp "$PROJECT_ROOT"/share/store-template/*.md   "$PKG/share/issue-mgmt/store-template/"

    IDX_HOME="$TMPDIR_ROOT/idx-home"
    IDX_STORE="$TMPDIR_ROOT/idx-store"
    IDX_PROJ="$TMPDIR_ROOT/idx-proj"
    mkdir -p "$IDX_HOME"

    # Fully isolated: HOME + git identity + ISSUE_STORE_ROOT all pinned to
    # the temp tree so the operator store and config are never touched.
    (
        export HOME="$IDX_HOME"
        export ISSUE_STORE_ROOT="$IDX_STORE"
        git config --global user.name  "im189-verify" 2>/dev/null
        git config --global user.email "$FIXTURE_EMAIL" 2>/dev/null
        git config --global init.defaultBranch main 2>/dev/null

        "$PKG/bin/issue" admin init-store "$IDX_STORE" --non-interactive >/dev/null 2>&1
        mkdir -p "$IDX_PROJ" && cd "$IDX_PROJ" && git init -q
        "$PKG/bin/issue" admin init-project --repo idx-proj --prefix IDX --non-interactive >/dev/null 2>&1
    ) || fail "installed-layout store scaffold/registration failed"

    # Create through the installed dispatcher and capture the new ID.
    NEW_OUT="$TMPDIR_ROOT/idx-new.out"
    (
        export HOME="$IDX_HOME"
        export ISSUE_STORE_ROOT="$IDX_STORE"
        cd "$IDX_PROJ"
        "$PKG/bin/issue" create "installed layout reindex probe" chore low
    ) > "$NEW_OUT" 2> "$TMPDIR_ROOT/idx-new.err"
    NEW_ID=$(grep -oE 'IDX-[0-9]+' "$NEW_OUT" | head -1)

    if [ -z "$NEW_ID" ]; then
        fail "installed-layout 'issue create' did not report a created IDX-NNN id"
        cat "$TMPDIR_ROOT/idx-new.err"
    else
        pass "installed-layout 'issue create' created $NEW_ID"
        # COVERS: cli.auto-reindex
        assert_contains "$NEW_ID" "$(cat "$IDX_STORE/IDX/INDEX.md" 2>/dev/null)" \
            "per-prefix INDEX.md lists $NEW_ID after installed-layout create (in-process reindex)"
        # COVERS: cli.auto-reindex
        assert_contains "$NEW_ID" "$(cat "$IDX_STORE/INDEX.md" 2>/dev/null)" \
            "store-root INDEX.md lists $NEW_ID after installed-layout create (in-process reindex)"
    fi
fi

# ---------------------------------------------------------------------------
# Summary + manual gate prompt.
# ---------------------------------------------------------------------------
echo ""
echo "PASS: $PASS  FAIL: $FAIL"
echo ""

if [ "$FAIL" -ne 0 ]; then
    echo "FAIL: pre-merge checks failed; do not run make install yet."
    exit 1
fi

cat <<EOF
All automated pre-merge checks passed.

Manual end-to-end install:

  make install
  issue admin install-skills   # printed by 'make install' already; rerun if curious

Then in a fresh shell, confirm the dispatcher is on PATH and reports the
stamped version:

  issue --version

And confirm all $SHIPPED_SKILL_COUNT skills landed under ~/.claude/skills/
as '<name>/SKILL.md' (no -dev suffix) -- this prints the diff, so empty
output means the installed set matches what this tree ships:

  diff <(ls share/issue-mgmt/skills/*.md | xargs -n1 basename | sed 's/\.md\$//' | sort) \\
       <(ls -d ~/.claude/skills/issue-*/ | xargs -n1 basename | sort)

And that no installed body carries the project-local alias (0 expected):

  grep -rc -- '-dev' ~/.claude/skills/*/SKILL.md | awk -F: '{ n += \$2 } END { print n + 0 }'
EOF
