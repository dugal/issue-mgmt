---
name: issue-search
description: Search issue bodies and frontmatter across the store. Wraps issue.search. Use to find issues mentioning a term, filtered by prefix/status/type. Read-only; no locks acquired.
user-invocable: true
---

# Issue Search

Substring or regex search across issue bodies, with frontmatter filters. Wraps `issue search`.

Read-only: no locks acquired; safe to run alongside in-flight writes (may return slightly stale results if a write commits during the scan).

Wraps the CLI subcommand that will be exposed as MCP tool `issue.search` in Phase 2.

## Invocation

```
/issue-search "<query>" [--syntax substring|regex] [--case-sensitive] [--prefix PFX[,PFX...]] [--status S[,S...]] [--type T[,T...]] [--json]
```

## Parameters

- `<query>` (required) -- search term. Default is case-insensitive literal substring, line-scoped. Pass `""` for a frontmatter-only filter.
- `--syntax substring|regex` (optional, default `substring`) -- query language. `regex` uses Python's `re.search` semantics.
- `--case-sensitive` (optional) -- disable the default case-insensitive flag.
- `--prefix PFX[,PFX...]` (optional) -- comma-separated prefix scope (e.g. `ABC`, `ABC,XYZ`).
- `--status S[,S...]` (optional) -- one or more of `open`, `in-progress`, `blocked`, `done`, `cancelled`.
- `--type T[,T...]` (optional) -- one or more of `bug`, `feature`, `chore`, `spike`, `decision`.
- `--json` (optional) -- emit structured per-issue records with body match line numbers (file-absolute).

Filter semantics: comma-separated values OR within a single flag; AND across distinct flags.

## How it works

```bash
issue search "<query>" [flags...]
```

Default output is TSV: `<id>\t<status>\t<priority>\t<title>` in scan order. `--json` adds per-issue body matches with file-absolute line numbers.

## Error semantics (exit codes)

- `0` -- at least one match.
- `1` -- zero matches (grep convention for clean pipelines).
- `2` -- argument/usage error: missing query, invalid regex, unknown `--syntax` value.

This skill is read-only; exit code 3 (`concurrent-edit`) is reserved for mutating subcommands and is not produced here.

## Examples

```
/issue-search "concurrent-edit"
/issue-search "" --prefix ABC --status open,in-progress
/issue-search "atomic_write" --prefix ABC --type feature --json
/issue-search "ISSUE-[0-9]+" --syntax regex --case-sensitive
```

## Notes

- Files with malformed frontmatter are silently skipped; structural validation is the job of `issue admin validate`.
- Line numbers in `--json` body matches are file-absolute (frontmatter and heading lines counted from 1), suitable for editor jumps.
- Performance: sub-second on the live workspace corpus.
