# issue-mgmt

**A git-native, local-first issue tracker that hardens multi-writer safety over
plain markdown files -- built so several AI-agent sessions can claim, transition,
and hand off work concurrently without a server, a database, or a single runtime
dependency.**

`issue-mgmt` stores each issue as a markdown file with YAML frontmatter. The
files are the system of record; git is the audit log. A single self-contained
CLI (`issue <verb>`) creates, transitions, edits, and queries them, with no
server, no database, and no runtime dependencies -- just stdlib Python and
system bash.

The substrate is general -- markdown + git, perfectly usable by a solo human --
but the design driver is specific: parallel AI-agent sessions need to coordinate
work without stepping on each other, and the rationale behind past decisions
needs to survive context resets. That is what the frontmatter schema, the
per-record write-race machinery, the readiness and lease primitives, and the
agent-ergonomic CLI are all for.

---

## What this is, and what it isn't

`issue-mgmt` is a small, sharp tool, not a platform:

- **No web UI, no board, no app.** It is a CLI over files. Your editor, `git`,
  and `grep` are the GUI.
- **No server, no database, no hosting.** Nothing to run, nothing to sign into.
  The tradeoff is no central API and no real-time push.
- **No auth, no notifications, no real-time sync.** Sharing is whatever git
  sharing you already do (a remote, a shared drive); there is no presence or
  live update layer.
- **No bundled agent runtime; the CLI is the agent interface.** Any agent drives
  `issue-mgmt` through its plain, `--json`-friendly CLI -- that is the whole
  point. The shipped Claude Code slash-command skills are an optional convenience
  on top, not a requirement, and an MCP server adapter is designed for but not
  yet shipped.

If you want a hosted tracker with a UI, dashboards, and a team workflow, use
Linear, Jira, or GitHub Issues. If you want plain markdown files with hardened
concurrency and an agent-ergonomic CLI, read on.

---

## Why issue-mgmt

Two obvious approaches to coordinating agent work both fail at the edges:

- **Freeform notes** (`plan.md`, `TODO.md`) are too unstructured for an agent to
  query, transition, or reason over programmatically. A model can read them; it
  cannot reliably manage state through them across sessions.
- **Heavyweight trackers** (Jira, Linear, GitHub Issues) impose enough ceremony
  and live behind enough API that agents and humans route around them.
  Coordination state then lives in chat history and dies at the next context
  reset.

`issue-mgmt` sits between those two: light enough that agents and humans
actually use it as the system of record, structured enough that agents can
query, transition, and reason over it programmatically. Concretely:

- **Files are the source of truth; git is the audit log.** No derived index, no
  database, no second substrate to keep in sync. What you commit is what the
  tools read.
- **Multi-writer safety from day one.** Every mutating command composes an
  atomic write, a per-record advisory lock, and an optimistic mtime/sha check,
  and surfaces write-time losses through a small, contiguous exit-code contract
  so a caller picks its recovery action from the exit code alone (retry, pick
  another target, refresh, escalate). This is the part most file-based trackers
  punt to "git will merge it."
- **A frontmatter-native readiness and dependency engine.** `issue ready`
  computes the claimable set directly from frontmatter -- open issues whose
  `blocks` dependencies are all done -- across prefixes, with a TTL claim/lease
  so parallel sessions hand work off atomically instead of by convention.
- **First-class Claude Code skills.** One `/issue-*` slash command per verb, and
  the workflow-side skills consume the exit-code contract directly (retry on a
  concurrent edit, pick another target on a lost race, and so on).

And the draw a chooser rarely thinks to ask about:

**The issue body is first-class, git-versioned markdown -- not a description
field behind a workflow engine.** Design rationale, decisions, and notes (the
*discourse* around the work) live and version in git right alongside the
workflow state, readable and writable by humans and agents with no tooling. The
git history of a body is itself the audit log of the thinking. The trade is real
and worth stating plainly: you forgo a database's richer query and coordination
surface in exchange for issues whose "why" is plain files you and your agents
can read, grep, and diff forever. For agent work, where the accumulated
rationale surviving context resets is load-bearing, that is often the deciding
property. See [docs/why-files-not-a-database.md](docs/why-files-not-a-database.md).

