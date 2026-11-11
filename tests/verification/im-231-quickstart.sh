#!/usr/bin/env bash
# Verification: the public README Quickstart works as written, and the project
# git-repo precondition is enforced with an actionable error.
#
# Motivation (IM-231): the shipped selftest git-inits its temp project
# internally, so it never exercised the DOCUMENTED onboarding sequence --
# which omitted `git init` and failed for a new project.
#
# Rewritten for IM-266, because the guard this file used to be could not catch
# the next Quickstart break and did not catch it. It claimed in its own header
# to run the quickstart "exactly as the README documents it" while being a
# hand-written re-implementation that had drifted from the README in three
# ways, one of them exactly where the bug lived:
#
#   - it SKIPPED README step 4 entirely (its comments jumped from "Step 3" to
#     "Step 5"), so `issue start` / `issue complete` never ran and `ready` was
#     called while the issue was still open -- accidentally testing the
#     correct order while the README documented a broken one;
#   - it passed `--repo MYP` where the README documents `--prefix MYP`
#     (`--repo` is the transitional synonym IM-237 retired and v3.5.0 drops);
#   - it passed `--non-interactive` to init-store/init-project, which the
#     README does not.
#
# So the README is now the ground truth: section A EXTRACTS the fenced block
# under `## Quickstart` and runs it, line by line, asserting every line exits
# 0. Any future change to the README's steps, flags, or ORDER is exercised
# here with no edit to this file -- and a step the README gets wrong fails the
# suite instead of passing a parallel re-implementation.
#
# The run is verbatim, with no placeholder substitution: HOME is pinned to a
# throwaway tree, so the block's own `~/issues-store` and
# `~/projects/myproject` expand inside it. The single deviation is PATH, which
# points `issue` at the dev tree under test rather than the installed build --
# a resolution concern, not a content rewrite.
#
# Sections:
#   A. The README Quickstart block, extracted and executed.
#   B. Precondition guard -- init-project OUTSIDE a git repo exits non-zero
#      with an actionable message naming the fix (`git init`) and the reason.
#   C. What init-project actually persists, pinning the README's claim.
#
# Run from the project root:
#     tests/verification/im-231-quickstart.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"

INIT_PROJECT="$(resolve_cli init-issue-project)"
README="$PROJECT_ROOT/README.md"

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
assert_eq() {
    if [ "$1" = "$2" ]; then pass "$3 ($2)"; else
        fail "$3: expected '$1', got '$2'"; fi
}
assert_contains() {
    if echo "$2" | grep -qF -- "$1"; then pass "$3 (contains '$1')"; else
        fail "$3: '$1' not found in:"; echo "$2" | sed 's/^/  | /'; fi
}

