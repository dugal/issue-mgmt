#!/usr/bin/env bash
# im-259-admin-dry-run.sh -- pre-merge verification for IM-259.
#
# The three multi-file admin verbs (`admin reindex`, `admin snapshot`,
# `admin install-skills`) offer `--dry-run` (surface.preview-not-confirmation):
# each preview emits the SHAPE the real run would emit -- same stdout
# structure, same `--json` keys, counts and paths naming exactly what the
# real run would write -- and writes NOTHING: no file write, no commit, no
# index regeneration. The preview is distinguishable from a real run under
# `--json` via the unconditional `dry_run` boolean (the same preview marker
# `admin init-store` and `admin reap` already carry).
#
# Every probe here pins its store to a mktemp tree and (for install-skills)
# pins HOME; the operator's store and ~/.claude are never touched.
#
# Run from the project root:
#   tests/verification/im-259-admin-dry-run.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
. "$(dirname "$0")/lib.sh"
ISSUE="$ISSUE_MGMT_BIN/issue"

fail_count=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

WORKDIR="$(mktemp -d -t im-259-dry-run.XXXXXX)"
trap 'cd /; rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

# Content digest of every file under a tree (paths + bytes). mtimes are
# not part of the contract; "byte-identical" means the same file set with
# the same contents, which is what a write-nothing preview must preserve.
tree_digest() {
    if [ ! -e "$1" ]; then
        echo "ABSENT"
        return
    fi
    ( cd "$1" && find . -type f | LC_ALL=C sort | while IFS= read -r f; do
          shasum "$f"
      done | shasum | awk '{print $1}' )
}

json_field() {  # json_field FILE PY-EXPR  -> prints eval over obj
    python3 - "$1" "$2" <<'PY'
import json, sys
obj = json.load(open(sys.argv[1]))
print(eval(" ".join(sys.argv[2].split()), {"obj": obj}))
PY
}

mk_store() {
    local path="$1"
    mkdir -p "$path/IM"
    git -C "$path" init -q
    git -C "$path" config user.email "$FIXTURE_EMAIL"
    git -C "$path" config user.name test
    cat > "$path/IM/IM-001.md" <<'MD'
---
id: IM-001
title: "dry-run fixture"
type: chore
status: open
priority: low
created: 2026-01-01
updated: 2026-01-01
---
## Summary

fixture body
MD
    git -C "$path" add -A
    git -C "$path" commit -qm "init store"
}

###########################################################################
echo "== admin reindex --dry-run =="
###########################################################################

STORE="$WORKDIR/store-reindex"
mk_store "$STORE"
head0=$(git -C "$STORE" rev-parse HEAD)
dig0=$(tree_digest "$STORE")

ISSUE_STORE_ROOT="$STORE" "$ISSUE" admin reindex --dry-run --json \
    >"$WORKDIR/rx.out" 2>"$WORKDIR/rx.err"
rc=$?
[ "$rc" = 0 ] && pass "reindex --dry-run --json exits 0" \
    || fail "reindex --dry-run --json exit $rc"

[ "$(json_field "$WORKDIR/rx.out" 'obj["dry_run"] is True')" = "True" ] \
    && pass "reindex --dry-run --json marks the preview (dry_run true)" \
    || fail "reindex --dry-run --json dry_run marker wrong"

