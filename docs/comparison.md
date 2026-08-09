# How issue-mgmt compares

"Issues as files in git" is a 15-year-old, repeatedly reinvented idea, and
"give the AI agent an issue tracker" became consensus, not insight, sometime in
2025-2026. `issue-mgmt` is entering a crowded, fast-moving field, not opening a
new one. This page is an honest map of that field and where `issue-mgmt` sits in
it.

The short version: `issue-mgmt`'s defensible edge is narrow and architectural --
**hardened per-record write-race concurrency over plain markdown files, with
zero runtime dependencies, keeping both workflow and discourse in the same
substrate.** Everywhere below, the distinction from an alternative is a *trade*,
not a claim to be better.

## The categories

### Plain markdown / TODO files (`plan.md`, `TODO.md`, checklists)

The baseline. Perfectly fine for a single person doing one thing at a time. They
have no concurrency story, no readiness or dependency model, and no schema an
agent can operate on programmatically. `issue-mgmt` is what you reach for when a
flat file stops coping -- parallel writers, dependencies, "what is ready to pick
up" -- and you still want the content to be plain files.

### Hosted trackers (GitHub Issues, Linear, Jira, and the SaaS field)

A different weight class. These are server-backed platforms with web UIs, boards,
notifications, permissions, and increasingly aggressive agent integrations (MCP
servers, agent assignment). If you want a team platform with a UI, use one of
these. `issue-mgmt` gives up all of that on purpose: it is local-first,
file-native, has no server to run and nothing to sign into, and treats the CLI
and the files as the whole interface. The trade is UI and team features versus
local-first, dependency-free, agent-ergonomic files.

### Git-native / offline trackers (the established lineage)

A long line of "issues as files/objects in your repo" tools:
[git-bug](https://github.com/git-bug/git-bug) (issues as git objects, with a
CRDT/Lamport-timestamp model for conflict-free distributed editing),
[dstask](https://github.com/naggie/dstask) (per-task YAML + git sync),
[Taskwarrior](https://taskwarrior.org/), [Fossil](https://fossil-scm.org/)
tickets, [tissue](https://github.com/), and older projects like
bugs-everywhere and ticgit. Several are mature and very good.

Two honest points here:

- These are mostly built for **human** writers. git-bug and Fossil solve
  distributed human concurrency well (CRDT / artifact merge); the lighter
  file-based ones generally punt write conflicts to "git will merge it."
- None of them target **concurrent AI-agent writers** specifically, and the
  lighter ones have no advisory-lock + atomic-write + optimistic-check +
  structured-exit-code contract. That specific combination -- serious write-race
  machinery *with files as the actual source of truth and no runtime dependency*
  -- is where `issue-mgmt` is genuinely uncommon.

### AI-agent markdown/task trackers (the 2025-2026 cluster)

The fast-moving neighbors: [beads](https://github.com/gastownhall/beads),
[Backlog.md](https://github.com/MrLesk/Backlog.md), tick-md, taskmd, and others.
This is where the overlap is real and the field is densest. Two are worth calling
out:

- **beads (`bd`)** is the closest neighbor by intent and the most important
  comparison. It is a strong, well-adopted, agent-first workflow engine over a
  version-controlled database. It gets its own page:
  [why-not-beads.md](why-not-beads.md).
- **tick-md** is the closest neighbor by *substrate* -- markdown + YAML
  frontmatter with a claim and readiness model -- and worth watching. It is early
  and could converge toward `issue-mgmt`'s concurrency depth; first-mover
  advantage on "hardened concurrency over frontmatter" is thin and perishable.
- **Backlog.md** pairs markdown-in-git with a CLI, a board, a web view, and MCP,
  and is actively developed. It leans on git merge for concurrency rather than a
  per-record write-race contract.

## At a glance

Qualitative only. Adoption and maturity in this space move fast and several data
points are single-source; this table deliberately avoids quoting star counts or
version numbers that would be stale or unverifiable.

| Tool | Local-first | Source of truth | Multi-writer model | AI-agent target |
|---|---|---|---|---|
| **issue-mgmt** | Yes | Markdown + frontmatter, file per issue | Per-record advisory lock + atomic write + optimistic mtime/sha + structured exit-code contract + TTL claim/lease | Yes (primary) |
| beads (`bd`) | Yes (embedded) / server | Dolt (versioned SQL DB); JSONL export | Atomic claim + Dolt cell-merge; server mode for true concurrency | Yes (primary) |
| Backlog.md | Yes | Markdown files | Git merge; no per-record lock/claim | Yes |
| tick-md | Yes | Markdown + frontmatter | Explicit claim; otherwise git merge | Yes |
| git-bug | Yes | Git objects | CRDT / Lamport, conflict-free distributed | No |
| dstask | Yes | Per-task YAML + git | Git sync / last-writer merge | No |
| Taskwarrior | Yes | Local DB (TaskChampion) | Replica sync, newest-wins | No |
| Fossil tickets | Yes | Fossil repo artifacts | Auto-merge artifacts, clock-ordered | No |
| GitHub Issues / Linear / Jira | No | Central DB | Central transactional DB | Partial to aggressive (MCP / agents) |

## Where the field is dense, and where issue-mgmt is thin

**Already well served (crowded):**

- Issues-as-files-in-git for solo and small-team humans. A long lineage; not a
  novel idea.
- AI-agent issue tracking as a thesis. As of 2026 this is a stampede -- purpose-
  built tools and SaaS incumbents alike.
- Markdown + YAML frontmatter as the agent-facing task format. Multiple tools
  independently landed on exactly this. It is `issue-mgmt`'s substrate and it is
  not uniquely its own.
- Distributed conflict-free concurrency for human writers (git-bug, Fossil).

**Where issue-mgmt is actually differentiated (narrow):**

- Hardened concurrency for *multiple concurrent AI-agent writers* on a
  local-first, file-native, zero-dependency store. The file-based competitors
  mostly punt to git merge; the one tool with a comparably serious concurrency
  story (beads) buys it with a database, not files.
- Frontmatter as *both* discourse substrate and workflow engine -- readiness,
  claim, and cycle-detection computed natively from the same files, no derived
  index and no external engine.
- A machine-consumable, contiguous exit-code contract for write losses, so an
  agent caller picks recovery from the exit code alone.
- A TTL claim/lease with a fencing generation counter for paused-agent races over
  a file store.

## Honest caveats

- The differentiation is architectural and narrow, not categorical. The category
  is crowded and fast-moving.
- The closest neighbor by intent (beads) is ahead on adoption and has a stronger
  raw-concurrency substrate; `issue-mgmt`'s counter is philosophical -- files not
  a database, zero dependencies.
- The closest neighbor by substrate (tick-md) is nascent and could close the gap.
- If none of the narrow differentiators above matter to you, one of the more
  established or better-adopted tools in this table is very likely the right
  choice, and that is a fine outcome.
