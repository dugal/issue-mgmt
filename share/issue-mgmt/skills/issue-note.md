---
name: issue-note
description: Append a date-stamped line to an issue's ## Implementation Notes. Wraps issue.note. Use whenever you would otherwise Edit an issue file to drop a YYYY-MM-DD progress line; default target is ## Implementation Notes but any level-2 section works.
user-invocable: true
---

# Issue Note

Append a `YYYY-MM-DD: <text>` line to an issue's `## Implementation Notes` (or any other level-2 section) without hand-editing the file. Thin convenience wrapper over `issue edit --section ... --append`, with the date prefix and section default pre-filled.

Wraps the CLI subcommand that will be exposed as MCP tool `issue.note` in Phase 2.

## Invocation

```
/issue-note <id> "<text>" [--date YYYY-MM-DD] [--section NAME] [--json] [--no-reindex] [--lock-timeout <seconds>]
/issue-note <id> -        # read text from stdin
```

## Parameters

- `<id>` (required) -- full issue ID, e.g. `ABC-071`, `XYZ-038`.
- `<text>` (required) -- the note text. A single positional `-` reads text from stdin (trailing newline stripped; internal newlines preserved).
- `--date YYYY-MM-DD` (optional) -- override the date prefix; only strict `YYYY-MM-DD` is accepted (any other format -> exit 2). Default: today.
- `--section NAME` (optional) -- override the target section. Default: `Implementation Notes`. Any level-2 section name works (the section must already exist).
- `--json` (optional) -- emit a one-shot summary object on stdout.
- `--no-reindex` (optional) -- skip the post-write auto-reindex.
- `--lock-timeout <seconds>` (optional, default 5) -- per-issue advisory-lock timeout.

## How it works

```bash
issue note <id> "<text>" [flags...]
```

Internally delegates to the same per-issue lock + snapshot + atomic-write + commit path as `issue edit`: builds the payload `<date>: <text>`, then runs `issue edit --section NAME --append "<payload>"`. The `updated:` frontmatter field is auto-stamped to today's date on success.

## Error semantics (exit codes)

- `0` -- success.
- `1` -- issue file not found, or target section not found.
- `2` -- argument / usage error: missing ID, missing text, invalid `--date`, empty stdin, or note text containing a `#`/`## ` header line that would escape the target section -- demote such headers (`## x` -> `### x`) and retry.
- `3` -- `concurrent-edit`: lock timeout or hand-edit divergence detected. **Retry once with fresh state** (re-read the issue file, re-issue the note); on persistent failure, surface the error verbatim and stop.

## Examples

Bare text append (date stamped today):
```
/issue-note ABC-071 "started work on the issue note binary"
```

Stdin variant (useful for multi-line notes from a heredoc or pipe):
```
echo "draft SKILL.md plus first verification pass" | /issue-note ABC-071 -
```

Back-dated note:
```
/issue-note ABC-071 "design decision recorded" --date 2026-05-29
```

Retarget to a non-default section (the section must already exist):
```
/issue-note ABC-079 "decision: defer ABC-067 and ABC-084 to v3" --section Design
```

JSON output for programmatic chaining:
```
/issue-note ABC-071 "ran verification suite, all PASS" --json
```

## Notes

- The note is always prefixed with `<date>: ` (single space after colon). The date is today's date (`YYYY-MM-DD`) unless `--date` overrides it.
- The target section must already exist in the issue file. Add the section first with `/issue-edit <id> --section "<name>" --append "..."` (which creates nothing -- if the section is missing, that exits 1 too) or by direct edit; in practice every issue created via `issue create` already carries `## Implementation Notes`.
- For changes that are not date-stamped progress lines, prefer `/issue-edit` directly.
- For status transitions, use the dedicated `/issue-start`, `/issue-complete`, `/issue-block`, or `/issue-cancel` skills -- those auto-stamp their own audit line and handle the legal-transition matrix.