---

## Install

```bash
brew tap dugal/tap https://github.com/dugal/homebrew-tap
brew install dugal/tap/issue-mgmt
issue admin install-skills       # one-time; re-run after each `brew upgrade`
```

The formula has no runtime dependencies -- the `issue` dispatcher is a
self-contained stdlib Python program -- so a fresh install is fully functional
with nothing else to provision. `issue admin install-skills` copies the Claude
Code skills into `~/.claude/skills/`; restart Claude Code afterward for the
`/issue-*` commands to register.

**Verify the install** end-to-end against a throwaway temp store (it never reads
or touches your real store):

```bash
issue admin selftest
```

---

## Quickstart

```bash
# 1. Scaffold a store (a git repo that holds your issues, outside any project).
issue admin init-store ~/issues-store

# 2. Point a project at it and give it an ID prefix. A project must be a git
#    repository -- issue-mgmt keys the registry on git identity. Starting from
#    scratch (already have a repo? cd into it and skip this line):
mkdir -p ~/projects/myproject && cd ~/projects/myproject && git init

issue admin init-project --store ~/issues-store --prefix MYP

# 3. File an issue (interactive, or non-interactive for agents).
issue create "add --dry-run flag" feature medium

# 4. See what is claimable right now (ready = open, all blockers done).
#    Run this BEFORE claiming: `ready` lists open work, so an issue you have
#    already started or completed is correctly absent from it.
issue ready --prefix MYP

# 5. Work the lifecycle.
issue start MYP-001 --branch issue-001-dry-run
issue complete MYP-001 --summary "all acceptance checks passed"

# 6. Read and search. Search matches the title and body (so this finds
#    MYP-001 by its title even before you have written any body text).
issue show MYP-001
issue search "dry-run"
```

Every verb takes `--json`, and the read verbs additionally accept gh-style
`--json id,title,...` field selection, so agents parse structured output instead
of scraping text. `issue --help` prints the tiered verb map; `issue <verb> -h`
prints a verb's flags.

### Claiming work across parallel sessions

When several sessions pull from the same backlog, a TTL lease makes the handoff
atomic instead of conventional:

```bash
# Claim: set in-progress and write claim/lease_until/claim_generation in one
# locked operation (default TTL 3600s).
issue start MYP-001 --claim session-a --lease-ttl 7200 --json

# A competing claim while the lease is valid loses cleanly: exit 4 (lost-race),
# stderr names the holder -- so the loser picks another target.
issue start MYP-001 --claim session-b

# What can I pick up? The ready set minus other actors' live leases.
issue ready --prefix MYP --actor session-a

# Reconcile leases abandoned by dead sessions (never touches a valid one).
issue admin reap
```

### Using it from Claude Code

After `issue admin install-skills`, fifteen slash commands are available --
`/issue-create`, `/issue-start`, `/issue-complete`, `/issue-edit`,
`/issue-dep`, `/issue-show`, `/issue-search`, `/issue-note` and more. The
workflow-side skills consume the exit-code contract: retry once on a concurrent
edit (exit 3), pick another target on a lost race (exit 4), refresh on a stale
view (exit 5), escalate on not-claimable (exit 7).

---

## Core concepts

- **The store.** Issues live in one git-tracked directory outside any project
  repo, at `<store>/<PREFIX>/<PREFIX>-NNN.md`. Each project registers a short
  uppercase prefix; cross-project references (`MYP-007`, `OPS-012`) resolve
  uniformly. Keeping issues out of the project repo is what removes the
  merge-conflict surface when parallel branches touch the same issue.
- **Frontmatter is the schema.** Required fields (`id`, `title`, `type`,
  `status`, `priority`, `created`, `updated`) plus optional ones (`branch`,
  `tags`, `depends_on`, lease fields) are the contract every consumer reads. A
  structural linter (`issue admin validate`) is the single source of truth for
  shape.
- **The state machine.** Five states -- `open`, `in-progress`, `blocked`,
  `done`, `cancelled` -- with a legal-transition matrix owned by one verb
  (`issue transition`, plus `start`/`complete`/`block`/`cancel` convenience
  verbs). Issue files are never deleted; `cancelled` is the substitute.