WORK=$(mktemp -d -t im231-verify.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
export PATH="$ISSUE_MGMT_BIN:$PATH"
unset ISSUE_STORE_ROOT

# Clean, isolated HOME so no inherited config/store leaks in -- and so the
# Quickstart's own `~` paths land inside the fixture. Git needs an identity
# there for store-mutation commits to succeed.
export HOME="$WORK/home"
mkdir -p "$HOME"
git config --global user.name "im231-verify" >/dev/null 2>&1
git config --global user.email "$FIXTURE_EMAIL" >/dev/null 2>&1
git config --global init.defaultBranch main >/dev/null 2>&1

# ---------------------------------------------------------------------------
echo "[A] README Quickstart block, extracted from README.md and executed"
# ---------------------------------------------------------------------------

# Extract the fenced block under `## Quickstart` and instrument each command
# line with an exit-code probe. The whole block runs in ONE shell so its `cd`
# persists across steps, while each line still reports its own status.
RUNNER="$WORK/quickstart.sh"
CMDLIST="$WORK/quickstart-commands.txt"

# The extractor lives in its own file rather than a heredoc inside $( ), where
# bash scans the body for a matching backtick and the python's own "```"
# fence literal breaks the parse.
cat > "$WORK/extract.py" <<'PY'
import sys

readme, out, cmdlist = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(readme).read().splitlines()

block, in_section, in_fence = [], False, False
for line in lines:
    if line.startswith("## "):
        in_section = line.strip() == "## Quickstart"
        continue
    if not in_section:
        continue
    if line.startswith("```"):
        if in_fence:
            break           # first fence under the heading is the Quickstart
        in_fence = True
        continue
    if in_fence:
        block.append(line)

if not block:
    sys.stderr.write("could not extract a fenced block under '## Quickstart'\n")
    raise SystemExit(1)
commands = [l for l in block if l.strip() and not l.strip().startswith("#")]
if not commands:
    sys.stderr.write("the Quickstart block contains no command lines\n")
    raise SystemExit(1)

with open(cmdlist, "w") as fh:
    for c in commands:
        fh.write("%s\n" % c.strip())

with open(out, "w") as fh:
    fh.write("#!/usr/bin/env bash\n# GENERATED from README.md -- do not edit.\n")
    for line in block:
        stripped = line.strip()
        fh.write("%s\n" % line)
        if not stripped or stripped.startswith("#"):
            continue
        # Report each step's own status without aborting, so one broken step
        # does not hide the state of the rest.
        fh.write('__rc=$?; [ "$__rc" -eq 0 ] || printf "STEPFAIL\\t%s\\t%s\\n" "$__rc" %s\n'
                 % ("%s", "%s", '"' + stripped.replace('\\', '\\\\').replace('"', '\\"') + '"'))
print("extracted %d command line(s)" % len(commands))
PY

EXTRACT_OUT=$(python3 "$WORK/extract.py" "$README" "$RUNNER" "$CMDLIST" 2>&1)
EXTRACT_RC=$?
if [ "$EXTRACT_RC" -ne 0 ]; then
    fail "could not extract the Quickstart block from README.md: $EXTRACT_OUT"
else
    pass "extracted the Quickstart block from README.md ($EXTRACT_OUT)"
fi

# Prove the run is VERBATIM: every command the runner executes must appear
# literally in README.md. This is what distinguishes a derived guard from the
# hand-transcribed copy that let IM-266 through -- a rewritten or substituted
# step would fail here.
if [ -f "$CMDLIST" ]; then
    DRIFT=""
    while IFS= read -r cmd; do
        [ -n "$cmd" ] || continue
        grep -qF -- "$cmd" "$README" || DRIFT="$DRIFT
  $cmd"
    done < "$CMDLIST"
    if [ -z "$DRIFT" ]; then
        pass "every executed command appears verbatim in README.md"
    else
        fail "the runner executes command(s) not present in README.md:$DRIFT"
    fi
fi

if [ -f "$RUNNER" ]; then
    QS_OUT=$(bash "$RUNNER" 2>&1)
    STEPFAILS=$(echo "$QS_OUT" | grep '^STEPFAIL' || true)
    if [ -z "$STEPFAILS" ]; then
        pass "every Quickstart step exits 0 when run as the README writes it"
    else
        fail "Quickstart step(s) failed as the README writes them:"
        echo "$STEPFAILS" | sed 's/^STEPFAIL	/  rc=/' | sed 's/	/  ->  /'
        echo "$QS_OUT" | grep -v '^STEPFAIL' | sed 's/^/  | /' | tail -20
    fi

    # The block's effects, asserted against what the README says it achieves.
    # Deliberately about OUTCOMES, not step wording, so they survive a
    # reordering of the documented steps.
    STORE="$HOME/issues-store"
    PROJ="$HOME/projects/myproject"
    # COVERS: substrate.init-issue-store
    [ -d "$STORE/.git" ] \
        && pass "step 1 scaffolded a git-managed store" \
        || fail "no git-managed store at $STORE"
    # COVERS: registry.basename-prefix-unique
    if [ -f "$STORE/.repos" ] && grep -q "myproject:MYP" "$STORE/.repos"; then
        pass "registry records myproject:MYP"
    else
        fail "registry missing myproject:MYP"
    fi
    # COVERS: substrate.init-project-sentinel
    [ -f "$PROJ/.issues/.prefix" ] \
        && pass "init-project wrote .issues/.prefix" \
        || fail "init-project did not write .issues/.prefix"
    [ -f "$STORE/MYP/MYP-001.md" ] \
        && pass "the create step filed MYP-001 into the store" \
        || fail "MYP-001.md not found under the store"
    # The lifecycle steps must have actually landed, not merely exited 0.
    FM=$( cd "$PROJ" && issue show MYP-001 --frontmatter 2>/dev/null )
    # COVERS: state.complete-passthrough
    assert_contains "status: done" "$FM" "the lifecycle steps moved MYP-001 to done"
    # COVERS: cli.search-modes
    SEARCH_OUT=$( cd "$PROJ" && issue search "dry-run" 2>/dev/null )
    assert_contains "MYP-001" "$SEARCH_OUT" \
        "issue search 'dry-run' matches by title (empty body)"
fi

# ---------------------------------------------------------------------------
echo
echo "[B] Precondition guard: init-project outside a git repo"
# ---------------------------------------------------------------------------
NONGIT="$WORK/not-a-repo"
mkdir -p "$NONGIT"
RC=0
ERR=$( cd "$NONGIT" && "$INIT_PROJECT" --store "$HOME/issues-store" --prefix ZZZ --non-interactive 2>&1 ) \
    || RC=$?
[ "$RC" -ne 0 ] \
    && pass "init-project outside a git repo exits non-zero ($RC)" \
    || fail "init-project outside a git repo unexpectedly exited 0"
assert_contains "git init" "$ERR" "error names the fix (git init)"
assert_contains "git repo" "$ERR" "error names the reason (must be a git repo)"

# ---------------------------------------------------------------------------
echo
echo "[C] What init-project persists -- pinning the README's claim"
# ---------------------------------------------------------------------------
# IM-266: the README said init-project "writes the config and sentinel for
# you". It writes the SENTINEL and `.prefix`; `~/.config/issue-mgmt/config` is
# written only on the interactive-prompt path, never on the `--store` flow the
# Quickstart documents. A user told the config was written for them, who later
# runs a verb from a directory with no sentinel, hits the IM-253
# `reason=store-unresolvable` payload instead.
CFGPROJ="$WORK/cfgproject"
mkdir -p "$CFGPROJ"
( cd "$CFGPROJ" && git init -q )
CFGHOME="$WORK/cfghome"
mkdir -p "$CFGHOME"
( cd "$CFGPROJ" && env HOME="$CFGHOME" "$INIT_PROJECT" \
    --store "$HOME/issues-store" --prefix CFG --non-interactive ) >/dev/null 2>&1
# COVERS: substrate.init-project-sentinel
[ -f "$CFGPROJ/.issues/.migrated-to-store" ] \
    && pass "the documented --store flow writes the sentinel" \
    || fail "the documented --store flow did not write the sentinel"
# COVERS: substrate.init-project-sentinel
[ -f "$CFGPROJ/.issues/.prefix" ] \
    && pass "the documented --store flow writes .prefix" \
    || fail "the documented --store flow did not write .prefix"
# COVERS: substrate.store-root-resolution
if [ -e "$CFGHOME/.config/issue-mgmt/config" ]; then
    fail "the --store flow wrote ~/.config/issue-mgmt/config; the README must not promise a config it does not write"
else
    pass "the --store flow writes NO user config (as the README now says)"
fi
# COVERS: substrate.store-root-resolution
if grep -q 'writes the config and sentinel for you' "$README"; then
    fail "README still claims init-project writes the config"
else
    pass "README no longer claims init-project writes the config"
fi

# ---------------------------------------------------------------------------
echo
echo "==================================================================="
echo "README quickstart: $PASS passed, $FAIL failed"
echo "==================================================================="
[ "$FAIL" -eq 0 ]
