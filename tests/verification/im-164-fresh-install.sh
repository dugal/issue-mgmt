#!/usr/bin/env bash
# im-164-fresh-install.sh -- fresh-install verification in a bd-less
# environment (repurposed for the v2.11.0 bd retirement).
#
# Verifies that a fresh `brew install` of issue-mgmt pulls NO beads
# dependency (the `depends_on "beads"` line was removed in v2.11.0) and
# that the tool works end-to-end with no `bd` on PATH: scaffold a store,
# create and transition an issue, and compute the ready set -- all
# frontmatter-native, no `.beads/` index, no bd subprocess.
#
# Requires docker and the docker-dev-env brew-runtime image: the
# Linuxbrew-runtime-only variant (Dockerfile.base built with
# BREW_RUNTIME=1 BREW_BUNDLE=0) -- Homebrew present, zero formulas
# installed, no taps, no bd, no issue-mgmt. That is the genuine
# fresh-machine baseline. The test runs entirely inside a throwaway
# container: it installs a freshly built formula from a container-local
# tap and asserts bd is never pulled and the tool still works. Host state
# is never touched.
#
# Usage: tests/verification/im-164-fresh-install.sh [IMAGE]
#   IMAGE defaults to docker-dev-env:brew-runtime. Pointing it at an
#   image that already carries beads/issue-mgmt (e.g. the dev variant)
#   also works: the baseline step uninstalls them first.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
. "$SCRIPT_DIR/lib.sh"
IMAGE="${1:-docker-dev-env:brew-runtime}"
CONTAINER="im164-verify-$$"

pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "SKIP: docker not available; run on a machine with the base image." >&2
    exit 0
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "SKIP: image $IMAGE not present (build it from docker-dev-env)." >&2
    exit 0
fi

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1; }
trap cleanup EXIT

echo "== build dist tarball + container-local formula =="
# Capture make's output and echo it on failure -- the strict stamp gate
# refuses `make dist` on an unstamped tree (the promote->stamp window), and
# a swallowed diagnostic cost two full coverage re-runs to trace.
if ! dist_out="$(make -C "$PROJECT_ROOT" dist 2>&1)"; then
    echo "FATAL: make dist failed" >&2
    echo "$dist_out" | tail -30 >&2
    exit 1
fi
TARBALL="$(ls -t "$PROJECT_ROOT"/dist/issue-mgmt-*.tar.gz | head -1)"
"$PROJECT_ROOT/scripts/gen-formula" "$TARBALL" \
    --url "file:///mnt/dist/$(basename "$TARBALL")" \
    > "$PROJECT_ROOT/dist/issue-mgmt-im164-test.rb"
# COVERS: dist.formula-bundling
grep -q 'depends_on "beads"' "$PROJECT_ROOT/dist/issue-mgmt-im164-test.rb" \
    && bad "generated formula still carries depends_on beads (should be removed in v2.11.0)" \
    || ok "generated formula carries no beads dependency"

echo "== stage container =="
docker run -d --name "$CONTAINER" "$IMAGE" sleep 3600 >/dev/null
docker exec -u root "$CONTAINER" mkdir -p /mnt/dist
docker exec -u root "$CONTAINER" chmod 777 /mnt /mnt/dist
docker cp "$TARBALL" "$CONTAINER:/mnt/dist/"
docker cp "$PROJECT_ROOT/dist/issue-mgmt-im164-test.rb" "$CONTAINER:/mnt/dist/issue-mgmt.rb"

# Close the skill chain end to end (IM-264). The host's shipped sources
# under share/issue-mgmt/skills/ are ground truth (IM-276 re-pointed this
# off the generated .claude/skills/*-dev copies); everything downstream -- the
# tarball's skills/, the formula's directory install, <pkgshare>/skills/,
# and ~/.claude/skills/ -- must carry exactly that many. This is the only
# non-tautological link in the chain: both sides of every in-repo check are
# derived from the same source, whereas here the host count is compared
# against what a real `brew install` actually materialized in the container.
HOST_SKILL_COUNT=$(shipped_skill_count) || exit 1
echo "== host ships $HOST_SKILL_COUNT skills; asserting the install carries all of them =="

# -i is required: without it docker exec does not attach stdin and
# `bash -s` runs an empty script, exiting 0 vacuously. The heredoc stays
# quoted (it is full of $-expressions meant for the container shell), so the
# host expectation crosses the boundary as an env var, not by interpolation.
docker exec -i -e HOST_SKILL_COUNT="$HOST_SKILL_COUNT" "$CONTAINER" bash -s <<'INNER'
set -u
pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }
eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"

echo "== bd-less baseline =="
# The brew-runtime image ships bare; converge non-bare images (dev variant).
# v3.0.0: the packaged binary is `issue` (the flat names are
# tombstone stubs), so presence is probed via `issue`.
if command -v bd >/dev/null 2>&1 || command -v issue >/dev/null 2>&1; then
    echo "note: image carries beads/issue-mgmt; uninstalling to reach the bd-less baseline"
    brew uninstall --ignore-dependencies issue-mgmt beads >/dev/null 2>&1
fi
command -v bd >/dev/null 2>&1 && bad "bd on PATH at baseline" || ok "bd absent at baseline"
command -v issue >/dev/null 2>&1 && bad "issue-mgmt on PATH at baseline" || ok "issue-mgmt absent at baseline"

echo "== install the built formula from a container-local tap =="
TAPDIR="$(brew --repository)/Library/Taps/local/homebrew-imtest/Formula"
mkdir -p "$TAPDIR"
cp /mnt/dist/issue-mgmt.rb "$TAPDIR/issue-mgmt.rb"
if brew install local/imtest/issue-mgmt >/tmp/install.log 2>&1; then
    ok "brew install succeeded"