- **Dependencies and readiness.** A `depends_on` edge of kind `blocks` holds an
  issue back until its blocker is done; `parent-child`, `related`, and
  `discovered-from` record structure without ordering. `issue ready` computes
  the claimable set from these edges directly. A `blocks` edge that would close
  a cycle is refused before the write commits.
- **The TTL lease.** `claim` / `lease_until` / `claim_generation` frontmatter,
  set atomically, give parallel actors a real claim with a fencing counter that
  survives release and reap, so a paused actor cannot write through a
  reap-and-reclaim.
- **Discourse vs workflow.** Frontmatter carries the *workflow* (status, deps,
  claims); the body carries the *discourse* (rationale, decisions, notes). Both
  live in the same plain file, versioned by git. There is no derived index and
  no external engine -- readiness, claiming, and cycle detection are computed
  natively from frontmatter.

---

## Comparison

Issue tracking in plain files is a well-populated space. Here is how the choices
trade off -- each row is a different set of choices, suited to different needs:

- **issue-mgmt** -- markdown files as the source of truth, with hardened
  per-record write-race concurrency (advisory locks + atomic writes + optimistic
  checks + a structured exit-code contract + TTL leases) and both workflow and
  discourse in the same substrate, zero runtime dependencies. The trade: no
  database's query power or raw-concurrency ceiling, no UI, no server.
- **Plain markdown / TODO files** -- no concurrency, no readiness, no schema; the
  trade is simplicity for a single writer.
- **GitHub Issues / Linear / Jira** -- server-bound, API-bound, not local-first,
  higher-friction; the trade is a UI and a team platform for the server and the
  ceremony.
- **Git-native / offline trackers** (`git-bug`, Fossil tickets, `dstask`,
  Taskwarrior, `tissue`) -- mature and capable, built for human writers; the
  trade is that most punt multi-writer conflicts to git merge rather than
  targeting concurrent AI-agent writers.
- **[beads (`bd`)](docs/why-not-beads.md)** -- the closest neighbor by intent
  (agent-first, readiness, atomic claim). A workflow engine over a
  version-controlled database; the trade against `issue-mgmt` is a richer
  coordination surface for a database dependency, versus workflow *and* discourse
  in plain files with zero deps.

The full landscape is in [docs/comparison.md](docs/comparison.md).

---

## FAQ

The **Why**, **Comparison**, and companion docs cover most "why not X?"
questions. A few that they do not:

**Does this work for teams?**
It is *designed for* concurrent writers, but the demonstrated evidence is
single-operator; a multi-human team story is not something the project claims
yet. Sharing today is via a shared git remote -- no server, auth, or
notifications.

**Do I need AI agents / Claude Code to use it?**
No. Solo human use works fine; the Claude Code skills are an optional bonus.

**Is my data locked in?**
No. It is plain markdown files in a git repo. Delete the tool and your issues are
still there, readable and diffable.

---

## Command reference

A single `issue` dispatcher exposes a three-tier verb surface: routine verbs, an
`issue history` read-only sub-namespace, and an `issue admin` sub-namespace for
maintenance and scaffolding. Every verb offers `--json`. Every mutating verb
takes a per-issue lock and commits its own change to the store; every read verb
is lock-free.

**Routine verbs**

| Verb | Purpose |
|---|---|
| `issue create` | Create an issue (non-interactive, or interactive from a terminal -- a bare invocation on non-TTY stdin exits 2 naming the non-interactive form); seeds body via `--section`/`--from-file` |
| `issue show` | Read an issue end-to-end, or only frontmatter / named sections; multi-id batch |
| `issue transition` | Drive the state machine; convenience verbs `start` / `complete` / `cancel` / `block` / `unblock`; lease flags `--claim` / `--renew` / `--release` |
| `issue ready` | Readiness computed from frontmatter; `--actor` hides others' leases; `--limit` caps the ready list without silent truncation; `--filtered all` explains what is not claimable |
| `issue edit` | Frontmatter and section edits; multi-section atomic; whole-body `--replace-body-file`; refuses guarded fields |
| `issue note` | Append a date-stamped line to `## Implementation Notes` (or any section) |
| `issue dep` | Author `depends_on` edges (`add` / `remove` / `list`) |
| `issue search` | Title + body text search; OR-within / AND-across frontmatter filters |
| `issue list` | Filter-only enumeration (prefix / status / type) |
| `issue index` | Grouped-by-status board |

