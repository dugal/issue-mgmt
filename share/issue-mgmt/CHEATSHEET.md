# issue-mgmt cheat sheet

One page for humans and agents working an issue store through the `issue`
CLI. It exists to close a measured discoverability gap: a 2026-05-31 survey
found roughly 30 of 81 direct-filesystem operations (raw `grep`, `ls`,
`cat`, and `git log` over the store) doing work a single `issue` verb
already does in one call. Every row below pairs a verb with the direct-FS
anti-pattern it replaces, so the reflex "reach for grep" becomes "reach for
`issue search`".

Reach for a verb, not the filesystem:

- Do not `grep -r` the store -- use `issue search` / `issue list`.
- Do not `ls`/`cat` files in the store dir -- use `issue show` / `issue index`.
- Do not `git log` an issue file -- use `issue history show`.
- Do not hand-edit frontmatter -- use `issue edit` / `issue transition` / `issue dep`.

The surface is three tiers. Run `issue <verb> -h` for any verb's full flags,
exit codes, and `--json` shape; this sheet is a map, not a substitute for
`--help`.

```
issue <verb>            routine work (create, start, edit, search, ...)
issue history <sub>     read-only git archaeology over the store
issue admin <sub>       maintenance, scaffolding, distribution
```

Global flags accepted before the verb args: `--store ROOT`, `--json`,
`--lock-timeout SECONDS`, `--no-reindex`, `--version`.

---

## Input conveniences (v3.1.0)

Two ergonomics landed alongside this sheet. Confirm the exact behavior with
`issue <verb> --help`.

- **ID and prefix canonicalization.** Any verb accepts a
  human-typed id or prefix and resolves it to the canonical on-disk form as
  a fallback after an exact match misses: the prefix is case-insensitive
  (`im` -> `IM`) and the number is integer-normalized against the real
  on-disk padding (`im-7`, `IM-07`, and `IM-007` all resolve to `IM-007`).
  This is normalization, not fuzzy matching -- only a provably unique
  mapping resolves; nothing is guessed. The canonical id flows into the
  per-issue lock, commit messages, `--json` `id` fields, audit notes, and
  `depends_on` `to:` edges. When the input actually changed it is announced
  on stderr, so `--json` stdout stays one clean document.
  ```
  issue show im-7                 # resolves to IM-007
  issue list --repo im            # resolves to IM
  issue dep add IM-102 blocks:im-84   # writes to: IM-084
  ```

- **`--json <fields>` field selection.** The read verbs (`show`,
  `search`, `list`, `index`, `ready`, and the `history` subverbs) accept a
  gh-style comma-separated field list after `--json`: bare `--json` emits
  the full documented shape, while `--json id,title,status` projects each
  object to just those top-level keys, in the order given. An unknown field
  name is a usage error (exit 2). There is no `--jq` passthrough -- pipe to
  jq yourself when you need nested selection.
  ```
  issue list --repo IM --json id,title,status
  issue show IM-050 IM-051 --json id,file
  ```

---

## Tier 1 -- routine verbs

Each row: what it does, the direct-FS habit it replaces, one example, and
its `/issue-*` Claude Code skill (or a note that it ships without one).

### create -- file a new issue
- Replaces: hand-writing a `PREFIX-NNN.md` file and picking the next id yourself.
- Example: `issue create "Fix embedding bug" bug high`
- Skill: `/issue-create`

### start -- claim an issue and move it to in-progress
- Replaces: editing `status:` and `branch:` frontmatter by hand; racing another session.
- Example: `issue start IM-024 --branch phase-x --claim claude-w2`
- Skill: `/issue-start` (also surfaces prior claims and runs the reaper)

### complete -- move an issue to done
- Replaces: two hand transitions; an `open` issue passes through in-progress atomically.
- Example: `issue complete IM-024 --reason "all acceptance checks passed"`
- Skill: `/issue-complete`

### cancel -- move an issue to cancelled (never delete)
- Replaces: deleting the file -- issue files are never removed.
- Example: `issue cancel IM-024 --reason "superseded by IM-099"`
- Skill: `/issue-cancel`

### block / unblock -- record or clear a blocker
- Replaces: hand-editing status plus a free-text note; `block` requires a reason.
- Example: `issue block IM-024 --reason "waiting on RM-038"` then `issue unblock IM-024`
- Skill: `/issue-block`

### transition -- the state-machine primitive (+ lease ops)
- Replaces: any manual `status:` edit; owns the legal-transition matrix and the TTL lease.
- Example: `issue transition IM-024 blocked --reason "waiting on RM-038"`
- Lease: `issue transition IM-024 --claim claude-w2 --lease-ttl 7200`, `--renew`, `--release`
- Skill: none directly -- use the `start`/`complete`/`cancel`/`block`/`unblock` skills, which wrap it.

### edit -- change frontmatter fields or body sections
- Replaces: opening the file in an editor to tweak a field or append to a section;
  `--replace-body-file PATH` ('-' = stdin) replaces the WHOLE body atomically for
  changes the section ops cannot express (e.g. a `###` subsection edit) -- never
  hand-edit a store file.
- Example: `issue edit IM-023 --set priority=high`
- Example: `issue edit IM-023 --section "Implementation Notes" --append "2026-08-02: x"`
- Example: `issue edit IM-023 --replace-body-file /tmp/new-body.md`
- Skill: `/issue-edit`

### note -- append a dated line to a section
- Replaces: hand-appending `<date>: <text>` to `## Implementation Notes`.
- Example: `issue note IM-072 "started work on the note verb"`
- Skill: `/issue-note`

