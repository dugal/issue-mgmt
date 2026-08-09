# issues-store

External git repository for issue records used by `issue-mgmt`-based
projects. Each issue is a Markdown file under `<PREFIX>/<ID>.md`, where
`PREFIX` is the per-project namespace (e.g. `IM`, `RM`). The store is
the durable artifact that outlives any one project's workspace; it is
intentionally minimal and contains only issue data.

See [LAYOUT.md](LAYOUT.md) for the canonical directory and locking
convention.

## Distribution model (P1)

The `issue-mgmt` Homebrew formula is the unit of distribution for every
agent-facing surface that operates on this store. Claude Code skills,
future MCP-server adapters, and enforcement hooks ship either inside the
`issue-mgmt` formula or in a formula that explicitly depends on it; no
agent-facing surface is owned by a workspace, copied between machines by
hand, or configured ad hoc. This repository is a pure data store: it
owns issue records and the `INDEX.md` derived from them, and it never
owns, vendors, or versions any skill file, hook script, or workspace
`CLAUDE.md` content. Downstream backends that read or write this data
install via `issue-mgmt` or one of its dependents.

## Concurrency baseline (P2)

Multiple Claude Code sessions, humans, and future autonomous agents will
operate semi-parallel against this store. Conflicting writers to the
same issue are expected, not exceptional, so every mutating tool that
ships with `issue-mgmt` applies the three-layer concurrency model
described in [LAYOUT.md](LAYOUT.md): atomic rename, per-record advisory
lock, and optimistic mtime/sha verification. The CLI surfaces conflicts
as the `concurrent-edit` structured error (exit code 3); callers retry
once against fresh state.
