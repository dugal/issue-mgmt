#!/usr/bin/env bash
# Verification script for IM-240 -- --json on the six admin verbs that shipped
# without it.
#
# docs/requirements.md output.json-stable promises, as [implemented]: "Every
# CLI subcommand offers a stable --json output shape documented in --help.
# Under --json, stdout carries exactly one JSON document -- informational
# notices go to stderr -- so callers may parse the whole stream without
# line-filtering."
#
# Six verbs shipped without it through v3.3.1:
#
#   admin snapshot        admin init-store        admin reindex
#   admin init-project    admin install-skills    admin selftest
#
# `admin selftest` and `admin install-skills` build their parser with
# prefix_chars=chr(0) plus an nargs="*" catch-all and walk argv by hand, so a
# source-level flag sweep is blind to them -- which is how IM-240 was filed
# naming five verbs and listing `selftest` as already compliant. Every check
# below DRIVES the CLI rather than reading registrations, for that reason.
#
# Each check asserts the three properties the requirement names:
#   1. --json is accepted (not an exit-2 usage error)
#   2. stdout parses as exactly ONE JSON document, with no prose riding along
#   3. the documented keys are present regardless of outcome, so output shape
#      is a function of argv and not of store state
# plus that --help documents the shape.
#
# Run from the project root:
#     tests/verification/im-240-admin-json.sh

set -u

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
. "$(dirname "$0")/lib.sh"
ISSUE="$ISSUE_MGMT_BIN/issue"

PASS=0
FAIL=0
pass() { echo "PASS: $*"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

WORKDIR=$(mktemp -d -t im-240-verify.XXXXXX)
trap 'rm -rf "$WORKDIR"' EXIT
unset ISSUE_STORE_ROOT

# Commits inside the temp stores must not depend on the operator's git
# identity (nor on an overridden HOME having one).
export GIT_AUTHOR_NAME=im240
export GIT_AUTHOR_EMAIL="$FIXTURE_EMAIL"
export GIT_COMMITTER_NAME=im240
export GIT_COMMITTER_EMAIL="$FIXTURE_EMAIL"

# assert_one_json <label> <stdout-file>
# The load-bearing stdout-purity check: the whole stream must parse as one
# JSON document. A prose line (the auto-reindex summary, a progress line,
# an "Installed: ..." line) riding along makes json.load fail, which is
# exactly the breakage output.json-stable forbids.
assert_one_json() {
    if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$2" \
            >/dev/null 2>&1; then
        pass "$1: stdout is exactly one JSON document"
    else
        fail "$1: stdout is not one JSON document:"
        sed 's/^/  | /' "$2"
    fi
}

# assert_keys <label> <stdout-file> <key> [<key> ...]
assert_keys() {
    label=$1; file=$2; shift 2
    missing=$(python3 - "$file" "$@" <<'PY'
import json, sys
try:
    obj = json.load(open(sys.argv[1]))
except Exception as e:
    print("unparseable: %s" % e)
    sys.exit(0)
print(",".join(k for k in sys.argv[2:] if k not in obj))
PY
)
    if [ -z "$missing" ]; then
        pass "$label: documented keys all present"
    else
        fail "$label: missing key(s): $missing"
    fi
}

# assert_json_field <label> <stdout-file> <python-expr-on-obj>
# The expression may be written across several shell lines for legibility;
# whitespace is collapsed before eval so the continuation indent does not
# read as a Python indent.
assert_json_field() {
    if python3 - "$2" "$3" <<'PY' 2>/dev/null
import json, sys
try:
    obj = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if eval(" ".join(sys.argv[2].split()), {"obj": obj}) else 1)
PY
    then
        pass "$1"
    else
        fail "$1 (expr: $3)"
    fi
}

assert_help_documents_json() {
    out=$("$ISSUE" admin $1 --help 2>&1)
    if echo "$out" | grep -qF -- "--json"; then
        pass "admin $1 --help documents --json"
    else
        fail "admin $1 --help does not mention --json"
    fi
}

mk_store() {
    "$ISSUE" admin init-store "$1" --non-interactive >/dev/null 2>&1
}

###########################################################################
echo "== admin init-store =="
###########################################################################

# COVERS: output.json-stable
assert_help_documents_json init-store

