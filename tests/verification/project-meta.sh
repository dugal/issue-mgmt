#!/usr/bin/env bash
# Project-meta verification.
#
# Covers requirements that are project-policy rather than behavioral CLI
# surface:
#   - general.bash-python-stdlib   -- no third-party Python imports
#   - general.changelog-releases-semver -- CHANGELOG version headings follow
#                                         vMAJOR.MINOR.PATCH

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
if [ "$ACTUAL_SKILLS" = "$EXPECTED_SKILLS" ]; then
    # COVERS: dist.skills-one-per-cli
    pass "skill set is exactly the 15 live-state + history skills"
else
    fail "skill set drifted from the documented boundary:"
    echo "    expected: $EXPECTED_SKILLS"
    echo "    actual:   $ACTUAL_SKILLS"
fi

NO_SKILL_TOOLS="reindex-issues validate-issue issue-snapshot init-issue-project init-issue-store migrate-to-store issue-ready issue-reap-expired-claims issue-mgmt-selftest issue-mgmt-install-skills"
LEAKED=""
for tool in $NO_SKILL_TOOLS; do
    if [ -d "$PROJECT_ROOT/.claude/skills/$tool-dev" ]; then
        LEAKED="$LEAKED $tool"
    fi
done
if [ -z "$LEAKED" ]; then
    # COVERS: dist.skills-one-per-cli
    pass "no skill exists for the exempt maintenance/utility binaries"
else
    fail "exempt binaries unexpectedly have skills:$LEAKED (update requirements.md if deliberate)"
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
