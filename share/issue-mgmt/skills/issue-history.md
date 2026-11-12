---
name: issue-history
description: Show the git commit history for a single issue file in the store. Wraps issue.history. Use to see who changed an issue and when. Read-only; no locks acquired. Store mode only (legacy fallback documented).
user-invocable: true
---

# Issue History

Print the git commit log for a single issue file under the store. Wraps `issue history show`.

Read-only: no locks acquired. Surfaces only store-repo history; pre-migration commits live in the originating project repo (see the fallback note below).

Wraps the CLI subcommand that will be exposed as MCP tool `issue.history` in Phase 2.

## Invocation

```
/issue-history <id> [--limit N] [--json]
```

## Parameters

- `<id>` (required) -- full issue ID, e.g. `ABC-028`.
- `--limit N` (optional) -- cap the number of commits returned, newest first.
- `--json` (optional) -- emit a structured object instead of TSV.

## How it works

```bash
issue history show <id> [--limit N] [--json]
```

Internally, the CLI runs `git log --follow --format=... [-n LIMIT] -- <PREFIX>/<id>.md` inside the store root. `--follow` traces renames if the file has ever been moved.

Default output is TSV: `<full-sha>\t<author-date-iso8601>\t<author>\t<subject>` newest-first. `--json` emits `{"id", "store", "file", "commits": [...]}` with `commits: []` for empty history.

## Error semantics (exit codes)

- `0` -- success, including the empty-history case (file exists in the store but has no commits yet).
- `1` -- precondition failed: legacy mode active (this CLI is store-only -- see fallback below), store root is not a git repo, prefix directory missing, issue file missing.
- `2` -- argument/usage error: missing ID, malformed ID, non-positive `--limit`.

This skill is read-only; exit code 3 (`concurrent-edit`) is not produced.

## Examples

```
/issue-history ABC-028
/issue-history ABC-028 --limit 5
/issue-history ABC-028 --json
```

## Legacy-mode fallback (pre-migration history)

If `issue history show` exits 1 with "legacy mode" in the stderr, the issue lives in a per-project `.issues/` directory that has not migrated to the store. Run the equivalent git command directly in the project repo:

```bash
git log --follow -- .issues/<id>.md
```

This boundary is intentional, not a missing feature; pre-migration history is intentionally split until a future contingency plan handles cross-repo lookup.

## Notes

- May return slightly stale results if a write commits during the read; the impact is at most one missing recent commit. Re-run to refresh.
- Author dates are ISO8601 (`%aI`). SHAs are full 40-character form.
- Empty history (file present, no commits) writes a one-line note to stderr but exits 0; the `--json` shape is unambiguous (`commits: []`).