# Preview run. --dry-run and --json compose; the preview marker is the
# `dry_run` boolean, the same field name admin reap --json already uses
# (agreed with IM-259, which owns --dry-run on the remaining verbs).
"$ISSUE" admin init-store "$WORKDIR/s1" --dry-run --json \
    >"$WORKDIR/is-dry.out" 2>"$WORKDIR/is-dry.err"
rc=$?
[ "$rc" = 0 ] && pass "init-store --dry-run --json exits 0" \
    || fail "init-store --dry-run --json exit $rc"
# COVERS: output.json-stable
assert_one_json "init-store --dry-run --json" "$WORKDIR/is-dry.out"
# COVERS: output.json-stable
assert_keys "init-store --dry-run --json" "$WORKDIR/is-dry.out" \
    store dry_run store_created git_initialized seeded present committed
assert_json_field "init-store --dry-run --json marks dry_run true" \
    "$WORKDIR/is-dry.out" 'obj["dry_run"] is True'
assert_json_field "init-store --dry-run --json plans all five seeds" \
    "$WORKDIR/is-dry.out" 'len(obj["seeded"]) == 5 and obj["present"] == []'
# The preview changed nothing.
[ ! -e "$WORKDIR/s1" ] && pass "init-store --dry-run wrote nothing" \
    || fail "init-store --dry-run created $WORKDIR/s1"

# Real run.
"$ISSUE" admin init-store "$WORKDIR/s1" --json \
    >"$WORKDIR/is-real.out" 2>/dev/null
# COVERS: output.json-stable
assert_one_json "init-store --json" "$WORKDIR/is-real.out"
assert_json_field "init-store --json reports the scaffold it performed" \
    "$WORKDIR/is-real.out" \
    'obj["dry_run"] is False and obj["git_initialized"] is True
     and obj["committed"] is True and len(obj["seeded"]) == 5'

# Idempotent convergence re-run: same keys, different values. Shape is a
# function of argv, never of store state.
"$ISSUE" admin init-store "$WORKDIR/s1" --json \
    >"$WORKDIR/is-again.out" 2>/dev/null
# COVERS: output.json-stable
assert_keys "init-store --json (converge)" "$WORKDIR/is-again.out" \
    store dry_run store_created git_initialized seeded present committed
assert_json_field "init-store --json converge: nothing seeded, nothing committed" \
    "$WORKDIR/is-again.out" \
    'obj["seeded"] == [] and obj["committed"] is False
     and len(obj["present"]) == 5'

# Human mode is untouched by the addition (surface.explicit-output-mode:
# text is the default, --json the opt-in).
out=$("$ISSUE" admin init-store "$WORKDIR/s1" 2>/dev/null)
echo "$out" | grep -q "^store: " \
    && pass "init-store without --json still prints the human report" \
    || fail "init-store human report changed: $out"

###########################################################################
echo "== admin init-project =="
###########################################################################

# COVERS: output.json-stable
assert_help_documents_json init-project

STORE="$WORKDIR/store"
mk_store "$STORE"
PROJ="$WORKDIR/proj"
mkdir -p "$PROJ"
git -C "$PROJ" init -q

( cd "$PROJ" && HOME="$WORKDIR/home" "$ISSUE" admin init-project \
    --store "$STORE" --prefix SMK --non-interactive --json ) \
    >"$WORKDIR/ip-first.out" 2>"$WORKDIR/ip-first.err"
rc=$?
[ "$rc" = 0 ] && pass "init-project --json exits 0" \
    || { fail "init-project --json exit $rc"; sed 's/^/  | /' "$WORKDIR/ip-first.err"; }
# COVERS: output.json-stable
assert_one_json "init-project --json" "$WORKDIR/ip-first.out"
# COVERS: output.json-stable
assert_keys "init-project --json" "$WORKDIR/ip-first.out" \
    repo prefix store registry already_registered template_seeded \
    prefix_file prefix_file_written sentinel sentinel_written
assert_json_field "init-project --json reports the fresh registration" \
    "$WORKDIR/ip-first.out" \
    'obj["prefix"] == "SMK" and obj["already_registered"] is False
     and obj["sentinel_written"] is True
     and obj["prefix_file_written"] is True'

