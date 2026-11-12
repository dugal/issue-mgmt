---
name: issue-cancel
description: Transition an issue to cancelled. Wraps issue.cancel. Use when an issue will not be worked -- supersedes "delete" since issue files are never deleted.
user-invocable: true
---

# Issue Cancel

Mark an issue as `cancelled`. Wraps `issue transition <id> cancelled [--reason "<text>"]`, passing the session's recorded `--claim-generation` as a fencing guard when available.

This is the project's "delete" -- issue files are never removed; cancellation is the terminal state for work that will not happen.

Wraps the CLI subcommand that will be exposed as MCP tool `issue.cancel` in Phase 2.

## Discourse / workflow split

This is one of the six workflow-side skills (create, start, complete, cancel, block, and the claim-guarded path of edit): it mutates execution state in the frontmatter-native workflow engine, and it consumes the contiguous 3-7 CLI exit-code contract. The seven discourse-side skills (show, history, search, search-history, show-at, activity, list -- plus issue-note and plain body edits) read and write prose only and are unchanged by the discourse-workflow split.

## Invocation

```
/issue-cancel <id> [--reason "<text>"]
```

## Parameters

- `<id>` (required) -- full issue ID.
- `--reason "<text>"` (optional but recommended) -- one-line explanation, recorded in `## Implementation Notes`. Examples: `"superseded by ABC-040"`, `"out of scope after a design decision"`.

## How it works

First read the claim generation recorded by `/issue-start` from the session-state file (canonical session-identity snippet -- see `/issue-start` "Session identity and state"):

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

Then invoke:

```bash
issue transition <id> cancelled [--reason "<text>"] $GUARD --json
```

Missing or stale session state degrades to no-guard with the one-line stderr note above -- common for cancellations of issues this session never started (e.g. filing-time duplicates), where no claim exists and no guard is needed.

## Error semantics (exit codes)

Contiguous 3-7 loser-side exit-code contract. Per-skill handling -- "Retry once" means: on the first occurrence, re-read fresh state and retry the operation; on a second occurrence, surface the structured error to the user and stop.

- `0` -- success.
- `1` -- issue file not found.
- `2` -- illegal transition. `cancelled` is reachable from `open`, `in-progress`, or `blocked`. Terminal-to-terminal (`done -> cancelled`) is illegal; reopen to an active state first.
- `3` -- `concurrent-edit` on the per-issue lock. **Retry once** with fresh state.
- `4` -- `lost-race`: someone else holds the claim. **Escalate** -- name the contender from the structured stderr and stop.
- `5` -- `stale-local-view`: the recorded generation drifted (the claim was reclaimed since `/issue-start`). **Escalate** -- re-read the issue (`issue show <id> --frontmatter --json`), show the user the current holder and generation, and stop; cancelling over someone else's active claim needs an explicit user decision.
- `7` -- `not-claimable` (`reason=lease-expired`: the generation matches but your lease expired or was reaped). **Escalate** -- surface the structured stderr and stop.

## Structured stderr

Always pass `--json` on the wrapped call. On a contract error stderr carries a single one-line JSON object (`exit`, `class`, plus `contender` / `current_generation` / `caller_generation` / `status` / `reason` as relevant). Notice lines (e.g. the auto-reindex summary) may precede the JSON line on stderr -- parse the JSON line, not the whole stream; only stdout carries the whole-stream parse guarantee under `--json`. Render it human-readably, naming the contender or generation drift.

## Examples

```
/issue-cancel ABC-040 --reason "duplicate of ABC-027"
/issue-cancel ABC-013 --reason "scope absorbed into ABC-016"
```

## Notes

- Cancellation is reversible: `issue transition <id> open` (or `in-progress`) brings the issue back. Use `/issue-start` for the latter.
- Issue files must never be deleted from the store. Use this skill for any "remove" or "abandon" intent.
- A successful `cancelled` leaves any `claim:` block in place. Optional housekeeping: release it with `issue transition <id> --release --claim-generation <N>`.
- The `branch:` field is cleared by the transition. `cancelled` is terminal, and a terminal transition drops `branch:` unless an explicit `--branch` is passed, so there is no stale branch left to clean up by hand.
