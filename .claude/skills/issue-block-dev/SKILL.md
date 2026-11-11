---
name: issue-block-dev
description: Block or unblock an issue with a recorded reason. Wraps issue.block / issue.unblock. Use when work cannot proceed pending an external dependency, or when the blocker has been resolved.
user-invocable: true
---

# Issue Block / Unblock

Transition an issue to `blocked` (with a required reason) or back out of `blocked` to an active state. Wraps `issue transition`, passing the session's recorded `--claim-generation` as a fencing guard when available.

Wraps the CLI subcommand that will be exposed as MCP tools `issue.block` and `issue.unblock` in Phase 2.

## Discourse / workflow split

This is one of the six workflow-side skills (create, start, complete, cancel, block, and the claim-guarded path of edit): it mutates execution state in the frontmatter-native workflow engine, and it consumes the contiguous 3-7 CLI exit-code contract. The seven discourse-side skills (show, history, search, search-history, show-at, activity, list -- plus issue-note and plain body edits) read and write prose only and are unchanged by the discourse-workflow split.

## Invocation

Block:

```
/issue-block-dev <id> --reason "<text>"
```

Unblock (transition back to `open` or `in-progress`):

```
/issue-block-dev <id> --unblock [--to open|in-progress] [--reason "<text>"]
```

## Parameters

- `<id>` (required) -- full issue ID.
- `--reason "<text>"` (required when blocking) -- one-line explanation of the blocker; recorded in `## Implementation Notes`. Optional but recommended when unblocking.
- `--unblock` (optional) -- invert the operation: transition out of `blocked`.
- `--to <state>` (optional, default `in-progress`) -- the active state to land in when unblocking. Must be `open` or `in-progress`.

## How it works

First read the claim generation recorded by `/issue-start-dev` from the session-state file (canonical session-identity snippet -- see `/issue-start-dev` "Session identity and state"):

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

Block:

```bash
issue transition <id> blocked --reason "<text>" $GUARD --json
```

Unblock:

```bash
issue transition <id> <target-state> [--reason "<text>"] $GUARD --json
```

A timestamped note is appended to `## Implementation Notes` for every transition, including the reason when supplied. Missing or stale session state degrades to no-guard with the one-line stderr note above.

## Error semantics (exit codes)

Contiguous 3-7 loser-side exit-code contract. Per-skill handling -- "Retry once" means: on the first occurrence, re-read fresh state and retry the operation; on a second occurrence, surface the structured error to the user and stop.

- `0` -- success (including same-state no-op when the issue is already `blocked` and a `--reason` is supplied; the reason is still recorded for audit continuity).
- `1` -- issue file not found.
- `2` -- usage error: blocking without `--reason`, or an illegal source state. The legal-transition matrix is owned by `issue transition` and is deliberately NOT restated here (`state.canonical-states`): a copy in a skill drifts from the CLI silently, and this one had -- it listed `blocked` as reachable from `open`, `in-progress`, or `done`, omitting `cancelled`, which `LEGAL_TRANSITIONS` permits. Run `issue transition -h` for the current matrix, or just attempt the transition and read the error.
- `3` -- `concurrent-edit` on the per-issue lock. **Retry once** with fresh state.
- `4` -- `lost-race`: someone else holds the claim. **Refresh and re-prompt** -- re-read the issue frontmatter, show the user the current holder, and ask whether to proceed (unguarded, with their confirmation) or abort.
- `5` -- `stale-local-view`: the recorded generation drifted. **Refresh and re-prompt** -- re-read the issue frontmatter (`issue show <id> --frontmatter --json`), show the user the current holder and generation, and ask whether to proceed or abort.
- `7` -- `not-claimable` (`reason=lease-expired`). **Escalate** -- surface the structured stderr and stop; the user may re-claim via `/issue-start-dev` first.

## Structured stderr

Always pass `--json` on the wrapped call. On a contract error stderr carries a single one-line JSON object (`exit`, `class`, plus `contender` / `current_generation` / `caller_generation` / `status` / `reason` as relevant). Notice lines (e.g. the auto-reindex summary) may precede the JSON line on stderr -- parse the JSON line, not the whole stream; only stdout carries the whole-stream parse guarantee under `--json`. Render it human-readably, naming the blocker of the operation, e.g.:

```
[issue-block] FAILED: stale-local-view. Recorded generation 8, store is at 9 -- refreshing. Proceed without the guard, or abort?
```

## Examples

```
/issue-block-dev ABC-029 --reason "blocked on QRS-038 landing"
/issue-block-dev ABC-029 --unblock --reason "QRS-038 shipped in v3.0.0"
/issue-block-dev ABC-029 --unblock --to open
```

## Notes

- The `--reason` argument is enforced by the CLI for `blocked` transitions (exit 2 without it). The skill must not silently default the reason.
- "Unblock" is not a state -- it is a transition out of `blocked` into one of `open` or `in-progress`. The CLI does have an `issue unblock` convenience verb (added in v3.0.0) alongside the `issue transition` call this skill composes; either reaches the same state machine, which `issue transition` owns.
- Same-state `blocked -> blocked` with a new reason is supported and recorded; use this to re-affirm a still-blocked status or document a change in the blocker.
- A `blocked` issue is not claimable (`issue transition --claim` exits 7). To resume work on a blocked issue, unblock it first, then `/issue-start-dev`.