[ "$(json_field "$WORKDIR/rx.out" \
     'obj["mode"] == "store" and obj["issues"] == 1
      and obj["indexes_written"] == 2')" = "True" ] \
    && pass "reindex --dry-run emits the real run's shape (mode/counts)" \
    || fail "reindex --dry-run shape diverged: $(cat "$WORKDIR/rx.out")"

dig1=$(tree_digest "$STORE")
head1=$(git -C "$STORE" rev-parse HEAD)
# COVERS: surface.preview-not-confirmation
if [ "$dig0" = "$dig1" ] && [ "$head0" = "$head1" ] \
        && [ ! -e "$STORE/INDEX.md" ] && [ ! -e "$STORE/IM/INDEX.md" ]; then
    pass "reindex --dry-run leaves the store byte-identical (no index, no commit)"
else
    fail "reindex --dry-run wrote something (digest $dig0 -> $dig1)"
fi

# The preview previewed a real write: the real run creates what the
# preview counted, with the same structured counts.
ISSUE_STORE_ROOT="$STORE" "$ISSUE" admin reindex --json \
    >"$WORKDIR/rx-real.out" 2>/dev/null
[ -f "$STORE/INDEX.md" ] && [ -f "$STORE/IM/INDEX.md" ] \
    && [ "$(json_field "$WORKDIR/rx-real.out" \
            'obj["dry_run"] is False and obj["indexes_written"] == 2
             and obj["issues"] == 1')" = "True" ] \
    && pass "real reindex writes exactly what the preview counted" \
    || fail "real reindex diverged from the preview"

# Legacy mode preview.
LEG="$WORKDIR/legacy/.issues"
mkdir -p "$LEG"
cp "$STORE/IM/IM-001.md" "$LEG/IM-001.md"
ldig0=$(tree_digest "$LEG")
( cd "$WORKDIR/legacy" && unset ISSUE_STORE_ROOT && \
    "$ISSUE" admin reindex "$LEG" --dry-run --json ) \
    >"$WORKDIR/rx-leg.out" 2>/dev/null
ldig1=$(tree_digest "$LEG")
# COVERS: surface.preview-not-confirmation
if [ "$ldig0" = "$ldig1" ] && [ ! -e "$LEG/INDEX.md" ] \
        && [ "$(json_field "$WORKDIR/rx-leg.out" \
                'obj["mode"] == "legacy" and obj["dry_run"] is True
                 and obj["issues"] == 1')" = "True" ]; then
    pass "legacy reindex --dry-run writes no INDEX.md and keeps the shape"
else
    fail "legacy reindex --dry-run diverged"
fi

###########################################################################
echo "== admin snapshot --dry-run =="
###########################################################################

STORE2="$WORKDIR/store-snapshot"
mk_store "$STORE2"
echo "pending edit" >> "$STORE2/IM/IM-001.md"
head0=$(git -C "$STORE2" rev-parse HEAD)
dig0=$(tree_digest "$STORE2")

"$ISSUE" admin snapshot --store "$STORE2" --dry-run --json \
    >"$WORKDIR/sn.out" 2>"$WORKDIR/sn.err"
rc=$?
[ "$rc" = 0 ] && pass "snapshot --dry-run --json exits 0" \
    || fail "snapshot --dry-run --json exit $rc"

[ "$(json_field "$WORKDIR/sn.out" \
     'obj["dry_run"] is True and obj["committed"] is False
      and obj["commit"] is None
      and obj["paths"] == ["IM/IM-001.md"]')" = "True" ] \
    && pass "snapshot --dry-run names exactly what the real run would commit" \
    || fail "snapshot --dry-run payload wrong: $(cat "$WORKDIR/sn.out")"

dig1=$(tree_digest "$STORE2")
head1=$(git -C "$STORE2" rev-parse HEAD)
# COVERS: surface.preview-not-confirmation
if [ "$dig0" = "$dig1" ] && [ "$head0" = "$head1" ]; then
    pass "snapshot --dry-run leaves the store byte-identical (no stage, no commit)"
else
    fail "snapshot --dry-run changed the store (HEAD $head0 -> $head1)"
fi

# Human preview lists the pending paths; a clean store reads as clean.
out=$("$ISSUE" admin snapshot --store "$STORE2" --dry-run 2>/dev/null)
echo "$out" | grep -q "would make catchup commit: 1 pending path(s)" \
    && echo "$out" | grep -q "IM/IM-001.md" \
    && pass "snapshot --dry-run human preview lists the pending paths" \
    || fail "snapshot --dry-run human preview wrong: $out"

# The real run commits what the preview named.
"$ISSUE" admin snapshot --store "$STORE2" >/dev/null 2>&1
[ "$(git -C "$STORE2" rev-parse HEAD)" != "$head0" ] \
    && git -C "$STORE2" show --stat --oneline HEAD | grep -q "IM-001.md" \
    && pass "real snapshot commits exactly the previewed paths" \
    || fail "real snapshot did not commit the previewed path"

out=$("$ISSUE" admin snapshot --store "$STORE2" --dry-run 2>/dev/null)
[ "$out" = "store is clean; no commit made" ] \
    && pass "snapshot --dry-run on a clean store keeps the real run's wording" \
    || fail "snapshot --dry-run clean wording changed: $out"

###########################################################################
echo "== admin install-skills --dry-run =="
###########################################################################

BP="$WORKDIR/brew-prefix"
mkdir -p "$BP/bin" "$BP/share/issue-mgmt/skills"
cat > "$BP/bin/brew" <<EOF
#!/usr/bin/env bash
[ "\$1" = "--prefix" ] && echo "$BP" && exit 0
exit 1
EOF
chmod +x "$BP/bin/brew"
printf 'skill a\n' > "$BP/share/issue-mgmt/skills/im259-a.md"
printf 'skill b\n' > "$BP/share/issue-mgmt/skills/im259-b.md"
printf 'cheatsheet\n' > "$BP/share/issue-mgmt/CHEATSHEET.md"
SKHOME="$WORKDIR/skills-home"
mkdir -p "$SKHOME"

HOME="$SKHOME" PATH="$BP/bin:$PATH" \
    "$ISSUE" admin install-skills --dry-run --json \
    >"$WORKDIR/sk.out" 2>"$WORKDIR/sk.err"
rc=$?
[ "$rc" = 0 ] && pass "install-skills --dry-run --json exits 0" \
    || fail "install-skills --dry-run --json exit $rc"

[ "$(json_field "$WORKDIR/sk.out" \
     'obj["dry_run"] is True and obj["count"] == 2
      and [s["name"] for s in obj["installed"]] == ["im259-a", "im259-b"]
      and [s["existed"] for s in obj["installed"]] == [False, False]
      and obj["cheatsheet"].endswith("issue-mgmt-CHEATSHEET.md")')" = "True" ] \
    && pass "install-skills --dry-run lists what would be installed (all new)" \
    || fail "install-skills --dry-run payload wrong: $(cat "$WORKDIR/sk.out")"

# COVERS: surface.preview-not-confirmation
if [ ! -e "$SKHOME/.claude" ]; then
    pass "install-skills --dry-run writes nothing under HOME"
else
    fail "install-skills --dry-run created files under $SKHOME/.claude"
fi

# Real install, then a second preview distinguishes overwrite from new.
HOME="$SKHOME" PATH="$BP/bin:$PATH" \
    "$ISSUE" admin install-skills >/dev/null 2>&1
printf 'skill c\n' > "$BP/share/issue-mgmt/skills/im259-c.md"
dig0=$(tree_digest "$SKHOME")
HOME="$SKHOME" PATH="$BP/bin:$PATH" \
    "$ISSUE" admin install-skills --dry-run --json \
    >"$WORKDIR/sk2.out" 2>/dev/null
dig1=$(tree_digest "$SKHOME")

[ "$(json_field "$WORKDIR/sk2.out" \
     '[(s["name"], s["existed"]) for s in obj["installed"]]
      == [("im259-a", True), ("im259-b", True), ("im259-c", False)]')" \
  = "True" ] \
    && pass "install-skills --dry-run separates overwrites from new installs" \
    || fail "install-skills --dry-run existed flags wrong: $(cat "$WORKDIR/sk2.out")"

# COVERS: surface.preview-not-confirmation
if [ "$dig0" = "$dig1" ]; then
    pass "install-skills --dry-run leaves the installed tree byte-identical"
else
    fail "install-skills --dry-run modified the installed tree"
fi

# Human preview marks itself and writes nothing it does not say.
out=$(HOME="$SKHOME" PATH="$BP/bin:$PATH" \
    "$ISSUE" admin install-skills --dry-run 2>/dev/null)
echo "$out" | grep -q "Would install: im259-c -> .*(new)" \
    && echo "$out" | grep -q "Would install: im259-a -> .*(overwrite)" \
    && echo "$out" | grep -q "Dry run. 3 skill(s) would be installed" \
    && pass "install-skills --dry-run human preview mirrors the real lines" \
    || fail "install-skills --dry-run human preview wrong: $out"

###########################################################################
echo ""
if [ "$fail_count" -eq 0 ]; then
    echo "All checks passed."
    exit 0
else
    echo "$fail_count check(s) failed."
    exit 1
fi
