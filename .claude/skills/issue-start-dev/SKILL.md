---
name: issue-start-dev
description: Claim an issue and transition it to in-progress with a TTL lease, recording the working branch. Wraps issue.start. Use when picking up an issue at the beginning of a work session.
user-invocable: true
---

# Issue Start

Acquire a TTL lease on an issue and mark it `in-progress`. Wraps `issue transition <id> --claim <actor>` (the discourse-workflow split claim path), then records the working branch via a guarded `issue edit`.

Wraps the CLI subcommand that will be exposed as MCP tool `issue.start` in Phase 2.

## Discourse / workflow split

This is one of the six workflow-side skills (create, start, complete, cancel, block, and the claim-guarded path of edit): it mutates execution state in the frontmatter-native workflow engine, and it consumes the contiguous 3-7 CLI exit-code contract. The seven discourse-side skills (show, history, search, search-history, show-at, activity, list -- plus issue-note and plain body edits) read and write prose only and are unchanged by the discourse-workflow split.

## Invocation

```
/issue-start-dev <id> [--branch <name>] [--actor <name>] [--lease-ttl <seconds>]
```

## Parameters

- `<id>` (required) -- full issue ID, e.g. `ABC-028`, `XYZ-035`.
- `--branch <name>` (optional, recommended) -- branch name to record on the issue's `branch:` field. If omitted, the skill resolves `git branch --show-current` and passes that value through. Use `--branch ""` to explicitly clear an existing branch value.
- `--actor <name>` (optional) -- claimant recorded in `claim.assignee` (matches `^[A-Za-z0-9._@-]{1,64}$`). Defaults to `claude-code-<ppid>` (see Session identity below).
- `--lease-ttl <seconds>` (optional) -- per-call override of the 3600s default lease TTL.

## Session identity and state

All five state-consuming skills (`/issue-start-dev`, `/issue-complete-dev`, `/issue-cancel-dev`, `/issue-block-dev`, `/issue-edit-dev` on claim-guarded edits) share one canonical snippet for session identity and the session-state path:

```bash
# $PPID inside a Claude Code Bash tool call is the PID of the Claude
# main-loop process: stable across tool calls within one session,
# distinct between concurrent sessions in the same worktree. Do NOT
# use python3 os.getppid() -- each Bash tool call runs in a fresh
# shell, so a python3 child's parent is that ephemeral shell, not
# Claude.
SESSION_STATE="$(git rev-parse --show-toplevel)/.claude/issue-mgmt-sessions/${PPID}.json"
ACTOR="claude-code-${PPID}"
```

The state file is per worktree, gitignored, JSON shape:

```json
{
  "issues": {
    "ABC-098": {"claim_generation": 8, "claimed_at": "2026-06-26T14:00:00Z"}
  }
}
```

## How it works

Run the steps below in order. `ID` is the target issue, `PFX` its prefix (the part before the dash).

**Step 1 -- prior-claim check.** Read `$SESSION_STATE`. If it already has a record for `ID`, peek at the issue's live frontmatter (`issue show <id> --frontmatter --json` -- read-only, no lock) and surface the prior-claim state before doing anything else:

- `claim.assignee == $ACTOR`, `lease_until` in the future, `claim_generation` matches the recorded value -> **held**: "you already hold <id> (generation N, lease until X)". The re-claim in step 3 is an exit-0 no-op; proceed.
- `lease_until` in the past (or `claim:` absent) -> **expired**: "your claim on <id> expired at X". Proceed; step 3 re-claims and bumps the generation.
- `claim.assignee` differs, or `claim_generation` moved past the recorded value -> **reclaimed**: "your claim on <id> was reclaimed by <assignee>". Proceed only if the user confirms; step 3 will exit 4 while the other actor's lease is valid.

**Step 2 -- opportunistic reap.** Clear any expired leases in the prefix before claiming:

```bash
issue admin reap --prefix <PFX>
```

Best-effort: on a nonzero exit, note the error and continue to step 3.

**Step 3 -- claim.** Resolve the branch (as before: `BRANCH="$(git -C "$PWD" branch --show-current)"` when `--branch` is not given), then acquire:

```bash
CLAIM_JSON=$(issue transition <id> --claim "$ACTOR" --json)
```

