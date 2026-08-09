# Migration Guide -- moving a project's `.issues/` into the issue store

This guide walks through migrating a project's per-project `.issues/`
directory into a central issue store. It is the human-runnable recipe
for the migration pipeline (`issue admin init-project` +
`issue admin migrate` + `issue admin reindex`).

Symbol convention used throughout this guide:

- `<project>` -- the project directory you are migrating (e.g. `myrepo`).
- `<PROJECT-PATH>` -- the path to that project
  (e.g. `~/projects/myrepo`).
- `<PREFIX>` -- the project's issue ID prefix (e.g. `ABC`). Must match
  `^[A-Z][A-Z0-9]{1,7}$`.
- `<store>` -- the issue store root (e.g. `~/issues-store`).

The worked example throughout uses myrepo / `ABC` /
`~/issues-store`. Substitute your own values.

## Who this is for

Use this guide when:

- A project already has a populated `<PROJECT-PATH>/.issues/` directory
  (one or more `<PREFIX>-NNN.md` files).
- You want to relocate those issues into a central store so that
  multiple projects can coexist and so the read-side history
  subcommands (`issue history show`, `issue history search`,
  `issue history at`, `issue history activity`) operate against a single
  git repo.
- The project has not already been migrated (no
  `<PROJECT-PATH>/.issues/.migrated-to-store` sentinel).

If the project has no existing issue management at all, see the
"Getting started" section of `README.md` instead. That path is the
green-field flow; this guide is the migration flow.

## Prerequisites

- The `issue-mgmt` formula is installed (see the install instructions in
  `README.md`) and `issue admin install-skills` has been run at least once
  if you want the Claude Code skills.
- The store directory exists. It is OK for the store to be empty
  apart from `LAYOUT.md` and `README.md` -- `.repos` and prefix
  subdirectories are created by the migration pipeline. The store
  must be a git repository.
- The project being migrated is itself a git repository so that
  `issue admin init-project` can derive the registry key from
  `basename $(git rev-parse --show-toplevel)`. Override the
  derivation with `--repo NAME` if you want a different key.
- The project has `<PROJECT-PATH>/.issues/.prefix` containing the
  prefix you intend to register, matching `^[A-Z][A-Z0-9]{1,7}$`.
- You have decided the prefix is correct. Once registered in the
  store's `.repos`, the project-to-prefix mapping is 1:1 and is not
  meant to change casually -- changing it later requires touching the
  registry by hand and rewriting every issue ID.

## Blast radius cheat sheet

| Step | Mutates | Reversible? |
|------|---------|-------------|
| 1. `issue admin init-project` | `<store>/.repos` (one line appended) | yes -- delete the line |
| 2. `issue admin migrate --dry-run` | nothing | n/a |
| 3. Inspect dry-run | nothing | n/a |
| 4. `issue admin migrate` (apply) | `<store>/<PREFIX>/*` and `<anchor>/.migrated-to-store` | yes -- delete the target tree and sentinel |
| 5. `shasum` check | nothing | n/a |
| 6. `issue admin validate` | nothing | n/a |
| 7. `issue admin reindex` | INDEX files only | yes -- regenerated |
| 8. `git commit` in store | new commit on store repo | yes -- standard git |
| 9. Read subcommands | nothing | n/a |
| 10. `issue create` smoke write | one new issue in store | yes -- transition to `cancelled` |

Only steps 1, 4, 7, 8, and 10 write. Everything else is read-only
inspection.

## Procedure

### Step 1. Register the project in the store

```bash
cd <PROJECT-PATH>
issue admin init-project --store <store> --prefix <PREFIX> --repo <project>
```

Worked example:

```bash
cd ~/projects/myrepo
issue admin init-project --store ~/issues-store --prefix ABC --repo myrepo
```

Expected:

- Exit 0.
- `<store>/.repos` exists and contains exactly one `<project>:<PREFIX>`
  line per registered project (lexicographically sorted by basename).
- `<PROJECT-PATH>/.issues/.prefix` is left alone if present and
  agrees with `--prefix`; created if absent.

### Step 2. Dry-run the migration

```bash
issue admin migrate --project <PROJECT-PATH> --store <store> --dry-run
```

Expected: a plan listing the issue copies (`<PREFIX>-NNN.md`),
the `.prefix` companion copy, the `template:` decision line, the
cross-reference scan result, and the line "no files written
(dry-run)." Exit 0.

The `template:` line will be one of:

