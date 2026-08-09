---
name: issue-show-dev
description: Print the full contents of an issue file as it is right now (working tree). Wraps issue.show. Use to read an issue end-to-end, fetch one named section, or batch-read several issues at once. Use when you would otherwise Read or head an issue file. Read-only; no locks acquired.
user-invocable: true
---

# Issue Show

Print the current working-tree contents of one or more issue files. Wraps the `issue show` binary, which will be exposed as MCP tool `issue.show` in Phase 2.

Read-only: no locks acquired; reads the file directly from disk so uncommitted edits are visible (unlike `/issue-show-at-dev` which reads from a git ref).

## Invocation

```
/issue-show-dev <id> [<id>...] [--section NAME]... [--frontmatter] [--no-body] [--json] [--store ROOT]
```

Under the hood the skill shells out to:

```
issue show <id> [<id>...] [--section NAME]... [--frontmatter] [--no-body] [--json] [--store ROOT]
```

`issue show` ships with the `issue-mgmt` Homebrew formula and resolves the issue file via the same precedence as the rest of the CLI (`--store` -> `ISSUE_STORE_ROOT` -> `.migrated-to-store` sentinel -> legacy `.issues/<id>.md`).

## Parameters

- `<id>` (required, repeatable) -- full issue ID, e.g. `ABC-068`, `XYZ-035`. Two or more positional ids enter multi-id batch mode.
- `--section NAME` (optional, repeatable) -- print only the named level-2 section (heading line plus body). Case-sensitive, exact match. Sections render in the order requested, separated by a single blank line.
- `--frontmatter` (optional) -- print the raw frontmatter block (between `---` fences, fences included) before any selected sections.
- `--no-body` (optional) -- print frontmatter only. Mutually exclusive with `--section` (rejected with exit 2).
- `--json` (optional) -- emit a structured object on stdout. Multi-id mode emits a top-level JSON array of single-id objects in positional order.
- `--store ROOT` (optional) -- override the store root for path resolution.

## How it works

Default (no flags) prints the file byte-for-byte. `--section` reads only what was requested. `--frontmatter` and `--no-body` are independent of `--section`.

**Missing section is exit 1.** Stderr names both the missing section and the issue id. Output is atomic per invocation: if any requested section is missing, nothing is written to stdout for that id (even if a sibling `--section` would have matched).

### When to use this skill

Use `/issue-show-dev` whenever you would otherwise `Read` an issue file or `head` it for a quick glance. Prefer `--section NAME` over reading the whole file when you only need one part (Summary, Acceptance Criteria, Implementation Notes, etc.).

For locating an issue file by content rather than by id, use `/issue-search-dev` or `/issue-list-dev`. For history, use `/issue-show-at-dev`, `/issue-history-dev`, or `/issue-search-history-dev`.

## Multi-id batch mode

Pass two or more positional ids to read several issues in one invocation. Text mode renders each id under a `=== <id> ===` header, separated by a blank line. JSON mode returns a top-level array of single-id objects in positional order.

Mixed-success semantics: per-id failures are reported but the run continues; the overall exit code is 1 if any id failed. In text mode, failure lines go to stderr as `=== <id> === [error: ...]`. In JSON mode, the per-id object carries an `error` field in place of the body.

Duplicate ids render once each in positional order (no implicit de-dup).

## JSON shape

Single id:

```json
{
  "id": "ABC-068",
  "file": "IM/ABC-068.md",
  "store": "/path/to/issues-store",
  "frontmatter": {"id": "ABC-068", "title": "...", "status": "in-progress", ...},
  "sections_requested": ["Summary"],
  "sections": {"Summary": "## Summary\n\n...body...\n"},
  "full_text": null
}
```

When no `--section` is given, `sections_requested` and `sections` are omitted and `full_text` carries the body. When `--section` is given, `full_text` is null. When `--no-body` is set without `--section`, `full_text` is null and `sections` is absent.

Multi-id: a top-level JSON array of the single-id object. Failure entries collapse to `{"id": ..., "error": "...", "file": ..., "store": ...}`.

## Error semantics

- `0` -- success.
- `1` -- precondition failed: issue file not found OR a requested section is absent. In multi-id mode, returned at end-of-run if any id failed.
- `2` -- argument/usage error: missing id, malformed ID, `--no-body` combined with `--section`, unknown flag.

This skill is read-only; exit code 3 (`concurrent-edit`) is not produced.

## Examples

```
/issue-show-dev ABC-068
/issue-show-dev ABC-079 --section Design
/issue-show-dev ABC-079 --section "Implementation Notes" --section Summary
/issue-show-dev ABC-068 --no-body
/issue-show-dev ABC-068 --frontmatter --section "Acceptance Criteria"
/issue-show-dev ABC-068 --section Summary --json
/issue-show-dev ABC-068 ABC-069 ABC-070
/issue-show-dev ABC-068 ABC-069 ABC-070 --json
/issue-show-dev ABC-068 ABC-069 --section Summary
```

## When to use which "show"

- `/issue-show-dev <id>` -- current working-tree contents (this skill). Includes uncommitted edits.
- `/issue-show-at-dev <id> --at <ref>` -- contents at a past commit, tag, branch, or date. Always reads from git, never from the working tree.

## Notes

- The default output is byte-for-byte (frontmatter, body, trailing newline preserved).
- For a structured view of the frontmatter, use `--json` -- the `frontmatter` field is the parsed key:value map.