# Idempotent re-run: the same keys with already_registered flipped.
( cd "$PROJ" && HOME="$WORKDIR/home" "$ISSUE" admin init-project \
    --store "$STORE" --prefix SMK --non-interactive --json ) \
    >"$WORKDIR/ip-again.out" 2>/dev/null
# COVERS: output.json-stable
assert_keys "init-project --json (re-run)" "$WORKDIR/ip-again.out" \
    repo prefix store registry already_registered template_seeded \
    prefix_file prefix_file_written sentinel sentinel_written
assert_json_field "init-project --json re-run marks already_registered" \
    "$WORKDIR/ip-again.out" \
    'obj["already_registered"] is True
     and obj["sentinel_written"] is False'

###########################################################################
echo "== admin snapshot =="
###########################################################################

# COVERS: output.json-stable
assert_help_documents_json snapshot

# Clean store: committed false, commit null, paths empty -- all keys still
# present, so a caller branches on `committed` not on key presence.
"$ISSUE" admin snapshot --store "$STORE" >/dev/null 2>&1   # drain registration
"$ISSUE" admin snapshot --store "$STORE" --json \
    >"$WORKDIR/sn-clean.out" 2>/dev/null
rc=$?
[ "$rc" = 0 ] && pass "snapshot --json exits 0 on a clean store" \
    || fail "snapshot --json clean exit $rc"
# COVERS: output.json-stable
assert_one_json "snapshot --json (clean)" "$WORKDIR/sn-clean.out"
# COVERS: output.json-stable
assert_keys "snapshot --json (clean)" "$WORKDIR/sn-clean.out" \
    store committed commit paths
assert_json_field "snapshot --json clean store: committed false, commit null" \
    "$WORKDIR/sn-clean.out" \
    'obj["committed"] is False and obj["commit"] is None
     and obj["paths"] == []'

# Dirty store: the sweep commits, and --json names both the sha and the
# swept paths (which the human line never reported).
printf 'pending\n' >> "$STORE/INDEX.md"
"$ISSUE" admin snapshot --store "$STORE" --json \
    >"$WORKDIR/sn-dirty.out" 2>/dev/null
# COVERS: output.json-stable
assert_one_json "snapshot --json (dirty)" "$WORKDIR/sn-dirty.out"
assert_json_field "snapshot --json dirty store: sha + swept paths" \
    "$WORKDIR/sn-dirty.out" \
    'obj["committed"] is True and len(obj["commit"]) == 40
     and obj["paths"] == ["INDEX.md"]'

###########################################################################
echo "== admin reindex =="
###########################################################################

# COVERS: output.json-stable
assert_help_documents_json reindex

# Store mode. The auto-reindex summary line is the informational notice
# output.json-stable names explicitly; under --json it must not be on
# stdout alongside the document.
ISSUE_STORE_ROOT="$STORE" "$ISSUE" admin reindex --json \
    >"$WORKDIR/rx-store.out" 2>/dev/null
rc=$?
[ "$rc" = 0 ] && pass "reindex --json exits 0 (store mode)" \
    || fail "reindex --json store exit $rc"
# COVERS: output.json-stable
assert_one_json "reindex --json (store)" "$WORKDIR/rx-store.out"
# COVERS: output.json-stable
assert_keys "reindex --json (store)" "$WORKDIR/rx-store.out" \
    mode targets indexes_written issues
assert_json_field "reindex --json store mode: one store target" \
    "$WORKDIR/rx-store.out" \
    'obj["mode"] == "store" and len(obj["targets"]) == 1
     and obj["targets"][0]["mode"] == "store"
     and "prefixes" in obj["targets"][0]'

# Legacy positional mode: same four top-level keys, a legacy target.
LEG="$WORKDIR/legacy/.issues"
mkdir -p "$LEG"
cat > "$LEG/AB-001.md" <<'EOF'
---
id: AB-001
title: "legacy sample"
type: chore
status: open
priority: low
updated: 2026-01-01
---
EOF
"$ISSUE" admin reindex "$LEG" --json >"$WORKDIR/rx-legacy.out" 2>/dev/null
# COVERS: output.json-stable
assert_one_json "reindex --json (legacy)" "$WORKDIR/rx-legacy.out"
# COVERS: output.json-stable
assert_keys "reindex --json (legacy)" "$WORKDIR/rx-legacy.out" \
    mode targets indexes_written issues