**`issue history` (read-only)**

| Verb | Purpose |
|---|---|
| `issue history show` | `git log --follow` for one issue file |
| `issue history search` | `git log -S` pickaxe for a token across the store |
| `issue history at` | Print an issue at a past commit, tag, branch, or date |
| `issue history activity` | Recent commits grouped by the issue they touched |

**`issue admin`**

| Verb | Purpose |
|---|---|
| `issue admin init-store` | Scaffold a fresh store in one command; idempotent |
| `issue admin init-project` | Register a project in the store registry; writes routing files |
| `issue admin migrate` | One-shot migration from a legacy in-repo `.issues/` layout |
| `issue admin reindex` | Regenerate `INDEX.md` from frontmatter |
| `issue admin validate` | Structural linter (source of truth for issue-file shape) |
| `issue admin reap` | Clear expired leases store-wide; never touches a valid one |
| `issue admin snapshot` | Run the catchup sweep outside a mutation; recovery surface |
| `issue admin selftest` | End-to-end functional smoke against a throwaway temp store |
| `issue admin install-skills` | Install the Claude Code skills into `~/.claude/skills/` |

### Exit codes

Reads report emptiness through the exit code -- one rule per class, so a script
branches on the code rather than parsing the output:

- **Enumeration reads** (`search`, `list`, `ready`, `index`, `history search`,
  `history activity`) follow the grep convention: `0` when at least one row was
  emitted, `1` when none was. The rows render either way; an empty board is
  still meaningful output.
- **Point reads** (`show`, `history show`, `history at`) exit `0` when the named
  record was found and `1` when it was not.

Two non-empty failures must not be read as a zero result: scoping to a
`--prefix` the store does not have is an argument error (`2`,
`error=unknown-prefix`, naming the known set), and a store that cannot be
resolved is a precondition failure (`1`, `reason=store-unresolvable`). Mutating
verbs add the loser-side contract for write-time races -- `3` concurrent-edit,
`4` lost-race, `5` stale-local-view, `7` not-claimable -- each carrying a
`key=value` stderr line that names the contended resource and the recovery
parameters, and the same condition as one JSON object under `--json`.

### Projects are git repositories

`issue admin init-project` registers a project under a prefix by deriving the
registry key from the project's git identity (`git rev-parse --git-common-dir`),
and it writes the routing files (`.issues/.prefix`, `.issues/.migrated-to-store`)
into the project so they are git-tracked and flow into every worktree and clone.
So a project **must be a git repository**: run it inside an existing repo, or
`git init` a new one first. (The issue *store* is a separate git repo of its
own; the project holds only the small routing pointers.)

### Where the store lives

Store-aware tools resolve the store root in this order (highest precedence
first): `--store ROOT` (where accepted), the `ISSUE_STORE_ROOT` env var, a
`<project>/.issues/.migrated-to-store` sentinel, the
`~/.config/issue-mgmt/config` `store_root` key, then an interactive prompt. No
default path ships in the code. `issue admin init-project` writes the project's
routing files -- `.issues/.prefix` and the `.issues/.migrated-to-store`
sentinel -- which is what makes the store resolvable from anywhere inside that
project. It does not write `~/.config/issue-mgmt/config`; that key is for
pointing at a store from OUTSIDE any registered project, and you create it
yourself if you want it.

---

## Documentation

- [docs/migration-guide.md](docs/migration-guide.md) -- full new-user
  walkthrough (store setup, project onboarding, cross-references,
  troubleshooting).
- [docs/why-files-not-a-database.md](docs/why-files-not-a-database.md) -- the
  discourse-in-git philosophy and its trade.
- [docs/why-not-beads.md](docs/why-not-beads.md) -- the closest-neighbor
  comparison, in full.
- [docs/comparison.md](docs/comparison.md) -- the wider landscape.
- [CHANGELOG.md](CHANGELOG.md) -- release history.

---

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 dougtech.
