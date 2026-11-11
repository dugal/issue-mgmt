#!/usr/bin/env bash
# Surface vocabulary registry + linter verification (IM-246).
#
# The linter checks docs/cli-surface.json (what the surface IS, extracted
# from the argparse registrations) against docs/cli-vocabulary.json (what it
# is allowed to MEAN). This script exercises it the way a release gate
# would: clean on the committed tree, red on an injected violation, and
# red again when the baseline stops describing real findings.
#
# Dev-tree only. The linter is not shipped -- it governs the surface rather
# than being part of it -- so this script runs against the source tree and
# never installs anything.
#
# Covers:
#   surface.vocabulary-registry   -- every flag name registered, synonym
#                                    sets uniform
#   surface.one-meaning-per-flag  -- one concept per spelling, value space
#                                    uniform unless licensed
#   surface.verb-class-contract   -- each class's obligatory flags
#   surface.long-flags-only       -- -h is the only short flag

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

LINT="$PROJECT_ROOT/scripts/surface-lint"
WORK=$(mktemp -d -t im246-surface.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------------------
echo "[1] the committed surface lints clean"
# ---------------------------------------------------------------------------

# COVERS: surface.vocabulary-registry
if (cd "$PROJECT_ROOT" && "$LINT" >"$WORK/clean.out" 2>"$WORK/clean.err"); then
  pass "surface-lint exits 0 on the committed tree"
else
  fail "surface-lint is red on the committed tree:"
  cat "$WORK/clean.err"
fi

# COVERS: surface.one-meaning-per-flag
if (cd "$PROJECT_ROOT" && "$LINT" --json 2>/dev/null) > "$WORK/lint.json" \
   && python3 - "$WORK/lint.json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
assert payload["totals"]["unwaived"] == 0, payload["totals"]
assert payload["totals"]["waived"] > 0, "the baseline describes nothing"
for row in payload["waived"]:
    assert row["issue"], row
sys.exit(0)
PY
then
  pass "--json reports zero unwaived findings, each waived one owned"
else
  fail "--json payload is not the documented shape"
fi

# COVERS: surface.verb-class-contract
VERBS=$(python3 - "$PROJECT_ROOT/docs/cli-surface.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
missing = [v["path"] for v in m["verbs"]
           if "--json" not in {n for f in v["flags"] for n in f["names"]}]
print(len(missing))
PY
)
if [ "$VERBS" = "0" ]; then
  pass "every verb offers --json, as its class contract obliges"
else
  fail "$VERBS verb(s) lack --json"
fi

# ---------------------------------------------------------------------------
echo "[2] the gate bites: an injected violation is caught"
# ---------------------------------------------------------------------------

# An unregistered flag name is the violation a new flag produces, so this
# is the path a developer actually hits.
cp -R "$PROJECT_ROOT/docs" "$WORK/docs"
python3 - "$WORK/docs/cli-surface.json" <<'PY'
import copy, json, sys
path = sys.argv[1]
m = json.load(open(path))
verb = next(v for v in m["verbs"] if v["path"] == "issue index")
entry = copy.deepcopy(next(f for f in verb["flags"] if f["name"] == "--limit"))
entry.update(name="--cap", names=["--cap"], dest="cap")
verb["flags"].append(entry)
json.dump(m, open(path, "w"), indent=2, sort_keys=True)
PY

# COVERS: surface.vocabulary-registry
OUT=$(cd "$PROJECT_ROOT" && python3 - "$WORK/docs" <<'PY'
import json, sys
sys.path.insert(0, "scripts")
import surface_lint as s
docs = sys.argv[1]
manifest = json.load(open(f"{docs}/cli-surface.json"))
registry = s.load_registry(f"{docs}/cli-vocabulary.json")
unwaived, _ = s.lint(manifest, registry)
print("\n".join(f.key for f in unwaived))
PY
)
if printf '%s\n' "$OUT" | grep -q "unregistered-flag:--cap"; then
  pass "an unregistered flag name turns the lint red"
else
  fail "the lint passed a flag name that is not in the registry"
fi

# A short flag is the one clause with no live instance, so it is the one
# most likely to have rotted unnoticed.
python3 - "$WORK/docs/cli-surface.json" <<'PY'
import json, sys
path = sys.argv[1]
m = json.load(open(path))
verb = next(v for v in m["verbs"] if v["path"] == "issue index")
flag = next(f for f in verb["flags"] if f["name"] == "--limit")
flag["names"] = ["-n", "--limit"]
json.dump(m, open(path, "w"), indent=2, sort_keys=True)
PY

# COVERS: surface.long-flags-only
OUT=$(cd "$PROJECT_ROOT" && python3 - "$WORK/docs" <<'PY'
import json, sys
sys.path.insert(0, "scripts")
import surface_lint as s
docs = sys.argv[1]
manifest = json.load(open(f"{docs}/cli-surface.json"))
registry = s.load_registry(f"{docs}/cli-vocabulary.json")
unwaived, _ = s.lint(manifest, registry)
print("\n".join(f.key for f in unwaived))
PY
)
if printf '%s\n' "$OUT" | grep -q "short-flag:-n@issue index"; then
  pass "an unregistered short flag turns the lint red"
else
  fail "the lint passed a short flag outside the registry"
fi

# ---------------------------------------------------------------------------
echo "[3] the waiver baseline is shrink-only"
# ---------------------------------------------------------------------------

# COVERS: surface.vocabulary-registry
OUT=$(cd "$PROJECT_ROOT" && python3 - <<'PY'
import json, sys
sys.path.insert(0, "scripts")
import surface_lint as s
manifest = s.manifest_from_disk()
registry = s.load_registry()
registry["waivers"].append({
    "clause": "surface.one-meaning-per-flag",
    "key": "value-space:--limit@issue index",
    "issue": "IM-236", "note": "fixture: matches no finding"})
unwaived, _ = s.lint(manifest, registry)
print("\n".join(f.key for f in unwaived))
PY
)
if printf '%s\n' "$OUT" | grep -q "obsolete-waiver:"; then
  pass "a waiver matching no finding is itself a finding"
else
  fail "an obsolete waiver passed, so the baseline could stop shrinking"
fi

# COVERS: surface.vocabulary-registry
OUT=$(cd "$PROJECT_ROOT" && python3 - <<'PY'
import sys
sys.path.insert(0, "scripts")
import surface_lint as s
manifest = s.manifest_from_disk()
registry = s.load_registry()
owner = registry["waivers"][0]["issue"]
unwaived, _ = s.lint(manifest, registry,
                     issue_status=lambda i: "done" if i == owner else "open")
print("\n".join(f.key for f in unwaived))
PY
)
if printf '%s\n' "$OUT" | grep -q "closed-waiver-issue:"; then
  pass "a waiver whose issue closed is itself a finding"
else
  fail "a closed issue left its waiver silently in place"
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