assert_json_field "reindex --json legacy mode: one legacy target, 1 issue" \
    "$WORKDIR/rx-legacy.out" \
    'obj["mode"] == "legacy" and obj["issues"] == 1
     and obj["indexes_written"] == 1
     and obj["targets"][0]["mode"] == "legacy"'

# --all mode: `targets` is the same list type with N entries. Scoped to a
# throwaway ISSUE_MGMT_ROOT with ISSUE_STORE_ROOT unset so the walk cannot
# reach the operator's real store.
( unset ISSUE_STORE_ROOT
  ISSUE_MGMT_ROOT="$WORKDIR/legacy" "$ISSUE" admin reindex --all --json ) \
    >"$WORKDIR/rx-all.out" 2>/dev/null
# COVERS: output.json-stable
assert_one_json "reindex --all --json" "$WORKDIR/rx-all.out"
assert_json_field "reindex --all --json: mode all, legacy target found" \
    "$WORKDIR/rx-all.out" \
    'obj["mode"] == "all" and len(obj["targets"]) == 1
     and obj["targets"][0]["mode"] == "legacy"'

# An --all run that finds nothing yields an empty list and zero counts --
# NOT a missing key and not the "No .issues/ directories" prose line.
mkdir -p "$WORKDIR/barren"
( unset ISSUE_STORE_ROOT
  ISSUE_MGMT_ROOT="$WORKDIR/barren" "$ISSUE" admin reindex --all --json ) \
    >"$WORKDIR/rx-none.out" 2>/dev/null
# COVERS: output.json-stable
assert_one_json "reindex --all --json (nothing found)" "$WORKDIR/rx-none.out"
assert_json_field "reindex --all --json empty: empty targets, zero counts" \
    "$WORKDIR/rx-none.out" \
    'obj["targets"] == [] and obj["indexes_written"] == 0
     and obj["issues"] == 0'

# Human mode unchanged.
out=$(ISSUE_STORE_ROOT="$STORE" "$ISSUE" admin reindex 2>/dev/null)
echo "$out" | grep -q "^Reindexed " \
    && pass "reindex without --json still prints the summary line" \
    || fail "reindex human summary changed: $out"

###########################################################################
echo "== admin install-skills =="
###########################################################################

# COVERS: output.json-stable
assert_help_documents_json install-skills

# Synthetic brew prefix + isolated HOME (pattern lifted from
# im-029-homebrew.sh Block 3), so the real ~/.claude/skills/ is untouched.
BP="$WORKDIR/brew-prefix"
mkdir -p "$BP/bin" "$BP/share/issue-mgmt/skills"
cat > "$BP/bin/brew" <<EOF
#!/usr/bin/env bash
[ "\$1" = "--prefix" ] && echo "$BP" && exit 0
exit 1
EOF
chmod +x "$BP/bin/brew"
printf 'sentinel-a\n' > "$BP/share/issue-mgmt/skills/im240-a.md"
printf 'sentinel-b\n' > "$BP/share/issue-mgmt/skills/im240-b.md"
printf 'cheatsheet\n' > "$BP/share/issue-mgmt/CHEATSHEET.md"

HOME="$WORKDIR/skills-home" PATH="$BP/bin:$PATH" \
    "$ISSUE" admin install-skills --json \
    >"$WORKDIR/sk.out" 2>"$WORKDIR/sk.err"
rc=$?
[ "$rc" = 0 ] && pass "install-skills --json exits 0" \
    || fail "install-skills --json exit $rc"
# COVERS: output.json-stable
assert_one_json "install-skills --json" "$WORKDIR/sk.out"
# COVERS: output.json-stable
assert_keys "install-skills --json" "$WORKDIR/sk.out" \
    source dest installed count cheatsheet
assert_json_field "install-skills --json lists each installed skill" \
    "$WORKDIR/sk.out" \
    'obj["count"] == 2
     and [s["name"] for s in obj["installed"]] == ["im240-a", "im240-b"]
     and obj["cheatsheet"].endswith("issue-mgmt-CHEATSHEET.md")'
