---
name: issue-show-at-dev
description: Print the contents of an issue file at a past commit, tag, branch, or date. Wraps issue.show-at. Use to compare an issue's wording to an earlier point in time. Read-only; no locks acquired.
user-invocable: true
---

# Issue Show At

Read an issue file as it existed at a given git ref or date. Wraps `issue history at`.

Read-only: no locks acquired. Surfaces only store-repo history; pre-migration content lives in the originating project repo (see fallback below).

Wraps the CLI subcommand that will be exposed as MCP tool `issue.show-at` in Phase 2.

## Invocation

```
/issue-show-at-dev <id> --at <ref-or-date> [--json]
```

## Parameters

- `<id>` (required) -- full issue ID, e.g. `ABC-028`.
- `--at <ref-or-date>` (required) -- one of:
  - SHA (full or short): `7475970`, `7475970e38f6b2`
  - tag: `v1.5.1`
  - branch: `main`, `phase-1-example`
  - ref expression: `HEAD`, `HEAD~3`
  - absolute date: `2026-04-01`, `2026-04-01T12:00:00`, `1/15/2026`
  - relative date: `yesterday`, `2.weeks.ago`
- `--json` (optional) -- emit a structured object with both the user-supplied `--at` value and the resolved 40-character SHA, alongside the file content.

## How it works

```bash
issue history at <id> --at <ref-or-date> [--json]
```

The CLI tries `git rev-parse --verify <at>^{commit}` first (matches SHAs, tags, branches, HEAD~N). On failure, for inputs that look date-like, it falls back to `git log -1 --format=%H --before=<at> HEAD` to resolve to the most recent commit at-or-before the date.

Default output is the raw file contents on stdout (byte-for-byte as `git show <sha>:<rel-path>`). `--json` emits `{"id", "store", "file", "ref", "resolved_sha", "content"}`.

## Error semantics (exit codes)

- `0` -- success.
- `1` -- precondition failed: legacy mode active (store-only, see fallback below), store root not a git repo, ref or date does not resolve to a commit (exit also covers "date predates the entire repo"), file did not exist at that commit (reported with a stderr message, never as empty stdout).
- `2` -- argument/usage error: missing `<id>`, missing `--at`, malformed ID, unknown flag.

This skill is read-only; exit code 3 (`concurrent-edit`) is not produced.

## Examples

```
/issue-show-at-dev ABC-028 --at HEAD
/issue-show-at-dev ABC-028 --at v1.5.1
/issue-show-at-dev ABC-028 --at 7475970
/issue-show-at-dev ABC-028 --at 2026-04-01
/issue-show-at-dev ABC-028 --at yesterday --json
```

## Legacy-mode fallback

If `issue history at` exits 1 with "legacy mode" in stderr, run the equivalent git command directly in the project repo:

```bash
git show <ref>:.issues/<id>.md
```

## When to use which "show"

- `/issue-show-dev <id>` -- current working-tree contents. Includes uncommitted edits.
- `/issue-show-at-dev <id> --at <ref>` -- contents at a past commit. Always reads from git.

## Notes

- The "could not resolve" diagnostic and the "no commit at or before this date" diagnostic are distinct -- the latter means the input is a valid date but predates the entire repo.
- `--at` does **not** silently fall back to HEAD for unparseable input; a ref-shaped typo (e.g. `--at ABC-030`) exits 1 with a clear "could not resolve" message rather than quietly returning HEAD.
