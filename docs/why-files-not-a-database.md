# Why files, not a database?

Most issue trackers -- hosted or local -- put your issues in a database and
give you a field to type a description into. `issue-mgmt` makes the opposite
bet: each issue is a markdown file with YAML frontmatter, the files are the
source of truth, and git is the audit log. There is no database and no derived
index.

This page explains that bet, and is honest about what it costs.

## Discourse and workflow, in one file

An issue has two kinds of content:

- **Workflow** -- status, priority, dependencies, who has it claimed. Structured,
  machine-operated, queried constantly.
- **Discourse** -- the design rationale, the decisions and their reasons, the
  running notes, the argument around the work. Prose, human- and agent-authored,
  read when someone needs to understand *why*.

Database-backed trackers model the workflow well and treat the discourse as a
description field: a blob hanging off a row. `issue-mgmt` keeps both in the same
plain file. The frontmatter carries the workflow; the markdown body below it
carries the discourse. They version together, in git, as one artifact.

## Why that matters

**Your rationale is a plain file you own.** The body of an issue -- every
decision, every "we tried X and it broke because Y" -- is markdown you can read,
write, grep, and diff with tools you already have, forever. No export, no API, no
schema migration stands between you and your own history. Delete `issue-mgmt`
tomorrow and your issues are still there, still readable.

**Git history is the audit log of the thinking.** Because the body lives in git,
`git log` over an issue file is a record of how the understanding of the work
evolved, not just when its status flipped. `issue history show <id>` is exactly
this. You get a versioned trail of the reasoning for free, because the reasoning
was never anywhere but in git.

**No schema wall.** There is no database schema to migrate, no index version to
outgrow, no "the tool upgraded and now the store needs a migration" moment. A
new field is a new frontmatter key. The store is just files; the tools read what
is committed.

**It survives context resets -- which is the point for agents.** When an AI-agent
session compacts its context or resets, the accumulated "why" behind the work has
to live somewhere durable and machine-readable, or it is re-derived from scratch
every time. Plain markdown in git is both: an agent can read the full rationale
of an issue with a single file read, and write back to it with the same CLI it
uses for everything else. The discourse surviving the reset is often the deciding
property for agent work, even though few people think to ask about it up front.

## What it costs

This is a real trade, not a free win. A database buys things files do not:

- **A richer query and coordination surface.** SQL, indexes, joins, transactions
  across many records. `issue-mgmt` gives you frontmatter filters, full-text
  search, and a frontmatter-native readiness computation -- enough for the
  workflows it targets, but not a query engine.
- **Stronger raw multi-writer concurrency.** A database with row- or cell-level
  merge handles concurrent writers more powerfully than advisory locks over
  files. `issue-mgmt` hardens the file case as far as it sensibly goes -- atomic
  writes, per-record locks, optimistic mtime/sha checks, a structured exit-code
  contract, TTL leases -- but a server-backed database is a different weight
  class if that is what you need.
- **Scale.** Thousands of files in a git repo is fine; millions is not the
  design target. This is a tool for a project's or a workspace's issues, not an
  organization's.

## The bet, stated plainly

`issue-mgmt` trades a database's query power and raw-concurrency ceiling for
issues that are plain, ownable, greppable, git-versioned files -- workflow and
rationale together, no second substrate, no runtime dependency. If your issues
are few enough to live in a git repo and you value owning your history in plain
text over a richer query surface, that is the trade this tool is built around.
If you need the database's power, you should use something backed by one -- and
[beads](why-not-beads.md) is a good place to start.
