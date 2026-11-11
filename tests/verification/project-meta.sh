#!/usr/bin/env bash
# Project-meta verification.
#
# Covers requirements that are project-policy rather than behavioral CLI
# surface:
#   - general.bash-python-stdlib   -- no third-party Python imports
#   - general.changelog-releases-semver -- CHANGELOG version headings follow
#                                         vMAJOR.MINOR.PATCH
#
# Plus one project policy with no requirement of its own: the public view
# carries ONE fixture git identity (IM-268). It lives here rather than only
# in the publish gate because the publish gate is the wrong place to learn
# it -- a new fixture address sat red on an integration branch for a day
# with nothing failing locally.

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

# ---------------------------------------------------------------------------
# Check 1: no third-party Python imports anywhere in lib/*.py or scripts/*.
# ---------------------------------------------------------------------------
echo "[1] Python stdlib only"

DENY_FILE=$(mktemp -t im093-meta.XXXXXX)
trap 'rm -f "$DENY_FILE"' EXIT

python3 - "$PROJECT_ROOT" > "$DENY_FILE" <<'PY'
import ast
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
# Local imports: every lib/*.py module, the `lib` name itself, AND every
# package directory under lib/ that carries an __init__.py. The v3.0.0
# dispatcher (scripts/issue) does `import cli` / `from cli import common`
# where `cli` is the local lib/cli/ verb package, not a third-party dep
# -- whitelist such packages so the scan does not flag them.
local_modules = {p.stem for p in (root / "lib").glob("*.py")} | {"lib"}
local_modules |= {
    p.name for p in (root / "lib").iterdir()
    if p.is_dir() and (p / "__init__.py").is_file()
}
# Sibling modules under scripts/ are local too: the dev-only surface-manifest
# entrypoint (scripts/cli-surface) imports scripts/cli_surface.py the same way
# the dispatcher imports lib/cli (IM-245). A scripts/-local module is as local
# as a lib/-local one; the check is for third-party PACKAGES.
local_modules |= {p.stem for p in (root / "scripts").glob("*.py")}

# Python 3.10+ exposes the authoritative stdlib set.
stdlib_names = getattr(sys, "stdlib_module_names", None)
if stdlib_names is None:
    # Pre-3.10 fallback: a small denylist of common third-party offenders.
    # Good enough for a pilot; upgrade to stdlib_module_names when the floor
    # is raised.
    deny = {
        "requests", "httpx", "yaml", "click", "rich", "pydantic",
        "numpy", "pandas", "jinja2", "markdown",
        "setuptools", "pkg_resources",
    }
    def is_external(name):
        return name in deny
else:
    def is_external(name):
        return name not in stdlib_names and name not in local_modules

candidates = list((root / "lib").glob("*.py"))
for entry in (root / "scripts").iterdir():
    if not entry.is_file():
        continue
    try:
        with entry.open("rb") as fh:
            head = fh.read(64)
    except OSError:
        continue
    if head.startswith(b"#!") and b"python" in head.split(b"\n", 1)[0]:
        candidates.append(entry)

bad = []
for path in candidates:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if is_external(top):
                    bad.append((path, top))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if is_external(top):
                bad.append((path, top))

for path, mod in bad:
    rel = path.relative_to(root)
    print(f"{rel}\t{mod}")
PY

if [ -s "$DENY_FILE" ]; then
    fail "third-party Python imports detected:"
    sed 's/^/    /' "$DENY_FILE"
else
    # COVERS: general.bash-python-stdlib
    pass "no third-party Python imports in lib/ or scripts/"
fi

# ---------------------------------------------------------------------------
# Check 2: every CHANGELOG.md version heading matches vMAJOR.MINOR.PATCH.
# ---------------------------------------------------------------------------
echo
echo "[2] CHANGELOG version headings follow SemVer"

BAD_HEADINGS=$(grep -E '^## \[v' "$PROJECT_ROOT/CHANGELOG.md" \
               | grep -vE '^## \[v[0-9]+\.[0-9]+\.[0-9]+\]' || true)
if [ -n "$BAD_HEADINGS" ]; then
    fail "CHANGELOG.md version headings that don't match vMAJOR.MINOR.PATCH:"
    echo "$BAD_HEADINGS" | sed 's/^/    /'
else
    # COVERS: general.changelog-releases-semver
    pass "all CHANGELOG.md version headings match vMAJOR.MINOR.PATCH"
fi

# ---------------------------------------------------------------------------
# Check 3: skill set matches the documented skill/no-skill boundary.
# One skill per live-state discourse/workflow verb + read-only history
# subcommand; maintenance/recovery/scaffolding/migration/distribution and
# workflow-utility binaries deliberately ship none (requirements.md
# "Distribution and skills").
# ---------------------------------------------------------------------------
echo
echo "[3] skill set matches the documented boundary"

EXPECTED_SKILLS="issue-activity issue-block issue-cancel issue-complete issue-create issue-dep issue-edit issue-history issue-list issue-note issue-search issue-search-history issue-show issue-show-at issue-start"
ACTUAL_SKILLS=$(ls -d "$PROJECT_ROOT"/.claude/skills/*-dev 2>/dev/null \
                | xargs -n1 basename | sed 's/-dev$//' | sort | tr '\n' ' ' | sed 's/ $//')
EXPECTED_SKILL_COUNT=$(printf '%s\n' $EXPECTED_SKILLS | wc -l | tr -d '[:space:]')
if [ "$ACTUAL_SKILLS" = "$EXPECTED_SKILLS" ]; then
    # COVERS: dist.skills-one-per-cli
    pass "skill set is exactly the $EXPECTED_SKILL_COUNT live-state + history skills"
else
    fail "skill set drifted from the documented boundary:"
    echo "    expected: $EXPECTED_SKILLS"
    echo "    actual:   $ACTUAL_SKILLS"
fi

# The former NO_SKILL_TOOLS loop was deleted in IM-264. It probed for
# .claude/skills/<name>-dev directories named after the retired FLAT
# binaries (reindex-issues, init-issue-project, ...), but post-v3.0.0 skills
# are named issue-<verb>-dev, so eight of its ten entries asked whether a
# directory the project cannot produce exists -- a vacuous PASS. It was also
# redundant: the set-equality check above is exact, so a skill added for ANY
# verb, exempt or not, already fails it. A mapping-based lint (does each
# shipped skill name a verb that still exists?) needs the skill-to-verb
# mapping IM-265 builds, and belongs there rather than in a second
# mechanism here.

# ---------------------------------------------------------------------------
# Check 4: the public view carries one fixture git identity (IM-268).
#
# Delegated to scripts/check-fixture-emails, which derives the shared
# address from tests/fixture_identity.py and the allowlist from
# .publish.yaml, so neither side of the comparison is restated here. Its
# gate-bites cases are pinned in tests/test_fixture_identity.py.
#
# No `# COVERS:` marker: this is project policy with no requirements.md
# entry, and inventing a tag with no `[REQ:]` behind it would register as a
# coverage-spec orphan.
# ---------------------------------------------------------------------------
echo
echo "[4] one fixture git identity across the public view"

if FIXTURE_OUT=$("$PROJECT_ROOT/scripts/check-fixture-emails" 2>&1); then
    pass "${FIXTURE_OUT#check-fixture-emails: }"
else
    fail "fixture git identities have drifted:"
    printf '%s\n' "$FIXTURE_OUT" | sed 's/^/    /'
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