- `template:      skipped -- matches canonical at <canonical-path> (sha=<short>)`
  -- the project's local `ISSUE-TEMPLATE.md` is byte-identical to the
  canonical (`<store>/ISSUE-TEMPLATE.md` if present, else the Homebrew
  generic), so no per-prefix copy is written. This is the expected
  case whenever the local copy is the unmodified Homebrew template.
- `template:      copied as override (local sha=... differs from canonical sha=... at <canonical-path>)`
  -- the local template diverges from the canonical and lands at
  `<store>/<PREFIX>/ISSUE-TEMPLATE.md` as a real per-prefix override.
- `template:      absent in source (none to copy)` -- no
  `<anchor>/ISSUE-TEMPLATE.md` exists.

### Step 3. Inspect the dry-run output

Human gate. Confirm:

- The number of issue copies matches `ls <PROJECT-PATH>/.issues/<PREFIX>-*.md`.
- No extraneous files surfaced (the planner records `*.md` files
  that do not match the prefix; investigate before applying).
- Cross-ref scan: every `XX-NNN` token either resolves into the
  in-flight corpus (own-prefix) or into already-migrated store
  prefixes, or is flagged as unresolved. Cross-project tokens to
  not-yet-migrated projects are expected warnings during the
  migration window, not errors -- they will resolve once the
  referenced project migrates.

### Step 4. Apply the migration

```bash
issue admin migrate --project <PROJECT-PATH> --store <store>
```

Side effects (in order):
1. Writes `<N>` issue files plus the `.prefix` companion into
   `<store>/<PREFIX>/`. `ISSUE-TEMPLATE.md` is copied only when the
   local template diverges from the canonical (see Step 2).
2. Drives a store-side commit -- the migration commit -- whose tree
   is exactly the newly-migrated files. Default message:
   `feat: migrate <repo> to store (<PFX>-MIN..<PFX>-MAX)`. Override
   with `--message MSG` (e.g. to append a coordinating note).
3. Appends one TSV line to `<store>/.migrations` and drives a second
   store-side commit (`chore: record .migrations entry for <PFX>`)
   capturing the boundary metadata. See "What .migrations records"
   below.
4. Writes `<PROJECT-PATH>/.issues/.migrated-to-store` sentinel last.
   The sentinel's content is the store-root path, which is what
   flips every store-mode-aware writer in the project's directory
   tree onto the store.

Both store-side commits are **path-restricted** (`git commit -- <paths>`),
so unrelated dirty state in the store working tree is preserved.

Idempotency:
- If the sentinel is present, the command exits 0 with
  `already migrated; sentinel at <path>` and does NOT touch
  `.migrations`.
- If the sentinel is absent but issue files for the prefix already
  exist in the store (signature of a crash between the migration
  commit and the sentinel write), the command refuses with
  `partial prior migration detected` so a re-run cannot
  double-commit. Resolve manually before re-running.

Source `.issues/` files remain on disk untouched. They are deleted
separately, not by this step.

Expected: the same plan as the dry-run, followed by:
```
  migration commit:  <sha12> feat: migrate <repo> to store (...)
  .migrations line:  <PFX>\t<path>\t<src-sha>\t<store-sha>\t<iso8601>
  .migrations commit: <sha12>
  sentinel written:  <path>
```
Exit 0.

### What `.migrations` records

`<store>/.migrations` is a git-tracked TSV file, one line per
migrated project, append-only. Field order:

```
<PREFIX>  <source_repo_abs_path>  <source_pre_sha>  <store_migration_sha>  <iso8601>
```

- `<PREFIX>` -- the migrated project's namespace (e.g. `IM`).
- `<source_repo_abs_path>` -- `os.path.realpath` of the source repo
  root, captured at migration time. Mount-form paths normalize.
- `<source_pre_sha>` -- the source repo's HEAD sha *before* the tool
  writes the source-side sentinel. This is the boundary: any
  `.issues/<id>.md` content at this sha or earlier is recoverable
  from the source repo.
- `<store_migration_sha>` -- the store-side commit sha that landed
  the migrated files (step 2 above). The `.migrations` line is
  written by the follow-up commit; this field points to its parent.
- `<iso8601>` -- timestamp of the migration commit, UTC.

This artifact preserves the boundary metadata a future history-import
or hybrid-pointer option would need, if the plain cutover ever proves
insufficient.

### Step 5. Verify byte-identity (sha256)

```bash
for f in <PROJECT-PATH>/.issues/<PREFIX>-*.md; do
  id=$(basename "$f")
  shasum -a 256 "$f" "<store>/<PREFIX>/$id"
done
```

Worked example:

```bash
for n in 001 002 003 004; do
  shasum -a 256 ~/projects/myrepo/.issues/ABC-$n.md \
                ~/issues-store/ABC/ABC-$n.md
done
```

