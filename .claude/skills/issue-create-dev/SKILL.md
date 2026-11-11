---
name: issue-create-dev
description: Create a new issue file in the issue store via the issue create CLI. Wraps issue.create. Use to file bugs, features, chores, spikes, or decisions surfaced during a session.
user-invocable: true
---

# Issue Create

Create a new issue file using `issue create`. The CLI resolves the target directory (issue store or legacy `.issues/`), picks the next free ID for the active prefix, writes the file from the canonical `ISSUE-TEMPLATE.md`, and runs `issue admin reindex` automatically.

Wraps the CLI subcommand that will be exposed as MCP tool `issue.create` in Phase 2.

## Discourse / workflow split

This is one of the six workflow-side skills (create, start, complete, cancel, block, and the claim-guarded path of edit): a create mutates execution state in the frontmatter-native workflow engine, and the skill consumes the contiguous 3-7 CLI exit-code contract. The seven discourse-side skills (show, history, search, search-history, show-at, activity, list -- plus issue-note and plain body edits) read and write prose only and are unchanged by the discourse-workflow split.

## Invocation

```
/issue-create-dev "<title>" <type> <priority>
```

## Parameters

- `<title>` (required) -- quoted human-readable title. Do not include the issue ID; the CLI assigns it.
- `<type>` (required) -- one of `bug | feature | chore | spike | decision`.
- `<priority>` (required) -- one of `high | medium | low`.

Optional flags forwarded to `issue create`:

- `--store <root>` -- override store root (otherwise resolved via `ISSUE_STORE_ROOT` or the `.migrated-to-store` sentinel).
- `--repo <name>` -- override the cwd-derived git toplevel basename for registry lookup.

## How it works

Run the underlying CLI with the user's arguments:

```bash
issue create "<title>" <type> <priority> --json
```

Capture stdout for the created path and the assigned ID. The CLI prints the created file path on success and reindexes automatically; no separate `issue admin reindex` call is needed.

## Error semantics (exit codes)

Contiguous 3-7 loser-side exit-code contract. Per-skill handling -- "Retry once" means: on the first occurrence, re-read fresh state and retry the operation; on a second occurrence, surface the structured error to the user and stop.

- `0` -- success; print the created path and ID to the user.
- `1` -- precondition failed (store not configured, repo not registered, missing template).
- `2` -- argument or usage error (invalid type, invalid priority, missing positional).
- `3` -- `concurrent-edit` on the registry, INDEX, or store-commit lock. **Retry once** with fresh state; if it fails again, surface the structured error verbatim and stop. Do not retry indefinitely.

## Structured stderr

Always pass `--json` on the wrapped call. On a contract error stderr carries a single one-line JSON object (`exit`, `class`, plus per-class fields). Notice lines (e.g. the auto-reindex summary) may precede the JSON line on stderr -- parse the JSON line, not the whole stream; only stdout carries the whole-stream parse guarantee under `--json`. Render it human-readably.

## Examples

```
/issue-create-dev "Validator misses lowercase prefix" bug high
/issue-create-dev "Add --json output to issue search" feature medium
/issue-create-dev "Investigate INDEX.md drift under concurrent writes" spike low
```

## Notes

- Invocation is non-interactive: positional args are required so this skill can run unattended.
- The skill does not edit the new issue's body beyond what the template provides. To add body content, follow with `/issue-edit-dev <id> --section "Implementation Notes" --append "..."`, or seed at create time via `issue create --section NAME=TEXT` / `--from-file PATH`.
- Issue numbering is monotonic within the prefix; the CLI handles concurrent `issue create` invocations safely via the registry lock.
- Creating an issue does not claim it. Pick it up with `/issue-start-dev <id>` when work begins.
