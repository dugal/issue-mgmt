---
name: issue-complete
description: Transition an issue to done. Wraps issue.complete. Use after all acceptance criteria pass and the user confirms the issue is finished.
user-invocable: true
---

# Issue Complete

Mark an issue as `done`. Wraps `issue transition <id> done`, passing the session's recorded `--claim-generation` as a fencing guard when available.

Wraps the CLI subcommand that will be exposed as MCP tool `issue.complete` in Phase 2.

## Discourse / workflow split

This is one of the six workflow-side skills (create, start, complete, cancel, block, and the claim-guarded path of edit): it mutates execution state in the frontmatter-native workflow engine, and it consumes the contiguous 3-7 CLI exit-code contract. The seven discourse-side skills (show, history, search, search-history, show-at, activity, list -- plus issue-note and plain body edits) read and write prose only and are unchanged by the discourse-workflow split.

## Invocation

```
/issue-complete <id> [--reason "<text>"]
```

## Parameters

- `<id>` (required) -- full issue ID, e.g. `ABC-028`.
- `--reason "<text>"` (optional) -- one-line summary recorded in `## Implementation Notes` alongside the state change. Useful for capturing the closing condition (e.g. `"all acceptance checks passed on phase-1-example"`).

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
issue transition <id> done [--reason "<text>"] $GUARD --json
```

A timestamped note is appended to the issue's `## Implementation Notes` section (`<date>: status: <old> -> done[ -- <reason>]`).

Missing or stale session state degrades to no-guard with the one-line stderr note above -- the transition still runs, just without fencing.

## Error semantics (exit codes)

Contiguous 3-7 loser-side exit-code contract. Per-skill handling -- "Retry once" means: on the first occurrence, re-read fresh state and retry the operation; on a second occurrence, surface the structured error to the user and stop.

- `0` -- success.
- `1` -- issue file not found.
- `2` -- illegal transition. `done` is reachable only from `in-progress`; if the issue is in `open`, `blocked`, or `cancelled`, transition through `in-progress` first.
- `3` -- `concurrent-edit` on the per-issue lock (body-edit path: the notes append rides in the same write). **Retry once** with fresh state.
- `4` -- `lost-race`: someone else holds the claim. **Escalate** -- name the contender from the structured stderr and stop.
- `5` -- `stale-local-view`: the recorded generation drifted (your claim was reclaimed and possibly re-claimed since `/issue-start`). **Escalate** -- re-read the issue (`issue show <id> --frontmatter --json`), show the user the current holder and generation, and stop; completing over someone else's claim needs an explicit user decision.
- `7` -- `not-claimable` (`reason=lease-expired`: the generation matches but your lease expired or was reaped). **Escalate** -- surface the structured stderr and stop; the user may re-claim via `/issue-start` and retry.

## Structured stderr

Always pass `--json` on the wrapped call. On a contract error stderr carries a single one-line JSON object (`exit`, `class`, plus `contender` / `current_generation` / `caller_generation` / `status` / `reason` as relevant). Notice lines (e.g. the auto-reindex summary) may precede the JSON line on stderr -- parse the JSON line, not the whole stream; only stdout carries the whole-stream parse guarantee under `--json`. Render it human-readably, e.g.:

```
[issue-complete] FAILED: stale-local-view. Recorded generation 8, store is at 10 -- your claim on ABC-098 was reclaimed.
```

## Examples

```
/issue-complete ABC-028
/issue-complete ABC-028 --reason "all 13 SKILL.md files present, verification PASS"
```

## Notes

- This skill does not check off plan.md items, edit `requirements.md`, or commit. Those are separate steps in the project's Task Completion Workflow and remain the user's call.
- Terminal-to-terminal transitions are illegal: to move from `done` to `cancelled` or vice versa, transition through an active state (`open` or `in-progress`) first.
- A successful `done` leaves any `claim:` block in place. Optional housekeeping: release it with `issue transition <id> --release --claim-generation <N>` (clears `claim:` + `lease_until`, preserves the generation). Leaving it is benign -- a done issue is out of every ready set and the lease lapses on its own.
- The `branch:` field is cleared by the transition. `done` is terminal, and a terminal transition drops `branch:` unless an explicit `--branch` is passed, so a completed issue carries no stale working branch and there is no follow-up command to run.
