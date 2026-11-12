---
name: issue-list
description: List issues filtered by prefix/status/type without a body query. Wraps issue.list. Use for "what's open in a given prefix?" or "show all blocked issues across the store". Read-only; no locks acquired.
user-invocable: true
---

# Issue List

List issues across the store filtered by frontmatter scope (no body query). Wraps `issue search` with an empty query.

Read-only: no locks acquired; safe to run alongside in-flight writes.

Wraps the CLI subcommand that will be exposed as MCP tool `issue.list` in Phase 2.

## Invocation

```
/issue-list [--prefix PFX[,PFX...]] [--status S[,S...]] [--type T[,T...]] [--json]
```

## Parameters

- `--prefix PFX[,PFX...]` (optional) -- comma-separated prefix scope (e.g. `ABC`, `ABC,XYZ`). Omit for the whole store.
- `--status S[,S...]` (optional) -- one or more of `open`, `in-progress`, `blocked`, `done`, `cancelled`.
- `--type T[,T...]` (optional) -- one or more of `bug`, `feature`, `chore`, `spike`, `decision`.
- `--json` (optional) -- emit structured per-issue records.

If no filters are supplied, every issue in scope is listed.

## How it works

```bash
issue search "" [filter flags...]
```

The empty positional query triggers frontmatter-only matching (the underlying matcher short-circuits the body scan when no body filter is active). Default output is TSV: `<id>\t<status>\t<priority>\t<title>` in scan order.

## Error semantics (exit codes)

- `0` -- at least one issue matches.
- `1` -- zero matches.
- `2` -- argument/usage error.

This skill is read-only; exit code 3 (`concurrent-edit`) is not produced.

## Examples

```
/issue-list --prefix ABC --status open
/issue-list --status in-progress
/issue-list --prefix ABC,XYZ --status open,blocked
/issue-list --type bug --json
```

## Notes

- This skill is a convenience wrapper over `/issue-search`. Prefer this one when there is no body query to express -- it makes intent ("list, no search") explicit.
- For "show me one specific issue", use `/issue-show <id>` instead of a single-issue search.
