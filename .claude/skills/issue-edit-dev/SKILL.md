---
name: issue-edit-dev
description: Edit an issue's frontmatter fields or level-2 sections non-interactively. Wraps issue.edit. Use to update tags, priority, branch, or append notes to ## Implementation Notes etc. Never changes status (use issue-start / issue-complete / issue-block / issue-cancel).
user-invocable: true
---

# Issue Edit

Modify an existing issue file -- frontmatter scalars and/or level-2 section bodies. Wraps `issue edit`.

Structure-preserving: byte-for-byte preservation of every field, comment, and section outside the targeted edits. The `updated:` field is auto-stamped to today's date on any successful edit.

Wraps the CLI subcommand that will be exposed as MCP tool `issue.edit` in Phase 2.

## Discourse / workflow split

This skill sits on both sides of the split. Plain body and frontmatter edits on unclaimed issues are discourse-side and unchanged by the discourse-workflow split. The workflow-side path is edits to an issue this session holds a claim on (acquired via `/issue-start-dev`): those pass `--claim-generation` from the session-state file as a fencing guard, and consume the contiguous 3-7 CLI exit-code contract.

**Design reconciliation note:** the design text calls this the "status-touching path", but `issue edit` has no status path -- `status` is a guarded field owned exclusively by `issue transition` (attempting `--set status=...` exits 2). The workflow-side path of this skill is therefore "edits under an active claim", guarded by `--claim-generation`; status changes always go through the four transition skills.

## Invocation

```
/issue-edit-dev <id> [--set FIELD=VALUE]... [--section NAME (--append "<text>" | --prepend "<text>" | --replace "<text>")]
```

## Parameters

- `<id>` (required) -- full issue ID.
- `--set FIELD=VALUE` (repeatable) -- set a frontmatter scalar (e.g. `--set priority=high --set tags="[phase-1, cleanup]"`). Quoted/unquoted style and trailing inline comments are preserved.
- `--section NAME` (optional, repeatable) -- name of a level-2 section to operate on (e.g. `"Implementation Notes"`).
- `--append "<text>"` / `--prepend "<text>"` / `--replace "<text>"` (exactly one per `--section`) -- the body operation. `--append` adds at the end of the section, `--prepend` at the top, `--replace` replaces the entire body. `--append-file` / `--prepend-file` / `--replace-file PATH` variants read the body from a file (`-` reads stdin) -- the recommended path for shell-hostile text.
- `--claim-generation N` (optional) -- fencing guard; see below.
- `--no-reindex` (optional) -- skip the auto-reindex after the edit.
- `--lock-timeout <seconds>` (optional, default 5) -- per-issue lock acquisition timeout.

Multi-section and structural ops (`--add-section`, `--rename-section`, `--remove-section`, `--move-section`) compose in one atomic invocation; see `issue edit --help` for the full surface.

Whole-body escape hatch: `--replace-body-file PATH` (`-` reads stdin) replaces the ENTIRE body below the frontmatter fence through the locked mutation path -- use it for body changes the section ops cannot express (e.g. editing a level-3 `###` subsection) instead of hand-editing the store file. The body is opaque (`## ` lines become real sections; no escaping-header check); frontmatter is untouched. Exclusive with section ops; composes with `--set`, dep ops, and `--if-unchanged-since SHA` (whole-body comparison).

## Guarded fields

These fields cannot be edited via this skill:

- `status` -- use `/issue-start-dev`, `/issue-complete-dev`, `/issue-block-dev`, or `/issue-cancel-dev`.
- `id` -- immutable. Renaming an issue requires a migration tool, not this skill.
- `created` -- immutable.

Attempts to `--set` any of these exit with code 2 and a message naming the owning command.

## Claim-generation guard (edits under an active claim)

When editing an issue this session claimed via `/issue-start-dev`, read the recorded generation and pass it through (canonical session-identity snippet -- see `/issue-start-dev` "Session identity and state"):

```bash
SESSION_STATE="$(git rev-parse --show-toplevel)/.claude/issue-mgmt-sessions/${PPID}.json"
GEN=$(python3 - "$SESSION_STATE" "<id>" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f)["issues"][sys.argv[2]]["claim_generation"])
except (OSError, ValueError, KeyError, TypeError):
    pass
PY
)
if [ -n "$GEN" ]; then GUARD="--claim-generation $GEN"; else
    GUARD=""
    echo "note: no recorded generation for <id> in this session; proceeding without the --claim-generation guard" >&2
fi
```

Then include `$GUARD` in the `issue edit` invocation. Missing or stale session state degrades to no-guard with the one-line stderr note -- the ordinary case for discourse-side edits of issues this session never claimed.

## How it works

```bash
issue edit <id> [flags...] $GUARD --json
```

The CLI acquires the per-issue lock, captures a pre-edit snapshot (mtime + size + sha256), applies all edits in memory, verifies the on-disk file has not been hand-edited mid-operation, then atomically writes the result. `--json` also switches contract-error stderr to the one-line JSON object shape.

## Error semantics (exit codes)

Contiguous 3-7 loser-side exit-code contract. Per-skill handling -- "Retry once" means: on the first occurrence, re-read fresh state and retry the operation; on a second occurrence, surface the structured error to the user and stop.

- `0` -- success (including no-op if no edits change content).
- `1` -- issue file not found, or `--section NAME` did not match.
- `2` -- argument/usage error: guarded field, no `--set` or `--section` provided, multiple body ops on one `--section`, or a body op whose content contains a `#`/`## ` header line that would escape the level-2 section and fragment the file -- demote such headers (`## x` -> `### x`) and retry.
- `3` -- `concurrent-edit`: lock timeout, hand-edit divergence, or `--if-unchanged-since` drift. **Retry once** with fresh state (re-read the issue file, re-construct the edit).
- `5` -- `stale-local-view`: the passed `--claim-generation` is behind the store. **Refresh, retry once** -- re-read the issue frontmatter and the session-state file; if the refreshed generation now matches the store (e.g. the state file was momentarily stale), retry with it; if the claim moved to another actor, surface to the user instead.
- `7` -- `not-claimable` (`reason=lease-expired`: the generation matches but the session's lease expired or was released). **Escalate** -- surface the structured stderr and stop; re-claim via `/issue-start-dev` before editing under a guard again.

## Structured stderr

Always pass `--json` on the wrapped call. On a contract error stderr carries a single one-line JSON object (`exit`, `class`, plus `current_generation` / `caller_generation` / `reason` as relevant). Notice lines (e.g. the auto-reindex summary) may precede the JSON line on stderr -- parse the JSON line, not the whole stream; only stdout carries the whole-stream parse guarantee under `--json`. Render it human-readably, naming the generation drift or expired lease.

## Examples

```
/issue-edit-dev ABC-028 --set priority=high
/issue-edit-dev ABC-028 --set tags="[phase-1, cleanup, skills]"
/issue-edit-dev ABC-028 --section "Implementation Notes" --append "2026-05-15: 13 SKILL.md files drafted; pre-merge verification passing locally."
/issue-edit-dev ABC-028 --set priority=high --section "Implementation Notes" --append "Priority bumped after QRS-038 dependency clarification."
```

## Notes

- Section names are matched by exact level-2 heading text (e.g. `## Implementation Notes`).
- `--append` adds the text as new content at the end of the section's body, with a leading blank line if the existing body is non-empty.
- `--set` with a value containing a comma or special character: pass it through your shell's quoting (e.g. `--set tags='[a, b, c]'`).
- For mass renames or migrations, write a script that calls `issue edit` directly rather than this skill.
