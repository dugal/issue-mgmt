---
name: issue-dep-dev
description: Author and read an issue's depends_on dependency edges. Wraps issue.dep. Use to add or remove a blocks / parent-child / related / discovered-from edge between two issues (cross-prefix allowed), or to list an issue's edges. Prefer this over hand-editing the depends_on frontmatter block.
user-invocable: true
---

# Issue Dep

Author and read the `depends_on` dependency edges on an issue. Thin wrapper over `issue edit --add-dep / --remove-dep` for the mutating paths, plus a lock-free `list`. Dependency edges are the source-side `{to, type}` block sequence that `issue ready` consumes -- so authoring them through this verb keeps the frontmatter authoritative and consistent, instead of hand-editing (which is racey against a parallel session's catchup sweep and lands under a generic `catchup:` commit).

Wraps the CLI subcommand that will be exposed as MCP tool `issue.dep` in Phase 2.

## Invocation

```
/issue-dep-dev add    <id> KIND:TARGET [--note TEXT] [--json] [--no-reindex] [--lock-timeout <seconds>]
/issue-dep-dev remove <id> TARGET [--type KIND] [--json] [--no-reindex] [--lock-timeout <seconds>]
/issue-dep-dev list   <id> [--json]
```

## Parameters

- `<id>` (required) -- the issue that carries the edge, e.g. `XYZ-064`, `ABC-102`.
- `KIND:TARGET` (add, required) -- `KIND` is one of `blocks`, `parent-child`, `related`, `discovered-from` (the four dependency kinds). `TARGET` is a `PREFIX-NNN` issue id. Splits on the first colon only (so `parent-child:ABC-101` parses correctly). Cross-prefix targets are accepted write-now / resolve-later -- no store lookup.
- `TARGET` (remove, required) -- the id whose edge(s) to drop.
- `--note TEXT` (add, optional) -- a note recorded on the new edge.
- `--type KIND` (remove, optional) -- only remove the edge to `TARGET` of this kind (otherwise all edges to `TARGET` are removed).
- `--json` (optional) -- emit a one-shot summary object on stdout.
- `--no-reindex` (optional, add/remove) -- skip the post-write auto-reindex.
- `--lock-timeout <seconds>` (optional, add/remove, default 5) -- per-issue advisory-lock timeout.

## How it works

```bash
issue dep add <id> KIND:TARGET [flags...]
issue dep remove <id> TARGET [--type KIND] [flags...]
issue dep list <id> [--json]
```

`add` / `remove` delegate to `issue edit`'s per-issue lock + snapshot + atomic-write + commit path (commit message `dep <id>`) and mutate the `depends_on:` frontmatter block, not the body. Re-adding an existing `(to, type)` edge is an idempotent no-op; removing an absent edge is a no-op. Removing the last edge writes `depends_on: []` -- it never deletes the key, because an absent key reads as "unmanaged"; writing the empty list records an explicit retraction instead of leaving an ambiguous absent-key state. The `updated:` field is auto-stamped on success.

`list` reads the frontmatter without acquiring a lock and may return slightly stale results if a write commits during the read.

## Error semantics (exit codes)

- `0` -- success (including an idempotent add/remove no-op).
- `1` -- issue file not found; malformed frontmatter.
- `2` -- argument / usage error: missing subcommand or id, malformed `KIND:TARGET`, a `KIND` outside the four dependency kinds, or a malformed `TARGET` id.
- `3` -- `concurrent-edit`: lock timeout or hand-edit divergence at verify time. **Retry once** with fresh state (re-read the issue, re-issue the edit); on persistent failure surface the error verbatim and stop.
- `4` -- `lost-race`: a claim CAS was lost. Pick another target.
- `5` -- `stale-local-view`: refresh the local read before retrying.
- `7` -- `not-claimable`: escalate; do not loop.

(The loser-side contract; exit `3` was `17` through v2.4.1. Under `--json` the same condition is emitted as a one-line JSON object on stderr.)

## Examples

Add a cross-prefix blocks edge (the motivating XYZ-064 -> QRS-107 case):
```
/issue-dep-dev add XYZ-064 blocks:QRS-107
```

Add a parent-child edge with a note (note the hyphen in the kind):
```
/issue-dep-dev add ABC-102 parent-child:ABC-101 --note "spun out of parent"
```

Remove a specific edge kind:
```
/issue-dep-dev remove XYZ-064 QRS-107 --type blocks
```

List an issue's edges:
```
/issue-dep-dev list XYZ-064
/issue-dep-dev list XYZ-064 --json
```

## Notes

- Only the four dependency kinds are valid; a `KIND` outside them exits 2.
- Reverse queries ("what is blocked by X") are not supported by `list` by design -- use `issue ready` for the ready-set rather than expecting `list` to show inbound edges.
- For frontmatter scalar fields use `/issue-edit-dev`; for body sections use `/issue-edit-dev` or `/issue-note-dev`. This skill is exclusively for `depends_on` edges.