# The per-skill "Installed: ..." progress lines must NOT be on stdout under
# --json (they are what would break json.load).
grep -q "Installed:" "$WORKDIR/sk.out" \
    && fail "install-skills --json leaked a progress line onto stdout" \
    || pass "install-skills --json: no progress lines on stdout"

# A formula predating the cheat sheet: cheatsheet is null, not absent, and
# the stderr note is the only thing on stderr.
rm "$BP/share/issue-mgmt/CHEATSHEET.md"
HOME="$WORKDIR/skills-home2" PATH="$BP/bin:$PATH" \
    "$ISSUE" admin install-skills --json \
    >"$WORKDIR/sk2.out" 2>"$WORKDIR/sk2.err"
# COVERS: output.json-stable
assert_one_json "install-skills --json (no cheat sheet)" "$WORKDIR/sk2.out"
assert_json_field "install-skills --json: absent cheat sheet is null, not missing" \
    "$WORKDIR/sk2.out" 'obj["cheatsheet"] is None'
grep -qF "no cheat sheet found" "$WORKDIR/sk2.err" \
    && pass "install-skills --json keeps the cheat-sheet notice on stderr" \
    || fail "install-skills --json cheat-sheet notice not on stderr"

###########################################################################
echo "== admin selftest =="
###########################################################################

# COVERS: output.json-stable
assert_help_documents_json selftest

# selftest pins HOME and ISSUE_STORE_ROOT inside its own mktemp store, so
# this drives the real end-to-end smoke without touching anything.
"$ISSUE" admin selftest --json >"$WORKDIR/st.out" 2>"$WORKDIR/st.err"
rc=$?
[ "$rc" = 0 ] && pass "selftest --json exits 0" \
    || { fail "selftest --json exit $rc"; sed 's/^/  | /' "$WORKDIR/st.err"; }
# COVERS: output.json-stable
assert_one_json "selftest --json" "$WORKDIR/st.out"
# COVERS: output.json-stable
assert_keys "selftest --json" "$WORKDIR/st.out" \
    result checks ok skipped failed
assert_json_field "selftest --json reports a pass with per-check records" \
    "$WORKDIR/st.out" \
    'obj["result"] == "pass" and obj["failed"] is None
     and obj["ok"] == len(obj["checks"]) and obj["ok"] >= 9
     and all(c["status"] == "ok" and c["name"] for c in obj["checks"])'
# The "ok: ..." progress lines are the ones that would break json.load.
grep -q "^ok: " "$WORKDIR/st.out" \
    && fail "selftest --json leaked progress lines onto stdout" \
    || pass "selftest --json: no progress lines on stdout"

# A FAILING selftest still emits the document, so a caller gets the failed
# check's name without scraping stderr. Forced by shadowing git with a stub
# that always fails, so the smoke dies on its first git call. (PATH cannot
# simply be emptied: the dispatcher's own `#!/usr/bin/env bash` needs it.)
BADGIT="$WORKDIR/badgit"
mkdir -p "$BADGIT"
printf '#!/usr/bin/env bash\nexit 1\n' > "$BADGIT/git"
chmod +x "$BADGIT/git"
PATH="$BADGIT:$PATH" "$ISSUE" admin selftest --json \
    >"$WORKDIR/st-fail.out" 2>"$WORKDIR/st-fail.err"
rc=$?
[ "$rc" = 1 ] && pass "selftest --json exits 1 on a failed check" \
    || fail "selftest --json failure exit $rc"
# COVERS: output.json-stable
assert_one_json "selftest --json (failing)" "$WORKDIR/st-fail.out"
# COVERS: output.json-stable
assert_keys "selftest --json (failing)" "$WORKDIR/st-fail.out" \
    result checks ok skipped failed
assert_json_field "selftest --json failure names the failed check" \
    "$WORKDIR/st-fail.out" \
    'obj["result"] == "fail" and isinstance(obj["failed"], str)
     and "git" in obj["failed"]'

###########################################################################
echo "---"
if [ "$FAIL" = 0 ]; then
    echo "ALL CHECKS PASSED ($PASS)"
    exit 0
else
    echo "$FAIL CHECK(S) FAILED ($PASS passed)"
    exit 1
fi