### dep -- author or read depends_on edges
- Replaces: hand-editing the `depends_on:` YAML block (and risking a cycle).
- Example: `issue dep add IM-102 parent-child:IM-101 --note "spun out"`
- Example: `issue dep list IM-102`
- Skill: `/issue-dep`

### show -- print an issue as it is on disk
- Replaces: `cat`-ing the file, or `sed`-ing out one section.
- Example: `issue show IM-079 --section Design`
- Example: `issue show IM-050 IM-051 --json id,file`
- Skill: `/issue-show`

### search -- body + frontmatter search
- Replaces: `grep -r` over the store; supports substring and regex plus filters.
- Example: `issue search "concurrent edit" --prefix IM,DOUG --status open`
- Skill: `/issue-search`

### list -- filter-only enumeration (no text query)
- Replaces: `ls` in the store dir, then eyeballing status; `--repo` aliases `--prefix`.
- Example: `issue list --repo IM --status open --type bug`
- Skill: `/issue-list`

### index -- grouped-by-status board
- Replaces: hand-tallying open/in-progress counts from a directory listing.
- Example: `issue index --repo IM --status open,in-progress`
- Skill: none (read-only board; use `issue list`/`issue search` skills for scripted reads).

### ready -- the ready-to-claim set
- Replaces: manually checking which open issues have all their `blocks` deps done.
- Example: `issue ready --repo IM --actor claude-w5`
- Skill: none directly -- `/issue-start` consumes it opportunistically.

---

## Tier 2 -- history (read-only git archaeology)

All four run inside the store git repo, acquire no locks, and refuse legacy
mode with an actionable `git` fallback.

### history show -- one issue's commit log
- Replaces: `git log --follow -- <store>/PFX/ID.md`.
- Example: `issue history show IM-030`
- Skill: `/issue-history`

### history search -- pickaxe across store history
- Replaces: `git log -S "<term>"` inside the store.
- Example: `issue history search "ConcurrentEdit" --since 2.weeks.ago`
- Skill: `/issue-search-history`

### history at -- an issue as of a past ref or date
- Replaces: `git show <ref>:<store>/PFX/ID.md`.
- Example: `issue history at IM-030 --at v1.5.0`
- Skill: `/issue-show-at`

### history activity -- recent store activity grouped by issue
- Replaces: `git log --name-only` plus manual grouping.
- Example: `issue history activity --since 30.days.ago`
- Skill: `/issue-activity`

---

## Tier 3 -- admin (maintenance, scaffolding, distribution)

Utility surface. These deliberately ship without skills -- they are operator
tools, not per-issue lifecycle steps.

- `issue admin reindex` -- regenerate INDEX.md (`--all` walks every `.issues/` + the store).
- `issue admin validate FILE...` -- structural frontmatter linter (schema + id/filename/prefix).
- `issue admin snapshot` -- one-shot catchup sweep; commits pending store state, else exit 0.
- `issue admin reap` -- clear expired TTL leases (`--dry-run` to preview); never touches a valid lease.
- `issue admin init-store PATH` -- scaffold or converge a store (idempotent).
- `issue admin init-project` -- register the current repo under a prefix in `<store>/.repos`.
- `issue admin migrate --project PATH` -- one-shot copy of a project's `.issues/` into the store.
- `issue admin selftest` -- end-to-end smoke against a throwaway temp store (no network, self-cleaning).
- `issue admin install-skills` -- install the shipped skills into `~/.claude/skills/` (post-brew step).

---

## Skill map

Fifteen `/issue-*` skills wrap the verbs 1:1. Restart Claude Code after
`issue admin install-skills` for them to register.

Live-state (eleven): `/issue-create`, `/issue-start`, `/issue-complete`,
`/issue-cancel`, `/issue-block` (block + unblock), `/issue-edit`,
`/issue-dep`, `/issue-note`, `/issue-search`, `/issue-list`, `/issue-show`.

Read-only history (four): `/issue-history`, `/issue-search-history`,
`/issue-show-at`, `/issue-activity`.

No skill by design: `transition` (use the workflow wrappers above), `ready`
and `index` (read-only aggregates), and every `admin` subverb.

---

## Composition recipes

Common multi-step flows, each replacing a handful of ad hoc commands.

### Find ready work, then claim it
```
issue ready --repo IM --actor claude-w5 --json id,title
issue start IM-024 --branch issue-024-foo --claim claude-w5
```

### Complete with a closing note
```
issue note IM-024 "verified acceptance checks A-D"
issue complete IM-024 --reason "all acceptance checks passed"
```
`complete` on an `open` issue passes through in-progress in one commit, so
you can complete without a separate `start`.

### Add a dependency (blocks), safely
```
issue dep add IM-102 blocks:IM-101 --note "needs the lib landed first"
issue dep list IM-102
```
A `blocks` edge that would close a cycle is refused (exit 1) before any write.

### Read one section instead of the whole file
```
issue show IM-079 --section "Acceptance Criteria"
```

### Triage: what is open, and what changed recently
```
issue index --repo IM --status open,in-progress
issue history activity --since 7.days.ago
```

### Find when a token entered or left the store
```
issue history search "translate_bd_error"
```

### Block, then unblock when the dependency ships
```
issue block IM-024 --reason "waiting on RM-038"
issue unblock IM-024 --to in-progress --reason "RM-038 shipped"
```

---

Every mutating verb honors the loser-side exit-code contract on a write-time
loss: 3 concurrent-edit (retry once), 4 lost-race (pick another target),
5 stale-local-view (refresh first), 7 not-claimable (escalate). Read verbs
follow the grep convention: exit 0 with matches, exit 1 on an empty result.
