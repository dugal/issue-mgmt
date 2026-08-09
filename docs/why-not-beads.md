# Why not just use beads?

If you are choosing an issue tracker for AI-agent work in 2026, you will run
into [beads](https://github.com/gastownhall/beads) (`bd`) quickly, and you
should. It is the closest neighbor to `issue-mgmt` by intent, it is well
built, and it is far ahead on adoption and mindshare. This page answers "why not
just use beads?" honestly -- as a peer comparison, framed as a trade, not as a
knock.

## What beads is

beads bills itself as "a memory upgrade for your coding agent": a
dependency-aware, distributed graph issue tracker built specifically for AI
coding agents, so an agent can do long-horizon work without losing the thread.
It gives you:

- A first-class dependency and readiness model (epics, tasks, subtasks;
  `blocks`, `relates-to`, `duplicates`, `supersedes` edges; a `bd ready` that
  surfaces unblocked work).
- An atomic claim primitive (`bd update --claim`) and, in server mode, true
  concurrent multi-writer support.
- Agent-first ergonomics: JSON output, `bd prime`, and setup helpers for
  Claude, Codex, and other agents.

Those are exactly the capabilities `issue-mgmt` also cares about. The overlap on
*intent* is near-total: claim/lease, readiness/dependencies, "what work is ready
to pick up," agent-first. So the interesting question is not features -- it is
the substrate.

## The one real difference: substrate

beads' source of truth is [Dolt](https://www.dolthub.com/), a version-controlled
SQL database. It ships an embedded single-writer mode by default and an external
Dolt SQL server for concurrent writers. A `.beads/issues.jsonl` export exists so
you can diff and review changes in git, but that JSONL is explicitly *not*
authoritative -- the database is.

`issue-mgmt` inverts that. The source of truth is the markdown files
themselves -- one file per issue, YAML frontmatter as the schema, git as the
audit log. There is no derived index and no database: readiness, claiming, and
cycle detection are computed directly from the frontmatter the same commit you
read. Nothing to project, nothing to keep in sync, nothing to rebuild.

That single choice is the whole comparison. Everything else follows from it.

## What each choice buys you

**beads' database buys richer coordination.** A SQL database with cell-level
merge is a stronger raw-concurrency substrate than advisory locks over files.
Server mode gives you true multi-writer concurrency; the query surface is richer
than "grep and parse frontmatter"; the graph operations are native. If you want
a coordination engine and you are willing to run (or embed) a database to get
it, beads' substrate is the more powerful one, full stop.

**issue-mgmt's files buy discourse-in-git and zero dependencies.** Because the
issue *is* the file, the issue body -- the design rationale, the decisions, the
running notes, the *discourse* around the work -- lives and versions in git
alongside the workflow state, readable and writable by any human or agent with
no tooling at all. The git history of a body is the audit log of the thinking.
There is no runtime dependency to install and nothing to run: a fresh `brew
install` is fully functional, and if you delete the tool, your issues are still
plain files you can read, grep, and diff forever. beads does not try to own the
discourse -- its JSONL export is for review, not authorship -- and that is a
fair division of labor, not a gap in beads.

## A corollary worth stating plainly

The parallel-agent primitives people often assume a workflow engine must provide
-- an atomic TTL lease with a fencing counter, a cross-prefix readiness filter,
a machine-consumable exit-code recovery contract -- were never beads' to give in
the first place. They are properties of how a *store* handles concurrent writers,
not of a graph engine bolted on top. `issue-mgmt` implements them directly over
files: a TTL claim/lease with a `claim_generation` fence, `issue ready` across
prefixes, and a contiguous exit-code contract (3 concurrent-edit, 4 lost-race, 5
stale-view, 7 not-claimable) so an agent caller picks its recovery from the exit
code alone. beads has an atomic claim too; the point is only that you do not need
beads' database to get these -- you need a store that takes multi-writer safety
seriously.

## The honest bottom line

- If you want the richer coordination surface and the stronger raw-concurrency
  substrate, and a database as your source of truth is fine, **use beads.** It is
  more mature, more adopted, and more powerful on that axis.
- If you want your issues -- workflow *and* the rationale around them -- to be
  plain markdown files in git, with zero runtime dependencies and a hardened
  multi-writer story over those files, **`issue-mgmt` is the narrower, more
  opinionated bet.**

It is a trade: plain files plus discourse-in-git, versus a database's richer
query and coordination surface. Pick the one whose trade you actually want.
