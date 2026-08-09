---
name: issue-search-history-dev
description: Pickaxe search across the store's git history for additions and removals of a literal term. Wraps issue.search-history. Use to find when a flag, function, or token was introduced or removed. Read-only; no locks acquired.
user-invocable: true
---

# Issue Search History

`git log -S <term>` across the issue store. Returns every commit that added or removed occurrences of the literal term in any tracked file. Wraps `issue history search`.

Read-only: no locks acquired. Surfaces only store-repo history; pre-migration commits live in the originating project repo (see the fallback note below).

Wraps the CLI subcommand that will be exposed as MCP tool `issue.search-history` in Phase 2.

## Invocation

```
/issue-search-history-dev "<term>" [--prefix PFX[,PFX...]] [--since DATE] [--json]
```

## Parameters

- `<term>` (required) -- literal term (not a regex). Use quotes if the term contains shell metacharacters.
- `--prefix PFX[,PFX...]` (optional) -- comma-separated prefix scope. Limits the pickaxe to `<PREFIX>/` directories under the store.
- `--since DATE` (optional) -- git approxidate cutoff. Accepts absolute (`2026-04-01`, `2026-04-01T12:00:00`) and relative (`2.weeks.ago`, `yesterday`) forms.
- `--json` (optional) -- emit a structured object instead of TSV.

## How it works

```bash
issue history search "<term>" [--prefix ...] [--since ...] [--json]
```

Internally: `git log --format=... [--since DATE] -S <term> -- [<PREFIX>/...]` inside the store root.

Default output is TSV: `<full-sha>\t<author-date-iso8601>\t<author>\t<subject>` newest-first. `--json` emits `{"term", "store", "prefixes", "since", "commits": [...]}` with `commits: []` for the zero-match case.

## Error semantics (exit codes)

- `0` -- success, including zero matches (writes a `no matches for "<term>" in <store>` note to stderr but exits 0).
- `1` -- precondition failed: legacy mode active (store-only, see fallback below), store root not a git repo, prefix directory missing.
- `2` -- argument/usage error: missing term, invalid `--prefix` shape, empty `--prefix` after parsing.

This skill is read-only; exit code 3 (`concurrent-edit`) is not produced.

## Examples

```
/issue-search-history-dev "concurrent-edit"
/issue-search-history-dev "ISSUE_STORE_ROOT" --prefix ABC
/issue-search-history-dev "atomic_write" --since 2026-01-01
/issue-search-history-dev "XYZ-037" --since 2.months.ago --json
```

## Legacy-mode fallback

If `issue history search` exits 1 with "legacy mode" in stderr, run the equivalent git command directly in the project repo:

```bash
git log -S "<term>" -- .issues/
```

## Notes

- Pickaxe semantics are literal-string, not regex. To search for a pattern across history, fall back to `git log --grep` or `git log -G` outside this skill.
- `--prefix` accepts a single value or comma-separated list (OR within the flag).
- Performance: sub-second on the synthetic verification corpus; scales with store size and history depth.
