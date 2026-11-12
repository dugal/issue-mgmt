---
name: issue-activity
description: Group recent store commits by the issue file they touched. Wraps issue.activity. Use for "what changed across the issue store this week?" Read-only; no locks acquired.
user-invocable: true
---

# Issue Activity

Show recent activity in the issue store grouped by issue. Wraps `issue history activity`.

Read-only: no locks acquired. Surfaces only store-repo history; pre-migration commits live in the originating project repo (see fallback below).

Wraps the CLI subcommand that will be exposed as MCP tool `issue.activity` in Phase 2.

## Invocation

```
/issue-activity [--since DATE] [--prefix PFX[,PFX...]] [--limit N] [--json]
```

## Parameters

- `--since DATE` (optional, default `7.days.ago`) -- git approxidate cutoff. Accepts absolute (`2026-04-01`) and relative (`2.weeks.ago`, `yesterday`). Pass `--since ""` to disable the window and scan all history.
- `--prefix PFX[,PFX...]` (optional) -- comma-separated prefix scope.
- `--limit N` (optional) -- cap the number of distinct issues in the result (not the number of commits). A capped-out issue retains its full `commit_count`.
- `--json` (optional) -- emit a structured object instead of TSV.

## How it works

```bash
issue history activity [--since ...] [--prefix ...] [--limit N] [--json]
```

Internally: `git log --format=... --name-only [--since DATE] -- [<PREFIX>/...]` inside the store root, with results grouped by `<PREFIX>/<ID>.md`. Only paths matching the issue-file shape are counted -- `INDEX.md`, `.repos`, and other maintenance files are ignored. A mixed commit touching both an issue and a maintenance file increments the issue's count by exactly one.

Default output is TSV, sorted by most-recent commit date descending, ties broken by issue ID ascending:

```
<id>\t<latest-date>\t<commit-count>\t<latest-author>\t<latest-sha>\t<latest-subject>
```

`--json` emits `{"since", "store", "prefixes", "limit", "issues": [...]}` with `issues: []` for the zero-activity case.

## Error semantics (exit codes)

- `0` -- success: at least one issue with activity in the window was emitted.
- `1` -- EITHER zero activity in the window (the grep convention every
  enumeration read follows) OR a precondition failure: legacy mode active,
  store root not a git repo, prefix directory missing. Distinguish them by
  the stderr line, not by the code: a zero-activity run writes
  `no activity in window (<since>) in <store>` and is a NORMAL outcome --
  often it just means a quiet week. Do not report it as a broken store.
- `2` -- argument/usage error: invalid `--prefix` shape, empty `--prefix` after parsing, non-positive `--limit`, unknown flag.

This skill is read-only; exit code 3 (`concurrent-edit`) is not produced.

## Examples

```
/issue-activity
/issue-activity --since 2026-04-01
/issue-activity --since 30.days.ago --prefix ABC
/issue-activity --prefix ABC,XYZ --limit 10 --json
/issue-activity --since "" --limit 5    # all-history, top 5
```

## Legacy-mode fallback

If `issue history activity` exits 1 with "legacy mode" in stderr, run the equivalent git command directly in the project repo:

```bash
git log --since=7.days.ago --name-only -- .issues/
```

## Notes

- `--since` is a git walk-stop condition, not a filter. A rebased migration commit that touches only maintenance files is gracefully excluded from the output without breaking the walk.
- `--limit N` caps issues, not commits. The skill is designed for "top-N most recently active issues."
- May return slightly stale results if a write commits during the scan; the impact is at most one missing recent commit. Re-run to refresh.