else
    bad "brew install failed"; tail -20 /tmp/install.log
fi
# v2.11.0: no beads dependency, so bd must NOT be pulled onto PATH.
command -v bd >/dev/null 2>&1 \
    && bad "bd on PATH after install (no beads dependency should be pulled)" \
    || ok "bd absent after install (no beads dependency pulled)"
brew deps local/imtest/issue-mgmt 2>/dev/null | grep -q '^beads$' \
    && bad "beads still resolves as a formula dependency" \
    || ok "beads is not a formula dependency"

echo "== the skill chain reaches ~/.claude/skills (IM-264) =="
PKGSHARE="$(brew --prefix issue-mgmt)/share/issue-mgmt/skills"
PKGSHARE_COUNT=$(ls "$PKGSHARE"/*.md 2>/dev/null | wc -l | tr -d '[:space:]')
[ "$PKGSHARE_COUNT" = "$HOST_SKILL_COUNT" ] \
    && ok "pkgshare carries all $HOST_SKILL_COUNT shipped skills" \
    || bad "pkgshare has $PKGSHARE_COUNT skills, host ships $HOST_SKILL_COUNT ($PKGSHARE)"
# The formula declares license "MIT"; the text has to actually ship.
[ -f "$(brew --prefix issue-mgmt)/share/doc/issue-mgmt/LICENSE" ] \
    && ok "LICENSE ships with the installed formula" \
    || bad "LICENSE absent from the install (formula declares license \"MIT\")"
INSTALL_JSON=/tmp/install-skills.json
if issue admin install-skills --json > "$INSTALL_JSON" 2>/tmp/install-skills.err; then
    INSTALLED_COUNT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["count"])' "$INSTALL_JSON")
    [ "$INSTALLED_COUNT" = "$HOST_SKILL_COUNT" ] \
        && ok "install-skills installed all $HOST_SKILL_COUNT skills into ~/.claude/skills" \
        || bad "install-skills reported $INSTALLED_COUNT, host ships $HOST_SKILL_COUNT"
    HOME_COUNT=$(ls -d "$HOME"/.claude/skills/*/ 2>/dev/null | wc -l | tr -d '[:space:]')
    [ "$HOME_COUNT" = "$HOST_SKILL_COUNT" ] \
        && ok "~/.claude/skills carries $HOST_SKILL_COUNT skill directories" \
        || bad "~/.claude/skills has $HOME_COUNT directories, host ships $HOST_SKILL_COUNT"
else
    bad "issue admin install-skills failed"; tail -10 /tmp/install-skills.err
fi

echo "== end-to-end mutation + ready set, entirely bd-less =="
git config --global init.defaultBranch main
git config --global user.name t
git config --global user.email t@t
STORE=/tmp/im164-store
PROJ=/tmp/im164-proj
issue admin init-store "$STORE" >/dev/null 2>&1
[ -d "$STORE/.beads" ] && bad "init-store created a .beads/ index" \
    || ok "init-store creates no .beads/ index"
mkdir -p "$PROJ" && cd "$PROJ" && git init -q .
issue admin init-project --store "$STORE" --prefix TT --non-interactive >/dev/null 2>&1
if issue create "im164 fresh-install smoke" chore low >/tmp/new-issue.log 2>&1; then
    ok "issue create exit 0 on a fresh bd-less store"
else
    bad "issue create failed"; tail -20 /tmp/new-issue.log
fi
grep -q "^status: open" "$STORE/TT/TT-001.md" \
    && ok "created issue is open in markdown" \
    || bad "created issue not found / not open"
issue transition TT-001 in-progress --branch t >/tmp/trans.log 2>&1 \
    && ok "issue transition exit 0" \
    || { bad "issue transition failed"; tail -10 /tmp/trans.log; }
grep -q "^status: in-progress" "$STORE/TT/TT-001.md" \
    && ok "transition updated the markdown status" \
    || bad "transition did not update markdown status"
# issue ready computes readiness from frontmatter with no bd.
if issue ready --repo TT >/tmp/ready.log 2>&1 || [ $? -eq 1 ]; then
    ok "issue ready runs bd-less (exit 0 or 1 no-ready-work)"
else
    bad "issue ready failed"; tail -10 /tmp/ready.log
fi
[ -d "$STORE/.beads" ] && bad "a .beads/ index appeared after mutations" \
    || ok "no .beads/ index after mutations (nothing spawned bd)"

echo "$pass passed, $fail failed" > /tmp/inner-results
echo "INNER_RESULTS $pass $fail"
[ "$fail" -eq 0 ] && [ "$pass" -gt 0 ]
INNER
inner_rc=$?
# Belt-and-suspenders against a vacuous pass (e.g. stdin not attached):
# require the INNER_RESULTS marker with a nonzero pass count.
INNER_LINE="$(docker exec "$CONTAINER" sh -c 'cat /tmp/inner-results 2>/dev/null')"
if [ "$inner_rc" -eq 0 ] && [ -n "$INNER_LINE" ]; then
    ok "container checks all passed ($INNER_LINE)"
else
    bad "container checks failed (rc=$inner_rc, results: ${INNER_LINE:-none recorded})"
fi

rm -f "$PROJECT_ROOT/dist/issue-mgmt-im164-test.rb"
echo
echo "im-164-fresh-install: $pass passed, $fail failed (see container PASS/FAIL lines above)."
[ "$fail" -eq 0 ]