Expected: each pair of hashes matches. This is the
byte-identity acceptance gate; the migration tool also computes the
sha internally and aborts on mismatch, but running it again from the
outside guarantees you can independently confirm the result.

### Step 6. Validate structurally

```bash
issue admin validate <store>/<PREFIX>/<PREFIX>-*.md
```

Worked example:

```bash
issue admin validate ~/issues-store/ABC/ABC-*.md
```

Expected: exit 0, no violations.

Note: scope the glob to `<PREFIX>-*.md`, not `*.md`. See Troubleshooting
below.

### Step 7. Reindex the store

```bash
ISSUE_STORE_ROOT=<store> issue admin reindex
```

Side effect: writes `<store>/INDEX.md` (root summary) and
`<store>/<PREFIX>/INDEX.md` (per-prefix detail) via the store's
index lock. Independent of any per-issue locks, so this is safe to
run concurrently with edits in other prefixes.

Expected: a summary line of the form `Reindexed <store>/INDEX.md
and 1 per-prefix index(es) -- N issue(s) total.`

### Step 8. Commit the INDEX and registry updates

```bash
cd <store>
git add .repos INDEX.md <PREFIX>/INDEX.md
git commit -m "chore: reindex store after <PREFIX> migration"
```

The migration commit itself (the migrated issue files plus the
`.prefix` companion) was already driven by `issue admin migrate` in
Step 4, along with the follow-up `.migrations` record commit. This
step covers only the derived state -- the regenerated INDEX files
from Step 7 and the registry entry from Step 1 -- which are not part
of the migration commit's tree.

`.locks/` is gitignored, so the per-issue lockfile tree under
`<store>/<PREFIX>/.locks/` is correctly excluded.

After this step, the read-side history subcommands
(`issue history show`, `issue history activity`) resolve cleanly against both
the migration commit and the index commit.

### Step 9. Exercise the four read subcommands

```bash
ISSUE_STORE_ROOT=<store> issue history show <PREFIX>-001
ISSUE_STORE_ROOT=<store> issue history search "<PREFIX>-001"
ISSUE_STORE_ROOT=<store> issue history at <PREFIX>-001 --at HEAD
ISSUE_STORE_ROOT=<store> issue history activity --since 1.hour.ago
```

Expected:

- `issue history show`: one TSV row, the step-8 commit, newest first.
- `issue history search`: at least the step-8 commit (any commit that
  added the literal `<PREFIX>-001` token).
- `issue history at --at HEAD`: the file contents at the step-8
  commit, ending with a newline.
- `issue history activity --since 1.hour.ago`: one row per migrated
  issue with `commit_count=1` and `latest` pointing at the step-8
  commit; sorted newest-first, ties broken by ID ascending.

### Step 10. Smoke-test the write path in store mode

```bash
cd <PROJECT-PATH>
issue create "Stage A0 smoke write" chore low
```

Expected: a new `<PREFIX>-NNN.md` lands in `<store>/<PREFIX>/`,
not in `<PROJECT-PATH>/.issues/`. The sentinel from step 4 is what
routes the writer onto the store. Store INDEX files are rewritten.

After confirming, cancel the smoke issue so it does not pollute the
live corpus:

```bash
ISSUE_STORE_ROOT=<store> issue cancel <PREFIX>-NNN \
  --reason "smoke write -- not real work"
```

### Step 11. Record the outcome

Record the outcome wherever your project tracks migration history.

## Troubleshooting

### Step 6: `issue admin validate <PREFIX>/*.md` flags `ISSUE-TEMPLATE.md`

If you run the validation step with the unscoped glob `<PREFIX>/*.md`,
the expansion includes a migrated `ISSUE-TEMPLATE.md` (when one was
written as a real per-prefix override -- see Step 2). That file carries
placeholder frontmatter values like `type: bug | feature | chore |
spike | decision` and `created: YYYY-MM-DD`. `issue admin validate` correctly
rejects those, exiting 1, even though every real issue is clean.

Scope to issue files with the prefix-shaped glob `<PREFIX>/<PREFIX>-*.md`
(as written in Step 6 above). This is documentation-only --
`issue admin validate` behaves correctly (a pure linter validates whatever it
is pointed at), and `issue admin reindex` skips `ISSUE-TEMPLATE.md` on its
own glob.

This is rare: the de-dup pass only writes a per-prefix
`ISSUE-TEMPLATE.md` when the project's local template diverges from the
canonical. Most projects get the skip path and never produce a
`<store>/<PREFIX>/ISSUE-TEMPLATE.md`.
