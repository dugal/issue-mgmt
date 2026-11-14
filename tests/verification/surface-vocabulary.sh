#!/usr/bin/env bash
# Surface vocabulary registry + linter verification (IM-246).
#
# The linter checks docs/cli-surface.json (what the surface IS, extracted
# from the argparse registrations) against docs/cli-vocabulary.json (what it
# is allowed to MEAN). This script exercises it the way a release gate
# would: clean on the committed tree, red on an injected violation, and
# red again when the baseline stops describing real findings.
#
# Every assertion here is independent of how large the waiver baseline is,
# including empty. A baseline of zero is what the surface-convergence phase
# is driving toward, so a script asserting "the baseline describes
# something" would have failed at the one moment the phase succeeded
# (IM-289); section [4] proves the zero state directly.
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

# The --json shape assertions live in one file so section [4] can run the
# very same code against a zero-waiver payload, instead of the script
# claiming zero-tolerance for assertions only ever exercised against a
# non-empty baseline.
cat >"$WORK/assert_shape.py" <<'SHAPE'
"""Assert surface-lint's --json shape. argv: <payload.json> <registry.json>

What is checked is the payload's consistency with the baseline that
produced it: no unwaived findings, totals that agree with the rows, and
every waived row naming both its owning issue and a waiver the registry
actually declares. Deliberately NOT checked is whether the baseline is
non-empty -- an empty baseline is the exit gate's success condition, not a
defect (IM-289).
"""
import json
import sys

payload = json.load(open(sys.argv[1]))
declared = {w["key"] for w in json.load(open(sys.argv[2]))["waivers"]}

assert payload["unwaived"] == [], payload["unwaived"]
assert payload["totals"]["unwaived"] == 0, payload["totals"]
assert payload["totals"]["waived"] == len(payload["waived"]), payload["totals"]
for row in payload["waived"]:
    assert row["issue"], row
    assert row["clause"].startswith("surface."), row
    assert row["key"] in declared, f"{row['key']} is waived but undeclared"
SHAPE

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
   && python3 "$WORK/assert_shape.py" "$WORK/lint.json" \
      "$PROJECT_ROOT/docs/cli-vocabulary.json"
then
  pass "--json: zero unwaived findings, each waived one declared and owned"
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

# The fixture names a synthetic issue rather than a live one: an owner id
# borrowed from the real baseline is state this fixture does not control.
# COVERS: surface.vocabulary-registry
OUT=$(cd "$PROJECT_ROOT" && python3 - <<'PY'
import sys
sys.path.insert(0, "scripts")
import surface_lint as s
manifest = s.manifest_from_disk()
registry = s.load_registry()
registry["waivers"].append({
    "clause": "surface.one-meaning-per-flag",
    "key": "value-space:--limit@issue index",
    "issue": "IM-000", "note": "fixture: deliberately matches no finding"})
unwaived, _ = s.lint(manifest, registry)
print("\n".join(f.key for f in unwaived))
PY
)
if printf '%s\n' "$OUT" \
   | grep -q "obsolete-waiver:value-space:--limit@issue index"; then
  pass "a waiver matching no finding is itself a finding"
else
  fail "an obsolete waiver passed, so the baseline could stop shrinking"
fi

# The fixture supplies BOTH halves -- a real finding (an unregistered flag
# name) and the waiver covering it -- so the check is proven against a
# waiver that was doing work. Reading waivers[0] off the committed registry
# would instead have raised IndexError the moment the baseline hit zero,
# which is this phase's success condition (IM-289).
# COVERS: surface.vocabulary-registry
OUT=$(cd "$PROJECT_ROOT" && python3 - <<'PY'
import copy
import sys
sys.path.insert(0, "scripts")
import surface_lint as s

FIXTURE = "IM-000"
manifest = copy.deepcopy(s.manifest_from_disk())
registry = copy.deepcopy(s.load_registry())
verb = next(v for v in manifest["verbs"] if v["path"] == "issue index")
entry = copy.deepcopy(next(f for f in verb["flags"] if f["name"] == "--limit"))
entry.update(name="--cap", names=["--cap"], dest="cap")
verb["flags"].append(entry)
registry["waivers"] = [{
    "clause": "surface.vocabulary-registry",
    "key": "unregistered-flag:--cap",
    "issue": FIXTURE, "note": "fixture: covers the finding injected above"}]
unwaived, waived = s.lint(
    manifest, registry,
    issue_status=lambda i: "done" if i == FIXTURE else "open")
keys = [f.key for f in unwaived]
assert "unregistered-flag:--cap" in {f.key for f, _ in waived}, keys
assert "obsolete-waiver:unregistered-flag:--cap" not in keys, keys
print("\n".join(keys))
PY
)
if printf '%s\n' "$OUT" | grep -q "closed-waiver-issue:unregistered-flag:--cap"
then
  pass "a waiver whose issue closed is itself a finding"
