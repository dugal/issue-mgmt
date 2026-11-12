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
# The shipped set, from lib.sh -- the authored canonical sources, not the
# derived .claude/skills/*-dev copies (IM-276).
ACTUAL_SKILLS=$(shipped_skill_names | tr '\n' ' ' | sed 's/ $//')
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

# ---------------------------------------------------------------------------
# Check 5: no verification script targets the OPERATOR's issue store by
# default (IM-255).
#
# The invariant: nothing under tests/verification/ may write to the resolved
# operator store unless the operator asked for it on purpose. It is a project
# policy rather than CLI behavior, which is why it lives here.
#
# It earned a gate the hard way. v2-release-smoke.sh resolved
# `${ISSUE_STORE_ROOT:-$HOME/issues-store}` and sat in this directory with
# the executable bit set, so every `for f in tests/verification/*.sh` sweep
# mutated the production store -- and because each mutation commits itself
# (concurrency.auto-commit-per-mutation), the script's cleanup trap could
# never undo it: 447 commits touching SMOKE/ by 2026-11-12, the newest from
# the v3.4.0 release ceremony's own sweep. Same class as the 2026-11-07
# audit-subagent incident, where store resolution fell through to
# ~/.config/issue-mgmt/config and flipped IM-001 back to open.
#
# The check is SHAPE-based, and deliberately so: whether a script writes to
# the operator store is decided at runtime by the resolution precedence, and
# the only way to settle it dynamically is to run the script -- which is the
# sweep this check exists to make safe. So it bans the four mechanical shapes
# by which a script can name the operator store, and the operator-store
# identity it does know is DERIVED from this checkout's own routing sentinel
# rather than restated:
#
#   R1  inherit-with-fallback: `${ISSUE_STORE_ROOT:-...}` / `:=`. A script
#       must PIN the store it writes to, or require it explicitly; a default
#       is how the defect shipped.
#   R2  a $HOME- or ~-rooted path ending in the store directory's basename,
#       appearing BEFORE the script redirects HOME into a temp tree (a script
#       that has already reassigned HOME cannot reach the operator's).
#   R3  the literal operator-store path this checkout resolves to -- from
#       .issues/.migrated-to-store, plus ~/.config/issue-mgmt/config
#       store_root when present (the two steps of the precedence chain that
#       name a real store; the config leg is read through the CLI's own
#       `read_config_store_root`, not a second parser of that format).
#   R4  assigning any ISSUE_MGMT_*REAL_STORE-shaped variable: an opt-in into
#       a real-store mode must come from the operator's environment, never
#       from inside the suite. Matched as a shape so renaming the variable
#       cannot make the ban vacuous.
#
# What it cannot see: a store path assembled at runtime from pieces, or one
# reached through a sentinel file a script writes pointing outside its temp
# tree. Both are caught by the standing release-ceremony practice of
# recording the store HEAD across a suite run.
#
# No `# COVERS:` marker: project policy with no requirements.md entry, so a
# tag here would register as a coverage-spec orphan (same reasoning as [4]).
# ---------------------------------------------------------------------------
echo
echo "[5] no verification script targets the operator issue store by default"

STORE_FINDINGS=$(python3 - "$PROJECT_ROOT" <<'PY'
import os
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
vdir = root / "tests" / "verification"

# --- derive the operator-store identity from the resolution chain ----------
operator_stores = []
sentinel = root / ".issues" / ".migrated-to-store"
if sentinel.is_file():
    payload = sentinel.read_text().strip()
    if payload:
        operator_stores.append(payload)

# The config leg is read through the SHIPPED reader rather than a second
# parser of the same file format -- two agreeing parsers is the shape IM-261
# catalogued. An import failure is loud, never a silent skip, because a
# skipped leg would quietly narrow the check.
sys.path.insert(0, str(root / "lib"))
try:
    from cli.admin_init_project import read_config_store_root
except Exception as exc:                                   # noqa: BLE001
    print("CANNOT-DERIVE\t0\tcannot import the shipped config reader\t%s" % exc)
    raise SystemExit(0)
cfg_store = read_config_store_root()
if cfg_store:
    operator_stores.append(os.path.expanduser(cfg_store))

# The store DIRECTORY basename, derived rather than restated: if the store is
# ever renamed, R2 follows it. The fallback only matters in a checkout with no
# sentinel (a public clone), where R2 degrades to the conventional name.
store_basename = os.path.basename(operator_stores[0]) if operator_stores else "issues-store"

# A non-empty default only. `${ISSUE_STORE_ROOT:-}` is the legitimate
# "is it set at all" probe and names no store.
R1 = re.compile(r"\$\{ISSUE_STORE_ROOT:[-=][^}]")
R2 = re.compile(r"(\$HOME\b|\$\{HOME\}|(^|[\s\"'=(])~/)[^\s\"']*" + re.escape(store_basename))
R4 = re.compile(r"^\s*(export\s+)?ISSUE_MGMT_[A-Z0-9_]*REAL_STORE\s*=")
HOME_ASSIGN = re.compile(r"^\s*(export\s+)?HOME=")

findings = []
for path in sorted(vdir.glob("*.sh")):
    if not os.access(path, os.X_OK):
        continue   # lib.sh and friends: sourced, never swept
    lines = path.read_text().splitlines()
    first_home_assign = None
    for n, line in enumerate(lines, 1):
        if HOME_ASSIGN.search(line):
            first_home_assign = n
            break
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        if R1.search(line):
            findings.append((path.name, n, "R1 inherit-with-fallback", line.strip()))
        if R2.search(line) and (first_home_assign is None or n < first_home_assign):
            findings.append((path.name, n, "R2 $HOME-rooted operator-store path", line.strip()))
        for store in operator_stores:
            if store in line:
                findings.append((path.name, n, "R3 literal operator-store path", line.strip()))
        if R4.search(line):
            findings.append((path.name, n, "R4 real-store opt-in set from inside the suite", line.strip()))

for name, n, rule, text in findings:
    print("%s:%d\t%s\t%s" % (name, n, rule, text))
PY
)

if [ -n "$STORE_FINDINGS" ]; then
    fail "verification scripts that can target the operator issue store:"
    printf '%s\n' "$STORE_FINDINGS" | sed 's/^/    /'
else
    pass "no executable verification script names the operator issue store"
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