On success stdout is one JSON object carrying the acquired `claim_generation` (W0a F4 shape). This atomically flips status to `in-progress` (a timestamped note is appended to `## Implementation Notes`), writes `claim:`/`lease_until`, and bumps `claim_generation`.

**Step 4 -- persist session state.** Parse `claim_generation` out of `$CLAIM_JSON` and record it:

```bash
mkdir -p "$(dirname "$SESSION_STATE")"
python3 - "$SESSION_STATE" "<id>" "$CLAIM_JSON" <<'PY'
import json, os, sys
path, issue_id, claim = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
try:
    with open(path) as f:
        state = json.load(f)
except (OSError, ValueError):
    state = {}
state.setdefault("issues", {})[issue_id] = {
    "claim_generation": claim["claim_generation"],
    "claimed_at": claim["claim"]["claimed_at"],
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
PY
```

**Step 5 -- record the branch.** `--claim` does not take `--branch`; set it with the just-acquired generation as the guard:

```bash
issue edit <id> --set branch="$BRANCH" --claim-generation <N>
```

## Error semantics (exit codes)

Contiguous 3-7 loser-side exit-code contract. Per-skill handling -- "Retry once" means: on the first occurrence, re-read fresh state and retry the operation; on a second occurrence, surface the structured error to the user and stop.

- `0` -- success, including the idempotent same-actor re-claim within an unexpired lease (no generation bump; the JSON still carries the current generation -- persist it as in step 4).
- `1` -- precondition failed (store unresolvable).
- `2` -- argument / usage error (malformed claimant, bad `--lease-ttl`).
- `3` -- `concurrent-edit` (per-issue lock timeout). Not part of this skill's decision matrix (no body edit), but the claim path can still surface it on lock contention: **retry once**.
- `4` -- `lost-race`: another actor holds a valid lease. Render the contender (see Structured stderr below), then suggest the next ready issue via `issue ready --prefix <PFX> --actor "$ACTOR"` and offer to wait for `lease_until` instead. Do not retry the same target while the contender's lease is valid.
- `5` -- `stale-local-view`: the passed `--claim-generation` is behind the store. Refresh (re-read frontmatter and `$SESSION_STATE`), **retry once**.
- `6` -- `dependency-state-changed`: a blocker changed mid-operation (reserved; no current emitter). Re-query `issue ready` and offer the next candidate.
- `7` -- `not-claimable`: status not claimable (`blocked`/`done`/`cancelled`), ID not found, or the lease died. **Escalate** -- surface the structured stderr (status / reason) to the user and stop; do not retry without operator input.

## Structured stderr

Always pass `--json` on the wrapped CLI calls. On a contract error stderr carries a single one-line JSON object with `exit`, `class`, and per-class fields (`contender`, `claimed_at`, `lease_until`, `current_generation`, `caller_generation`, `status`, `reason`). Notice lines (e.g. the auto-reindex summary) may precede the JSON line on stderr -- parse the JSON line, not the whole stream; only stdout carries the whole-stream parse guarantee under `--json`. Parse it and render a human-readable line, e.g.:

```
[issue-start] Acquiring claim on ABC-098...
[issue-start] FAILED: lost-race. Contender: claude-code-other (claimed at 2026-06-26T14:00:00Z, lease until 2026-06-26T15:00:00Z).
[issue-start] Suggested action: try a different ready issue (issue ready --prefix ABC), or wait for the lease to lapse.
```

## Examples

```
/issue-start-dev ABC-028 --branch phase-1-example
/issue-start-dev XYZ-035
/issue-start-dev ABC-098 --actor claude-code-w2 --lease-ttl 7200
```

## Notes

- Claimable source states: `open`, plus an `in-progress` issue whose claim is absent or expired. `blocked`, `done`, and `cancelled` are exit 7; use `/issue-block-dev --unblock` or reopen first.
- NO renew loop: the skill does not set up periodic background renewal. Long-running sessions renew manually with `issue transition <id> --renew --claim-generation <N>`.
- NO auto-release on session exit: an abandoned claim expires at `lease_until` and the next `/issue-start-dev` in the prefix reaps it.
- `issue ready` computes the ready set natively from store frontmatter -- no external index or engine precondition; use it directly for suggestions.
- The `branch:` field is free-form in Phase 1; future enforcement is deferred to Phase 2.