else
  fail "a closed issue left its waiver silently in place"
fi

# ---------------------------------------------------------------------------
echo "[4] an empty baseline is the exit gate's success, not a failure"
# ---------------------------------------------------------------------------

# Simulate the state this phase drives toward: the waived violations fixed
# AND their waivers gone. Each waiver key names the surface element it is
# about -- a flag, or a (concept, verb) positional pair -- so retiring that
# element from BOTH sides, the surface and the declarations describing it,
# is a self-consistent stand-in for whatever fix the owning issue lands,
# without this script having to predict it. It stays valid once the real
# baseline is empty, because there is then nothing to retire.
# COVERS: surface.vocabulary-registry
OUT=$(cd "$PROJECT_ROOT" && python3 - "$WORK" <<'PY'
import copy
import json
import re
import sys
sys.path.insert(0, "scripts")
import surface_lint as s

work = sys.argv[1]
manifest = copy.deepcopy(s.manifest_from_disk())
registry = copy.deepcopy(s.load_registry())

flags = set()
for waiver in registry["waivers"]:
    spelled = re.match(r"positional-spelling:([a-z0-9-]+)@(.+)$",
                       waiver["key"])
    if spelled:
        cid, verb_path = spelled.groups()
        dests = {row["dest"] for row in registry["positionals"]
                 if row["verb"] == verb_path and row.get("concept") == cid}
        registry["positionals"] = [
            row for row in registry["positionals"]
            if not (row["verb"] == verb_path and row["dest"] in dests)]
        entry = next(v for v in manifest["verbs"] if v["path"] == verb_path)
        entry["positionals"] = [p for p in entry["positionals"]
                                if p["dest"] not in dests]
        continue
    found = re.search(r"--[a-z0-9-]+", waiver["key"])
    if found:
        flags.add(found.group(0))
for verb in manifest["verbs"]:
    verb["flags"] = [f for f in verb["flags"] if not set(f["names"]) & flags]
for name in flags:
    registry["flags"].pop(name, None)
for concept in [k for k, c in registry["concepts"].items()
                if c["canonical"] in flags]:
    registry["concepts"].pop(concept)
registry["waivers"] = []

unwaived, waived = s.lint(manifest, registry)
with open(f"{work}/zero.json", "w") as fh:
    json.dump({
        "unwaived": [f.as_dict() for f in unwaived],
        "waived": [dict(f.as_dict(), issue=w["issue"]) for f, w in waived],
        "totals": {"unwaived": len(unwaived), "waived": len(waived)},
    }, fh)
with open(f"{work}/zero-registry.json", "w") as fh:
    json.dump(registry, fh)
print("%d %d" % (len(unwaived), len(waived)))
for finding in unwaived:
    print(finding.key, file=sys.stderr)
PY
)
if [ "$OUT" = "0 0" ]; then
  pass "the lint is clean with no findings and no waivers left to declare"
else
  fail "a zero-waiver baseline is not clean (unwaived/waived: $OUT)"
fi

# The same assertions section [1] runs against the live payload, re-run
# against the zero-baseline one. This is the half that used to fail.
# COVERS: surface.vocabulary-registry
if python3 "$WORK/assert_shape.py" "$WORK/zero.json" \
   "$WORK/zero-registry.json"; then
  pass "the --json shape assertions hold at a zero baseline"
else
  fail "the shape assertions require a non-empty baseline"
fi

echo
echo "PASS: $PASS    FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
